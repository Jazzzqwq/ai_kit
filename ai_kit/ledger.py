# -*- coding: utf-8 -*-
"""
成本账本 — 这次 run 花了多少、还能花多少、具体花在哪一步

三件事分开做:

    Budget     一次 run 的预算上限 (AI_KIT_BUDGET_CNY, 0/未配 = 不限)。超了就不再
               发起新的付费调用 —— 已经拿到的结果照用, 不为一条失败把整次 run 作废。
    RunLedger  每次 LLM 调用追加一行到 runs.jsonl: model / prompt_version /
               tokens / cost / latency。行是 append-only 的, 事后要算「哪一步最贵」
               不需要重跑。
    meter()    把 ask callable 包一圈: 查预算 -> 计时 -> token 差值 -> 按 model
               价目换算 -> 记账 -> 下次调用前再查预算。

为什么不塞进 client.chat: client 只管发请求, 它不知道「这次调用属于哪个阶段、
这次 run 还剩多少预算」。改 client 要动所有调用方, 而计划缓存那条路上的 LLM
入口只有一个 ask 函数 —— 包它一层, 只碰一个地方。

token 怎么来的: client 在响应体里回填进程级累计 (get_cumulative_usage), 这里取
调用前后的差值。比自己解析 usage 可靠 —— chat/chat_json/多模态都走同一条回填路径。

一个必须说明的边界: 价目表里没有的 model (config.price_for_model 返回 None) 算不出
成本, 这一行 cost_cny 记 null 且不计入 spend —— **此时预算拦不住它**, 账本会把
这类调用数记在 unpriced_calls 里。要真正管住钱, 先把 model 加进 PRICE_TABLE。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# 预算上限 (元 / 每次 run): 空 / 0 / 非法值 = 不限
BUDGET_ENV = "AI_KIT_BUDGET_CNY"
# 账本落盘位置; 默认跟着 cwd 走, 与 artifacts/ 同级
RUNS_LOG_ENV = "AI_KIT_RUNS_LOG"
DEFAULT_RUNS_LOG = "runs.jsonl"

# 一行账的字段 (卡片 5.9: model / prompt_version / tokens / cost / latency)
ROW_KEYS: Tuple[str, ...] = (
    "ts", "tag", "model", "prompt_version", "prompt_tokens", "completion_tokens",
    "total_tokens", "cost_cny", "latency_ms", "calls", "ok",
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 预算
# ---------------------------------------------------------------------------

def budget_limit() -> float:
    """本次 run 的预算 (元); 0 = 不限。

    非法值按 0 处理而不是抛: 预算配错不该让整条流水线连一次都跑不了 —— 与
    AI_KIT_WAIT / AI_KIT_ARTIFACTS 一个口径, 配置永远只是「尽力而为」。
    """
    raw = (os.environ.get(BUDGET_ENV) or "").strip()
    if not raw:
        return 0.0
    try:
        limit = float(raw)
    except (TypeError, ValueError):
        print(f"  [ledger] {BUDGET_ENV}={raw!r} 不是数字, 按不限预算处理")
        return 0.0
    return max(0.0, limit)


class BudgetExceeded(RuntimeError):
    """预算用完: 不再发起新的付费调用

    继承 RuntimeError 是为了兼容「LLM 出问题就降级」的既有写法; 但降级路径必须
    先把它重新抛出 —— 被 `except Exception` 吞成「调用失败, 用确定性步骤」,
    就等于预算生效了却没人知道为什么计划是旧的。
    """

    def __init__(self, spent: float, limit: float, tag: str = "") -> None:
        self.spent = float(spent)
        self.limit = float(limit)
        self.tag = str(tag or "")
        stage = f" ({self.tag})" if self.tag else ""
        super().__init__(
            f"预算用完{stage}: 已花 ¥{self.spent:.4f} / 上限 ¥{self.limit:.4f}, "
            f"不再发起新的 LLM 调用 (改 {BUDGET_ENV} 或复用缓存)")


class Budget:
    """一次 run 的花费上限 + 已花金额

    spent 只累计「算得出成本」的调用; 价目未收录的调用计进 unpriced_calls,
    它们不占预算额度 (算不出钱, 假装花了某个数只会更不透明)。
    """

    def __init__(self, limit: Optional[float] = None) -> None:
        self.limit = budget_limit() if limit is None else max(0.0, float(limit))
        self.spent = 0.0
        self.calls = 0
        self.unpriced_calls = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @property
    def exhausted(self) -> bool:
        """已经花到上限 (含刚好用完): 下一次调用前停"""
        return self.enabled and self.spent >= self.limit

    @property
    def remaining(self) -> Optional[float]:
        return max(0.0, self.limit - self.spent) if self.enabled else None

    def check(self, tag: str = "") -> None:
        """调用前检查; 超了就抛 —— 这是「超限即停」唯一真正拦得住的位置"""
        if self.exhausted:
            raise BudgetExceeded(self.spent, self.limit, tag)

    def add(self, cost: Optional[float], tag: str = "") -> None:
        """记一笔; cost=None 表示价目未收录 (只计数, 不入账)"""
        self.calls += 1
        if cost is None:
            self.unpriced_calls += 1
            return
        self.spent += max(0.0, float(cost))

    def summary(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "limit_cny": self.limit if self.enabled else "不限",
            "spent_cny": round(self.spent, 6),
            "calls": self.calls,
        }
        if self.unpriced_calls:
            out["unpriced_calls"] = self.unpriced_calls
            out["note"] = "有调用没配价目, 算不出成本, 预算对它们无效"
        if self.enabled:
            out["remaining_cny"] = round(self.remaining or 0.0, 6)
            out["exhausted"] = self.exhausted
        return out


# ---------------------------------------------------------------------------
# 账本 (runs.jsonl)
# ---------------------------------------------------------------------------

class RunLedger:
    """append-only 的调用账本; 写不进去只提示, 绝不让 run 挂掉

    没有加锁: 一次 run 是一个进程, 一次调用一行、一次 write。真要并发跑多个
    进程写同一个文件, 行可能交错 —— 那时该换 sqlite, 不是在这里塞 flock。
    """

    def __init__(self, path: Any = None) -> None:
        self.path = Path(path or os.environ.get(RUNS_LOG_ENV) or DEFAULT_RUNS_LOG)

    def record(self, entry: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """追加一行; 返回写进去的 dict (失败返回 None)"""
        row: Dict[str, Any] = {"ts": _utcnow()}
        for key in ROW_KEYS:
            if key in entry:
                row[key] = entry[key]
        # 卡片之外的补充字段 (如 cache_hit) 也留着, 方便事后按阶段分账
        for key, value in entry.items():
            row.setdefault(key, value)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:      # 只读磁盘 / 路径是目录 / 编码问题都不该中断 run
            print(f"  [ledger] 账本写不进去 ({self.path}): {exc}")
            return None
        return row

    def read(self) -> List[Dict[str, Any]]:
        """读回所有行; 坏行跳过 (手改过的账本不该让统计崩在上面)

        只读一个固定的行数上限, 避免一个跑了几年的账本把内存吃掉。
        """
        if not self.path.is_file():
            return []
        rows: List[Dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError as exc:
            print(f"  [ledger] 账本读不出来 ({self.path}): {exc}")
        return rows

    def total(self) -> Dict[str, Any]:
        """行 -> 汇总 (calls / tokens / cost / 平均延迟 / 各阶段花费)"""
        rows = self.read()
        cost = 0.0
        priced = unpriced = 0
        latencies: List[int] = []
        prompt_tokens = completion_tokens = 0
        by_tag: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            prompt_tokens += int(row.get("prompt_tokens") or 0)
            completion_tokens += int(row.get("completion_tokens") or 0)
            if row.get("latency_ms") is not None:
                latencies.append(int(row.get("latency_ms") or 0))
            value = row.get("cost_cny")
            tag = str(row.get("tag") or "")
            bucket = by_tag.setdefault(tag, {"calls": 0, "cost_cny": 0.0,
                                             "unpriced_calls": 0})
            bucket["calls"] += 1
            if value is None:
                unpriced += 1
                bucket["unpriced_calls"] += 1
            else:
                priced += 1
                cost += float(value)
                bucket["cost_cny"] = round(bucket["cost_cny"] + float(value), 6)
        return {
            "calls": len(rows),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_cny": round(cost, 6) if priced else ("n/a" if unpriced else 0.0),
            "unpriced_calls": unpriced,
            "avg_latency_ms": (round(sum(latencies) / len(latencies)) if latencies else 0),
            "by_tag": by_tag,
            "path": str(self.path),
        }


# ---------------------------------------------------------------------------
# 计价 + 包装
# ---------------------------------------------------------------------------

def price_of(model: str) -> Tuple[Optional[float], Optional[float]]:
    """按 model 查 (元/1K 输入, 元/1K 输出); 未收录 (None, None)"""
    try:
        from ai_kit.config import price_for_model
    except Exception:               # noqa: BLE001 — 配置模块被裁掉时账本照记
        return (None, None)
    entry = price_for_model(model)
    if not entry:
        return (None, None)
    return (float(entry.get("in", 0.0)), float(entry.get("out", 0.0)))


def cost_of(model: str, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    """token -> 元; 价目未收录返回 None (不拿 0 冒充「免费」)"""
    price_in, price_out = price_of(model)
    if price_in is None or price_out is None:
        return None
    return (prompt_tokens / 1000.0 * price_in
            + completion_tokens / 1000.0 * price_out)


def _usage() -> Dict[str, int]:
    """client 的进程级累计; 拿不到就是全 0 (差值恒 0 -> 不记账, 不做假账)"""
    try:
        from ai_kit.client import get_cumulative_usage
        return get_cumulative_usage()
    except Exception:               # noqa: BLE001
        return {"prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "calls": 0}


def _model_name() -> str:
    try:
        from ai_kit.config import get_active_config
        return str(get_active_config().get("model") or "")
    except Exception:               # noqa: BLE001 — 没配好也要能把账记下去
        return ""


def meter(ask: Callable[..., Any], *, tag: str = "", prompt_version: str = "",
          model: str = "", budget: Optional[Budget] = None,
          ledger: Optional[RunLedger] = None) -> Callable[..., Any]:
    """把 ask 包成「查预算 -> 计时 -> 记账」的版本; 签名与返回值原样透传

    - 预算在**调用前**查: 上一轮已经把额度花完, 这一轮就别再花钱了;
    - 记账在 finally 里: ask 抛异常 (超时/解析失败) 也已经产生费用, 那笔必须记;
    - 只有 calls 真的涨了才记一行: 没 key / 未配置时 chat 直接返回 None, 记一堆
      latency=0 的空行只会把账本变成噪音;
    - 预算的 add 也只在真调用后做: 一次都没发出去, 计入 calls 会虚报花费。

    ask 收到的参数原样转过去 —— 这里不认识 prompt 长什么样, 也不该认识。
    """
    book = ledger
    limit = budget

    def metered(prompt: Any, *args: Any, **kwargs: Any) -> Any:
        if limit is not None:
            limit.check(tag)            # 超限即停的唯一关卡
        before = _usage()
        started = time.perf_counter()
        ok = True
        try:
            return ask(prompt, *args, **kwargs)
        except BaseException:           # noqa: BLE001 — KeyboardInterrupt 也得把已花的钱记下
            ok = False
            raise
        finally:
            latency_ms = int((time.perf_counter() - started) * 1000)
            after = _usage()
            prompt_tokens = max(0, after.get("prompt_tokens", 0) - before.get("prompt_tokens", 0))
            completion_tokens = max(0, after.get(
                "completion_tokens", 0) - before.get("completion_tokens", 0))
            calls = max(0, after.get("calls", 0) - before.get("calls", 0))
            if calls:
                name = str(model or _model_name())
                cost = cost_of(name, prompt_tokens, completion_tokens)
                if book is not None:
                    book.record({
                        "tag": tag, "model": name, "prompt_version": prompt_version,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                        "cost_cny": (round(cost, 6) if cost is not None else None),
                        "latency_ms": latency_ms, "calls": calls, "ok": ok,
                    })
                if limit is not None:
                    limit.add(cost, tag)

    return metered
