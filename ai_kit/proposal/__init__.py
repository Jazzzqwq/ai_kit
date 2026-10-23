# -*- coding: utf-8 -*-
"""
方案层 — 失败之后"给出可执行修复", 而不是只给一句建议

模块:
    diagnoser  : FailureDiagnosis v2 (category/confidence/blame/evidence/...)
    solutions  : 修复方案类型学 (改定位器/加等待/改断言/提缺陷/修环境) + 生成器
    report     : 人工确认用 HTML 报告 (v1 的人机交互面: 看方案 -> 采纳/拒绝)
    applier    : 应用方案 -> 回滚 -> 重跑验证 (闭环)
"""
