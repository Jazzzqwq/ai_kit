# -*- coding: utf-8 -*-
"""
套件2: 归因评测 (Failure Analyzer) — 分类准确率 + 过度/欠自信 + ECE [+ 可选 LLM 对比/成本]

默认 (无 --llm): 只跑 Phase-1 规则, 纯离线 / 0 成本 / 确定性。
--llm: 同一批再跑 Phase-2 LLM (含多模态截图), 对比"规则 vs LLM"准确率与校准, 并统计每次成本。
       直接验证 README 里 0.80 vs 0.97 的估算。

两个准确率一起报 (分母同一个, 黄金集 30 条):
- accuracy          严格: 预测 == expected.category
- accuracy_tolerant 含容错: 预测 ∈ expected.category ∪ expected.acceptable_categories
  容错由人在黄金集里写 (不是规则自报), 所以不会自己给自己放宽。
"""
from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Optional

from ai_kit.analyzer import RULES_VERSION, diagnose, diagnose_with_llm
from ai_kit.evaluation import driver_provider, golden, metrics
from ai_kit.evaluation import cost_tracker as _ct  # t7 提供; 若无则 --llm 成本记 n/a
from ai_kit.proposal import diagnoser as _blame

_GOLDEN = golden.DEFAULT_DIR
_FIXTURES = os.path.join(_GOLDEN, "fixtures")
# 6.6 数据飞轮: 人采纳并验证通过的补丁 -> 正样本, 与手写 analyzer case 同形
_FLYWHEEL = "flywheel_positive.json"

# 7.1: ECE 分 5 箱, 标题指标另用过度/欠自信 gap (单个数可读, 分箱细节留给 ECE 表)
_ECE_BINS = 5


def _load_cases(include_flywheel: bool = True) -> List[Dict[str, Any]]:
    """手写黄金集 + 飞轮正样本 (6.6), 都过 golden.normalize 归一成 7.2 schema

    飞轮 case 与手写 case 同形, 合并时不改字段 —— 它照常进 accuracy 与 ECE 的分母,
    评测集随使用增长。没有 expected.category 的条目跳过: 空期望值会让准确率虚高。
    """
    extra = (_FLYWHEEL,) if include_flywheel else ()
    cases = golden.load("analyzer", directory=_GOLDEN, extra=extra)
    return [c for c in cases
            if str((c.get("expected") or {}).get("category") or "").strip()]


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
               driver=None, force_llm: bool = False) -> Dict[str, Any]:
    """force_llm: 冒烟用。diagnose() 里有"规则置信 >= 0.8 就不升级"的生产门槛,
    黄金集第一条几乎总能被规则高分命中 —— 拿它冒烟会让 llm_calls 恒为 0,
    把"通路是好的"误报成"通路坏了"。冒烟验的是 LLM 通路/usage/价目表三件事,
    不是升级判断, 所以 smoke 模式直接走 Phase 2; LLM 挂了仍降级回规则。"""
    pairs: List[tuple] = []
    calib: List[tuple] = []
    tolerant_flags: List[bool] = []
    n_tolerated = 0
    case_rows: List[Dict[str, Any]] = []
    cost = _ct.CostTracker()

    for c in cases:
        sample = c.get("input") or {}
        shot = None
        if use_llm and sample.get("screenshot"):
            shot = _render_screenshot_b64(driver, sample["screenshot"])

        tb = str(sample.get("traceback") or "")
        log = str(sample.get("log") or "")
        if use_llm:
            before = cost.snapshot_hook()  # 记录调用前 token 累计
            if force_llm:
                llm_diag = diagnose_with_llm(tb, log, screenshot_base64=shot)
                diag = llm_diag if llm_diag is not None else diagnose(tb, log, use_llm=False)
            else:
                diag = diagnose(tb, log, use_llm=True, screenshot_base64=shot)
            cost.record_since(before)
        else:
            diag = diagnose(tb, log, use_llm=False)

        expected = str((c.get("expected") or {}).get("category") or "")
        acceptable = [str(a) for a in
                      ((c.get("expected") or {}).get("acceptable_categories") or [])]
        predicted = diag.category
        correct = (predicted == expected)
        accepted = correct or predicted in acceptable   # 含容错口径
        pairs.append((predicted, expected))
        calib.append((diag.confidence, correct))
        tolerant_flags.append(accepted)
        if accepted and not correct:
            n_tolerated += 1
        case_rows.append({
            "id": c["id"],
            "origin": c.get("source", "manual"),
            "expected": expected,
            "acceptable": acceptable,
            "predicted": predicted,
            "confidence": round(diag.confidence, 3),
            "source": diag.source,
            "correct": correct,
            "tolerant": accepted,
            "had_screenshot": bool(shot),
        })

    acc = metrics.classification_accuracy(pairs)
    tolerant = metrics.rate(tolerant_flags)
    cal = metrics.expected_calibration_error(calib, n_bins=_ECE_BINS)
    return {
        "use_llm": use_llm,
        "rule_version": RULES_VERSION,
        "accuracy": acc,
        "accuracy_tolerant": tolerant,
        "n_tolerated": n_tolerated,
        "confusion_matrix": metrics.confusion_matrix(pairs),
        "calibration": cal,
        "confidence": metrics.confidence_gap(calib),
        "avg_confidence": round(sum(cf for cf, _ in calib) / len(calib), 3) if calib else 0.0,
        "cost": cost.summary(),
        "cases": case_rows,
    }


