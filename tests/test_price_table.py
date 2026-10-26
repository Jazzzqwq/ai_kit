# -*- coding: utf-8 -*-
"""
任务 4: 价目表 PRICE_TABLE 契约

- 按 **model** 索引 (元 / 1K token), 带快照月份 + 来源说明
- 结构: 每条目 in/out 均为非负数字, 输出单价不低于输入
- price_for_model: 精确匹配 -> 版本后缀最长前缀 -> 未收录 None
- get_price(provider): 跟随 {PROVIDER}_MODEL 覆盖; 未知 provider / 未收录 model -> (None, None)
- PROVIDERS 里不再残留 price_per_1k_* (价目只有一个真相)
- 与 cost_tracker 串起来: 成本按 model 单价算

运行: pytest tests/test_price_table.py
"""
from __future__ import annotations

import re

import pytest

from ai_kit import client
from ai_kit import config as C
from ai_kit.evaluation.cost_tracker import CostTracker

_ENV_KEYS = [
    "AI_ACTIVE_PROVIDER", "AI_API_KEY", "AI_KIT_ENV_FILE",
    "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
    "DOUBAO_API_KEY", "DOUBAO_MODEL", "DOUBAO_BASE_URL",
    "OLLAMA_API_KEY", "OLLAMA_MODEL", "OLLAMA_BASE_URL",
    "QIANWEN_API_KEY", "QIANWEN_MODEL", "QIANWEN_BASE_URL",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """默认 qianwen, 所有 key/model 变量清空 —— 结果与本机 .env 无关"""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AI_ACTIVE_PROVIDER", "qianwen")
    return monkeypatch


# ---------------------------------------------------------------------------
# 结构与元数据
# ---------------------------------------------------------------------------

def test_table_entries_are_well_formed():
    assert C.PRICE_TABLE, "价目表不能为空"
    for model, entry in C.PRICE_TABLE.items():
        assert set(entry) == {"in", "out"}, f"{model} 只允许 in/out 两个字段"
        for field in ("in", "out"):
            price = entry[field]
            assert isinstance(price, (int, float)) and not isinstance(price, bool)
            assert price >= 0, f"{model}.{field} 不该是负数"
        assert entry["out"] >= entry["in"], f"{model}: 输出单价不应低于输入单价"


def test_table_keys_are_lowercase_without_spaces():
    """price_for_model 会把查询名 lower 化, 表 key 必须同规格, 否则永远匹配不上"""
    for model in C.PRICE_TABLE:
        assert model == model.strip().lower(), f"{model!r} 应是小写、无首尾空白"


def test_snapshot_month_and_source_documented():
    assert re.fullmatch(r"\d{4}-\d{2}", C.PRICE_TABLE_SNAPSHOT), "快照月份格式应为 YYYY-MM"
    assert C.PRICE_TABLE_SNAPSHOT >= "2026-01", "快照不能比任务基线还旧"
    assert C.PRICE_TABLE_SOURCE.strip(), "必须注明来源"
    assert "快照" in C.PRICE_TABLE_SOURCE


def test_providers_no_longer_carry_price_fields():
    """价目从 provider 档案迁出后, 不能两处都留 (否则改一处忘一处)"""
    for name, profile in C.PROVIDERS.items():
        assert "price_per_1k_input" not in profile, f"{name} 仍带 price_per_1k_input"
        assert "price_per_1k_output" not in profile, f"{name} 仍带 price_per_1k_output"


# ---------------------------------------------------------------------------
# 查表规则
# ---------------------------------------------------------------------------

def test_qwen_plus_values_match_task_card():
    assert C.PRICE_TABLE["qwen-plus"] == {"in": 0.0008, "out": 0.002}
    assert C.price_for_model("qwen-plus") == {"in": 0.0008, "out": 0.002}


def test_ollama_is_free():
    assert C.price_for_model("qwen2.5:7b") == {"in": 0.0, "out": 0.0}
    assert C.get_price("ollama") == (0.0, 0.0)


@pytest.mark.parametrize("model, expect_key", [
    ("qwen-plus-2025-04-28", "qwen-plus"),        # 日期版本后缀
    ("qwen-plus-latest", "qwen-plus"),
    ("qwen2.5:7b-instruct-q4_K_M", "qwen2.5:7b"),  # ollama tag 变体
    ("Qwen-Plus", "qwen-plus"),                   # 大小写不敏感
    ("  deepseek-chat  ", "deepseek-chat"),       # 首尾空白
])
def test_version_suffix_and_case_insensitive_match(model, expect_key):
    assert C.price_for_model(model) == C.PRICE_TABLE[expect_key]


@pytest.mark.parametrize("model", ["", "   ", None, "gpt-4o", "qwen", "qwen-plusx", "qwen-max"])
def test_unknown_model_returns_none(model):
    assert C.price_for_model(model) is None


def test_price_for_model_returns_copy():
    got = C.price_for_model("qwen-plus")
    got["in"] = 999.0
    assert C.PRICE_TABLE["qwen-plus"]["in"] == 0.0008


# ---------------------------------------------------------------------------
# get_price 跟随 model, 而不是 provider
# ---------------------------------------------------------------------------

def test_get_price_follows_active_provider_defaults(clean_env):
    assert C.get_price() == (0.0008, 0.002)          # qianwen -> qwen-plus
    assert C.get_price("deepseek") == (0.001, 0.002)  # deepseek-chat


def test_get_price_follows_model_env_override(clean_env):
    """核心变化: 换模型, 价格跟着换; 换到没收录的模型就记 n/a (不能沿用旧价)"""
    clean_env.setenv("QIANWEN_MODEL", "qwen-plus-2025-04-28")
    assert C.get_price() == (0.0008, 0.002)          # 版本后缀仍命中 qwen-plus

    clean_env.setenv("QIANWEN_MODEL", "qwen-max")     # 未收录
    assert C.get_price() == (None, None), "换到未收录模型后不能继续按 qwen-plus 计价"


def test_get_price_unknown_or_unpriced_provider(clean_env):
    assert C.get_price("doubao") == (None, None)      # model 为空
    assert C.get_price("nope") == (None, None)        # 未知 provider 不炸


# ---------------------------------------------------------------------------
# 与 cost_tracker 串起来
# ---------------------------------------------------------------------------

def test_cost_tracker_uses_model_prices(clean_env, monkeypatch):
    """600 输入 + 400 输出 token, 按 qwen-plus 计: 0.6*0.0008 + 0.4*0.002"""
    snapshots = [
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0},
        {"prompt_tokens": 600, "completion_tokens": 400, "total_tokens": 1000, "calls": 1},
    ]
    monkeypatch.setattr(client, "get_cumulative_usage", lambda: snapshots.pop(0))

    cost = CostTracker()
    assert (cost.price_in, cost.price_out) == (0.0008, 0.002)
    cost.record_since(cost.snapshot_hook())
    assert cost.cost_yuan == pytest.approx(0.00128)
    assert cost.summary()["per_call"] == pytest.approx(0.00128)
    assert snapshots == []


def test_cost_tracker_marks_na_for_unpriced_model(clean_env):
    clean_env.setenv("QIANWEN_MODEL", "qwen-max")     # 未收录
    cost = CostTracker()
    assert cost.price_known is False
    assert cost.summary()["total_cost_yuan"] == "n/a"


# ---------------------------------------------------------------------------
# 自检输出
# ---------------------------------------------------------------------------

def test_selfcheck_prints_prices_without_leaking_key(clean_env, capsys):
    clean_env.setenv("QIANWEN_API_KEY", "sk-unit-test-fake-key-00000000")
    assert C._selfcheck(ping=False) == 0

    out = capsys.readouterr().out
    assert C.PRICE_TABLE_SNAPSHOT in out, "自检要显示价格快照月份"
    assert "qwen-plus" in out and "0.0008" in out
    assert "未收录" in out, "豆包没价目时也要明确标出"
    assert "sk-unit-test-fake-key-00000000" not in out
