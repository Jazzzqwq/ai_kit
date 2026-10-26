# -*- coding: utf-8 -*-
"""
任务 5: rank_candidates 纯函数契约

锁死四件事 (任务卡):
    1. 合并 L1/L2/L3 候选, 按 (by, locator) 去重, 按置信降序, 每条标出 layer
    2. 不写缓存 (不碰 _load_cache / _save_cache)
    3. 不返回 WebElement (只返回 Candidate 纯数据)
    4. 不抛异常 (driver / profile / llm_client 畸形或抛错都只降级)

外加: profile 站点规则优先、llm_client 注入才产 L4、同输入可复现。

运行: pytest tests/test_healer_rank.py
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement

from ai_kit import healer
from ai_kit.healer import Candidate, rank_candidates

LAYERS = {"L1", "L2", "L3", "L4"}
CONFIRM_SELECTOR = (".el-message-box__btns button.el-button--danger, "
                    ".el-message-box__btns button.el-button--primary, "
                    ".el-message-box__btns button:last-child")


def rank(driver=None, locator="", **kwargs):
    """测试内统一用 CSS 定位。

    签名是 (driver, by, locator) —— 直接 rank_candidates(driver, ".x") 会把 ".x"
    当成 by, locator 变成 None 从而返回空列表, 断言会因为"空"而假通过。
    """
    return rank_candidates(driver, By.CSS_SELECTOR, locator, **kwargs)


# ---------------------------------------------------------------------------
# 假 DOM: 只实现 rank_candidates 真正用到的那几个调用
# ---------------------------------------------------------------------------

class _FakeElement:
    def __init__(self, visible: bool = True):
        self._visible = visible
        self.size = {"width": 200, "height": 30}

    def is_displayed(self) -> bool:
        return self._visible

    def get_attribute(self, name: str):
        if name == "outerHTML":
            return '<div class="el-message-box"><button>确定</button></div>'
        return None


class _FakeDriver:
    """find_element 只认白名单里的 selector, 其余抛 NoSuchElement; explode=True 时全抛"""

    def __init__(self, visible=(".el-message-box",), explode: bool = False):
        self._visible = set(visible)
        self._explode = explode

    def find_element(self, by, value):
        if self._explode:
            raise RuntimeError("driver 炸了")
        if value in self._visible:
            return _FakeElement()
        raise NoSuchElementException(value)

    def find_elements(self, by, value):
        if self._explode:
            raise RuntimeError("driver 炸了")
        return [_FakeElement()] if value in self._visible else []


# ---------------------------------------------------------------------------
# L1 / 分层标记 / 排序去重
# ---------------------------------------------------------------------------

def test_l1_rule_is_top1_and_tagged():
    cands = rank(locator="body > div.el-message-box")

    top = cands[0]
    assert top.layer == "L1"
    assert top.locator == ".el-message-box"
    assert top.by == By.CSS_SELECTOR
    assert top.confidence == 1.1
    assert top.reason, "L1 要带上规则理由, 否则报告里没法解释为什么这么修"


def test_result_is_candidate_objects_not_web_elements():
    cands = rank(_FakeDriver(), ".el-table__rows", intent="表格数据体")

    assert cands, "有 driver + 已知语义时不该是空列表"
    for cand in cands:
        assert isinstance(cand, Candidate)
        assert not isinstance(cand, WebElement)
        assert isinstance(cand.by, str) and isinstance(cand.locator, str)
        assert cand.layer in LAYERS


def test_all_three_base_layers_present_with_driver():
    cands = rank(_FakeDriver(), ".el-message-box__container", intent="提示框")
    layers = {c.layer for c in cands}

    assert cands
    assert "L2" in layers, "语义候选应在 L2"
    assert "L3" in layers, "有 driver 时相对 XPath 应在 L3"
    assert "L4" not in layers, "没注入 llm_client 不该出现 L4"


def test_no_l3_without_driver():
    with_driver = rank(_FakeDriver(), ".el-message-box__container")
    without = rank(None, ".el-message-box__container")

    assert "L3" in {c.layer for c in with_driver}
    assert "L3" not in {c.layer for c in without}
    assert without, "没有 driver 也要给出 L2 候选, 不能整体为空"


def test_confidence_is_descending():
    cands = rank(_FakeDriver(), ".el-table__rows", intent="确定")
    confs = [c.confidence for c in cands]
    assert len(confs) >= 3
    assert confs == sorted(confs, reverse=True)


def test_same_locator_deduped_keeping_highest_confidence():
    """".el-message-box" 既被 L1 规则给出, 也被 L2 语义给出 → 只留 L1 那条"""
    cands = rank(locator="body > div.el-message-box")

    keys = [(c.by, c.locator) for c in cands]
    assert keys and len(keys) == len(set(keys)), "去重后 (by, locator) 必须唯一"
    hits = [c for c in cands if c.locator == ".el-message-box"]
    assert len(hits) == 1
    assert hits[0].layer == "L1" and hits[0].confidence == 1.1


def test_dedupe_tie_breaks_by_layer_order():
    same_locator = [Candidate("css", ".x", 0.9, "L2"), Candidate("css", ".x", 0.9, "L1")]
    out = healer._dedupe_sorted(same_locator)
    assert len(out) == 1 and out[0].layer == "L1"

    same_conf = [Candidate("css", ".a", 0.9, "L3"), Candidate("css", ".b", 0.9, "L1")]
    assert [c.locator for c in healer._dedupe_sorted(same_conf)] == [".b", ".a"]


def test_output_is_repeatable():
    first = [c.as_tuple() for c in rank(_FakeDriver(), ".el-dialog__container", intent="关闭")]
    assert first
    for _ in range(4):
        again = [c.as_tuple() for c in rank(_FakeDriver(), ".el-dialog__container", intent="关闭")]
        assert again == first


def test_candidate_is_immutable():
    cand = rank(locator="body > div.el-message-box")[0]
    with pytest.raises(FrozenInstanceError):
        cand.confidence = 0.0


def test_raw_locator_kept_as_low_confidence_fallback():
    cands = rank(None, ".el-table__rows")
    tail = cands[-1]
    assert tail.locator == ".el-table__rows" and tail.by == By.CSS_SELECTOR
    assert tail.confidence == 0.1


def test_empty_locator_returns_empty_list():
    assert rank_candidates() == []
    assert rank_candidates(None, None, None) == []
    assert rank_candidates(None, By.CSS_SELECTOR, "   ") == []


def test_positional_signature_is_driver_by_locator():
    """签名顺序固定: 第二个位置参数是 by —— 传错就只能是 by, 不会被当 locator"""
    cands = rank_candidates(None, By.CSS_SELECTOR, ".el-table__rows")
    assert cands and cands[-1].locator == ".el-table__rows"
    assert cands[-1].by == By.CSS_SELECTOR


# ---------------------------------------------------------------------------
# 不写缓存
# ---------------------------------------------------------------------------

def test_never_touches_heal_cache(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("rank_candidates 不得读写自愈缓存 (否则评测会污染生产缓存)")

    monkeypatch.setattr(healer, "_load_cache", _boom)
    monkeypatch.setattr(healer, "_save_cache", _boom)

    cands = rank(_FakeDriver(), ".el-dialog__container", intent="确定")
    assert cands


# ---------------------------------------------------------------------------
# profile: 站点规则优先, 畸形一律忽略
# ---------------------------------------------------------------------------

def test_profile_l1_rule_overrides_builtin():
    profile = {"l1_rules": {
        "body > div.el-message-box": {
            "by": By.CSS_SELECTOR,
            "locator": ".site-message-box",
            "reason": "站点弹窗根节点 class 不同",
        },
    }}
    cands = rank(locator="body > div.el-message-box", profile=profile)

    assert cands[0].layer == "L1" and cands[0].locator == ".site-message-box"
    assert "站点弹窗根节点" in cands[0].reason
    assert not any(c.layer == "L1" and c.locator == ".el-message-box" for c in cands), \
        "profile 命中时不应再出内置 L1 规则 (同一层只留一条)"


def test_profile_l1_rule_list_form():
    profile = {"l1_rules": {"my.old.xpath": [By.XPATH, "//div[@id='site-box']"]}}
    cands = rank(locator="my.old.xpath", profile=profile)
    assert cands[0].by == By.XPATH and cands[0].locator == "//div[@id='site-box']"
    assert cands[0].layer == "L1"


def test_profile_semantic_candidates_rank_above_builtin():
    profile = {"semantic_candidates": {"dialog": [".site-dialog"]}}
    cands = rank(locator=".el-dialog__body__old", profile=profile)

    site = [c for c in cands if c.locator == ".site-dialog"]
    builtin = [c for c in cands if c.locator == ".el-dialog"]
    assert site and builtin
    assert site[0].confidence > builtin[0].confidence, "站点候选应排在框架内置候选之前"


def test_profile_intent_keywords_extend_builtin():
    plain = rank(locator=".el-message-box__btns .zzz", intent="ok")
    assert not any(c.confidence == 1.0 for c in plain), "内置词表不认识 ok"

    prof = rank(locator=".el-message-box__btns .zzz", intent="ok",
                profile={"intent_keywords": {"confirm": ["ok"]}})
    intent_hits = [c for c in prof if c.confidence == 1.0]
    assert intent_hits and intent_hits[0].locator == CONFIRM_SELECTOR


@pytest.mark.parametrize("profile", [
    {"l1_rules": "oops", "semantic_candidates": [1, 2], "intent_keywords": {"confirm": "x"}},
    ["not", "a", "mapping"],
    42,
    object(),
    {"l1_rules": {"body > div.el-message-box": {"by": By.CSS_SELECTOR}}},   # 缺 locator
    {"l1_rules": {"body > div.el-message-box": "just-a-string"}},
])
def test_malformed_profile_is_ignored(profile):
    base = rank(_FakeDriver(), "body > div.el-message-box", intent="确定删除")
    got = rank(_FakeDriver(), "body > div.el-message-box", intent="确定删除", profile=profile)
    assert base
    assert [c.as_tuple() for c in got] == [c.as_tuple() for c in base]


# ---------------------------------------------------------------------------
# llm_client 注入
# ---------------------------------------------------------------------------

def test_llm_client_none_produces_no_l4():
    cands = rank(_FakeDriver(), ".el-message-box__btns .old", intent="确定")
    assert cands and "L4" not in {c.layer for c in cands}


def test_llm_client_injected_adds_l4_and_keeps_top1():
    calls = []

    def fake_llm(system_prompt, user_prompt):
        calls.append((system_prompt, user_prompt))
        return {"candidates": [{"selector": ".llm-btn", "confidence": 0.66, "reason": "文本匹配"}]}

    cands = rank(_FakeDriver(), ".el-message-box__btns button.el-button--primary",
                 intent="确定删除", llm_client=fake_llm)

    assert calls and "Failed locator" in calls[0][1] and "el-message-box" in calls[0][1]
    l4 = [c for c in cands if c.layer == "L4"]
    assert len(l4) == 1 and l4[0].locator == ".llm-btn" and l4[0].confidence == 0.66
    assert cands[0].layer == "L1", "L4 不得把规则硬命中挤下 top-1"


def test_llm_client_exception_is_swallowed():
    def broken_llm(system_prompt, user_prompt):
        raise RuntimeError("网络炸了")

    got = rank(_FakeDriver(), ".el-message-box__btns .old", intent="确定", llm_client=broken_llm)
    base = rank(_FakeDriver(), ".el-message-box__btns .old", intent="确定")
    assert base
    assert [c.as_tuple() for c in got] == [c.as_tuple() for c in base]


@pytest.mark.parametrize("payload", [
    "not-a-dict",
    {"candidates": "not-a-list"},
    {"candidates": [None, 42, {"selector": 123}, {"selector": "  "}]},
    {"candidates": [{"selector": ".ok", "confidence": "abc"}]},
    {"candidates": [{"selector": ".clamp-high", "confidence": 9},
                    {"selector": ".clamp-low", "confidence": -3}]},
])
def test_llm_client_bad_payload_is_tolerated(payload):
    got = rank(_FakeDriver(), ".el-message-box__btns .old",
               intent="确定", llm_client=lambda s, u: payload)
    assert got, "怪 payload 只该丢 L4 候选, 不能把 L1/L2/L3 一起清空"
    for cand in got:
        if cand.layer == "L4":
            assert 0.5 <= cand.confidence <= 1.0, "置信必须落在 [0.5, 1.0]"


def test_llm_client_ignored_without_driver():
    def fake_llm(system_prompt, user_prompt):
        raise AssertionError("没有 driver 就不该调 LLM")

    cands = rank(None, ".el-message-box__btns .old", llm_client=fake_llm)
    assert cands and "L4" not in {c.layer for c in cands}


# ---------------------------------------------------------------------------
# 不抛异常
# ---------------------------------------------------------------------------

def test_broken_driver_does_not_raise():
    cands = rank(_FakeDriver(explode=True), ".el-message-box__container")
    assert cands, "driver 全炸时至少还有 L1/L2 候选"
    assert "L3" not in {c.layer for c in cands}


@pytest.mark.parametrize("kwargs", [
    {"locator": "."},
    {"locator": "x", "by": object()},
    {"locator": "x", "intent": None},
    {"locator": "x", "intent": 12345},
    {"locator": "x", "profile": "string-profile"},
    {"locator": "x", "llm_client": object()},
])
def test_garbage_arguments_return_list(kwargs):
    assert isinstance(rank_candidates(None, **kwargs), list)


def test_unknown_locator_still_yields_raw_fallback():
    cands = rank(None, "div.ghost-element-zzz")
    assert [c.locator for c in cands] == ["div.ghost-element-zzz"]
