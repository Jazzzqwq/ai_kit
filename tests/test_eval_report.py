# -*- coding: utf-8 -*-
"""
评测报告 (7.1 口径落在 md 上) 自检 — 标题指标 / 分层对照 / 每页成本 / 冒烟

跑: pytest tests/test_eval_report.py
"""
from __future__ import annotations

from ai_kit.evaluation import eval_analyzer, run_eval as R


def _analyzer_rule() -> dict:
    return {
        "rule_version": "rules.v1",
        "accuracy": 0.9,
        "accuracy_tolerant": 0.95,
        "n_tolerated": 1,
        "avg_confidence": 0.8,
        "confidence": {"gap": -0.1, "mean_confidence": 0.8, "accuracy": 0.9,
                       "verdict": "欠自信", "band": 0.02},
        "calibration": {"ece": 0.08, "n": 20, "reliability": [
            {"bin": f"{i/5:.1f}-{(i+1)/5:.1f}",
             "count": (20 if i == 4 else 0),
             "avg_confidence": (0.8 if i == 4 else None),
             "accuracy": (0.9 if i == 4 else None),
             "gap": (0.1 if i == 4 else None)} for i in range(5)]},
        "confusion_matrix": {"产品缺陷": {"产品缺陷": 20}},
        "cases": [],
    }


def _report() -> dict:
    return {
        "meta": {"generated_at": "2026-10-27 10:00:00", "use_llm": False,
                 "driver_available": True, "suites": ["analyzer"], "limit": 0},
        "reports": {
            "analyzer": {"suite": "analyzer", "status": "OK", "n": 20,
                         "n_manual": 20, "n_flywheel": 0, "rule": _analyzer_rule()},
            "healer": {"suite": "healer", "status": "OK", "n": 4, "use_llm": False,
                       "top1": 0.75, "top3": 1.0, "no_hit_cases": 0,
                       "per_layer_winner": {"L1": 1, "L2": 3, "L3": 0, "L4": 0},
                       "per_layer_hit": {"L1": 1, "L2": 4, "L3": 1, "L4": 0},
                       "layers": {"n": 4, "no_hit": 0, "no_hit_share": 0.0, "per_layer": {
                           "L1": {"count": 1, "share": 0.25, "baseline": 0.3, "delta": -0.05},
                           "L2": {"count": 3, "share": 0.75, "baseline": 0.5, "delta": 0.25},
                           "L3": {"count": 0, "share": 0.0, "baseline": 0.1, "delta": -0.1},
                           "L4": {"count": 0, "share": 0.0, "baseline": 0.1, "delta": -0.1},
                       }},
                       "cases": []},
            "generator": {"suite": "generator", "status": "OK", "mode": "skeleton",
                          "skeleton_mode": True, "n": 2, "n_pages": 2,
                          "py_compile_pass_rate": 1.0, "avg_coverage_recall": 0.5,
                          "avg_name_collision_rate": 0.0, "avg_step_near_dup_rate": 0.0,
                          "cost": {"per_call": 0.0, "per_page": 0.0, "llm_calls": 0}},
        },
    }


# ---------------------------------------------------------------------------
# 标题指标
# ---------------------------------------------------------------------------

def test_the_report_leads_with_over_under_confidence_not_bare_ece():
    md = R._md(_report())

    assert "过度/欠自信" in md
    assert "**-0.100** (欠自信)" in md
    assert "ECE (5 箱)" in md


def test_both_accuracies_are_shown_side_by_side():
    md = R._md(_report())

    assert "严格 90.0%" in md and "含容错 95.0% (宽容 1 条)" in md


def test_the_report_says_which_rule_version_produced_the_numbers():
    assert "规则版本 rules.v1" in R._md(_report())


# ---------------------------------------------------------------------------
# 分层对照 README
# ---------------------------------------------------------------------------

def test_the_layered_table_shows_the_gap_to_the_readme_estimate():
    md = R._md(_report())

    assert "+25.0 pp" in md and "-5.0 pp" in md
    assert "| L2 | 3 | 75.0% | 4 | 50.0% | +25.0 pp |" in md


def test_an_offline_zero_l4_is_explained_not_hidden():
    md = R._md(_report())

    assert "L4 只有 `--llm`" in md, "L4=0 是口径限制, 得写在表下面"


# ---------------------------------------------------------------------------
# 成本: 每次 / 每页
# ---------------------------------------------------------------------------

def test_the_generator_block_reports_cost_per_page():
    md = R._md(_report())

    assert "每页成本: 0.0 元/页 (共 2 页)" in md


def test_an_offline_report_does_not_pretend_to_have_costs():
    md = R._md(_report())

    assert "本次未调用 LLM" in md


# ---------------------------------------------------------------------------
# --smoke: 三条都要过
# ---------------------------------------------------------------------------

