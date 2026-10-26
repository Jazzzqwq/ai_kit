# -*- coding: utf-8 -*-
"""
任务 A-6: healer 与规则包的接线

外置的验收标准不是"代码看起来更干净", 而是 **默认路径行为一字不变**:
不传 profile 时 healer 用的就是 element_plus 那一份知识, 候选序列、置信度、
L4 容器抓取顺序都必须和改造前的硬编码一致; 传 profile 时站点/框架规则才生效。

运行: pytest tests/test_healer_rules.py
"""
from __future__ import annotations

import pytest
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

from ai_kit import healer
from ai_kit.profiles import effective_rules, load_profile


# ---------------------------------------------------------------------------
# 兼容视图: 老代码 import 的模块常量不能因为外置而失效
# ---------------------------------------------------------------------------

def test_l1_rules_view_still_available():
    pack = load_profile("element_plus")["l1_rules"]

    assert set(healer.L1_RULES) == set(pack)
    for broken, (by, locator, reason) in healer.L1_RULES.items():
        assert (by, locator, reason) == (pack[broken]["by"], pack[broken]["locator"],
                                         pack[broken]["reason"])


def test_semantic_views_still_available():
    hints = load_profile("element_plus")["semantic_hints"]

    assert [pat.pattern for pat, _ in healer.SEMANTIC_HINTS] == [h[0] for h in hints]
    assert healer.SEMANTIC_CANDIDATES == load_profile("element_plus")["semantic_candidates"]


# ---------------------------------------------------------------------------
# 默认候选序列 = 改造前
# ---------------------------------------------------------------------------

def test_default_rank_matches_legacy_sequence():
    """L1 命中 + L2 同语义去重后, 序列与置信度必须与改造前完全一致"""
    cands = healer.rank_candidates(None, By.CSS_SELECTOR, "body > div.el-message-box")

    assert [(c.locator, c.confidence, c.layer) for c in cands] == [
        (".el-message-box", 1.1, "L1"),
        (".el-message-box__wrapper .el-message-box", 0.85, "L2"),
        ("body > .el-message-box__wrapper > .el-message-box", 0.8, "L2"),
        ("body > div.el-message-box", 0.1, "L2"),
    ]


def test_default_intent_candidate_still_1_0():
    cands = healer.rank_candidates(None, By.CSS_SELECTOR, ".el-message-box__btns .old",
                                   intent="确定删除")

    assert cands[0].confidence == 1.0
    assert cands[0].locator == load_profile("element_plus")["intent_selectors"]["confirm"]


def test_default_pack_ignores_other_frameworks():
    """内置包只装 Element Plus 知识: antd 定位器在默认路径下不该被"猜"到"""
    broken = ".ant-modal__body__old"
    cands = healer.rank_candidates(None, By.CSS_SELECTOR, broken)

    assert [c.locator for c in cands] == [broken]


def test_antd_profile_teaches_modal_semantics():
    broken = ".ant-modal__body__old"
    cands = healer.rank_candidates(None, By.CSS_SELECTOR, broken, profile="antd")

    locators = [c.locator for c in cands]
    assert locators[0] == ".ant-modal"
    assert locators[1] == ".ant-modal-wrap .ant-modal"


# ---------------------------------------------------------------------------
# L4 容器: 框架容器在前, 通用模式兜底
# ---------------------------------------------------------------------------

class _VisibleElement:
    size = {"width": 100, "height": 20}

    def __init__(self, html: str):
        self._html = html

    def is_displayed(self) -> bool:
        return True

    def get_attribute(self, name: str):
        return self._html if name == "outerHTML" else None


class _RecordingDriver:
    """只记录被问了哪些选择器; 命中的返回一个可见元素"""

    def __init__(self, html_by_selector=None):
        self.asked = []
        self._html = html_by_selector or {}

    def find_elements(self, by, value):
        self.asked.append(value)
        if value in self._html:
            return [_VisibleElement(self._html[value])]
        return []


