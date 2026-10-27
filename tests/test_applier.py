# -*- coding: utf-8 -*-
"""任务 6.4: 三个动作 — 应用 -> 重跑验证 -> 回滚, 每一步都留账

锁死这些事 (任务卡):
    --accept / --reject / --defer 三个动作
    decisions.jsonl 一行一条: {ts, failure_id, proposal_id, decision, verify_result,
    operator, note} —— 就这七个键, 不许悄悄加

为什么 accept 的用例都造一个真 git 仓库: 回滚是 git checkout --, 只有真的打过一次补丁,
才验证得出"失败自动回滚"是不是真回滚了。假 git 只能测出"我调用过某个函数"。

验证器一律注入 (两个参数的假 runner), 真跑 pytest 的只有一条冒烟用例 —— 测试不该
依赖别人的用例目录长什么样。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ai_kit.proposal import applier as A
from ai_kit.proposal.solutions import Proposal

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="需要 git 才能验证 apply / rollback")

TARGET = "tests/test_user.py"
CASE = "test_open_dialog"
BROKEN = "body > div.el-dialog"

SOURCE = ('# -*- coding: utf-8 -*-\n'
          '\n'
          '\n'
          'def test_open_dialog():\n'
          '    locator = "body > div.el-dialog"\n'
          '    assert locator\n')

# 注意前两行是空上下文行 (一个空格 + 换行): 真实 diff 里最常见, 也最容易在 Windows 上
# 被文本模式的换行翻译搞坏
DIFF = ("--- a/tests/test_user.py\n"
        "+++ b/tests/test_user.py\n"
        "@@ -2,5 +2,5 @@\n"
        " \n"
        " \n"
        " def test_open_dialog():\n"
        '-    locator = "body > div.el-dialog"\n'
        '+    locator = ".el-dialog"\n'
        "     assert locator\n")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def proposal(**overrides) -> Proposal:
    base = dict(proposal_id="S001", type="locator_patch",
                title=f"{BROKEN} → .el-dialog", target_file=TARGET, diff=DIFF,
                risk="low", auto_applicable=True, confidence=0.9,
                verify_cmd=f"pytest {TARGET}::{CASE} -q",
                rollback_cmd=f"git checkout -- {TARGET}",
                reason="L1 规则给出 .el-dialog")
    base.update(overrides)
    return Proposal(**base)


def make_repo(root: Path) -> Path:
    """一个真仓库: 一个测试文件, 已提交"""
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / TARGET).write_text(SOURCE, encoding="utf-8", newline="\n")
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run([GIT, *args], cwd=repo, check=True, capture_output=True)
    return repo


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run([GIT, *args], cwd=repo, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    return proc.stdout.strip()


def passed(argv, cwd) -> tuple:
    return A.VERIFY_PASSED, "1 passed in 0.01s"


def failed(argv, cwd) -> tuple:
    return A.VERIFY_FAILED, "1 failed"


def body_of(repo: Path) -> str:
    return (repo / TARGET).read_text(encoding="utf-8")


def lines(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---------------------------------------------------------------------------
# 账本: 七个键, 一个不多
# ---------------------------------------------------------------------------

def test_a_decision_record_has_exactly_the_seven_keys_of_the_card():
    item = A.DecisionRecord(ts="2026-10-27T10:00:00Z", failure_id="LC-002",
                            proposal_id="S001", decision="accept",
                            verify_result="passed", operator="syn", note="ok")

    assert list(item.as_dict()) == ["ts", "failure_id", "proposal_id", "decision",
                                    "verify_result", "operator", "note"]
    assert json.loads(item.to_line()) == item.as_dict()


def test_an_unknown_decision_is_rejected():
    with pytest.raises(ValueError, match="决策只能是"):
        A.DecisionRecord(ts="t", failure_id="f", proposal_id="S001", decision="maybe",
                         verify_result="skipped", operator="syn")


def test_an_unknown_verify_result_is_rejected():
    with pytest.raises(ValueError, match="verify_result"):
        A.DecisionRecord(ts="t", failure_id="f", proposal_id="S001", decision="accept",
                         verify_result="ok-ish", operator="syn")


def test_a_record_without_a_proposal_id_is_rejected():
    with pytest.raises(ValueError, match="proposal_id"):
        A.DecisionRecord(ts="t", failure_id="f", proposal_id="  ", decision="defer",
                         verify_result="skipped", operator="syn")


def test_the_timestamp_is_utc_iso(tmp_path):
    item = A.reject(proposal(), failure_id="LC-002", decisions=tmp_path / "d.jsonl")

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", item.ts), item.ts


# ---------------------------------------------------------------------------
# reject / defer: 不动一个字节
# ---------------------------------------------------------------------------

@needs_git
def test_reject_only_writes_a_record(tmp_path):
    repo = make_repo(tmp_path)
    decisions = tmp_path / "decisions.jsonl"

    item = A.reject(proposal(), failure_id="LC-002", note="文案变更应同步产品",
                    operator="syn", decisions=decisions)

    assert item.decision == "reject" and item.verify_result == A.VERIFY_SKIPPED
    assert item.note == "文案变更应同步产品"
    assert body_of(repo) == SOURCE
    assert git(repo, "status", "--porcelain") == ""
    assert lines(decisions)[0]["decision"] == "reject"


@needs_git
def test_defer_only_writes_a_record(tmp_path):
    repo = make_repo(tmp_path)
    decisions = tmp_path / "decisions.jsonl"

    item = A.defer(proposal(), failure_id="LC-002", operator="syn", decisions=decisions)

    assert item.decision == "defer" and item.verify_result == A.VERIFY_SKIPPED
    assert body_of(repo) == SOURCE
    assert len(lines(decisions)) == 1


@needs_git
def test_defer_then_accept_is_allowed(tmp_path):
    """搁置不是终点: 人想清楚了还能再采纳"""
    repo = make_repo(tmp_path)
    decisions = tmp_path / "decisions.jsonl"
    item = proposal()

    A.defer(item, failure_id="LC-002", decisions=decisions)
    A.accept(item, failure_id="LC-002", root=str(repo), decisions=decisions, runner=passed)

    rows = lines(decisions)
    assert [row["decision"] for row in rows] == ["defer", "accept"]
    assert ".el-dialog" in body_of(repo)


# ---------------------------------------------------------------------------
# accept: 打补丁 -> 验证 -> 留下或回滚
# ---------------------------------------------------------------------------

@needs_git
def test_accept_applies_the_patch_and_keeps_it_when_verify_passes(tmp_path):
    repo = make_repo(tmp_path)
    decisions = tmp_path / "decisions.jsonl"

    item = A.accept(proposal(), failure_id="LC-002", root=str(repo),
                    decisions=decisions, runner=passed, operator="syn")

    assert item.decision == "accept" and item.verify_result == A.VERIFY_PASSED
    assert "L1 规则" not in item.note, "note 是决策理由, 不是方案的 reason"
    assert "1 passed" in item.note
    assert 'locator = ".el-dialog"' in body_of(repo)
    assert "M " + TARGET in git(repo, "status", "--porcelain") or \
        git(repo, "status", "--porcelain").startswith(" M")


@needs_git
def test_a_patch_with_blank_context_lines_still_applies(tmp_path):
    """diff 的上下文行常是空行; 一旦 stdin 被文本模式翻成 CRLF, git 就再也搜不到它们"""
    repo = make_repo(tmp_path)

    A.accept(proposal(), failure_id="LC-002", root=str(repo),
             decisions=tmp_path / "d.jsonl", runner=passed)

    assert BROKEN not in body_of(repo)


@needs_git
def test_a_failed_verify_rolls_the_file_back(tmp_path):
    repo = make_repo(tmp_path)
    decisions = tmp_path / "decisions.jsonl"

    item = A.accept(proposal(), failure_id="LC-002", root=str(repo),
                    decisions=decisions, runner=failed, note="先试试")

    assert item.decision == "accept" and item.verify_result == A.VERIFY_FAILED
    assert "已回滚" in item.note and "先试试" in item.note
    assert body_of(repo) == SOURCE, "验证没过还留着补丁, 仓库就脏了"
    assert BROKEN in body_of(repo)


@needs_git
def test_the_record_survives_a_rollback(tmp_path):
    """回滚不能把记录也一起撤掉: 复盘时要看得见"试过, 不行" """
    repo = make_repo(tmp_path)
    decisions = tmp_path / "decisions.jsonl"

    A.accept(proposal(), failure_id="LC-002", root=str(repo),
             decisions=decisions, runner=failed)

    row = lines(decisions)[0]
    assert row["decision"] == "accept" and row["verify_result"] == "failed"


@needs_git
def test_accept_refuses_a_dirty_file_and_leaves_it_alone(tmp_path):
    repo = make_repo(tmp_path)
    target = repo / TARGET
    target.write_text(SOURCE + "# 我自己改的\n", encoding="utf-8", newline="\n")

    with pytest.raises(A.ApplierError, match="未提交的改动"):
        A.accept(proposal(), failure_id="LC-002", root=str(repo),
                 decisions=tmp_path / "d.jsonl", runner=passed)

    assert "我自己改的" in body_of(repo), "回滚会连带丢掉这份改动, 所以宁可不动"


@needs_git
def test_accept_twice_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    decisions = tmp_path / "decisions.jsonl"
    item = proposal()

    A.accept(item, failure_id="LC-002", root=str(repo), decisions=decisions, runner=passed)
    with pytest.raises(A.ApplierError, match="已经采纳并验证通过"):
        A.accept(item, failure_id="LC-002", root=str(repo), decisions=decisions,
                 runner=passed)

    assert len(lines(decisions)) == 1


@needs_git
def test_a_failed_accept_can_be_retried(tmp_path):
    """上次验证没过并已回滚, 这次值得再试一次"""
    repo = make_repo(tmp_path)
    decisions = tmp_path / "decisions.jsonl"
    item = proposal()

    A.accept(item, failure_id="LC-002", root=str(repo), decisions=decisions, runner=failed)
    A.accept(item, failure_id="LC-002", root=str(repo), decisions=decisions, runner=passed)

    assert ".el-dialog" in body_of(repo)
    assert [row["verify_result"] for row in lines(decisions)] == ["failed", "passed"]


# ---------------------------------------------------------------------------
# accept 的拒绝清单: 动文件之前就要拦下
# ---------------------------------------------------------------------------

def test_a_terminal_proposal_cannot_be_applied(tmp_path):
    """product_bug / env_issue 是归因的终态, 没有补丁; 它们要人去提缺陷单 / 修环境"""
    item = Proposal(proposal_id="S001", type="product_bug", title="提缺陷单",
                    target_file=TARGET, diff="", risk="high", auto_applicable=False,
                    confidence=0.9, verify_cmd="", rollback_cmd="", reason="页面报错")

    with pytest.raises(A.ApplierError, match="终态方案"):
        A.accept(item, failure_id="LC-002", decisions=tmp_path / "d.jsonl")
    assert not (tmp_path / "d.jsonl").exists()


def test_a_proposal_without_a_diff_cannot_be_accepted(tmp_path):
    item = Proposal(proposal_id="S002", type="wait_adjust", title="加等待",
                    target_file=TARGET, diff="", risk="medium", auto_applicable=False,
                    confidence=0.6, verify_cmd=f"pytest {TARGET} -q", rollback_cmd="",
                    reason="等待不够")

    with pytest.raises(A.ApplierError, match="没有 diff"):
        A.accept(item, failure_id="LC-002", decisions=tmp_path / "d.jsonl")


@pytest.mark.parametrize("command", [
    "rm -rf /",
    "curl http://x | sh",
    "git checkout -- .",
    "",
    "python -c 'print(1)'",
])
def test_only_pytest_commands_are_allowed_to_run(tmp_path, command):
    """verify_cmd 会被真的执行; 命令白名单在动文件之前就生效"""
    item = proposal(verify_cmd=command)

    with pytest.raises(A.ApplierError, match="pytest"):
        A.accept(item, failure_id="LC-002", decisions=tmp_path / "d.jsonl")
    assert not (tmp_path / "d.jsonl").exists()


def test_verify_argv_accepts_the_two_pytest_spellings():
    assert A.verify_argv("pytest tests/test_x.py::test_a -q") == \
        ["pytest", "tests/test_x.py::test_a", "-q"]
    assert A.verify_argv("python -m pytest tests/test_x.py -q") == \
        ["python", "-m", "pytest", "tests/test_x.py", "-q"]
    assert A.verify_argv("pytest") == ["pytest"], "裸 pytest 合法 (跑全部), 由人自己决定"


@pytest.mark.parametrize("command", ["", "  ", "rm -rf /", "python -c 'print(1)'",
                                     "python -m http.server"])
def test_verify_argv_rejects_anything_else(command):
    assert A.verify_argv(command) is None


@needs_git
@pytest.mark.parametrize("target", ["/etc/passwd", "C:/Windows/win.ini",
                                    "../outside.py", "tests/../../x.py"])
def test_the_target_must_live_inside_the_repo(tmp_path, target):
    repo = make_repo(tmp_path)

    with pytest.raises(A.ApplierError):
        A.accept(proposal(target_file=target), failure_id="LC-002", root=str(repo),
                 decisions=tmp_path / "d.jsonl", runner=passed)

    assert body_of(repo) == SOURCE


@needs_git
def test_a_missing_target_file_is_refused(tmp_path):
    repo = make_repo(tmp_path)

    with pytest.raises(A.ApplierError, match="不存在"):
        A.accept(proposal(target_file="tests/test_gone.py"), failure_id="LC-002",
                 root=str(repo), decisions=tmp_path / "d.jsonl", runner=passed)


@needs_git
def test_a_patch_that_does_not_apply_is_refused_without_writing(tmp_path):
    repo = make_repo(tmp_path)
    stale = DIFF.replace("-    locator", "-    locatorX")

    with pytest.raises(A.ApplierError, match="补丁打不上"):
        A.accept(proposal(diff=stale), failure_id="LC-002", root=str(repo),
                 decisions=tmp_path / "d.jsonl", runner=passed)

    assert body_of(repo) == SOURCE


@needs_git
def test_an_unknown_failure_id_is_not_a_problem_but_git_is_required(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(A.ApplierError, match="不在 git 仓库里"):
        A.accept(proposal(), failure_id="", root=str(plain),
                 decisions=tmp_path / "d.jsonl", runner=passed)


def test_repo_root_resolves_the_top_of_the_worktree():
    root = A.repo_root(Path(__file__).resolve().parents[1])

    assert Path(root).is_dir()
    assert (Path(root) / "ai_kit").is_dir()


def test_a_missing_git_is_explained_not_a_traceback(monkeypatch, tmp_path):
    """没装 git 的机器上, 人要看到一句能懂的话"""
    def boom(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(A.subprocess, "run", boom)

    with pytest.raises(A.ApplierError, match="git 用不了"):
        A.repo_root(str(tmp_path))


# ---------------------------------------------------------------------------
# 验证器 / 账本工具
# ---------------------------------------------------------------------------

def test_run_verify_maps_the_exit_code_to_a_result(tmp_path):
    ok = A.run_verify([sys.executable, "-c", "print('fine')"], str(tmp_path))
    bad = A.run_verify([sys.executable, "-c", "import sys; sys.exit(3)"], str(tmp_path))

    assert ok[0] == A.VERIFY_PASSED and "fine" in ok[1]
    assert bad[0] == A.VERIFY_FAILED


def test_run_verify_reports_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "VERIFY_TIMEOUT", 0.2)
    status, output = A.run_verify(
        [sys.executable, "-c", "import time; time.sleep(5)"], str(tmp_path))

    assert status == A.VERIFY_TIMEOUT
    assert "0.2" in output


def test_decisions_are_appended_not_overwritten(tmp_path):
    decisions = tmp_path / "nested" / "decisions.jsonl"

    A.reject(proposal(proposal_id="S001"), failure_id="F1", decisions=decisions)
    A.defer(proposal(proposal_id="S002"), failure_id="F1", decisions=decisions)

    assert [row["proposal_id"] for row in lines(decisions)] == ["S001", "S002"]


def test_a_broken_line_does_not_ruin_the_ledger(tmp_path):
    decisions = tmp_path / "decisions.jsonl"
    A.defer(proposal(), failure_id="F1", decisions=decisions)
    with open(decisions, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("{ not json\n")

    assert len(A.read_decisions(decisions)) == 1


def test_the_operator_falls_back_to_the_environment_then_the_user(monkeypatch):
    assert A._operator("explicit") == "explicit"

    monkeypatch.setenv(A.OPERATOR_ENV, "from-env")
    assert A._operator() == "from-env"
    assert A._operator("explicit") == "explicit"

    monkeypatch.delenv(A.OPERATOR_ENV)
    assert A._operator() in {A._operator(), "unknown"}
    assert A._operator().strip() != ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _pending(tmp_path: Path, item: Proposal = None, failure_id: str = "LC-002") -> Path:
    """一份最小的 report.json (报告层另有测试, 这里只要 applier 能读)"""
    from ai_kit.proposal import report

    payload = {"version": report.REPORT_VERSION, "failure_id": failure_id,
               "proposals": [(item or proposal()).as_dict()]}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@needs_git
def test_the_cli_records_a_reject(tmp_path, capsys):
    repo = make_repo(tmp_path)
    decisions = tmp_path / "decisions.jsonl"

    code = A.main(["--decision", "S001", "--reject", "--note", "文案变了",
                   "--pending", str(_pending(tmp_path)), "--decisions", str(decisions),
                   "--repo", str(repo), "--operator", "syn"])

    captured = capsys.readouterr()
    assert code == 0
    assert "S001" in captured.out and "decisions.jsonl" in captured.out
    row = lines(decisions)[0]
    assert row["decision"] == "reject" and row["note"] == "文案变了"
    assert row["operator"] == "syn"


@needs_git
def test_the_cli_accepts_and_runs_verify(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(A, "run_verify", passed)

    code = A.main(["--decision", "S001", "--accept", "--pending", str(_pending(tmp_path)),
                   "--decisions", str(tmp_path / "d.jsonl"), "--repo", str(repo)])

    assert code == 0
    assert 'locator = ".el-dialog"' in body_of(repo)
    assert "passed" in capsys.readouterr().out


@needs_git
def test_the_cli_returns_one_when_verify_fails(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setattr(A, "run_verify", failed)

    code = A.main(["--decision", "S001", "--accept", "--pending", str(_pending(tmp_path)),
                   "--decisions", str(tmp_path / "d.jsonl"), "--repo", str(repo)])

    assert code == 1
    assert body_of(repo) == SOURCE


def test_the_cli_explains_an_unknown_proposal_id(tmp_path, capsys):
    code = A.main(["--decision", "S009", "--defer", "--pending", str(_pending(tmp_path)),
                   "--decisions", str(tmp_path / "d.jsonl")])

    captured = capsys.readouterr()
    assert code == 2
    assert "S009" in captured.err and "S001" in captured.err


def test_the_cli_explains_a_missing_pending_file(tmp_path, capsys):
    code = A.main(["--decision", "S001", "--defer", "--pending", str(tmp_path / "no.json"),
                   "--decisions", str(tmp_path / "d.jsonl")])

    assert code == 2
    assert "no.json" in capsys.readouterr().err


def test_the_cli_requires_one_action(tmp_path):
    with pytest.raises(SystemExit):
        A.main(["--decision", "S001", "--pending", str(_pending(tmp_path))])


def test_the_cli_reads_the_failure_id_from_the_pending_file(tmp_path):
    decisions = tmp_path / "d.jsonl"
    pending = _pending(tmp_path, failure_id="LC-7")

    A.main(["--decision", "S001", "--defer", "--pending", str(pending),
            "--decisions", str(decisions)])

    assert lines(decisions)[0]["failure_id"] == "LC-7"
