# -*- coding: utf-8 -*-
"""
generator Step 1: 页面采集改由 perception.collector 负责

这里钉的是一个真实缺陷的回归 —— 老的 DOM_SCRAPE_JS 是**语句形式**的 IIFE
(`(function(){...})();`), selenium 把它当函数体执行时永远返回 undefined, 于是
`scrape_page_structure` 每次都静默返回空结构, LLM 在"这页什么都没有"的前提下
生成用例, 而整条链路一个错都不报。

分两半:
1. 纯 Python 半: 接线是否真的转到 collector / slim 是否把 HTML 摘干净 / 脏输入不崩。
2. 真浏览器半: 真实页面上必须采到按钮 (老实现恰恰恒为空)。拉不起浏览器就 skip。
"""
from __future__ import annotations

import json
import os

import pytest

from ai_kit.generator import core
from ai_kit.perception.collector import empty_inventory

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "ai_kit", "evaluation", "golden", "fixtures")


class FakeDriver:
    """只实现采集用到的 execute_script"""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def execute_script(self, script, *args):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return self.result


# ---------------------------------------------------------------------------
# 1. 接线
# ---------------------------------------------------------------------------

def test_generator_no_longer_carries_its_own_scrape_js():
    """旧副本必须彻底消失, 否则下一个人又会去改那份恒返回 undefined 的脚本"""
    assert not hasattr(core, "DOM_SCRAPE_JS")
    assert not hasattr(core._prompts, "DOM_SCRAPE_JS")


def test_scrape_page_structure_delegates_to_collector(monkeypatch):
    seen = {}

    def fake_collect(driver, profile=None, *, page_name="", **kwargs):
        seen["driver"] = driver
        seen["page_name"] = page_name
        return {"title": "假页面", "inputs": [], "buttons": []}

    monkeypatch.setattr(core._collector, "collect", fake_collect)
    marker = object()

    out = core.scrape_page_structure(marker, "用户管理")

    assert seen["driver"] is marker
    assert seen["page_name"] == "用户管理"
    assert out["title"] == "假页面"


def test_scrape_page_structure_without_driver_degrades_to_empty():
    inv = core.scrape_page_structure(None, "用户管理")

    assert set(inv) == set(empty_inventory()), "降级骨架的键必须齐全"
    assert inv["inputs"] == [] and inv["buttons"] == []
    assert inv["notes"], "降级原因要能被上层看到"


def test_scrape_page_structure_survives_broken_driver():
    with pytest.warns(RuntimeWarning, match="采集 JS 执行失败"):
        inv = core.scrape_page_structure(FakeDriver(error="page crashed"))

    assert inv["buttons"] == []


def test_scrape_page_structure_accepts_json_string():
    payload = {"title": "t", "buttons": [{"eid": "e1", "text": "保存", "tag": "button"}]}

    inv = core.scrape_page_structure(FakeDriver(result=json.dumps(payload)))

    assert [b["text"] for b in inv["buttons"]] == ["保存"]


# ---------------------------------------------------------------------------
# 2. slim: 进提示词前的压缩
# ---------------------------------------------------------------------------

RICH_INVENTORY = {
    "title": "用户管理",
    "url": "file:///tmp/u.html",
    "counts": {"discovered": 2, "interactive": 2, "inputs": 1, "buttons": 1, "tables": 1},
    "container_summary": {"form": 1, "toolbar": 1},
    "risks": {"write": 1},
    "dialogs": [{"eid": "d1", "selector": ".el-dialog", "text": "新增用户",
                 "buttons": ["确定", "取消"]}],
    "inputs": [{
        "eid": "e1", "kind": "input", "tag": "input", "text": "", "name": "username",
        "role": "textbox", "container": "form", "risk": None,
        "attrs": {"name": "username"}, "aria": {"label": "用户名"},
        "locator": {"by": "css selector", "value": '[name="username"]',
                    "stability": 0.85, "strategy": "name"},
        "alternatives": [{"by": "xpath", "value": "//input[1]", "stability": 0.4}],
        "constraints": {"required": True, "maxlength": 20},
        "actions": ["fill", "clear"],
        "html": '<input name="username">',
    }],
    "buttons": [{
        "eid": "e2", "kind": "button", "tag": "button", "text": "保存", "name": "",
        "role": "button", "container": "toolbar", "risk": "write",
        "attrs": {"class": "el-button"}, "aria": {},
        "locator": {"by": "css selector", "value": "#save", "stability": 1.0, "strategy": "id"},
        "alternatives": [],
        "constraints": {"type": "button"},
        "actions": ["click"],
        "html": "<button id='save'>保存</button>",
    }],
    "selects": [],
    "tables": [{
        "eid": "c1", "kind": "table", "tag": "table", "text": "", "container": "page",
        "columns": ["名称", "状态"], "row_count": 42,
        "sample_rows": [["A", "正常"], ["B", "停用"], ["C", "正常"]],
        "row_actions": [{"text": "编辑", "eid": "e9", "count": 42,
                         "locator": {"by": "css selector", "value": "td .edit"}}],
        "html": "<table>...</table>",
    }],
}


