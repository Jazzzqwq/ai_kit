# -*- coding: utf-8 -*-
"""
套件3: 生成评测 (Test Generator) — py_compile 通过率 / 覆盖率 / 重复率

默认 (无 --llm): 用 core._minimal_plan 骨架走一遍"清单→代码→编译/去重"管道,
  验证管道本身可运行 (报告明确标注 skeleton_mode, 不代表真实生成质量)。
--llm: 调 generate_case_plan 产出真实清单, 三项指标才有意义, 并统计每次成本。

可移植: page_structure/po_methods 来自自带 golden JSON, 不爬真实网站/不 import pages。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from ai_kit.generator import core as gcore
from ai_kit.evaluation import metrics, cost_tracker as _ct

try:
    from ai_kit.generator.prompts import TEST_USER_PREFIXES
except Exception:  # noqa: BLE001
    TEST_USER_PREFIXES = ["gen_"]

_GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")


def _load_cases() -> List[Dict[str, Any]]:
    with open(os.path.join(_GOLDEN, "generator_cases.json"), encoding="utf-8") as f:
        return json.load(f)["cases"]


def _plan_to_text(plan: Dict[str, Any]) -> str:
    """把清单拼成一段全文, 供覆盖率关键词匹配"""
    parts: List[str] = []
    for cls in plan.get("classes", []):
        parts.append(str(cls.get("class_name", "")))
        parts.append(str(cls.get("category", "")))
        for case in cls.get("cases", []):
            parts.append(str(case.get("method", "")))
            parts.append(str(case.get("title", "")))
            parts += [str(s) for s in case.get("steps", [])]
            parts += [str(e) for e in case.get("expected", [])]
    return "\n".join(parts)


def _one_case(c: Dict[str, Any], use_llm: bool, cost: "_ct.CostTracker") -> Dict[str, Any]:
    page_name = c.get("page_name", "")
    po_module = c.get("po_module", "module")
    po_class = c.get("po_class", "Page")
    fixture_name = c.get("fixture_name", "page")
    po_methods = c.get("po_methods", "")
    page_structure = c.get("page_structure", {})

    llm_ok = True
    if use_llm:
        before = cost.snapshot_hook()
        plan = gcore.generate_case_plan(
            page_structure=page_structure,
            po_methods=po_methods,
            page_name=page_name,
            fixture_name=fixture_name,
            prefixes=TEST_USER_PREFIXES,
        )
        cost.record_since(before)
        if plan is None:
            llm_ok = False
            plan = gcore._minimal_plan(po_module, po_class)
    else:
        plan = gcore._minimal_plan(po_module, po_class)

    code = gcore.plan_to_pytest_code(
        plan=plan, page_name=page_name, po_module=po_module,
        po_class=po_class, fixture_name=fixture_name,
    )

    compiled_ok, compile_err = metrics.py_compile_pass(code)
    gen_text = _plan_to_text(plan)
    cov = metrics.coverage_recall(
        gen_text,
        expected_keywords=c.get("expected_keywords", []),
        expected_categories=c.get("expected_categories", []),
    )
    dup = metrics.duplication_rate(code)

    n_cases = sum(len(cls.get("cases", [])) for cls in plan.get("classes", []))
    return {
        "id": c["id"],
        "llm_ok": llm_ok,
        "py_compile_pass": compiled_ok,
        "compile_error": compile_err,
        "n_cases_generated": n_cases,
        "coverage": cov,
        "duplication": dup,
    }


def run(use_llm: bool = False) -> Dict[str, Any]:
    cases = _load_cases()
    cost = _ct.CostTracker()
    rows: List[Dict[str, Any]] = []

    for c in cases:
        try:
            rows.append(_one_case(c, use_llm, cost))
        except Exception as e:  # noqa: BLE001 — 单条失败不拖垮套件
            rows.append({"id": c.get("id", "?"), "error": str(e),
                         "py_compile_pass": False,
                         "coverage": {"recall": 0.0, "total": 0},
                         "duplication": {}})

    compiles = [bool(r.get("py_compile_pass")) for r in rows]
    covs = [r["coverage"]["recall"] for r in rows if "coverage" in r]
    collisions = [r["duplication"]["name_collision"]["collision_rate"]
                  for r in rows if "duplication" in r and r["duplication"]]
    near_dup = [r["duplication"]["step_duplication"]["near_dup_rate"]
                for r in rows if "duplication" in r and r["duplication"]]

    def _mean(xs):
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    return {
        "suite": "generator",
        "status": "OK",
        "mode": "llm" if use_llm else "skeleton",
        "skeleton_mode": not use_llm,   # True 时不代表真实生成质量
        "n": len(cases),
        "py_compile_pass_rate": metrics.rate(compiles),
        "avg_coverage_recall": _mean(covs),
        "avg_name_collision_rate": _mean(collisions),
        "avg_step_near_dup_rate": _mean(near_dup),
        "cost": cost.summary(),
        "cases": rows,
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(run(use_llm=False))
