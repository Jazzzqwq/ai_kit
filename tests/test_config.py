# -*- coding: utf-8 -*-
"""
config.py 密钥解析行为自检 — key 只能来自环境变量 / 本地 .env, 文件里不留明文。

运行:
    pytest tests/test_config.py
"""
from __future__ import annotations

import pytest

from ai_kit import config as C

# 测试期间需要清干净的变量 (含 .env 可能注入的), 保证结果与本机配置无关
_ENV_KEYS = [
    "AI_ACTIVE_PROVIDER", "AI_API_KEY", "AI_KIT_ENV_FILE",
    "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
    "DOUBAO_API_KEY", "DOUBAO_MODEL", "DOUBAO_BASE_URL",
    "OLLAMA_API_KEY", "OLLAMA_MODEL", "OLLAMA_BASE_URL",
    "QIANWEN_API_KEY", "QIANWEN_MODEL", "QIANWEN_BASE_URL",
]

FAKE_KEY = "sk-unit-test-fake-key-00000000"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """默认选中 qianwen, 且所有 key 变量为空 —— 各用例再自行注入"""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AI_ACTIVE_PROVIDER", "qianwen")
    return monkeypatch


# ---------------------------------------------------------------------------
# 结构约束: 明文 key 无处可放
# ---------------------------------------------------------------------------

def test_providers_carry_no_api_key_field():
    for name, profile in C.PROVIDERS.items():
        assert "api_key" not in profile, f"{name} 的档案里不该有 api_key 字段"


def test_mask_key_never_returns_full_key():
    assert C.mask_key("") == ""
    assert C.mask_key("short") == "****"
    masked = C.mask_key(FAKE_KEY)
    assert masked != FAKE_KEY and FAKE_KEY not in masked
    assert masked.startswith(FAKE_KEY[:4]) and masked.endswith(FAKE_KEY[-2:])


# ---------------------------------------------------------------------------
# key 解析优先级
# ---------------------------------------------------------------------------

def test_key_read_from_provider_env(clean_env):
    clean_env.setenv("QIANWEN_API_KEY", FAKE_KEY)
    assert C.get_active_config()["api_key"] == FAKE_KEY


def test_generic_ai_api_key_fallback(clean_env):
    clean_env.setenv("AI_API_KEY", FAKE_KEY)
    assert C.resolve_api_key("qianwen") == FAKE_KEY
    assert C.get_active_config()["api_key"] == FAKE_KEY


def test_provider_specific_beats_generic(clean_env):
    clean_env.setenv("AI_API_KEY", "sk-generic-fake-key-1111111111")
    clean_env.setenv("QIANWEN_API_KEY", FAKE_KEY)
    assert C.get_active_config()["api_key"] == FAKE_KEY


def test_env_value_is_stripped(clean_env):
    clean_env.setenv("QIANWEN_API_KEY", f"  {FAKE_KEY}  ")
    assert C.get_active_config()["api_key"] == FAKE_KEY


# ---------------------------------------------------------------------------
# 校验与报错 (报错不得回显 key)
# ---------------------------------------------------------------------------

def test_missing_key_raises(clean_env):
    with pytest.raises(ValueError) as exc:
        C.get_active_config()
    assert "QIANWEN_API_KEY" in str(exc.value)
    assert ".env" in str(exc.value)


def test_doubao_missing_model_raises(clean_env):
    clean_env.setenv("AI_ACTIVE_PROVIDER", "doubao")
    clean_env.setenv("DOUBAO_API_KEY", FAKE_KEY)
    with pytest.raises(ValueError) as exc:
        C.get_active_config()
    assert "model 未填写" in str(exc.value)


def test_unknown_provider_raises():
    with pytest.raises(ValueError) as exc:
        C.get_active_config("nope")
    assert "未知 provider" in str(exc.value)


def test_error_message_does_not_leak_key(clean_env):
    clean_env.setenv("AI_ACTIVE_PROVIDER", "doubao")   # 有 key 但 model 空 → 抛错
    clean_env.setenv("DOUBAO_API_KEY", FAKE_KEY)
    with pytest.raises(ValueError) as exc:
        C.get_active_config()
    assert FAKE_KEY not in str(exc.value)


# ---------------------------------------------------------------------------
# endpoint / model 也走环境覆盖
# ---------------------------------------------------------------------------

def test_model_and_base_url_env_override(clean_env):
    clean_env.setenv("QIANWEN_API_KEY", FAKE_KEY)
    clean_env.setenv("QIANWEN_MODEL", "qwen-max")
    clean_env.setenv("QIANWEN_BASE_URL", "https://example.com/v1")
    cfg = C.get_active_config()
    assert cfg["model"] == "qwen-max"
    assert cfg["base_url"] == "https://example.com/v1"


# ---------------------------------------------------------------------------
# 无鉴权本地服务
# ---------------------------------------------------------------------------

def test_ollama_usable_without_key(clean_env):
    clean_env.setenv("AI_ACTIVE_PROVIDER", "ollama")
    cfg = C.get_active_config()
    assert cfg["requires_api_key"] is False
    assert cfg["api_key"] == ""
    assert cfg["base_url"].startswith("http://localhost:11434")
    assert C.is_configured() is True


def test_nothing_configured_means_not_configured(clean_env):
    """默认 qianwen 无 key 时, is_configured() 必须为 False (评测才能 0 成本离线)"""
    assert C.is_configured() is False
    assert C.resolve_api_key() == ""


def test_is_configured_true_when_any_key_present(clean_env):
    """active 是 qianwen 但只配了 deepseek 的 key → 至少配了一家的旧语义仍成立"""
    clean_env.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    assert C.is_configured() is True
    assert C.is_configured("deepseek") is True
    assert C.is_configured("qianwen") is False


# ---------------------------------------------------------------------------
# 价目表
# ---------------------------------------------------------------------------

def test_get_price_from_table(clean_env):
    assert C.get_price() == (0.0008, 0.002)
    assert C.get_price("deepseek") == (0.001, 0.002)


def test_get_price_missing_table_returns_none(clean_env):
    assert C.get_price("doubao") == (None, None)      # 没价目 → n/a
    assert C.get_price("nope") == (None, None)        # 未知 provider 也不炸


def test_available_providers_lists_only_resolvable(clean_env):
    clean_env.setenv("QIANWEN_API_KEY", FAKE_KEY)
    names = C.available_providers()
    assert "qianwen" in names
    assert "doubao" not in names      # model 未填


def test_masked_config_safe_to_print(clean_env):
    clean_env.setenv("QIANWEN_API_KEY", FAKE_KEY)
    cfg = C.masked_config()
    assert FAKE_KEY not in repr(cfg)
    assert cfg["api_key"] == C.mask_key(FAKE_KEY)