def test_l4_container_order_matches_legacy_then_generic():
    driver = _RecordingDriver({".el-dialog": "<div class='el-dialog'></div>"})

    healer._l4_llm_candidates(driver, ".x", llm_client=lambda s, u: {"candidates": []})

    framework = load_profile("element_plus")["l4_containers"]
    generic = load_profile("generic")["l4_containers"]
    assert driver.asked == framework + generic, "先试框架容器, 再用通用模式兜底"


def test_l4_universal_container_is_found_without_any_framework_class():
    driver = _RecordingDriver({"[role=dialog]": "<div role='dialog'>原生弹窗</div>"})
    captured = {}

    def fake_llm(system_prompt, user_prompt):
        captured["prompt"] = user_prompt
        return {"candidates": [{"selector": "button.ok", "confidence": 0.7}]}

    out = healer._l4_llm_candidates(driver, ".ghost", llm_client=fake_llm)

    assert out == [(By.CSS_SELECTOR, "button.ok", 0.7)]
    assert "role='dialog'" in captured["prompt"], "通用容器 HTML 应该被抓进提示词"


def test_l4_containers_can_be_overridden_by_rules():
    driver = _RecordingDriver({})

    healer._l4_llm_candidates(driver, ".x", llm_client=lambda s, u: {"candidates": []},
                              containers=["[role=dialog]", ".shop-shell"])

    assert driver.asked == ["[role=dialog]", ".shop-shell"]


def test_site_l4_containers_come_before_framework_ones():
    rules = effective_rules(load_profile("example_shop"))

    assert rules["l4_containers"][:2] == [".filter-card", ".shop-grid"]
    assert ".el-dialog" in rules["l4_containers"]


# ---------------------------------------------------------------------------
# try_heal 生产路径也吃规则包
# ---------------------------------------------------------------------------

class _HealElement:
    tag_name = "div"
    size = {"width": 100, "height": 20}

    def is_displayed(self) -> bool:
        return True

    def is_enabled(self) -> bool:
        return True

    def get_attribute(self, name: str):
        return {"role": None, "type": None, "class": "el-message-box"}.get(name)


class _HealDriver:
    def __init__(self, ok_selectors):
        self.ok = set(ok_selectors)
        self.asked = []

    def find_element(self, by, value):
        self.asked.append(value)
        if value in self.ok:
            return _HealElement()
        raise NoSuchElementException(value)


@pytest.fixture()
def no_cache(monkeypatch):
    """try_heal 会读写自愈缓存; 单测里换成内存空表, 不碰 ~/.ai_kit"""
    monkeypatch.setattr(healer, "_load_cache", lambda: {})
    monkeypatch.setattr(healer, "_save_cache", lambda cache: None)


def test_try_heal_uses_builtin_rule(no_cache):
    driver = _HealDriver({".el-message-box"})

    el = healer.try_heal(driver, By.CSS_SELECTOR, "body > div.el-message-box")

    assert el is not None
    assert driver.asked == [".el-message-box"], "L1 命中就该收手, 不该再扫 L2"


def test_try_heal_accepts_site_profile(no_cache):
    driver = _HealDriver({".shop-message-box"})

    el = healer.try_heal(driver, By.CSS_SELECTOR, "body > div.el-message-box",
                         profile=load_profile("example_shop"))

    assert el is not None
    assert driver.asked[0] == ".shop-message-box"


def test_try_heal_profile_does_not_leak_into_default(no_cache):
    """传过站点 profile 之后, 下一次不传还应该用内置规则"""
    healer.try_heal(_HealDriver({".shop-message-box"}), By.CSS_SELECTOR,
                    "body > div.el-message-box", profile=load_profile("example_shop"))

    driver = _HealDriver({".el-message-box"})
    assert healer.try_heal(driver, By.CSS_SELECTOR, "body > div.el-message-box") is not None
    assert driver.asked == [".el-message-box"]
