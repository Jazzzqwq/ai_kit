# -*- coding: utf-8 -*-
"""5.9 成本账本: 预算 / 记账 / meter 包装器

真浏览器不参与, 全部用假 usage 打表 —— 这套东西的价值在「算得对、拦得住、
写不进去也不崩」, 与页面无关。
"""
import json
import sys
from pathlib import Path

import pytest

import ai_kit.client as client
from ai_kit import ledger as L


# ---------------------------------------------------------------------------
# 工具: 假 usage (meter 靠调用前后的累计差值算 token)
# ---------------------------------------------------------------------------

@pytest.fixture
def usage(monkeypatch):
    """把进程累计换成一个可推进的假计数器"""
    state = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
    monkeypatch.setattr(client, "get_cumulative_usage", lambda: dict(state))
    return state


def burned(state, prompt=600, completion=400):
    """模拟一次真调用: 涨 token + 涨 calls"""
    state["prompt_tokens"] += prompt
    state["completion_tokens"] += completion
    state["total_tokens"] += prompt + completion
    state["calls"] += 1


@pytest.fixture
def book(tmp_path):
    return L.RunLedger(tmp_path / "runs.jsonl")


# ---------------------------------------------------------------------------
# 1. 预算上限从哪来
# ---------------------------------------------------------------------------

def test_budget_is_unlimited_without_env(monkeypatch):
    monkeypatch.delenv(L.BUDGET_ENV, raising=False)

    assert L.budget_limit() == 0.0
    assert L.Budget().enabled is False


def test_budget_limit_reads_env(monkeypatch):
    monkeypatch.setenv(L.BUDGET_ENV, "2.5")

    assert L.budget_limit() == pytest.approx(2.5)
    assert L.Budget().limit == pytest.approx(2.5)


@pytest.mark.parametrize("raw", ["", "abc", "  ", "-3"])
def test_bad_budget_is_treated_as_unlimited(monkeypatch, raw):
    """配错预算不该让整次 run 连一次都跑不了"""
    monkeypatch.setenv(L.BUDGET_ENV, raw)

    assert L.budget_limit() == 0.0


def test_explicit_limit_wins_over_env(monkeypatch):
    monkeypatch.setenv(L.BUDGET_ENV, "99")

    assert L.Budget(1.5).limit == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# 2. Budget: 超限即停
# ---------------------------------------------------------------------------

def test_unlimited_budget_never_stops():
    budget = L.Budget(0)

    budget.add(1.0)
    budget.add(1.0)
    budget.check()

    assert budget.summary()["limit_cny"] == "不限"
    assert "exhausted" not in budget.summary()


def test_budget_stops_right_after_the_limit_is_reached():
    budget = L.Budget(0.002)

    budget.add(0.00128)
    assert budget.exhausted is False
    budget.check()

    budget.add(0.00128)
    assert budget.exhausted is True, "花超了就该停, 不能等下一轮结算"
    with pytest.raises(L.BudgetExceeded) as caught:
        budget.check("plan")

    assert caught.value.spent == pytest.approx(0.00256)
    assert caught.value.limit == pytest.approx(0.002)
    assert caught.value.tag == "plan"
    assert "预算用完" in str(caught.value)


def test_budget_is_exhausted_exactly_at_the_limit():
    """刚好花到上限也算用完: 再花一分就超了"""
    budget = L.Budget(0.5)

    budget.add(0.5)

    assert budget.exhausted is True
    assert budget.remaining == 0.0


def test_unpriced_calls_are_counted_but_not_charged():
    budget = L.Budget(0.001)

    budget.add(None, "plan")

    assert budget.calls == 1
    assert budget.unpriced_calls == 1
    assert budget.spent == 0.0
    assert budget.exhausted is False
    assert "预算对它们无效" in budget.summary()["note"]


def test_remaining_shrinks_and_never_goes_negative():
    budget = L.Budget(1.0)

    budget.add(0.4)
    assert budget.remaining == pytest.approx(0.6)

    budget.add(0.9)
    assert budget.remaining == 0.0


# ---------------------------------------------------------------------------
# 3. 计价
# ---------------------------------------------------------------------------

def test_price_of_known_and_unknown_model(monkeypatch):
    monkeypatch.setenv("QIANWEN_MODEL", "qwen-plus")

    assert L.price_of("qwen-plus") == (0.0008, 0.002)
    assert L.price_of("某未收录模型") == (None, None)


