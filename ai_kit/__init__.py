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

公开 API 用 PEP 562 惰性导出:
    `import ai_kit` 只执行本文件, 一个子模块都不加载 (1.7ms 量级);
    第一次访问 ai_kit.try_heal 才真正 import ai_kit.healer (+ selenium 约 450ms)。
    只想用 metrics / profiles 的调用方, 不必先替 healer 付浏览器栈的导入成本。
    解析过的名字会写回 globals(), 第二次访问不再走 __getattr__。
"""
from __future__ import annotations

__version__ = "0.1.0"

# 名字 -> (子模块, 子模块里的属性)。同一子模块的多个名字共享一次 import。
# 清单口径: 每个模块的"调用方从这里起步"的那个函数/类型, 不追求把模块内所有
# 公开符号都搬上来 —— 层次多的用 ai_kit.xxx 显式导入反而更清楚。
_LAZY: dict[str, tuple[str, str]] = {
    # healer: 分层自愈定位
    "try_heal": ("ai_kit.healer", "try_heal"),
    "rank_candidates": ("ai_kit.healer", "rank_candidates"),
    "Candidate": ("ai_kit.healer", "Candidate"),
    # analyzer: 失败归因
    "diagnose": ("ai_kit.analyzer", "diagnose"),
    "diagnose_with_llm": ("ai_kit.analyzer", "diagnose_with_llm"),
    "analyze_allure_results": ("ai_kit.analyzer", "analyze_allure_results"),
    "FailureDiagnosis": ("ai_kit.analyzer", "FailureDiagnosis"),
    # client: LLM 调用与用量
    "chat": ("ai_kit.client", "chat"),
    "chat_json": ("ai_kit.client", "chat_json"),
    "chat_with_image": ("ai_kit.client", "chat_with_image"),
    "chat_json_with_image": ("ai_kit.client", "chat_json_with_image"),
    "get_last_usage": ("ai_kit.client", "get_last_usage"),
    "get_cumulative_usage": ("ai_kit.client", "get_cumulative_usage"),
    "llm_available": ("ai_kit.client", "llm_available"),
    # config: provider 配置与计价
    "active_provider": ("ai_kit.config", "active_provider"),
    "get_active_config": ("ai_kit.config", "get_active_config"),
    "price_for_model": ("ai_kit.config", "price_for_model"),
    # profiles: 规则包加载与合并
    "load_profile": ("ai_kit.profiles", "load_profile"),
    "resolve_profile": ("ai_kit.profiles", "resolve_profile"),
    "merge_profiles": ("ai_kit.profiles", "merge_profiles"),
    "effective_rules": ("ai_kit.profiles", "effective_rules"),
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> object:
    """PEP 562: 顶层符号按需导入, 并缓存在模块字典里"""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list:
    # 惰性名字还没进 globals(), dir() 得把它们补上, 否则 IDE 和 help() 看不见
    return sorted(set(globals()) | set(_LAZY))