def _smoke_report(analyzer_calls: int, generator_calls: int) -> dict:
    return {"reports": {
        "analyzer": {"llm": {"cost": {"llm_calls": analyzer_calls,
                                      "prompt_tokens": 100 * analyzer_calls,
                                      "completion_tokens": 10 * analyzer_calls,
                                      "per_call": 0.001,
                                      "price_per_1k": {"input": 0.0008, "output": 0.002}}}},
        "generator": {"cost": {"llm_calls": generator_calls,
                               "prompt_tokens": 200 * generator_calls,
                               "completion_tokens": 20 * generator_calls,
                               "per_call": 0.002,
                               "price_per_1k": {"input": 0.0008, "output": 0.002}}},
    }}


def test_smoke_passes_when_both_suites_really_called_the_model():
    result = R.smoke_check(_smoke_report(1, 1))

    assert result["ok"] is True and [c["suite"] for c in result["checks"]] == \
        ["analyzer", "generator"]


def test_smoke_fails_when_usage_was_never_collected():
    """没调到模型 / 没采到 token, 冒烟就不算过 —— 否则 CI 绿着但链路是断的"""
    result = R.smoke_check(_smoke_report(0, 1))

    assert result["ok"] is False
    assert result["checks"][0]["ok"] is False


def test_smoke_fails_when_the_price_table_does_not_know_the_model():
    report = _smoke_report(1, 1)
    report["reports"]["generator"]["cost"]["price_per_1k"] = "n/a"

    assert R.smoke_check(report)["ok"] is False


def test_smoke_forces_phase_2_even_when_rules_are_confident(monkeypatch):
    """回归: 黄金集第一条被规则 0.9 高分命中, diagnose() 的生产门槛不升级 LLM,
    llm_calls 恒为 0 —— 冒烟会把"通路是好的"误报成"通路坏了"。
    smoke 模式必须绕过门槛直接走 Phase 2。"""
    from ai_kit.analyzer import CATEGORY_SCRIPT_HEALER, FailureDiagnosis

    calls = []

    def fake_llm(tb, log, screenshot_base64=None):
        calls.append(tb)
        return FailureDiagnosis(category=CATEGORY_SCRIPT_HEALER, confidence=0.95,
                                summary="fake", suggestion="fake", source="llm")

    monkeypatch.setattr(eval_analyzer, "diagnose_with_llm", fake_llm)
    monkeypatch.setattr(eval_analyzer.driver_provider, "get_driver", lambda: None)

    result = eval_analyzer.run(use_llm=True, limit=1)

    assert calls, "smoke 模式必须真的走到 Phase 2"
    assert result["llm"]["cases"][0]["source"] == "llm"


def test_non_smoke_llm_run_still_respects_the_escalation_gate(monkeypatch):
    """门槛本身不能被拆: 非 smoke 的规则高分命中仍然不该调 LLM"""
    calls = []
    monkeypatch.setattr(eval_analyzer, "diagnose_with_llm",
                        lambda tb, log, screenshot_base64=None: calls.append(tb) or None)
    monkeypatch.setattr(eval_analyzer.driver_provider, "get_driver", lambda: None)

    eval_analyzer._eval_mode(eval_analyzer._load_cases()[:1], use_llm=True,
                             driver=None, force_llm=False)

    assert not calls


def test_smoke_falls_back_to_rules_when_the_llm_is_unavailable(monkeypatch):
    """强制升级不等于裸奔: LLM 不可用还是要降级回规则结果"""
    monkeypatch.setattr(eval_analyzer, "diagnose_with_llm",
                        lambda tb, log, screenshot_base64=None: None)
    monkeypatch.setattr(eval_analyzer.driver_provider, "get_driver", lambda: None)

    result = eval_analyzer.run(use_llm=True, limit=1)

    assert result["llm"]["cases"][0]["source"] == "rule"


# ---------------------------------------------------------------------------
# 真跑一次离线归因套件: 口径要在真数据上成立
# ---------------------------------------------------------------------------

def test_the_real_analyzer_run_reports_strict_and_tolerant_separately():
    result = eval_analyzer.run(use_llm=False)
    rule = result["rule"]

    assert result["n"] >= 30
    assert rule["accuracy"] <= rule["accuracy_tolerant"]
    assert rule["n_tolerated"] == sum(
        1 for c in rule["cases"] if c["tolerant"] and not c["correct"])


def test_the_real_run_computes_ece_in_five_bins():
    rule = eval_analyzer.run(use_llm=False)["rule"]

    assert len(rule["calibration"]["reliability"]) == 5


def test_the_confidence_gap_is_mean_confidence_minus_accuracy():
    rule = eval_analyzer.run(use_llm=False)["rule"]
    gap = rule["confidence"]

    assert abs(gap["gap"] - (gap["mean_confidence"] - gap["accuracy"])) < 1e-3
    assert gap["verdict"] in ("过度自信", "欠自信", "校准良好")