# ---------------------------------------------------------------------------
# blame 层 (6.2): 规则版本 + 容错分类只有在这一层才有, 不跑一次就只是死字段
# ---------------------------------------------------------------------------

def _eval_blame(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    """同一批黄金集再过一遍方案层 (纯规则, 0 成本)

    accuracy 是严格口径; tolerant_accuracy 额外把黄金集里人写的 acceptable_categories
    也算对。容错源头是人工标注, 不是规则自报 —— 否则规则说什么都对, 这个数就不算数了。
    规则自己报的歧义另列在 rule_acceptable, 只作为审计信息。
    """
    rows: List[Dict[str, Any]] = []
    pairs: List[tuple] = []
    tolerant_flags: List[bool] = []
    for c in cases:
        sample = c.get("input") or {}
        context = _blame.FailureContext(traceback=str(sample.get("traceback") or ""),
                                        log=str(sample.get("log") or ""))
        diag = _blame.diagnose(context, use_llm=False)
        expected = str((c.get("expected") or {}).get("category") or "")
        acceptable = [str(a) for a in
                      ((c.get("expected") or {}).get("acceptable_categories") or [])]
        strict = diag.category == expected
        tolerant = strict or diag.category in acceptable
        pairs.append((diag.category, expected))
        tolerant_flags.append(tolerant)
        rows.append({
            "id": c["id"],
            "expected": expected,
            "predicted": diag.category,
            "blame": diag.blame,
            "root_cause": diag.root_cause,
            "confidence": round(diag.confidence, 3),
            "acceptable": acceptable,
            "rule_acceptable": list(diag.acceptable_categories),
            "rule_version": diag.rule_version,
            "correct": strict,
            "tolerant": tolerant,
        })

    blame_counts: Dict[str, int] = {}
    for row in rows:
        blame_counts[row["blame"]] = blame_counts.get(row["blame"], 0) + 1
    return {
        "rule_version": _blame.RULE_VERSION,
        "accuracy": metrics.classification_accuracy(pairs),
        "tolerant_accuracy": metrics.rate(tolerant_flags),
        "n_tolerated": sum(1 for row in rows if row["tolerant"] and not row["correct"]),
        "n_rule_ambiguous": sum(1 for row in rows if row["rule_acceptable"]),
        "blame_counts": blame_counts,
        "cases": rows,
    }


def run(use_llm: bool = False, driver=None,
        include_flywheel: bool = True, limit: Optional[int] = None) -> Dict[str, Any]:
    """limit: 只跑前 N 条 (--smoke 冒烟用, 别拿它当正式评测)"""
    cases = _load_cases(include_flywheel)
    smoke_mode = bool(limit and limit > 0)
    if smoke_mode:
        cases = cases[:limit]

    # 规则模式 (始终跑, 0 成本)
    rule = _eval_mode(cases, use_llm=False, driver=None)

    n_flywheel = sum(1 for c in cases if c.get("source") == "flywheel")
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
            llm = _eval_mode(cases, use_llm=True, driver=driver, force_llm=smoke_mode)
        finally:
            if own:
                driver_provider.quit_driver(own)
        result["llm"] = llm
        # 直接给"规则 vs LLM"置信/准确率对比 (对 README 0.80/0.97)
        result["compare"] = {
            "rule_accuracy": rule["accuracy"],
            "llm_accuracy": llm["accuracy"],
            "rule_accuracy_tolerant": rule["accuracy_tolerant"],
            "llm_accuracy_tolerant": llm["accuracy_tolerant"],
            "rule_avg_confidence": rule["avg_confidence"],
            "llm_avg_confidence": llm["avg_confidence"],
            "rule_gap": rule["confidence"]["gap"],
            "llm_gap": llm["confidence"]["gap"],
            "rule_ece": rule["calibration"]["ece"],
            "llm_ece": llm["calibration"]["ece"],
            "llm_cost_per_case": llm["cost"].get("per_call", "n/a"),
        }

    return result


if __name__ == "__main__":
    import pprint
    pprint.pprint(run(use_llm=False))
