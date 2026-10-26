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

阶段进度: ① collect / ② inventory / ③ archetype 已落地, 其余待做。

用法:
    from ai_kit.perception import collect, from_inventory, classify, annotate

    raw = collect(driver)                       # 内置规则包 -> dict
    raw = collect(driver, "example_shop")       # 站点规则包 (风险词/容器提示可覆盖)
    raw["counts"], raw["dialogs"], raw["risks"]

    inv = from_inventory(raw)                   # dict -> PageInventory (冻结, eid 引用)
    inv = from_collect(driver)                  # 采集 + 定型, 一步到位
    inv.element("e12").selector, inv.fingerprint

    result = classify(inv)                      # 原型分类 (纯规则, 0 成本)
    result.archetype, result.confidence, result.evidence
    inv = annotate(inv)                         # 回填 inv.archetype, 指纹不变
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

__all__ = [
    "ARCHETYPES",
    "COLLECT_JS",
    "COLLECTOR_VERSION",
    "DEFAULT_MAX_ELEMENTS",
    "DEFAULT_MAX_HTML",
    "FINGERPRINT_PREFIX",
    "INVENTORY_VERSION",
    "LLM_CONFIDENCE_THRESHOLD",
    "LOCATOR_ATTRS",
    "RISK_LEVELS",
    "ArchetypeResult",
    "ElementSpec",
    "PageInventory",
    "annotate",
    "build_options",
    "canonical_dom",
    "classify",
    "collect",
    "confirm_with_llm",
    "empty_inventory",
    "fingerprint_of",
    "from_collect",
    "from_inventory",
    "normalize",
]
