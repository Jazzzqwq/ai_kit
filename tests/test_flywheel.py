# -*- coding: utf-8 -*-
"""任务 6.6: 数据飞轮 —— 人做过的判断, 变成下一轮评测的黄金数据

锁死这些事:
    accept + 验证通过            -> 正样本 (进归因评测的分母)
    人拒 / 采纳但验证不过        -> 负样本 (带理由, 但不进任何分母)
    defer / 缺 report.json / 报告里没这个方案 -> 跳过并点名
    重复导出只追加, 既有条目一个字都不改 (黄金集是资产, 不是缓存)

为什么正负分两个文件: reject 只说"这个方案不对", 不说明"归因错了"。把一条被拒的
定位器补丁塞进分类准确率, 等于把正确的归因算成错 —— 分母里不能有它。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_kit.evaluation import eval_analyzer as E
from ai_kit.proposal import flywheel as F

TRACE = ("Traceback (most recent call last):\n"
         "  File \"pages/login.py\", line 88, in open_dialog\n"
         "selenium.common.exceptions.NoSuchElementException: Message: no such element: "
         "Unable to locate element: {\"method\":\"css selector\","
         "\"selector\":\"body > div.el-dialog\"}")
LOG = "click with retry failed after 10s"

DIFF = ("--- a/tests/test_user.py\n"
        "+++ b/tests/test_user.py\n"
        "@@ -2,5 +2,5 @@\n"
        " \n"
        " \n"
        " def test_open_dialog():\n"
        '-    locator = "body > div.el-dialog"\n'
        '+    locator = ".el-dialog"\n'
        "     assert locator\n")

PROPOSAL = {
    "proposal_id": "S001",
    "type": "locator_patch",
    "title": "body > div.el-dialog → .el-dialog",
    "target_file": "tests/test_user.py",
    "diff": DIFF,
    "risk": "low",
    "auto_applicable": True,
    "confidence": 0.9,
    "verify_cmd": "pytest tests/test_user.py::test_open_dialog -q",
    "rollback_cmd": "git checkout -- tests/test_user.py",
    "reason": "L1 规则给出 .el-dialog",
}


# ---------------------------------------------------------------------------
# 夹具: 一份真 report.json / 一份真账本
# ---------------------------------------------------------------------------

def report_payload(failure_id: str = "LC-002", *, category: str = "脚本-定位器问题",
                   created_at: str = "2026-10-20T10:00:00Z",
                   proposals=None, traceback: str = TRACE,
                   log: str = LOG) -> dict:
    """report.write 的 json 结构 (字段与 report.FailureReport.as_dict 一致)"""
    return {
        "version": 1,
        "failure_id": failure_id,
        "created_at": created_at,
        "case_id": "login_001",
        "test_name": "test_open_dialog",
        "url": "http://localhost/login",
        "screenshot": "",
        "traceback": traceback,
        "log": log,
        "dom_diff": "",
        "steps": [],
        "diagnosis": {
            "category": category,
            "blame": "test_code",
            "confidence": 0.9,
            "root_cause": "定位器写死了层级",
            "evidence": [],
            "suggestion": "换成语义类",
            "source": "rule",
            "rule_version": "rule.v2+blame.v1",
            "acceptable_categories": [],
        },
        "decision": {"proposal_type": "locator_patch", "may_edit_test_code": True,
                     "reason": "改测试代码"},
        "proposals": [dict(PROPOSAL)] if proposals is None else proposals,
    }


def write_report(folder: Path, payload: dict, name: str = "report.json") -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def decision(decision: str = "accept", verify: str = "passed", *,
             failure_id: str = "LC-002", proposal_id: str = "S001",
             ts: str = "2026-10-20T10:05:00Z", operator: str = "syn",
             note: str = "") -> dict:
    return {"ts": ts, "failure_id": failure_id, "proposal_id": proposal_id,
            "decision": decision, "verify_result": verify,
            "operator": operator, "note": note}


def write_ledger(path: Path, *rows: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")
    return path


def golden_cases(folder: Path, name: str) -> list:
    return json.loads((folder / name).read_text(encoding="utf-8"))["cases"]


@pytest.fixture
def scene(tmp_path):
    """一份账本 + 一份报告: 默认就是一条"采纳且验证通过"的正样本"""
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision())
    write_report(tmp_path / "reports", report_payload())
    return tmp_path, ledger, tmp_path / "reports", tmp_path / "golden"


# ---------------------------------------------------------------------------
# 正样本: 人采纳 + 验证通过
# ---------------------------------------------------------------------------

def test_a_verified_decision_becomes_a_positive_sample(scene):
    root, ledger, reports, golden = scene

    result = F.export(ledger, reports, golden=golden)

    assert result["positive_added"] == 1 and result["negative_added"] == 0
    case = golden_cases(golden, F.POSITIVE_NAME)[0]
    assert case["id"] == "FLY-LC-002-S001"
    assert case["label"] == "positive" and case["origin"] == "flywheel"
    assert case["traceback"] == TRACE and case["log"] == LOG
    assert case["expected_category"] == "脚本-定位器问题"
    assert case["expected_type"] == "locator_patch"
    assert case["expected_target_file"] == "tests/test_user.py"
    assert case["expected_diff"] == DIFF, "正样本要带上那个被验证通过的补丁"
    assert case["decided_by"] == "syn"
    assert case["decided_at"] == "2026-10-20T10:05:00Z"


def test_the_category_says_where_it_came_from(scene):
    """分类是机器给的, 人只确认了动作 —— 出处要写在 case 里, 别让人当人工标注用"""
    _, ledger, reports, golden = scene

    F.export(ledger, reports, golden=golden)

    case = golden_cases(golden, F.POSITIVE_NAME)[0]
    assert case["category_source"] == "diagnosis@rule.v2+blame.v1"
    assert case["blame"] == "test_code"


def test_the_case_id_is_stable():
    assert F._case_id("LC-002", "S001") == F._case_id("LC-002", "S001")
    assert F._case_id("LC-002", "S001") != F._case_id("LC-002", "S002")


# ---------------------------------------------------------------------------
# 负样本: 含理由, 但不进分母
# ---------------------------------------------------------------------------

def test_a_reject_becomes_a_negative_sample_with_the_reason(tmp_path):
    ledger = write_ledger(tmp_path / "decisions.jsonl",
                          decision("reject", "skipped", note="文案变更应同步产品"))
    write_report(tmp_path / "reports", report_payload())

    F.export(ledger, tmp_path / "reports", golden=tmp_path / "golden")

    case = golden_cases(tmp_path / "golden", F.NEGATIVE_NAME)[0]
    assert case["label"] == "negative"
    assert case["rejected_reason"] == "文案变更应同步产品", "负样本的全部价值就在这句理由"
    assert case["rejected_diff"] == DIFF


def test_an_accepted_but_failed_patch_is_also_negative(tmp_path):
    """采纳了但验证没过: 比"人眼拒绝"更硬的证据 —— 这个补丁真的不行"""
    ledger = write_ledger(tmp_path / "decisions.jsonl",
                          decision("accept", "failed", note="验证输出: 1 failed"))
    write_report(tmp_path / "reports", report_payload())

    result = F.export(ledger, tmp_path / "reports", golden=tmp_path / "golden")

    assert result["positive_added"] == 0 and result["negative_added"] == 1
    case = golden_cases(tmp_path / "golden", F.NEGATIVE_NAME)[0]
    assert case["verify_result"] == "failed"


def test_the_two_labels_never_share_a_file(scene):
    _, ledger, reports, golden = scene

    F.export(ledger, reports, golden=golden)

    assert (golden / F.POSITIVE_NAME).is_file()
    assert [c["label"] for c in golden_cases(golden, F.POSITIVE_NAME)] == ["positive"]
    assert json.loads((golden / F.POSITIVE_NAME).read_text(encoding="utf-8"))["kind"] \
        == "positive"


# ---------------------------------------------------------------------------
# 跳过: 没有标签或没有现场, 就宁可不导
# ---------------------------------------------------------------------------

def test_a_defer_is_not_a_sample_yet(tmp_path):
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision("defer", "skipped"))
    write_report(tmp_path / "reports", report_payload())

    result = F.export(ledger, tmp_path / "reports", golden=tmp_path / "golden")

    assert result["positive_added"] == result["negative_added"] == 0
    assert "defer" in result["skipped"][0]["reason"]


def test_a_decision_without_a_report_is_skipped_by_name(tmp_path):
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision())
    (tmp_path / "reports").mkdir()          # 目录在, 里面没有这一份

    result = F.export(ledger, tmp_path / "reports", golden=tmp_path / "golden")

    assert result["positive_added"] == 0
    assert result["skipped"][0]["failure_id"] == "LC-002"
    assert "report.json" in result["skipped"][0]["reason"]
    assert not (tmp_path / "golden" / F.POSITIVE_NAME).exists(), "没有样本就别造空文件"


def test_a_report_without_that_proposal_is_skipped(tmp_path):
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision(proposal_id="S009"))
    write_report(tmp_path / "reports", report_payload())

    result = F.export(ledger, tmp_path / "reports", golden=tmp_path / "golden")

    assert result["positive_added"] == 0
    assert "S009" in result["skipped"][0]["proposal_id"]


def test_a_positive_without_a_category_is_skipped(tmp_path):
    """空期望值会让准确率虚高 —— 没有 category 就不是归因样本"""
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision())
    write_report(tmp_path / "reports", report_payload(category=""))

    result = F.export(ledger, tmp_path / "reports", golden=tmp_path / "golden")

    assert result["positive_added"] == 0
    assert "category" in result["skipped"][0]["reason"]


def test_since_keeps_this_week_and_skips_the_rest(tmp_path):
    ledger = write_ledger(
        tmp_path / "decisions.jsonl",
        decision(ts="2026-10-01T09:00:00Z", failure_id="LC-001"),
        decision(ts="2026-10-25T09:00:00Z"))
    write_report(tmp_path / "reports", report_payload())
    write_report(tmp_path / "reports", report_payload("LC-001"), name="old.json")

    result = F.export(ledger, tmp_path / "reports", golden=tmp_path / "golden",
                      since="2026-10-20")

    assert result["positive_added"] == 1
    assert any("早于 2026-10-20" in item["reason"] for item in result["skipped"])


def test_a_missing_ledger_explains_itself(tmp_path):
    with pytest.raises(F.FlywheelError, match="没有账本"):
        F.export(tmp_path / "nope.jsonl", tmp_path, golden=tmp_path / "golden")


def test_a_missing_report_folder_explains_itself(tmp_path):
    write_ledger(tmp_path / "decisions.jsonl", decision())

    with pytest.raises(F.FlywheelError, match="报告位置不存在"):
        F.export(tmp_path / "decisions.jsonl", tmp_path / "nope",
                 golden=tmp_path / "golden")


# ---------------------------------------------------------------------------
# 幂等: 只追加, 不改既有条目
# ---------------------------------------------------------------------------

def test_exporting_twice_does_not_duplicate(scene):
    _, ledger, reports, golden = scene

    F.export(ledger, reports, golden=golden)
    second = F.export(ledger, reports, golden=golden)

    assert second["positive_added"] == 0
    assert second["duplicates"] == 1
    assert len(golden_cases(golden, F.POSITIVE_NAME)) == 1


def test_export_never_rewrites_existing_cases(scene):
    """黄金集可能被人手工整理过 (加 note / 调分类), 导出不能把那些改动冲掉"""
    _, ledger, reports, golden = scene
    golden.mkdir(parents=True)
    (golden / F.POSITIVE_NAME).write_text(json.dumps({
        "version": 1, "kind": "positive", "cases": [
            {"id": "FLY-OLD-S001", "label": "positive", "traceback": "old",
             "log": "", "expected_category": "产品缺陷", "note": "人工复核过"},
        ]}, ensure_ascii=False), encoding="utf-8")

    F.export(ledger, reports, golden=golden)

    cases = golden_cases(golden, F.POSITIVE_NAME)
    assert [case["id"] for case in cases] == ["FLY-OLD-S001", "FLY-LC-002-S001"]
    assert cases[0]["note"] == "人工复核过"


def test_dry_run_writes_nothing(scene):
    _, ledger, reports, golden = scene

    result = F.export(ledger, reports, golden=golden, dry_run=True)

    assert result["positive_added"] == 1 and result["dry_run"] is True
    assert not golden.exists()


# ---------------------------------------------------------------------------
# 现场: 截断 / 多份报告 / 递归找
# ---------------------------------------------------------------------------

def test_a_long_traceback_is_clipped_but_keeps_the_ending(tmp_path):
    """栈的开头是用例名, 结尾才是那句异常 —— 中间截掉, 两头都留"""
    long_trace = "Traceback\n" + ("frame line\n" * 2000) + "ValueError: 真正的原因"
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision())
    write_report(tmp_path / "reports", report_payload(traceback=long_trace))

    F.export(ledger, tmp_path / "reports", golden=tmp_path / "golden")

    case = golden_cases(tmp_path / "golden", F.POSITIVE_NAME)[0]
    assert len(case["traceback"]) < F.MAX_TEXT_CHARS + 100
    assert case["traceback"].startswith("Traceback")
    assert case["traceback"].endswith("ValueError: 真正的原因")
    assert case["truncated"] is True


def test_the_newest_report_wins_when_a_failure_has_two(tmp_path):
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision())
    write_report(tmp_path / "reports", report_payload(
        created_at="2026-10-19T10:00:00Z", traceback="旧现场"), name="old.json")
    write_report(tmp_path / "reports", report_payload(
        created_at="2026-10-21T10:00:00Z", traceback="新现场"), name="new.json")

    F.export(ledger, tmp_path / "reports", golden=tmp_path / "golden")

    assert golden_cases(tmp_path / "golden", F.POSITIVE_NAME)[0]["traceback"] == "新现场"


def test_reports_are_found_recursively_and_venv_is_skipped(tmp_path):
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision())
    write_report(tmp_path / "reports" / "run-2026-10-20", report_payload())
    # 依赖目录里也有 json: 扫进去既慢又可能撞上别的 failure_id
    write_report(tmp_path / ".venv" / "lib", report_payload("LC-999"), name="x.json")

    reports = F.read_reports(tmp_path)

    assert list(reports) == ["LC-002"], ".venv 里的 report 不该被扫进来"
    assert F.read_reports(tmp_path / ".venv" / "lib" / "x.json")["LC-999"], \
        "显式给文件时不再挑目录"


# ---------------------------------------------------------------------------
# 接上评测: 正样本进分母, 负样本不进
# ---------------------------------------------------------------------------

MANUAL = {
    "_comment": "手写两条",
    "cases": [
        {"id": "M1", "traceback": "NoSuchElementException: no such element",
         "log": "", "expected_category": "脚本-定位器问题"},
        {"id": "M2", "traceback": "AssertionError: 期望列表 10 条, 实际 3 条",
         "log": "", "expected_category": "产品缺陷"},
    ],
}


def test_exported_samples_feed_the_analyzer(monkeypatch, tmp_path):
    """飞轮写出的 case 与手写 analyzer case 同形 —— 不用改评测代码就能进分母"""
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "analyzer_cases.json").write_text(
        json.dumps(MANUAL, ensure_ascii=False), encoding="utf-8")
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision())
    write_report(tmp_path / "reports", report_payload())
    F.export(ledger, tmp_path / "reports", golden=golden)
    monkeypatch.setattr(E, "_GOLDEN", str(golden))

    result = E.run()

    assert result["n"] == 3 and result["n_manual"] == 2 and result["n_flywheel"] == 1
    origins = {row["id"]: row["origin"] for row in result["rule"]["cases"]}
    assert origins["FLY-LC-002-S001"] == "flywheel" and origins["M1"] == "manual"


def test_the_analyzer_can_step_back_to_the_handwritten_baseline(monkeypatch, tmp_path):
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "analyzer_cases.json").write_text(
        json.dumps(MANUAL, ensure_ascii=False), encoding="utf-8")
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision())
    write_report(tmp_path / "reports", report_payload())
    F.export(ledger, tmp_path / "reports", golden=golden)
    monkeypatch.setattr(E, "_GOLDEN", str(golden))

    result = E.run(include_flywheel=False)

    assert result["n"] == 2 and result["n_flywheel"] == 0


def test_a_negative_sample_never_reaches_the_analyzer(monkeypatch, tmp_path):
    """把被拒的补丁放进分类准确率, 等于把正确的归因算成错"""
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "analyzer_cases.json").write_text(
        json.dumps(MANUAL, ensure_ascii=False), encoding="utf-8")
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision("reject", "skipped"))
    write_report(tmp_path / "reports", report_payload())
    F.export(ledger, tmp_path / "reports", golden=golden)
    monkeypatch.setattr(E, "_GOLDEN", str(golden))

    ids = [case["id"] for case in E._load_cases()]

    assert ids == ["M1", "M2"], "负样本在另一个文件里, 不该被加载"


def test_the_eval_report_says_where_the_samples_came_from(monkeypatch, tmp_path):
    """数字变大了得看得出是哪来的, 否则基线漂移了没人知道"""
    from ai_kit.evaluation import run_eval as R

    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "analyzer_cases.json").write_text(
        json.dumps(MANUAL, ensure_ascii=False), encoding="utf-8")
    ledger = write_ledger(tmp_path / "decisions.jsonl", decision())
    write_report(tmp_path / "reports", report_payload())
    F.export(ledger, tmp_path / "reports", golden=golden)
    monkeypatch.setattr(E, "_GOLDEN", str(golden))

    markdown = R._md(R.run(suites=["analyzer"]))

    assert "样本: 3 条 (手写 2 + 飞轮 1)" in markdown


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_dry_run_tells_what_would_be_added(scene, capsys):
    _, ledger, reports, golden = scene

    code = F.main(["--decisions", str(ledger), "--reports", str(reports),
                   "--golden", str(golden), "--dry-run"])

    out = capsys.readouterr().out
    assert code == 0
    assert "正样本 +1" in out and "dry-run" in out
    assert not golden.exists()


def test_cli_without_a_ledger_exits_two(tmp_path, capsys):
    code = F.main(["--decisions", str(tmp_path / "nope.jsonl"),
                   "--golden", str(tmp_path / "golden")])

    assert code == 2
    assert "没有账本" in capsys.readouterr().err