def test_cost_of_matches_the_price_table(monkeypatch):
    """600 输入 + 400 输出, qwen-plus: 0.6*0.0008 + 0.4*0.002"""
    monkeypatch.setenv("QIANWEN_MODEL", "qwen-plus")

    assert L.cost_of("qwen-plus", 600, 400) == pytest.approx(0.00128)


def test_cost_of_says_unknown_instead_of_free():
    assert L.cost_of("某未收录模型", 600, 400) is None


def test_cost_of_survives_a_broken_config_module(monkeypatch):
    """配置模块被裁掉时账本照记 (价格未知), 不该连账都记不了"""
    monkeypatch.setitem(sys.modules, "ai_kit.config", None)

    assert L.cost_of("qwen-plus", 1, 1) is None


# ---------------------------------------------------------------------------
# 4. 账本落盘
# ---------------------------------------------------------------------------

def test_record_appends_one_json_line(book):
    row = book.record({"tag": "plan", "model": "qwen-plus", "prompt_version": "planner.v3",
                       "prompt_tokens": 600, "completion_tokens": 400,
                       "total_tokens": 1000, "cost_cny": 0.00128,
                       "latency_ms": 1200, "calls": 1, "ok": True})

    assert row is not None and row["ts"]
    lines = book.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    for key in ("model", "prompt_version", "prompt_tokens", "completion_tokens",
                "cost_cny", "latency_ms"):
        assert key in stored, f"卡片要求的 {key} 必须进账本"


def test_record_keeps_extra_fields(book):
    """阶段之外的补充字段 (如 cache_hit) 也该留着, 否则事后没法按阶段分账"""
    book.record({"tag": "plan", "cache_hit": False, "cost_cny": 0.0})

    assert book.read()[0]["cache_hit"] is False


def test_ledger_creates_the_parent_directory(tmp_path):
    ledger = L.RunLedger(tmp_path / "deep" / "nested" / "runs.jsonl")

    assert ledger.record({"tag": "x"}) is not None
    assert ledger.path.is_file()


def test_ledger_survives_an_unwritable_path(tmp_path):
    """记账失败绝不能把 run 拖挂: 目录当文件写必然失败"""
    ledger = L.RunLedger(tmp_path)

    assert ledger.record({"tag": "x"}) is None
    assert ledger.read() == []


def test_read_skips_broken_lines(book):
    book.record({"tag": "one"})
    with book.path.open("a", encoding="utf-8") as handle:
        handle.write("{半行坏账\n")
    book.record({"tag": "two"})

    assert [row["tag"] for row in book.read()] == ["one", "two"]


def test_read_missing_file_is_empty(book):
    assert book.read() == []
    assert book.total()["calls"] == 0


# ---------------------------------------------------------------------------
# 5. 汇总
# ---------------------------------------------------------------------------

def test_total_sums_tokens_cost_latency_and_tags(book):
    book.record({"tag": "plan", "prompt_tokens": 600, "completion_tokens": 400,
                 "cost_cny": 0.00128, "latency_ms": 1000})
    book.record({"tag": "plan", "prompt_tokens": 100, "completion_tokens": 100,
                 "cost_cny": 0.00028, "latency_ms": 3000})
    book.record({"tag": "heal", "prompt_tokens": 50, "completion_tokens": 50,
                 "cost_cny": 0.00004, "latency_ms": 2000})

    total = book.total()

    assert total["calls"] == 3
    assert total["prompt_tokens"] == 750
    assert total["completion_tokens"] == 550
    assert total["total_tokens"] == 1300
    assert total["cost_cny"] == pytest.approx(0.0016)
    assert total["avg_latency_ms"] == 2000
    assert total["by_tag"]["plan"]["calls"] == 2
    assert total["by_tag"]["heal"]["cost_cny"] == pytest.approx(0.00004)


def test_total_marks_cost_na_when_nothing_is_priced(book):
    book.record({"tag": "plan", "prompt_tokens": 10, "completion_tokens": 10,
                 "cost_cny": None})

    assert book.total()["cost_cny"] == "n/a"
    assert book.total()["unpriced_calls"] == 1


