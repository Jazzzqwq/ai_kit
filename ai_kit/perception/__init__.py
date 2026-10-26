# -*- coding: utf-8 -*-
"""
感知层 — 从"看到一个页面"到"生成可跑用例代码"

流水线:
    collector  : JS 采集当前页面可操作元素 (inputs/buttons/tables/selects)
    inventory  : ElementSpec / PageInventory 数据模型 + 结构校验
    archetype  : 页面原型分类 (列表页/表单页/详情页/弹窗页; 规则优先, LLM 可选确认)
    strategies : 策略模板库 (每个原型该测什么: checklist 数据, 不是代码)
    planner    : 按原型 + 策略 + 元素清单生成测试计划, 并做约束校验闭环
    renderer   : 计划 -> pytest 源码 (po 模式调用 Page Object / standalone 模式用 runtime 原语)

阶段进度: ① collector 已落地, 其余待做。

用法:
    from ai_kit.perception import collect

    inv = collect(driver)                       # 内置规则包
    inv = collect(driver, "example_shop")       # 站点规则包 (风险词/容器提示可覆盖)
    inv["counts"], inv["dialogs"], inv["risks"]
"""
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

__all__ = [
    "COLLECT_JS",
    "COLLECTOR_VERSION",
    "DEFAULT_MAX_ELEMENTS",
    "DEFAULT_MAX_HTML",
    "LOCATOR_ATTRS",
    "build_options",
    "collect",
    "empty_inventory",
    "normalize",
]
