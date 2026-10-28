# -*- coding: utf-8 -*-
"""
套件3: 生成评测 (Test Generator) — py_compile 通过率 / 覆盖率 / 重复率

默认 (无 --llm): 用 core._minimal_plan 骨架走一遍"清单→代码→编译/去重"管道,
  验证管道本身可运行 (报告明确标注 skeleton_mode, 不代表真实生成质量)。
--llm: 调 generate_case_plan 产出真实清单, 三项指标才有意义, 并统计每次成本。

带 input.inventory 的 case (source=collected, 真页面采集冻结) 另走感知链:
  from_inventory -> plan -> render -> verify, 汇总 eid 解析率 / 可执行率。
  确定性规划器"派生不出来就 skip", 离线模式解析率预期接近 100%; 活页面层无 driver
  时进 live_skipped, 口径在报告里注明。

可移植: page_structure/po_methods 来自自带 golden JSON, 不爬真实网站/不 import pages。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ai_kit.generator import core as gcore
from ai_kit.evaluation import golden, metrics, cost_tracker as _ct
from ai_kit.perception import from_inventory, plan as pplan, render as prender
from ai_kit.perception import resolve_ref, verify as pverify
from ai_kit.perception.archetype import classify as classify_archetype

try:
    from ai_kit.generator.prompts import TEST_USER_PREFIXES
except Exception:  # noqa: BLE001
    TEST_USER_PREFIXES = ["gen_"]

_GOLDEN = golden.DEFAULT_DIR


def _load_cases() -> List[Dict[str, Any]]:
    return golden.load("generator", directory=_GOLDEN)


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


def _chain_case(c: Dict[str, Any]) -> Dict[str, Any]:
    """采集冻结的 inventory -> 感知链实跑, 抽两项率的原始计数 (失败不拖垮套件)

    refs 取法与 planner.verify 对齐: 全部用例 steps+cleanup 里的非空 step.eid,
    复合引用 (c1.column:X / chart:N) 也走同一扇 resolve_ref 的门。
    """
    raw_inv = (c.get("input") or {}).get("inventory") or {}
    inv = from_inventory(raw_inv)
    # 原型分桶用纯规则分类 (0 成本), 给跨页泛化看"规则是不是只对某类页有效"
    archetype = classify_archetype(inv).archetype
    p0 = pplan(inv)
    src = prender(p0, inventory=inv)            # standalone: {case_id: 源码}, .problems 是拒收理由
    v = pverify(p0, inventory=inv, sources=src,
                render_problems={str(getattr(pr, "case_id", "") or ""):
                                 [str(getattr(pr, "message", "") or pr)]
                                 for pr in (getattr(src, "problems", ()) or ())})
    ref_rows = [{"ref": step.eid, "resolved": resolve_ref(inv, step.eid) is not None}
                for case in v.cases for step in (*case.steps, *case.cleanup) if step.eid]
    case_rows = [{"id": case.case_id,
                  "rendered": bool(src.get(case.case_id)),
                  "needs_review": bool(case.needs_review)}
                 for case in v.cases]
    # 元素覆盖率: 分母是 inventory 的可交互元素 (by_eid, 表格/弹窗走 lookup 不在内),
    # 分子是被 steps/cleanup 真点到的那些 —— 与计划用词无关, 补 coverage_recall 看不出的"漏点"
    elem_cov = metrics.element_coverage(inv.by_eid.keys(), [row["ref"] for row in ref_rows])
    return {
        "id": c["id"],
        "archetype": archetype,
        "n_plan_cases": len(v.cases),
        "refs": ref_rows,
        "cases": case_rows,
        "element_coverage": elem_cov,
        "live_skipped": list((v.diagnostics or {}).get("live_skipped") or ()),
        "verify": str((v.diagnostics or {}).get("verify") or ""),
    }


def _one_case(c: Dict[str, Any], use_llm: bool, cost: "_ct.CostTracker") -> Dict[str, Any]:
    sample = c.get("input") or {}
    expected = c.get("expected") or {}
    page_name = sample.get("page_name", "")
    po_module = sample.get("po_module", "module")
    po_class = sample.get("po_class", "Page")
    fixture_name = sample.get("fixture_name", "page")
    po_methods = sample.get("po_methods", "")
    page_structure = sample.get("page_structure", {})

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
        expected_keywords=expected.get("keywords", []),
        expected_categories=expected.get("categories", []),
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


def run(use_llm: bool = False, limit: Optional[int] = None) -> Dict[str, Any]:
    """limit: 只跑前 N 条 (--smoke 冒烟用)"""
    cases = _load_cases()
    if limit is not None and limit > 0:
        cases = cases[:limit]
    cost = _ct.CostTracker()
    rows: List[Dict[str, Any]] = []

    for c in cases:
        try:
            row = _one_case(c, use_llm, cost)
            if (c.get("input") or {}).get("inventory"):
                try:
                    row["chain"] = _chain_case(c)
                except Exception as e:  # noqa: BLE001 — 链跑不通也是读数, 记下来不装成没这条
                    row["chain"] = {"id": c["id"], "error": str(e),
                                    "refs": [], "cases": [], "n_plan_cases": 0}
            rows.append(row)
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

    # 感知链两项率: 只统计真跑了链的 case (带 inventory 的 collected 样本)
    chains = [r["chain"] for r in rows if isinstance(r.get("chain"), dict)]
    ref_rows = [x for ch in chains for x in ch.get("refs") or ()]
    chain_case_rows = [x for ch in chains for x in ch.get("cases") or ()]
    chain_errors = [ch for ch in chains if ch.get("error")]
    # 元素覆盖率跨页汇总: 分子分母各自相加 (元素口径), 不是对各页 rate 求平均 ——
    # 否则 3 个元素的小页和 30 个元素的大页权重一样
    elem_rows = [ch["element_coverage"] for ch in chains
                 if isinstance(ch.get("element_coverage"), dict)]
    elem_total = sum(int(x.get("total") or 0) for x in elem_rows)
    elem_covered = sum(int(x.get("covered") or 0) for x in elem_rows)
    # 跨页泛化: 每页一条 (原型 / 被引用元素 / 派生用例数)。链路报错的页同样进分母、
    # 覆盖率按 0 计 —— 它归到 zero_case_pages 里点名, 不静默从分母里消失。
    gen_pages = [{"id": ch["id"],
                  "archetype": ch.get("archetype"),
                  "covered": (ch.get("element_coverage") or {}).get("covered", 0),
                  "total": (ch.get("element_coverage") or {}).get("total", 0),
                  "n_cases": int(ch.get("n_plan_cases") or 0)}
                 for ch in chains]

    # 一条 case 对应一个页面 → 每页成本才有意义 (每次成本随 prompt 长短飘)
    return {
        "suite": "generator",
        "status": "OK",
        "mode": "llm" if use_llm else "skeleton",
        "skeleton_mode": not use_llm,   # True 时不代表真实生成质量
        "n": len(cases),
        "n_pages": len(cases),
        "py_compile_pass_rate": metrics.rate(compiles),
        "avg_coverage_recall": _mean(covs),
        "avg_name_collision_rate": _mean(collisions),
        "avg_step_near_dup_rate": _mean(near_dup),
        "chain": {
            "n_cases": len(chains),
            "n_errors": len(chain_errors),
            "metrics": metrics.perception_chain_metrics(ref_rows, chain_case_rows),
            "element_coverage": {
                "n_pages": len(elem_rows),
                "total": elem_total,
                "covered": elem_covered,
                "rate": round(metrics.safe_div(elem_covered, elem_total), 4),
            },
            "generalization": metrics.cross_page_generalization(gen_pages),
        },
        "cost": cost.summary(units=len(cases)),
        "cases": rows,
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(run(use_llm=False))
