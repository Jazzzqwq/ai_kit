# -*- coding: utf-8 -*-
"""
成本累计器 — 把 LLM token 用量按价目表换算成"每次成本"(元)

依赖:
- ai_kit.client.get_cumulative_usage()  : 进程内累计 token (t7 已加)
- ai_kit.config.get_price()             : 每 1K token 单价 (元), 缺则 n/a

用法:
    cost = CostTracker()
    before = cost.snapshot_hook()   # 记录一次评测 case 前的累计
    ... 触发 diagnose/generate (内部会调 chat) ...
    cost.record_since(before)       # 累加本 case 的 token 增量
    cost.summary()                  # {calls, tokens, per_call(元/"n/a")}
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def _load_price() -> Tuple[Optional[float], Optional[float]]:
    try:
        from ai_kit.config import get_price
        return get_price()
    except Exception:  # noqa: BLE001
        return (None, None)


def _cumulative() -> Dict[str, int]:
    try:
        from ai_kit.client import get_cumulative_usage
        return get_cumulative_usage()
    except Exception:  # noqa: BLE001
        return {"prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "calls": 0}


class CostTracker:
    def __init__(self) -> None:
        self.price_in, self.price_out = _load_price()
        self.price_known = (self.price_in is not None and self.price_out is not None)
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.cost_yuan = 0.0

    def snapshot_hook(self) -> Dict[str, int]:
        """返回当前进程累计用量快照, 作为某次 case 调用前的基线"""
        return _cumulative()

    def record_since(self, before: Dict[str, int]) -> None:
        """用 after - before 得到本 case 触发的 token/调用增量并累加"""
        after = _cumulative()
        dp = max(0, after.get("prompt_tokens", 0) - before.get("prompt_tokens", 0))
        dc = max(0, after.get("completion_tokens", 0) - before.get("completion_tokens", 0))
        dn = max(0, after.get("calls", 0) - before.get("calls", 0))
        self.prompt_tokens += dp
        self.completion_tokens += dc
        self.calls += dn
        if self.price_known:
            self.cost_yuan += dp / 1000.0 * self.price_in + dc / 1000.0 * self.price_out

    def summary(self) -> Dict[str, Any]:
        per_call: Any
        if not self.price_known:
            per_call = "n/a"      # 没配价目表 → 无法换算成本
        elif self.calls == 0:
            per_call = 0.0        # 本次评测没真正调 LLM (如规则模式/LLM降级)
        else:
            per_call = round(self.cost_yuan / self.calls, 6)
        return {
            "llm_calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_cost_yuan": (round(self.cost_yuan, 6) if self.price_known else "n/a"),
            "per_call": per_call,
            "price_per_1k": {"input": self.price_in, "output": self.price_out} if self.price_known else "n/a",
        }
