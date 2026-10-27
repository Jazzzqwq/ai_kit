# -*- coding: utf-8 -*-
"""
方案层 — 失败之后"给出可执行修复", 而不是只给一句建议

模块:
    diagnoser  : FailureDiagnosis v2 (category/blame/confidence/root_cause/evidence/
                 source/rule_version/acceptable_categories)
                 + blame 三分类 (product / environment / test_code) -> decide()
                 + 两个终态产物 (缺陷单草稿 / 环境检查清单)
    solutions  : 修复方案类型学 (改定位器/加等待/改断言/提缺陷/修环境) + 生成器
    report     : 人工确认用单文件 HTML 报告 (失败现场 + 诊断 + 方案, 含 report.json)
    applier    : 应用方案 -> 重跑验证 -> 回滚, 每个动作记一行 decisions.jsonl

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

6.3 已落地 solutions。用法:

    from ai_kit.proposal.solutions import propose

    proposals = propose(diagnosis, context=context, by="css selector",
                        locator="body > div.el-dialog", target_file="tests/test_user.py",
                        test_name="test_open_dialog")   # 规则优先, 拿不到才问 LLM
    for item in proposals:
        print(item, item.reason)      # S001 [locator_patch/low] ... (可自动应用)
        if item.auto_applicable:      # 仅 risk=low + diff 非空 + 改代码的类型
            ...                       # 6.4 applier 负责 apply / rollback / 重跑

7 类方案: locator_patch / wait_adjust / data_fix / assertion_update / config_change
/ product_bug / env_issue。locator_patch 的 diff 由原文经 difflib 算出 (不是拼的),
wait_adjust 只给参数不给假 diff, 复杂类型才调 LLM (附失败上下文与 DOM 差异)。

6.4 已落地 report + applier。用法:

    from ai_kit.proposal.report import build, write

    html_path, pending = write(build(context, diagnosis, proposals,
                                     screenshot=shot, dom_diff=dom_diff),
                               "artifacts/report.html")   # 同时写出 report.json
    # 打开 html 看现场与方案, 三条命令直接复制:
    #   python -m ai_kit.proposal.applier --decision S001 --accept
    #   python -m ai_kit.proposal.applier --decision S001 --reject --note "..."
    #   python -m ai_kit.proposal.applier --decision S001 --defer

accept 会真的改文件并跑 verify_cmd, 验证不过自动 git checkout 回滚; 三个动作都往
decisions.jsonl 追加 {ts, failure_id, proposal_id, decision, verify_result, operator,
note}。不做 Web 控制台: 报告是一张能发出去的纸, 决策是命令行。
"""
