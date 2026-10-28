# -*- coding: utf-8 -*-
"""任务卡6: 方案层人决策评测 —— 接受率 / 验证成功率 (按方案类型分列)

锁死这些事:
    没有账本 / 空账本        -> SKIPPED, 绝不拿 0 条编出"接受率 0%"
    accept+passed            -> 计入验证成功率分子
    reject                   -> 进接受率分母, 不进验证 (accepted=0 时是 n/a 不是 0%)
    defer                    -> 未决定, 不进接受率分母
    类型不在账本七个键里      -> 靠 join report.json; join 不上落 unknown, 不猜

账本是本地文件、不进 git (decisions.jsonl); 这里全部用 tmp_path 造数据。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_kit.evaluation import eval_proposal as P
from ai_kit.evaluation import run_eval as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _row(decision="accept", verify="passed", *, failure_id="LC-1",
         proposal_id="S001", ts="2026-10-20T10:00:00Z", operator="syn",
         note="") -> dict:
    return {"ts": ts, "failure_id": failure_id, "proposal_id": proposal_id,
            "decision": decision, "verify_result": verify,
            "operator": operator, "note": note}


def _write_ledger(path: Path, *rows: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def _report(folder: Path, failure_id: str, proposals: list) -> Path:
    """满足 _is_report 的最小报告: 有 failure_id + proposals + diagnosis"""
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"failure_id": failure_id, "created_at": "2026-10-20T09:00:00Z",
               "diagnosis": {"category": "脚本-定位器问题"}, "proposals": proposals}
    path = folder / f"{failure_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# SKIPPED: 没账本 / 空账本 —— 不编数字
# ---------------------------------------------------------------------------

def test_run_skips_when_there_is_no_ledger(tmp_path):
    missing = tmp_path / "decisions.jsonl"

    result = P.run(decisions=str(missing), reports=str(tmp_path))

    assert result["status"] == "SKIPPED"
    assert "没有账本" in result["reason"]
    assert "n" not in result, "SKIPPED 不能带任何指标数字"


def test_run_skips_when_ledger_is_empty(tmp_path):
    ledger = _write_ledger(tmp_path / "decisions.jsonl")  # 零行

    result = P.run(decisions=str(ledger), reports=str(tmp_path))

    assert result["status"] == "SKIPPED"
    assert "空的" in result["reason"]


# ---------------------------------------------------------------------------
# 两项指标 + 类型 join
# ---------------------------------------------------------------------------

def test_metrics_and_type_join_from_report(tmp_path):
    ledger = _write_ledger(
        tmp_path / "decisions.jsonl",
        _row("accept", "passed", proposal_id="S001"),
        _row("reject", "skipped", proposal_id="S002"),
    )
    reports = tmp_path / "reports"
    _report(reports, "LC-1", [
        {"proposal_id": "S001", "type": "locator_patch"},
        {"proposal_id": "S002", "type": "wait_adjust"},
    ])

    result = P.run(decisions=str(ledger), reports=str(reports))

    assert result["status"] == "OK"
    assert result["n_type_resolved"] == 2 and result["n_type_unknown"] == 0
    by = result["by_type"]
    assert by["locator_patch"]["acceptance_rate"] == 1.0
    assert by["locator_patch"]["verify_success_rate"] == 1.0
    # wait_adjust 只有 1 条 reject: accepted=0, 纯函数把率算成 0.0 (渲染层显 n/a)
    assert by["wait_adjust"]["rejected"] == 1 and by["wait_adjust"]["accepted"] == 0
    assert result["overall"]["acceptance_rate"] == 0.5  # accept 1 / decided 2


def test_defer_stays_out_of_acceptance_denominator(tmp_path):
    ledger = _write_ledger(
        tmp_path / "decisions.jsonl",
        _row("defer", "skipped", proposal_id="S001"),
        _row("accept", "passed", proposal_id="S002"),
    )
    reports = tmp_path / "reports"
    _report(reports, "LC-1", [
        {"proposal_id": "S001", "type": "data_fix"},
        {"proposal_id": "S002", "type": "data_fix"},
    ])

    result = P.run(decisions=str(ledger), reports=str(reports))

    o = result["overall"]
    assert o["deferred"] == 1 and o["decided"] == 1 and o["accepted"] == 1
    assert o["acceptance_rate"] == 1.0, "defer 不进分母: accept/(accept+reject)=1/1"


def test_unjoinable_rows_fall_into_unknown(tmp_path):
    """仓库里没有对应 report.json -> 类型 join 不上, 老实落 unknown 桶"""
    ledger = _write_ledger(tmp_path / "decisions.jsonl", _row("reject", "skipped"))

    result = P.run(decisions=str(ledger), reports=str(tmp_path / "no_reports"))

    assert result["n_type_resolved"] == 0 and result["n_type_unknown"] == 1
    assert "unknown" in result["by_type"]
    assert result["by_type"]["unknown"]["rejected"] == 1


def test_env_var_locates_the_ledger(tmp_path, monkeypatch):
    ledger = _write_ledger(tmp_path / "custom.jsonl", _row("accept", "passed"))
    monkeypatch.setenv(P.DECISIONS_ENV, str(ledger))

    result = P.run(reports=str(tmp_path))

    assert result["status"] == "OK"
    assert result["decisions"] == str(ledger)


# ---------------------------------------------------------------------------
# run_eval 第四节渲染
# ---------------------------------------------------------------------------

def _base_report() -> dict:
    return {"meta": {"generated_at": "2026-10-28 10:00:00", "use_llm": False,
                     "driver_available": False, "suites": [], "limit": 0},
            "reports": {}}


def test_md_renders_section_four_table():
    report = _base_report()
    report["proposal"] = {
        "suite": "proposal", "status": "OK",
        "decisions": "decisions.jsonl", "reports_scanned": ".",
        "n_rows": 1, "n_type_resolved": 0, "n_type_unknown": 1, "n": 1,
        "overall": {"n": 1, "decided": 1, "accepted": 0, "rejected": 1,
                    "deferred": 0, "verified": 0,
                    "acceptance_rate": 0.0, "verify_success_rate": 0.0},
        "by_type": {"unknown": {"n": 1, "decided": 1, "accepted": 0, "rejected": 1,
                                "deferred": 0, "verified": 0,
                                "acceptance_rate": 0.0, "verify_success_rate": 0.0}},
    }

    md = R._md(report)

    assert "## 4. 方案层人决策" in md
    assert "| 方案类型 | 已决定 | 采纳 | 拒绝 | 挂起 | 接受率 | 验证成功率 |" in md
    assert "| unknown | 1 | 0 | 1 | 0 | 0.0% | n/a |" in md, "accepted=0 时验证成功率显 n/a"
    assert "认出 0 / unknown 1" in md
    assert "不是编的类型" in md


def test_md_cost_and_smoke_sections_shifted():
    report = _base_report()
    report["proposal"] = {"status": "SKIPPED", "reason": "没有账本 /tmp/x"}

    md = R._md(report)

    assert "## 4. 方案层人决策" in md and "⚠ SKIPPED" in md
    assert "## 5. 成本汇总" in md, "成本汇总已从 4 顺延到 5"


# ---------------------------------------------------------------------------
# 数据安全: 账本是本地真数据, 绝不进 git
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_decisions_ledger_is_gitignored_and_untracked():
    for name in ("decisions.jsonl", "decisions_2026.jsonl", "patches/"):
        r = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "--", name],
                           capture_output=True)
        assert r.returncode == 0, f"{name!r} 未被 .gitignore 忽略 (账本是本地隐私源)"
    tracked = subprocess.run(["git", "-C", ROOT, "ls-files", "--error-unmatch",
                              "decisions.jsonl"], capture_output=True)
    assert tracked.returncode != 0, "decisions.jsonl 已被 git 跟踪, 必须 git rm --cached"