# ---------------------------------------------------------------------------
# 6. meter: 计时 + 记账 + 查预算
# ---------------------------------------------------------------------------

def test_meter_records_a_full_row(usage, book):
    def ask(prompt):
        burned(usage)
        return {"ok": True}

    answer = L.meter(ask, tag="plan", prompt_version="planner.v3", model="qwen-plus",
                     ledger=book)("hi")

    assert answer == {"ok": True}
    row = book.read()[0]
    assert row["model"] == "qwen-plus"
    assert row["prompt_version"] == "planner.v3"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (600, 400)
    assert row["cost_cny"] == pytest.approx(0.00128)
    assert row["calls"] == 1
    assert row["ok"] is True
    assert row["latency_ms"] >= 0


def test_meter_passes_arguments_and_return_value_through(usage):
    def ask(prompt, marker=None):
        burned(usage, 1, 1)
        return (prompt, marker)

    assert L.meter(ask)("p", marker=7) == ("p", 7)


def test_meter_uses_the_delta_not_the_total(usage, book):
    """累计值一直涨, 每次只记这一次的增量"""
    burned(usage, 1000, 1000)          # 上一轮遗留的累计

    def ask(prompt):
        burned(usage, 600, 400)

    wrapped = L.meter(ask, model="qwen-plus", ledger=book)
    wrapped("a")
    wrapped("b")

    rows = book.read()
    assert [(row["prompt_tokens"], row["completion_tokens"]) for row in rows] == \
           [(600, 400), (600, 400)]


def test_meter_does_not_log_when_no_request_was_made(usage, book):
    """没 key / 未配置时 chat 直接返回 None: 记一堆 latency=0 的空行只是噪音"""
    def ask(prompt):
        return None

    L.meter(ask, tag="plan", ledger=book)("hi")

    assert book.read() == []


def test_meter_logs_a_failed_call(usage, book):
    """超时的请求也可能已经计费, 这笔必须记下来"""
    def ask(prompt):
        burned(usage, 100, 0)
        raise ValueError("网络挂了")

    with pytest.raises(ValueError):
        L.meter(ask, tag="plan", model="qwen-plus", ledger=book)("x")

    row = book.read()[0]
    assert row["ok"] is False
    assert row["prompt_tokens"] == 100


def test_meter_works_without_a_ledger(usage):
    def ask(prompt):
        burned(usage)
        return "done"

    assert L.meter(ask, tag="plan")("x") == "done"


def test_meter_charges_the_budget(usage):
    budget = L.Budget(1.0)

    L.meter(lambda prompt: burned(usage) or "ok", model="qwen-plus",
            budget=budget)("x")

    assert budget.spent == pytest.approx(0.00128)
    assert budget.calls == 1


def test_meter_stops_before_spending_again(usage, book):
    budget = L.Budget(0.002)
    calls = []

    def ask(prompt):
        burned(usage)
        calls.append(prompt)
        return "ok"

    wrapped = L.meter(ask, tag="plan", model="qwen-plus", budget=budget, ledger=book)
    wrapped("first")
    wrapped("second")                 # 这笔之后正好越过上限
    with pytest.raises(L.BudgetExceeded):
        wrapped("third")

    assert calls == ["first", "second"], "超限后不该再发第三次请求"


def test_meter_does_not_charge_when_nothing_was_sent(usage):
    budget = L.Budget(1.0)

    L.meter(lambda prompt: None, budget=budget)("x")

    assert budget.calls == 0
    assert budget.spent == 0.0


def test_meter_marks_unpriced_models(usage, book):
    budget = L.Budget(1.0)

    L.meter(lambda prompt: burned(usage) or "ok", model="某未收录模型",
            budget=budget, ledger=book)("x")

    assert book.read()[0]["cost_cny"] is None
    assert budget.unpriced_calls == 1
    assert budget.spent == 0.0


def test_meter_reads_the_model_from_config_when_not_given(usage, monkeypatch, book):
    monkeypatch.setenv("AI_ACTIVE_PROVIDER", "qianwen")
    monkeypatch.setenv("QIANWEN_MODEL", "qwen-plus")
    monkeypatch.setenv("QIANWEN_API_KEY", "sk-unit-test-fake-key")

    L.meter(lambda prompt: burned(usage) or "ok", ledger=book)("x")

    assert book.read()[-1]["model"] == "qwen-plus"
