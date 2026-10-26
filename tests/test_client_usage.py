# -*- coding: utf-8 -*-
"""
任务 3 回归测试: usage 回填 (client.py)

契约 (现有函数签名/行为不变):
- 模块级 _LAST_USAGE 保存最近一次响应的 usage, get_last_usage() 返回副本
- chat() 解析响应体 usage -> 同时写 _LAST_USAGE 与 _CUMULATIVE_USAGE
- usage 缺失 / 不是 dict / 字段是脏值, 只影响 token 统计, 不影响 chat 的返回值
- chat_json / chat_with_image / chat_json_with_image 都经由 chat() 回填
- 四个公开调用函数的签名 (含默认值) 与返回值类型不变

全部离线: requests.post 换成桩, 不读本机 .env, 不发网络请求。
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from ai_kit import client

pytestmark = pytest.mark.skipif(not client._HAS_REQUESTS, reason="requests 未安装")

_EMPTY = inspect.Parameter.empty
_USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


class _FakeResp:
    def __init__(self, status_code: int = 200, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _ok(content: str, usage: Any = _USAGE) -> _FakeResp:
    return _FakeResp(200, {"choices": [{"message": {"content": content}}],
                           "usage": usage})


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """干净基线: 离线配置 + 归零的用量全局, 保证断言可以写死"""
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "get_active_config", lambda: {
        "provider": "unit-test", "base_url": "http://llm.invalid/v1",
        "model": "unit-model", "timeout": 1, "api_key": "sk-test-fake-000",
    })
    monkeypatch.setattr(client, "_LAST_USAGE", {})
    monkeypatch.setattr(client, "_CUMULATIVE_USAGE",
                        {"prompt_tokens": 0, "completion_tokens": 0,
                         "total_tokens": 0, "calls": 0})


def _install(monkeypatch, *responses: _FakeResp):
    """把 requests.post 换成按序返回假响应的桩, 返回调用记录 (含发出去的 payload)"""
    sent = []

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append({"url": url, "headers": headers, "json": json})
        return responses[min(len(sent) - 1, len(responses) - 1)]

    monkeypatch.setattr(client.requests, "post", fake_post)
    return sent


def _usage_of(*, prompt=0, completion=0, total=0, calls=0):
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total, "calls": calls}


# ---------------------------------------------------------------------------
# 模块级状态 + 读取接口
# ---------------------------------------------------------------------------

def test_last_usage_is_module_level_and_starts_empty():
    assert isinstance(vars(client)["_LAST_USAGE"], dict)
    assert client.get_last_usage() == {}


def test_get_last_usage_returns_copy():
    client._record_usage({"usage": dict(_USAGE)})
    client.get_last_usage()["prompt_tokens"] = 999
    assert client.get_last_usage()["prompt_tokens"] == 11
    client.get_cumulative_usage()["calls"] = 999
    assert client.get_cumulative_usage()["calls"] == 1


# ---------------------------------------------------------------------------
# chat() 解析 usage
# ---------------------------------------------------------------------------

def test_chat_records_usage(monkeypatch):
    _install(monkeypatch, _ok("  ok  \n"))
    assert client.chat([{"role": "user", "content": "hi"}]) == "ok"   # 返回值仍是纯文本
    assert client.get_last_usage() == _USAGE
    assert client.get_cumulative_usage() == _usage_of(prompt=11, completion=7, total=18, calls=1)


def test_cumulative_usage_is_additive(monkeypatch):
    _install(monkeypatch, _ok("a"), _ok("b"))
    client.chat([{"role": "user", "content": "1"}])
    client.chat([{"role": "user", "content": "2"}])
    assert client.get_cumulative_usage() == _usage_of(prompt=22, completion=14, total=36, calls=2)


def test_chat_without_usage_still_returns_content(monkeypatch):
    _install(monkeypatch, _ok("ok", usage=None))
    assert client.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert client.get_last_usage() == {}
    assert client.get_cumulative_usage() == _usage_of(calls=1)      # 调用照样计数, token 记 0


@pytest.mark.parametrize("dirty_usage", [
    "n/a",                                          # 不是 dict
    {"prompt_tokens": "abc", "total_tokens": None},  # 字段不是数字
    {"prompt_tokens": [1]},
])
def test_dirty_usage_does_not_break_chat(monkeypatch, dirty_usage):
    """回填是旁路: 脏 usage 绝不能把一次成功响应的返回值吞成 None (旧版会)"""
    _install(monkeypatch, _ok("ok", usage=dirty_usage))
    assert client.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert client.get_cumulative_usage()["calls"] == 1


def test_record_usage_tolerates_weird_payloads():
    for payload in (None, "oops", [1, 2], 42, {"usage": "n/a"}, {}):
        client._record_usage(payload)
    assert client.get_cumulative_usage() == _usage_of(calls=6)
    assert client.get_last_usage() == {}


def test_failed_call_does_not_count_and_keeps_previous_usage(monkeypatch):
    """失败(HTTP 500) 不产生 usage: 不计调用次数, get_last_usage 仍保留上次成功值"""
    _install(monkeypatch, _ok("ok"), _FakeResp(500, None, "boom"))
    client.chat([{"role": "user", "content": "hi"}])
    assert client.chat([{"role": "user", "content": "hi"}], max_retries=0) is None
    assert client.get_cumulative_usage() == _usage_of(prompt=11, completion=7, total=18, calls=1)
    assert client.get_last_usage() == _USAGE


# ---------------------------------------------------------------------------
# 其它入口都经由 chat() 回填
# ---------------------------------------------------------------------------

def test_chat_json_backfills_usage(monkeypatch):
    _install(monkeypatch, _ok('{"a": 1}'))
    assert client.chat_json("sys", "usr") == {"a": 1}
    assert client.get_last_usage() == _USAGE


def test_chat_with_image_is_multimodal_and_backfills_usage(monkeypatch):
    sent = _install(monkeypatch, _ok("ok"))
    assert client.chat_with_image("sys", "看图", "QUJD") == "ok"

    parts = sent[0]["json"]["messages"][1]["content"]
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,QUJD"
    assert sent[0]["json"]["temperature"] == 0.2
    assert client.get_last_usage() == _USAGE


def test_chat_json_with_image_backfills_usage(monkeypatch):
    _install(monkeypatch, _ok('```json\n{"hit": true}\n```'))
    assert client.chat_json_with_image("sys", "看图", "QUJD") == {"hit": True}
    assert client.get_last_usage() == _USAGE


# ---------------------------------------------------------------------------
# 签名不变 (任务卡硬约束)
# ---------------------------------------------------------------------------

def _params(fn):
    return [(p.name, p.default) for p in inspect.signature(fn).parameters.values()]


def test_public_signatures_unchanged():
    # 5.10 第 3 条只加了一个尾巴参数 max_tokens (显式上限), 前面一个都没动:
    # 位置参数一旦错位, 线上调用会静默地把 prompt 传到 temperature 上。
    assert _params(client.chat) == [
        ("messages", _EMPTY), ("temperature", 0.1), ("max_retries", 2),
        ("max_tokens", None)]
    assert _params(client.chat_json) == [
        ("system_prompt", _EMPTY), ("user_prompt", _EMPTY), ("max_retries", 2),
        ("temperature", 0.1), ("max_tokens", None)]
    assert _params(client.chat_with_image) == [
        ("system_prompt", _EMPTY), ("user_text", _EMPTY), ("image_base64", _EMPTY),
        ("image_mime", "image/png"), ("max_retries", 2), ("max_tokens", None)]
    assert _params(client.chat_json_with_image) == [
        ("system_prompt", _EMPTY), ("user_text", _EMPTY), ("image_base64", _EMPTY),
        ("image_mime", "image/png"), ("max_retries", 2), ("max_tokens", None)]
    assert _params(client.get_last_usage) == []
    assert _params(client.get_cumulative_usage) == []
    assert _params(client.test_connection) == []
    assert _params(client.llm_available) == []
