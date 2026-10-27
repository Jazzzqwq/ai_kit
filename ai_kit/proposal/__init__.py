# -*- coding: utf-8 -*-
"""
方案层 — 失败之后"给出可执行修复", 而不是只给一句建议

模块:
    diagnoser  : FailureDiagnosis v2 (category/blame/confidence/root_cause/evidence/
                 source/rule_version/acceptable_categories)
                 + blame 三分类 (product / environment / test_code) -> decide()
                 + 两个终态产物 (缺陷单草稿 / 环境检查清单)
    solutions  : 修复方案类型学 (改定位器/加等待/改断言/提缺陷/修环境) + 生成器
    report     : 人工确认用 HTML 报告 (v1 的人机交互面: 看方案 -> 采纳/拒绝)
    applier    : 应用方案 -> 回滚 -> 重跑验证 (闭环)

6.1 已落地 diagnoser。用法:

    from ai_kit.proposal.diagnoser import (FailureContext, PageObservation,
                                           diagnose, decide, require_patch_allowed)

    context = FailureContext(traceback=tb, log=log_text, case_id="LC-002",
                             observation=PageObservation.from_driver(driver))
    diagnosis = diagnose(context)              # 规则 -> 置信度 < 0.8 才问 LLM
    decision = decide(diagnosis)
    if decision.may_edit_test_code:
        require_patch_allowed(diagnosis)       # 不是 test_code 会直接抛

6.2 的两个字段是给评测用的: rule_version 记"哪版规则 + 哪版 blame 投影"判的,
acceptable_categories 列"另一个评审也会点头"的分类 (评测容错)。
"""
