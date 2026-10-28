# -*- coding: utf-8 -*-
"""
套件1: 自愈评测 (Locator Healer) — top-1 / top-3 命中率 + L1/L2/L3/L4 分层归因

做法:
  无头浏览器 file:// 加载每条 case 自带的静态 HTML 夹具 →
  rank_candidates() 得有序候选 → 逐个 find_element 判断候选是否解析到
  夹具里打了 data-eval-target 的目标元素 → 统计 top1/top3 + 首个命中候选所属层。

可移植: 只依赖 ai_kit.healer + selenium + 自带夹具; 拿不到浏览器 → SKIPPED (不报错)。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from selenium.webdriver.common.by import By

from ai_kit.healer import rank_candidates
from ai_kit.evaluation import driver_provider, golden, metrics

_BY_MAP = {"css": By.CSS_SELECTOR, "xpath": By.XPATH}

_GOLDEN = golden.DEFAULT_DIR
_FIXTURES = os.path.join(_GOLDEN, "fixtures")

# README 里的估算分布 L1 30% / L2 50% / L3 10% / L4 10% —— 实测拿来对照,
# 不是拿来凑数的: 差多少就写多少。L4 只有注入 LLM 才会产生候选, 离线跑恒为 0。
_README_SHARE = {"L1": 0.30, "L2": 0.50, "L3": 0.10, "L4": 0.10}


def _load_cases() -> List[Dict[str, Any]]:
    return golden.load("healer", directory=_GOLDEN)


def _element_target(driver, by_const: str, locator: str) -> Optional[str]:
    """find_element 后读其 data-eval-target; 找不到返回 None"""
    try:
        el = driver.find_element(by_const, locator)
        return el.get_attribute("data-eval-target")
    except Exception:  # noqa: BLE001
        return None


def _llm_client(use_llm: bool):
    """把 use_llm 开关翻译成注入式客户端; 不可用则 None (评测依旧离线可复现)"""
    if not use_llm:
        return None
    try:
        from ai_kit.client import chat_json, llm_available
    except Exception:  # noqa: BLE001
        return None
    return chat_json if llm_available() else None


def run(use_llm: bool = False, driver=None) -> Dict[str, Any]:
    """
    执行自愈评测。driver 可由 run_eval 共享注入; 为 None 时自建, 建不了则 SKIPPED。
    """
    cases = _load_cases()
    own_driver = None
    if driver is None:
        driver = driver_provider.get_driver()
        own_driver = driver

    if driver is None:
        return {
            "suite": "healer",
            "status": "SKIPPED",
            "reason": "无可用无头浏览器 (Edge/Chrome), 自愈套件跳过",
            "n": len(cases),
        }

    case_reports: List[Dict[str, Any]] = []
    candidate_hits_series: List[List[bool]] = []
    winners: List[Optional[str]] = []
    per_layer_hit = {"L1": 0, "L2": 0, "L3": 0, "L4": 0}
    llm_client = _llm_client(use_llm)

    try:
        for c in cases:
            sample = c.get("input") or {}
            expected = c.get("expected") or {}
            fixture_path = os.path.join(_FIXTURES, sample["fixture"])
            driver.get(driver_provider.to_file_url(fixture_path))

            broken_by = _BY_MAP.get(sample.get("broken_by"), By.CSS_SELECTOR)
            cands = rank_candidates(
                driver, broken_by, sample["broken_locator"],
                intent=sample.get("intent", ""), llm_client=llm_client,
            )

            # 稳定性: 同一夹具同一入参连跑两次, 候选序列是否逐字节一致。
            # 只有纯规则链路才重复跑 —— 注入 LLM 时再问一次等于把这次评测的钱翻倍。
            repeat_same: Optional[bool] = None
            if llm_client is None:
                again = rank_candidates(driver, broken_by, sample["broken_locator"],
                                        intent=sample.get("intent", ""))
                repeat_same = ([(c.by, c.locator) for c in again]
                               == [(c.by, c.locator) for c in cands])

            expected_target = expected["target"]
            hits: List[bool] = []
            winner_rank = None
            winner_layer = None
            layers_hit = set()
            for idx, cand in enumerate(cands):
                t = _element_target(driver, cand.by, cand.locator)
                hit = (t is not None and t == expected_target)
                hits.append(hit)
                if hit:
                    layers_hit.add(cand.layer)
                if hit and winner_rank is None:
                    winner_rank = idx
                    winner_layer = cand.layer

            candidate_hits_series.append(hits)
            winners.append(winner_layer)
            for layer in layers_hit:
                per_layer_hit[layer] = per_layer_hit.get(layer, 0) + 1

            case_reports.append({
                "id": c["id"],
                "n_candidates": len(cands),
                "winner_rank": winner_rank,          # None = 全部候选都没命中
                "winner_layer": winner_layer,
                "layers_hit": sorted(layers_hit),
                "layer_hint": expected.get("layer_hint"),
                "top1_hit": metrics.top_k_hit(hits, 1),
                "top3_hit": metrics.top_k_hit(hits, 3),
                "n_hits": sum(1 for h in hits if h),
                "repeat_same": repeat_same,
                "candidates": [(cand.by, cand.locator, round(cand.confidence, 2), cand.layer)
                                for cand in cands[:6]],
            })
    finally:
        if own_driver:
            driver_provider.quit_driver(own_driver)

    tk = metrics.top_k_rates(candidate_hits_series, ks=(1, 3))
    layers = metrics.layer_breakdown(winners, baseline=_README_SHARE)
    stability = metrics.healing_stability(case_reports)
    n = len(cases)
    return {
        "suite": "healer",
        "status": "OK",
        "n": n,
        "use_llm": use_llm,
        "top1": tk.get(1, 0.0),
        "top3": tk.get(3, 0.0),
        "per_layer_winner": {layer: row["count"] for layer, row in layers["per_layer"].items()},
        "per_layer_hit": per_layer_hit,
        "layers": layers,
        "readme_share": dict(_README_SHARE),
        "no_hit_cases": layers["no_hit"],
        "stability": stability,
        "cases": case_reports,
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(run(use_llm=False))
