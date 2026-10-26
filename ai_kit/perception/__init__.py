# -*- coding: utf-8 -*-
"""
感知层 — 从"看到一个页面"到"生成可跑用例代码"

流水线:
    collector  : JS 采集当前页面可操作元素 (inputs/buttons/tables/selects)
    inventory  : ElementSpec / PageInventory 数据模型 + 结构校验
    archetype  : 页面原型分类 (登录/列表CRUD/表单/详情/看板; 规则优先, LLM 可选兜底)
    strategies : 策略模板库 (每个原型该测什么: checklist 数据, 不是代码)
    planner    : 按原型 + 策略 + 元素清单生成测试计划, 并做约束校验闭环
    renderer   : 计划 -> pytest 源码 (po 模式调用 Page Object / standalone 模式用 runtime 原语)

阶段进度: ① collect / ② inventory / ③ archetype / ④ strategies / ⑤ planner 已落地, renderer 待做。

用法:
    from ai_kit.perception import collect, from_inventory, classify, annotate, expand, plan

    raw = collect(driver)                       # 内置规则包 -> dict
    raw = collect(driver, "example_shop")       # 站点规则包 (风险词/容器提示可覆盖)
    raw["counts"], raw["dialogs"], raw["risks"]

    inv = from_inventory(raw)                   # dict -> PageInventory (冻结, eid 引用)
    inv = from_collect(driver)                  # 采集 + 定型, 一步到位
    inv.element("e12").selector, inv.fingerprint

    result = classify(inv)                      # 原型分类 (纯规则, 0 成本)
    result.archetype, result.confidence, result.evidence
    inv = annotate(inv)                         # 回填 inv.archetype, 指纹不变

    exp = expand(inv)                           # 策略清单 -> 用例种子 (eid 级)
    exp.cases, exp.missing, exp.stats

    p = plan(inv)                               # 用例种子 -> 结构化动作计划 (卡片 5.6)
    p.to_dict()["cases"][0]["steps"]            # [{'action': 'fill', 'eid': 'e12', 'value': ...}, ...]
    p.report()                                  # 缺项/跳过/未解析/校验问题 (不进 JSON)
    plan_with_llm(inv)                          # 可选: LLM 改步骤, 过了校验才落地
"""
from ai_kit.perception.archetype import (
    ARCHETYPES,
    LLM_CONFIDENCE_THRESHOLD,
    ArchetypeResult,
    annotate,
    classify,
    confirm_with_llm,
)
from ai_kit.perception.collector import (
    COLLECT_JS,
    COLLECTOR_VERSION,
    DEFAULT_MAX_ELEMENTS,
    DEFAULT_MAX_HTML,
    LOCATOR_ATTRS,
    build_options,
    collect,
    empty_inventory,
    normalize,
)
from ai_kit.perception.inventory import (
    FINGERPRINT_PREFIX,
    INVENTORY_VERSION,
    RISK_LEVELS,
    ElementSpec,
    PageInventory,
    canonical_dom,
    fingerprint_of,
    from_collect,
    from_inventory,
)
from ai_kit.perception.planner import (
    ACTIONS,
    PLANNER_VERSION,
    PRIORITIES,
    CaseBundle,
    PlanCase,
    Step,
    TestPlan,
    build_cases,
    plan,
    plan_cache_key,
    plan_with_llm,
    resolve_ref,
    validate_case,
    validate_step,
)
from ai_kit.perception.strategies import (
    CHECKLISTS,
    STRATEGY_VERSION,
    TARGETS,
    Binding,
    CaseSeed,
    Expansion,
    Strategy,
    Variant,
    checklist,
    describe,
    expand,
    find,
    groups,
)

__all__ = [
    "ACTIONS",
    "ARCHETYPES",
    "CHECKLISTS",
    "COLLECT_JS",
    "COLLECTOR_VERSION",
    "DEFAULT_MAX_ELEMENTS",
    "DEFAULT_MAX_HTML",
    "FINGERPRINT_PREFIX",
    "INVENTORY_VERSION",
    "LLM_CONFIDENCE_THRESHOLD",
    "LOCATOR_ATTRS",
    "PLANNER_VERSION",
    "PRIORITIES",
    "RISK_LEVELS",
    "STRATEGY_VERSION",
    "TARGETS",
    "ArchetypeResult",
    "Binding",
    "CaseBundle",
    "CaseSeed",
    "ElementSpec",
    "Expansion",
    "PageInventory",
    "PlanCase",
    "Step",
    "Strategy",
    "TestPlan",
    "Variant",
    "annotate",
    "build_cases",
    "build_options",
    "canonical_dom",
    "checklist",
    "classify",
    "collect",
    "confirm_with_llm",
    "describe",
    "empty_inventory",
    "expand",
    "find",
    "fingerprint_of",
    "from_collect",
    "from_inventory",
    "groups",
    "normalize",
    "plan",
    "plan_cache_key",
    "plan_with_llm",
    "resolve_ref",
    "validate_case",
    "validate_step",
]
