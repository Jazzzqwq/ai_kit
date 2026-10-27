# -*- coding: utf-8 -*-
"""
套件2: 归因评测 (Failure Analyzer) — 分类准确率 + 置信度校准 (ECE) [+ 可选 LLM 对比/成本]

默认 (无 --llm): 只跑 Phase-1 规则, 纯离线 / 0 成本 / 确定性。
--llm: 同一批再跑 Phase-2 LLM (含多模态截图), 对比"规则 vs LLM"准确率与校准, 并统计每次成本。
       直接验证 README 里 0.80 vs 0.97 的估算。
"""
from __future__ import annotations

import base64
import json
import os
from typing import Any, Dict, List, Optional

from ai_kit.analyzer import RULES_VERSION, diagnose
from ai_kit.evaluation import driver_provider, metrics
from ai_kit.evaluation import cost_tracker as _ct  # t7 提供; 若无则 --llm 成本记 n/a
from ai_kit.proposal import diagnoser as _blame

_GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
_FIXTURES = os.path.join(_GOLDEN, "fixtures")
_MANUAL = "analyzer_cases.json"
# 6.6 数据飞轮: 人采纳并验证通过的补丁 -> 正样本, 与手写 case 同形
_FLYWHEEL = "flywheel_positive.json"


def _read_cases(name: str) -> List[Dict[str, Any]]:
    """读一份 golden 文件; 不存在 = 空 (飞轮文件只在导出过一次后才存在)"""
    path = os.path.join(_GOLDEN, name)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    cases = data.get("cases") if isinstance(data, dict) else None
    return [c for c in cases if isinstance(c, dict)] if isinstance(cases, list) else []


def _load_cases(include_flywheel: bool = True) -> List[Dict[str, Any]]:
    """手写黄金集 + 飞轮正样本 (6.6)

    飞轮 case 与手写 analyzer case 同形 (traceback / log / expected_category), 合并时
    只标 origin, 不改字段 —— 它照常进 accuracy 与 ECE 的分母, 评测集随使用增长。
    没有 expected_category 的条目跳过: 空期望值会让准确率虚高。
    """
    cases = [dict(c, origin="manual") for c in _read_cases(_MANUAL)]
    if include_flywheel:
        cases += [dict(c, origin="flywheel") for c in _read_cases(_FLYWHEEL)
                  if str(c.get("expected_category") or "").strip()]
    return cases


def _render_screenshot_b64(driver, fixture_name: str) -> Optional[str]:
    """把夹具 HTML 渲染成 PNG base64 (供多模态)"""
    if driver is None:
        return None
    try:
        driver.get(driver_provider.to_file_url(os.path.join(_FIXTURES, fixture_name)))
        png = driver.get_screenshot_as_png()
        if png and len(png) > 100:
            return base64.b64encode(png).decode("ascii")
    except Exception:  # noqa: BLE001
        pass
    return None


def _eval_mode(cases: List[Dict[str, Any]], use_llm: bool,
               driver=None) -> Dict[str, Any]:
    pairs: List[tuple] = []
    calib: List[tuple] = []
    case_rows: List[Dict[str, Any]] = []
    cost = _ct.CostTracker()

    for c in cases:
        shot = None
        if use_llm and c.get("screenshot_fixture"):
            shot = _render_screenshot_b64(driver, c["screenshot_fixture"])

        tb = c.get("traceback", "")
        log = c.get("log", "")
        if use_llm:
            before = cost.snapshot_hook()  # 记录调用前 token 累计
            diag = diagnose(tb, log, use_llm=True, screenshot_base64=shot)
            cost.record_since(before)
        else:
            diag = diagnose(tb, log, use_llm=False)

        predicted = diag.category
        expected = c["expected_category"]
        correct = (predicted == expected)
        pairs.append((predicted, expected))
        calib.append((diag.confidence, correct))
        case_rows.append({
            "id": c["id"],
            "origin": c.get("origin", "manual"),
            "expected": expected,
            "predicted": predicted,
            "confidence": round(diag.confidence, 3),
            "source": diag.source,
            "correct": correct,
            "had_screenshot": bool(shot),
        })

    acc = metrics.classification_accuracy(pairs)
    cal = metrics.expected_calibration_error(calib, n_bins=10)
    return {
        "use_llm": use_llm,
        "rule_version": RULES_VERSION,
        "accuracy": acc,
        "confusion_matrix": metrics.confusion_matrix(pairs),
        "calibration": cal,
        "avg_confidence": round(sum(cf for cf, _ in calib) / len(calib), 3) if calib else 0.0,
        "cost": cost.summary(),
        "cases": case_rows,
    }