def test_slim_strips_html_and_alternatives():
    slim = core.slim_page_structure(RICH_INVENTORY)

    for bucket in ("inputs", "buttons", "selects", "tables"):
        for element in slim[bucket]:
            assert "html" not in element, "outerHTML 进提示词就是纯烧 token"
            assert "alternatives" not in element
            assert "aria" not in element


def test_slim_keeps_what_the_llm_needs():
    slim = core.slim_page_structure(RICH_INVENTORY)
    field, button = slim["inputs"][0], slim["buttons"][0]

    assert field["eid"] == "e1"
    assert field["locator"] == '[name="username"]', "定位串压成一行, 但保留"
    assert field["constraints"] == {"required": True, "maxlength": 20}
    assert button["risk"] == "write"
    assert button["container"] == "toolbar"
    assert "risk" not in field, "None 不该占位"
    assert slim["risks"] == {"write": 1}
    assert slim["containers"] == {"form": 1, "toolbar": 1}
    assert slim["dialogs"][0]["buttons"] == ["确定", "取消"]


def test_slim_table_keeps_columns_and_row_actions():
    table = core.slim_page_structure(RICH_INVENTORY)["tables"][0]

    assert table["columns"] == ["名称", "状态"]
    assert table["row_count"] == 42
    assert len(table["sample_rows"]) == 2, "样例行只留 2 行"
    assert table["row_actions"] == [{"text": "编辑", "eid": "e9"}], "行内按钮要带 eid"


def test_slim_truncates_each_kind():
    slim = core.slim_page_structure({
        "inputs": [{"eid": f"i{i}"} for i in range(40)],
        "buttons": [{"eid": f"b{i}"} for i in range(40)],
        "selects": [{"eid": f"s{i}"} for i in range(40)],
        "tables": [{"eid": f"c{i}"} for i in range(10)],
    })

    assert (len(slim["inputs"]), len(slim["buttons"]),
            len(slim["selects"]), len(slim["tables"])) == (15, 20, 5, 3)


def test_slim_reports_truncation_and_notes():
    slim = core.slim_page_structure({
        "truncated": True,
        "groups": [{"container": "page", "count": 40, "samples": []}],
        "notes": ["交互元素超过上限 400, 已按容器分组摘要"],
    })

    assert slim["truncated"] is True
    assert slim["groups"][0]["count"] == 40
    assert slim["notes"]


def test_slim_accepts_garbage():
    """slim 会被喂 {'}'(driver=None) / 半截 payload, 一律降级不崩"""
    for payload in ({}, None, "text", {"inputs": "not-a-list"},
                    {"buttons": [None, 1, "x"]}):
        slim = core.slim_page_structure(payload, page_name="用户管理")

        assert slim["title"] == "用户管理"
        assert slim["inputs"] == [] or all(isinstance(e, dict) for e in slim["inputs"])
        assert slim["buttons"] == [] or all(isinstance(e, dict) for e in slim["buttons"])


# ---------------------------------------------------------------------------
# 3. 真浏览器: 老 bug 的正面回归
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def driver():
    from ai_kit.evaluation import driver_provider

    drv = driver_provider.get_driver()
    if drv is None:
        pytest.skip("本机拉不起无头 Edge/Chrome, 跳过真页面采集")
    yield drv
    driver_provider.quit_driver(drv)


def test_real_page_yields_buttons_through_generator(driver):
    from ai_kit.evaluation import driver_provider

    driver.get(driver_provider.to_file_url(os.path.join(FIXTURES, "el_message_box.html")))
    inv = core.scrape_page_structure(driver, "消息框")

    assert inv["title"] == "el-message-box fixture"
    assert len(inv["buttons"]) >= 3, \
        "老 DOM_SCRAPE_JS 在这个页面上恒为空 —— 采到按钮才算真的通了"

    slim = core.slim_page_structure(inv)
    assert slim["counts"]["buttons"] == len(inv["buttons"])
    assert all("html" not in b for b in slim["buttons"])
