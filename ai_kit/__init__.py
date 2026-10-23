# -*- coding: utf-8 -*-
"""
ai_kit — 可移植的 Web UI 测试 AI 工具箱

分层:
    perception/  感知: DOM 采集 -> 元素清单 -> 页面原型 -> 策略 -> 计划 -> pytest 渲染
    proposal/    方案: 失败诊断(含 blame) -> 修复方案类型 -> 确认报告 -> 应用/回滚/验证
    evaluation/  评测: 黄金集 + 指标 + 成本 -> md/json 报告 (默认 0 成本, 可进 CI)
    runtime/     standalone 运行原语: actions / waits (统一显式等待, 替代 time.sleep)
    utils/       通用工具: PII 脱敏 / DOM 差异
    profiles/    规则包: 内置 generic/element_plus/antd + 站点 profile (JSON)

    client.py    LLM 客户端 (OpenAI Chat Completions 兼容, 多模态, usage 回填)
    config.py    Provider 配置 (密钥走环境变量) + 单价表
    analyzer.py  失败归因门面: 规则优先 -> LLM 降级
    healer.py    分层自愈定位器: L1 规则 -> L2 语义 CSS -> L3 相对 XPath -> L4 LLM

依赖预算 (硬约束):
    stdlib + selenium + requests。渲染 pytest 代码用字符串模板, 不引模板引擎;
    profile 用 JSON, 不引 PyYAML。本包绝不 import 具体项目的 pages/config/common。

公开 API 再导出在各模块落地后按任务卡分批补入本文件。
"""

__version__ = "0.1.0"

# 各模块落地后再导出, 例如:
#   from ai_kit.client import chat, chat_json, chat_with_image
#   from ai_kit.healer import try_heal, rank_candidates
#   from ai_kit.analyzer import diagnose, FailureDiagnosis
__all__ = []