# ---------------------------------------------------------------------------
# blame 层 (6.2): 规则版本 + 容错分类只有在这一层才有, 不跑一次就只是死字段
# ---------------------------------------------------------------------------

def _eval_blame(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """同一批黄金集再过一遍方案层 (纯规则, 0 成本)

    accuracy 是严格口径; tolerant_accuracy 额外把所有 acceptable_categories 里列出的
    分类也算对 —— 两个数分开报, 容错不会把主指标抬高。
    """
    rows: List[Dict[str, Any]] = []
    pairs: List[tuple] = []
    for c in cases:
        context = _blame.FailureContext(traceback=c.get("traceback", ""),
                                        log=c.get("log", ""))
        diag = _blame.diagnose(context, use_llm=False)
        expected = c["expected_category"]
        strict = diag.category == expected
        pairs.append((diag.category, expected))
        rows.append({
            "id": c["id"],
            "expected": expected,
            "predicted": diag.category,
            "blame": diag.blame,
            "root_cause": diag.root_cause,
            "confidence": round(diag.confidence, 3),
            "acceptable": list(diag.acceptable_categories),
            "rule_version": diag.rule_version,
            "correct": strict,
            "tolerant": strict or expected in diag.acceptable_categories,
        })

    blame_counts: Dict[str, int] = {}
    for row in rows:
        blame_counts[row["blame"]] = blame_counts.get(row["blame"], 0) + 1
    return {
        "rule_version": _blame.RULE_VERSION,
        "accuracy": metrics.classification_accuracy(pairs),
        "tolerant_accuracy": metrics.rate([row["tolerant"] for row in rows]),
        "n_tolerated": sum(1 for row in rows if row["tolerant"] and not row["correct"]),
        "blame_counts": blame_counts,
        "cases": rows,
    }


def run(use_llm: bool = False, driver=None,
        include_flywheel: bool = True) -> Dict[str, Any]:
    cases = _load_cases(include_flywheel)

    # 规则模式 (始终跑, 0 成本)
    rule = _eval_mode(cases, use_llm=False, driver=None)

    n_flywheel = sum(1 for c in cases if c.get("origin") == "flywheel")
    result: Dict[str, Any] = {
        "suite": "analyzer",
        "status": "OK",
        "n": len(cases),
        "n_manual": len(cases) - n_flywheel,
        "n_flywheel": n_flywheel,
        "rule": rule,
        "blame": _eval_blame(cases),
    }

    # LLM 模式 (可选对比)
    if use_llm:
        own = None
        if driver is None:
            driver = driver_provider.get_driver()
            own = driver
        try:
            llm = _eval_mode(cases, use_llm=True, driver=driver)
        finally:
            if own:
                driver_provider.quit_driver(own)
        result["llm"] = llm
        # 直接给"规则 vs LLM"置信/准确率对比 (对 README 0.80/0.97)
        result["compare"] = {
            "rule_accuracy": rule["accuracy"],
            "llm_accuracy": llm["accuracy"],
            "rule_avg_confidence": rule["avg_confidence"],
            "llm_avg_confidence": llm["avg_confidence"],
            "rule_ece": rule["calibration"]["ece"],
            "llm_ece": llm["calibration"]["ece"],
            "llm_cost_per_case": llm["cost"].get("per_call", "n/a"),
        }

    return result


if __name__ == "__main__":
    import pprint
    pprint.pprint(run(use_llm=False))
