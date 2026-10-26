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

阶段进度: ① collect / ② inventory / ③ archetype / ④ strategies / ⑤ planner 已落地;
⑥ renderer 也已落地 (po / standalone 双模式, 产物先过 ③ 的 AST 白名单才交出)。
⑤ 的校验闭环 (5.7) 内置在 planner: schema -> 活页面 -> AST 白名单 -> 去重,
不过的用例标 needs_review, 不阻塞其他用例; renderer 自己拒收的用例同样进 ③。
5.9 的成本与缓存: plan_cached 把校验完的计划存进 plans/<fingerprint>.json, 键含
指纹 + profile + model + 提示词版本; 命中直接复用, 未命中才算钱 (ai_kit.ledger 管预算
与 runs.jsonl 记账)。
5.10 的 prompt: 模板集中在 perception/prompts.py —— system 只写硬约束 (只能用清单里
的 eid / 只能用列出的 PO 方法 / 不许写代码), user 只贴五块事实 (原型 / checklist /
元素清单 JSON / PO 签名 / 输出 schema), temperature=0.1 且 max_tokens 显式给,
重试时把校验错误原文回填。模型只产用例清单, 代码一律由 renderer 渲染。

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

    v = verify(p, inventory=inv, driver=driver) # 5.7 四层校验: schema / 活页面 / AST / 去重
    v.review, v.problems                        # 待人工确认的用例 + 问题清单
    plan_verified(inv, render=renderer.render)  # 校验 + 问题回填重生成 (最多 2 轮)

    from ai_kit.perception import render, write, make_renderer, PoPage

    src = render(p, inventory=inv)              # 计划 -> {case_id: pytest 源码}
    src.report()                                # 出代码的条数 + 渲染不了的用例与理由
    write(p, inventory=inv, out_dir="generated")  # 模块 + locators.json 一起落盘
    render(p, mode="po", po=PoPage(              # po 模式: 动作 -> PO 方法, 映射表由人给
        module="pages.patient_list", cls="PatientListPage",
        mapping={"fill": "search_by_name($value)"}, methods=("search_by_name",)))

    from ai_kit.ledger import Budget, RunLedger
    from ai_kit.perception import plan_cached

    p = plan_cached(inv, render=render, ask=client.chat_json,   # 命中缓存就不调模型
                    budget=Budget(0.5), ledger=RunLedger())     # 超 0.5 元即停
    p.diagnostics["cache"]                                      # 命中/未命中 都有一行说明

    from ai_kit.perception import po_signatures_from_dir

    sigs = po_signatures_from_dir("pages")                      # ['search_by_name(name)', ...]
    plan_cached(inv, ask=client.chat_json, mode="po",           # po 模式: 签名进 prompt,
                po_methods=po_methods_from_dir("pages"),        # 模型只能照签名排参数
                po_signatures=sigs)
"""
from ai_kit.perception.archetype import (
    ARCHETYPES,
    LLM_CONFIDENCE_THRESHOLD,
    ArchetypeResult,
    annotate,
    classify,
    confirm_with_llm,
)
from ai_kit.perception.cache import (
    CACHE_VERSION,
    DEFAULT_PLANS_DIR,
    cache_filename,
    cache_key,
    load_plan,
    plan_cache_path,
    plan_cached,
    plan_payload,
    plans_dir,
    save_plan,
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
    CASE_KEYS,
    DUP_THRESHOLD,
    PLANNER_VERSION,
    PRIORITIES,
    RUNTIME_PRIMITIVES,
    STEP_KEYS,
    CaseBundle,
    Duplicate,
    LintProblem,
    PlanCase,
    Step,
    TestPlan,
    VerifySummary,
    build_cases,
    case_from_dict,
    case_schema_problems,
    check_live,
    dedupe_cases,
    find_duplicates,
    lint_source,
    normalize_title,
    plan,
    plan_cache_key,
    plan_from_dict,
    plan_verified,
    plan_with_llm,
    po_methods_from_dir,
    po_signatures_from_dir,
    resolve_ref,
    validate_case,
    validate_schema,
    validate_step,
    verify,
)
from ai_kit.perception.renderer import (
    DEFAULT_DRIVER_FIXTURE,
    LOCATORS_FILENAME,
    MODES,
    MODE_PO,
    MODE_STANDALONE,
    RENDERER_VERSION,
    UNSUPPORTED_REASONS,
    PoPage,
    RenderProblem,
    RenderResult,
    Written,
    build_locators,
    make_renderer,
    render,
    write,
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
    "CACHE_VERSION",
    "CASE_KEYS",
    "CHECKLISTS",
    "COLLECT_JS",
    "COLLECTOR_VERSION",
    "DEFAULT_DRIVER_FIXTURE",
    "DEFAULT_MAX_ELEMENTS",
    "DEFAULT_MAX_HTML",
    "DEFAULT_PLANS_DIR",
    "DUP_THRESHOLD",
    "FINGERPRINT_PREFIX",
    "INVENTORY_VERSION",
    "LLM_CONFIDENCE_THRESHOLD",
    "LOCATORS_FILENAME",
    "LOCATOR_ATTRS",
    "MODES",
    "MODE_PO",
    "MODE_STANDALONE",
    "PLANNER_VERSION",
    "PRIORITIES",
    "RENDERER_VERSION",
    "RISK_LEVELS",
    "RUNTIME_PRIMITIVES",
    "STEP_KEYS",
    "STRATEGY_VERSION",
    "TARGETS",
    "UNSUPPORTED_REASONS",
    "ArchetypeResult",
    "Binding",
    "CaseBundle",
    "CaseSeed",
    "Duplicate",
    "ElementSpec",
    "Expansion",
    "LintProblem",
    "PageInventory",
    "PlanCase",
    "PoPage",
    "RenderProblem",
    "RenderResult",
    "Step",
    "Strategy",
    "TestPlan",
    "Variant",
    "VerifySummary",
    "Written",
    "annotate",
    "build_cases",
    "build_locators",
    "build_options",
    "cache_filename",
    "cache_key",
    "canonical_dom",
    "case_from_dict",
    "case_schema_problems",
    "check_live",
    "checklist",
    "classify",
    "collect",
    "confirm_with_llm",
    "dedupe_cases",
    "describe",
    "empty_inventory",
    "expand",
    "find",
    "find_duplicates",
    "fingerprint_of",
    "from_collect",
    "from_inventory",
    "groups",
    "lint_source",
    "load_plan",
    "make_renderer",
    "normalize",
    "normalize_title",
    "plan",
    "plan_cache_key",
    "plan_cache_path",
    "plan_cached",
    "plan_from_dict",
    "plan_payload",
    "plan_verified",
    "plan_with_llm",
    "plans_dir",
    "po_methods_from_dir",
    "po_signatures_from_dir",
    "render",
    "resolve_ref",
    "save_plan",
    "validate_case",
    "validate_schema",
    "validate_step",
    "verify",
    "write",
]
