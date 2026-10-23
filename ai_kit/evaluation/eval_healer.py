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

import json
import os
from typing import Any, Dict, List, Optional

from selenium.webdriver.common.by import By

from ai_kit.healer import rank_candidates
from ai_kit.evaluation import driver_provider, metrics

_BY_MAP = {"css": By.CSS_SELECTOR, "xpath": By.XPATH}

_GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")
_FIXTURES = os.path.join(_GOLDEN, "fixtures")


def _load_cases() -> List[Dict[str, Any]]:
    with open(os.path.join(_GOLDEN, "healer_cases.json"), encoding="utf-8") as f:
        return json.load(f)["cases"]


def _element_target(driver, by_const: str, locator: str) -> Optional[str]:
    """find_element 后读其 data-eval-target; 找不到返回 None"""
    try:
        el = driver.find_element(by_const, locator)
        return el.get_attribute("data-eval-target")
    except Exception:  # noqa: BLE001
        return None


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
    layer_hits = {"L1": 0, "L2": 0, "L3": 0, "L4": 0}
    winner_layer_none = 0

    try:
        for c in cases:
            fixture_path = os.path.join(_FIXTURES, c["fixture"])
            driver.get(driver_provider.to_file_url(fixture_path))

            broken_by = _BY_MAP.get(c["broken_by"], By.CSS_SELECTOR)
            cands = rank_candidates(
                driver, broken_by, c["broken_locator"],
                intent=c.get("intent", ""), use_llm=use_llm,
            )

            expected_target = c["expected_target"]
            hits: List[bool] = []
            winner_rank = None
            winner_layer = None
            for idx, (by, loc, conf, layer) in enumerate(cands):
                t = _element_target(driver, by, loc)
                hit = (t is not None and t == expected_target)
                hits.append(hit)
                if hit and winner_rank is None:
                    winner_rank = idx
                    winner_layer = layer

            candidate_hits_series.append(hits)
            if winner_layer:
                layer_hits[winner_layer] += 1
            else:
                winner_layer_none += 1

            case_reports.append({
                "id": c["id"],
                "n_candidates": len(cands),
                "winner_rank": winner_rank,          # None = 全部候选都没命中
                "winner_layer": winner_layer,
                "layer_hint": c.get("layer_hint"),
                "top1_hit": metrics.top_k_hit(hits, 1),
                "top3_hit": metrics.top_k_hit(hits, 3),
                "candidates": [(by, loc, round(conf, 2), layer)
                                for by, loc, conf, layer in cands[:6]],
            })
    finally:
        if own_driver:
            driver_provider.quit_driver(own_driver)

    tk = metrics.top_k_rates(candidate_hits_series, ks=(1, 3))
    n = len(cases)
    return {
        "suite": "healer",
        "status": "OK",
        "n": n,
        "use_llm": use_llm,
        "top1": tk.get(1, 0.0),
        "top3": tk.get(3, 0.0),
        "per_layer_winner": layer_hits,
        "no_hit_cases": winner_layer_none,
        "cases": case_reports,
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(run(use_llm=False))
