# -*- coding: utf-8 -*-
"""
元素清单: dict -> PageInventory 定型 + 指纹 + 派生视图

分两半:
1. 纯 Python: 用手写的采集结果钉住折叠规则 (定位串 / 可读名 / 状态 / 风险)、
   指纹的"该变就变、不该变就不变"、脏输入降级、往返不丢信息。
2. 真浏览器: collect 出来的东西必须能定型 —— 这条防的是"单测都在手写 dict 上
   跑, 真采集一接就字段对不上"。拉不起无头浏览器就整组 skip。
"""
from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError

import pytest

from ai_kit.perception import inventory as inv_mod
from ai_kit.perception.inventory import (
    FINGERPRINT_PREFIX,
    INVENTORY_VERSION,
    ElementSpec,
    PageInventory,
    canonical_dom,
    fingerprint_of,
    from_collect,
    from_inventory,
)


def _locator(value, stability, strategy, by="css selector"):
    return {"by": by, "value": value, "stability": stability, "strategy": strategy}


BASE = {
    "url": "https://shop.example.com/order/list?page=2",
    "title": "订单列表",
    "truncated": False,
    "notes": ["一行说明"],
    "inputs": [
        {"eid": "e1", "kind": "input", "tag": "input", "type": "text", "role": "textbox",
         "text": "", "name": "keyword",
         "attrs": {"name": "keyword", "placeholder": "订单号", "class": "el-input__inner"},
         "aria": {"label": "关键字"}, "container": "filter-bar", "depth": 8,
         "locator": _locator('[name="keyword"]', 0.85, "name"),
         "alternatives": [_locator("input.el-input__inner", 0.7, "semantic-css"),
                          _locator("//input[1]", 0.4, "relative-xpath", by="xpath")],
         "constraints": {"required": True, "type": "text", "maxlength": 32,
                         "readonly": False, "disabled": False, "has_value": False},
         "actions": ["fill", "clear"], "risk": None,
         "html": '<input name="keyword" value="[REDACTED]">'},
        {"eid": "e5", "kind": "input", "tag": "input", "type": "text", "role": "textbox",
         "text": "", "name": "ro", "attrs": {"name": "ro", "readonly": ""}, "aria": {},
         "container": "form", "depth": 6,
         "locator": _locator('[name="ro"]', 0.85, "name"), "alternatives": [],
         "constraints": {"type": "text", "readonly": True, "disabled": False},
         "actions": ["fill", "clear"], "risk": None, "html": "<input name=\"ro\">"},
    ],
    "buttons": [
        # locator 被采坏, 只能用备选顶上; 主定位应该是备选里的 semantic-css
        {"eid": "e2", "kind": "button", "tag": "button", "text": "删除", "name": "",
         "attrs": {"class": "btn btn-danger"}, "aria": {}, "container": "table-row", "depth": 12,
         "locator": None,
         "alternatives": [_locator("button.btn-danger", 0.7, "semantic-css")],
         "constraints": {}, "actions": ["click"], "risk": "destructive",
         "html": "<button class=\"btn btn-danger\">删除</button>"},
        # 疑似动态 id 分数低, 不能被当成主定位
        {"eid": "e3", "kind": "button", "tag": "button", "text": "保存", "name": "",
         "attrs": {"id": "el-id-1234-5", "aria-label": "保存"}, "aria": {"label": "保存"},
         "container": "dialog", "depth": 14,
         "locator": _locator("#el-id-1234-5", 0.6, "id(疑似动态)"),
         "alternatives": [_locator('[aria-label="保存"]', 0.9, "aria-label")],
         "constraints": {}, "actions": ["click"], "risk": "write",
         "html": "<button>保存</button>"},
        {"eid": "e4", "kind": "button", "tag": "a", "text": "查看", "name": "",
         "attrs": {"href": "/order/123", "class": "link"}, "aria": {},
         "container": "table-row", "depth": 12,
         "locator": _locator("//a[1]", 0.4, "relative-xpath", by="xpath"),
         "alternatives": [_locator("//a[normalize-space()='查看']", 0.4, "text", by="xpath")],
         "constraints": {}, "actions": ["click"], "risk": None,
         "html": "<a href=\"/order/123\">查看</a>"},
    ],
    "selects": [
        {"eid": "e6", "kind": "select", "tag": "select", "role": "combobox", "text": "",
         "name": "status", "attrs": {"name": "status"}, "aria": {},
         "container": "filter-bar", "depth": 8,
         "locator": _locator('[name="status"]', 0.85, "name"), "alternatives": [],
         "constraints": {"type": "select"}, "actions": ["select"], "risk": None,
         "options": ["全部", "已完成"], "html": "<select name=\"status\"></select>"},
    ],
    "tables": [{
        "eid": "c1", "kind": "table", "tag": "table", "role": "table", "text": "",
        "attrs": {}, "aria": {}, "container": "page", "depth": 10,
        "locator": _locator("table", 0.7, "semantic-css"), "alternatives": [],
        "columns": ["订单号", "状态", "操作"], "row_count": 22,
        "sample_rows": [["A001", "已完成", "编辑 删除"], ["A002", "待付款", "编辑 删除"]],
        "row_actions": [{"text": "编辑", "eid": None, "count": 22},
                        {"text": "删除", "eid": "e2", "count": 22}],
    }],
    "dialogs": [{"eid": "d1", "selector": ".el-dialog", "text": "确认保存",
                 "buttons": ["保存", "取消"]}],
}


def sample(**overrides):
    payload = copy.deepcopy(BASE)
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1. 元素定型
# ---------------------------------------------------------------------------

def test_elements_are_typed_and_ordered():
    inv = from_inventory(sample())

    assert isinstance(inv, PageInventory)
    assert [e.eid for e in inv.elements] == ["e1", "e5", "e2", "e3", "e4", "e6"]
    assert [e.kind for e in inv.elements][:2] == ["input", "input"]
    assert len(inv) == 6 and list(inv)[0].eid == "e1", "PageInventory 要能直接当序列用"
    assert inv.url.startswith("https://shop.example.com/order/list")
    assert inv.title == "订单列表"
    assert inv.captured_at and inv.captured_at.endswith("+00:00")
    assert inv.page_name == ""


def test_locators_fold_into_card_shape():
    inv = from_inventory(sample())
    field = inv.element("e1")

    assert list(field.locators) == ["name", "css", "xpath"], "semantic-css -> css, relative-xpath -> xpath"
    assert field.locator_by["name"] == "css selector"
    assert field.locator_by["xpath"] == "xpath"
    assert field.primary == "name"
    assert field.stability == 0.85
    assert field.selector == '[name="keyword"]'


def test_primary_is_the_most_stable_not_the_first():
    """疑似动态 id 排在前面但分数更低, 主定位要落到 aria-label 上"""
    button = from_inventory(sample()).element("e3")

    assert button.primary == "aria-label"
    assert button.stability == 0.9
    assert "id" in button.locators, "低分定位串要留着当备选"


def test_locator_promoted_from_alternatives_when_broken():
    button = from_inventory(sample()).element("e2")

    assert button.primary == "css"
    assert button.selector == "button.btn-danger"
    assert button.stability == 0.7


def test_selector_falls_back_to_eid_marker():
    payload = sample(inputs=[{"eid": "e9", "kind": "input", "tag": "input", "locator": None}])
    element = from_inventory(payload).element("e9")

    assert element.locators == {}
    assert element.stability == 0.0
    assert element.selector == '[data-ai-eid="e9"]', "一个定位串都没采到时, 只能靠采集时打的标记"
    assert element.selector_by == "css selector"


def test_name_falls_back_through_readable_sources():
    inv = from_inventory(sample())

    assert inv.element("e1").name == "关键字", "aria-label 优先于 placeholder"
    assert inv.element("e2").name == "删除", "按钮用文本"
    assert inv.element("e5").name == "ro", "什么都没有时退回 name 属性"


def test_unnameable_element_keeps_empty_name():
    """纯图标按钮确实没有可读名; 用空串而不是 eid, 否则指纹跟着 eid 编号跑"""
    payload = sample(inputs=[], selects=[], tables=[], dialogs=[],
                     buttons=[{"eid": "e1", "kind": "button", "tag": "button", "text": ""}])

    assert from_inventory(payload).element("e1").name == ""


def test_input_value_never_survives():
    """attrs.value 是用户数据; 采集器已拦, 这里再拦一次 (脏 payload 也要挡住)"""
    payload = sample(inputs=[{"eid": "e1", "kind": "input", "tag": "input",
                              "attrs": {"name": "u", "value": "admin"}}])
    inv = from_inventory(payload)

    assert "value" not in inv.element("e1").attrs
    assert "admin" not in json.dumps(inv.to_dict(), ensure_ascii=False)


def test_state_is_derived_from_constraints():
    inv = from_inventory(sample())

    assert inv.element("e1").state == {"visible": True, "disabled": False, "readonly": False}
    assert inv.element("e5").state["readonly"] is True, "只读事实只存一份, 从约束里读"


def test_risk_none_becomes_safe():
    inv = from_inventory(sample())

    assert inv.element("e1").risk == "safe"
    assert inv.element("e2").risk == "destructive"
    assert [e.eid for e in inv.risky_elements] == ["e2", "e3"]


def test_unknown_risk_is_preserved():
    """采集器/规则包多给一档时, 不能悄悄改写成 safe —— 那会让危险动作失去保护"""
    payload = sample(buttons=[{"eid": "e7", "kind": "button", "tag": "button",
                               "text": "作废", "risk": "irreversible"}])
    assert from_inventory(payload).element("e7").risk == "irreversible"


def test_dirty_elements_are_dropped_and_deduped():
    payload = sample(
        inputs=[{"eid": "e1", "kind": "input", "tag": "input"},
                {"eid": "e1", "kind": "input", "tag": "input"},   # eid 重复
                {"eid": "", "kind": "input"},                     # 没 eid
                "not-a-dict",                                     # 脏条目
                {"kind": "input"}],                               # 没 eid
        buttons=[], selects=[], tables=[], dialogs=[]
    )
    inv = from_inventory(payload)

    assert [e.eid for e in inv.elements] == ["e1"]
    assert inv.stats["inputs"] == 1


def test_element_kind_and_type_are_kept():
    payload = sample(inputs=[{"eid": "e1", "kind": "input", "tag": "input",
                              "type": "password", "constraints": {"type": "password"}}])
    element = from_inventory(payload).element("e1")

    assert element.kind == "input"
    assert element.type == "password", "原型分类靠它区分密码框与普通文本框"


def test_select_options_and_actions_survive():
    select = from_inventory(sample()).element("e6")

    assert select.options == ("全部", "已完成")
    assert select.actions == ("select",)
    assert select.kind == "select"


# ---------------------------------------------------------------------------
# 2. 派生视图
# ---------------------------------------------------------------------------

def test_tables_and_dialogs_are_passed_through():
    inv = from_inventory(sample())

    table = inv.tables[0]
    assert table["eid"] == "c1"
    assert table["columns"] == ["订单号", "状态", "操作"]
    assert table["row_count"] == 22
    assert table["sample_rows"][0][:2] == ["A001", "已完成"]
    assert [a["text"] for a in table["row_actions"]] == ["编辑", "删除"]
    assert table["row_actions"][1]["eid"] == "e2"

    assert inv.dialogs[0]["selector"] == ".el-dialog"
    assert inv.dialogs[0]["buttons"] == ["保存", "取消"]


def test_lookup_covers_elements_tables_and_dialogs():
    inv = from_inventory(sample())

    assert isinstance(inv.lookup("e1"), ElementSpec)
    assert inv.lookup("c1")["kind"] == "table"
    assert inv.lookup("d1")["selector"] == ".el-dialog"
    assert inv.lookup("nope") is None
    assert inv.element("c1") is None, "表格不是可交互元素, 不该混进 by_eid"


def test_links_are_derived_from_anchors():
    payload = sample(
        inputs=[], selects=[], tables=[], dialogs=[],
        buttons=[
            {"eid": "e1", "kind": "button", "tag": "a", "text": "查看",
             "attrs": {"href": "/order/123"}, "container": "table-row",
             "locator": _locator("//a[1]", 0.4, "relative-xpath", by="xpath")},
            {"eid": "e2", "kind": "button", "tag": "a", "text": "删除",
             "attrs": {"href": "javascript:void(0)"}, "container": "table-row",
             "locator": _locator("//a[2]", 0.4, "relative-xpath", by="xpath")},
            {"eid": "e3", "kind": "button", "tag": "button", "text": "确定"},
        ])
    inv = from_inventory(payload)

    assert [link["eid"] for link in inv.links] == ["e1"]
    assert inv.links[0]["href"] == "/order/123"
    assert inv.links[0]["container"] == "table-row"


def test_forms_are_grouped_by_container():
    inv = from_inventory(sample())
    by_container = {form["container"]: form for form in inv.forms}

    assert list(by_container) == ["form", "filter-bar"], "按固定容器顺序排, 不跟 dict 顺序走"
    assert by_container["filter-bar"]["fields"] == ["e1", "e6"]
    assert by_container["filter-bar"]["required"] == ["e1"]
    assert by_container["filter-bar"]["has_required"] is True
    assert by_container["filter-bar"]["field_count"] == 2
    assert by_container["form"]["fields"] == ["e5"]
    assert "table-row" not in by_container, "只有按钮的容器不是表单"


def test_signals_are_normalised_to_int():
    inv = from_inventory(sample(signals={"charts": 2, "junk": "x"}))

    assert inv.signals == {"charts": 2, "junk": 0}, "信号只留非负整数"
    assert from_inventory(sample()).signals == {}


def test_stats_are_complete_ints():
    inv = from_inventory(sample())
    stats = inv.stats

    assert all(isinstance(value, int) for value in stats.values()), "stats 的值必须是 int"
    assert stats["interactive_count"] == 6
    assert (stats["inputs"], stats["buttons"], stats["selects"]) == (2, 3, 1)
    assert (stats["tables"], stats["links"], stats["dialogs"], stats["forms"]) == (1, 1, 1, 2)
    assert stats["risky_write"] == 1 and stats["risky_destructive"] == 1
    assert stats["max_depth"] == 14, "深度取元素与表格里的最大值"
    assert stats["truncated"] == 0 and inv.is_truncated is False


# ---------------------------------------------------------------------------
# 3. 指纹: 该变就变, 不该变就不变
# ---------------------------------------------------------------------------

def test_fingerprint_ignores_element_order_and_eids():
    base = sample()
    # 无名图标按钮 (× 关闭) 在两边都要有: 只加在一边等于改了结构, 不是本条要测的东西
    base["buttons"].append({"eid": "e99", "kind": "button", "tag": "button", "text": ""})
    first = fingerprint_of(base)

    shuffled = copy.deepcopy(base)
    shuffled["buttons"] = list(reversed(shuffled["buttons"]))
    for index, button in enumerate(shuffled["buttons"]):
        button["eid"] = f"e{90 + index}"        # eid 重编号

    assert fingerprint_of(shuffled) == first, "结构没变, 指纹不该变"


def test_fingerprint_changes_when_action_label_changes():
    renamed = sample()
    renamed["buttons"][0]["text"] = "作废"

    assert fingerprint_of(renamed) != fingerprint_of(sample())


def test_fingerprint_ignores_data_volume():
    """列表页多 20 条数据不该让缓存失效"""
    more = sample()
    more["tables"][0]["row_count"] = 999
    more["tables"][0]["sample_rows"] = [["B001", "已完成", "编辑"]]

    assert fingerprint_of(more) == fingerprint_of(sample())


def test_fingerprint_changes_when_table_columns_change():
    wider = sample()
    wider["tables"][0]["columns"] = ["订单号", "状态", "金额", "操作"]

    assert fingerprint_of(wider) != fingerprint_of(sample())


def test_fingerprint_changes_when_dialog_buttons_change():
    other = sample(dialogs=[{"eid": "d1", "selector": ".el-dialog",
                             "text": "确认保存", "buttons": ["确定", "取消"]}])

    assert fingerprint_of(other) != fingerprint_of(sample())


def test_fingerprint_changes_when_charts_appear():
    """图表数量是页面结构的一部分: 同一路径多了图表, 就不该命中旧计划"""
    assert fingerprint_of(sample(signals={"charts": 1})) != fingerprint_of(sample())


def test_fingerprint_keys_on_path_not_query():
    same = sample(url="https://shop.example.com/order/list?page=7&size=50")
    other = sample(url="https://shop.example.com/order/detail?page=2")

    assert fingerprint_of(same) == fingerprint_of(sample()), "翻页/筛选参数不该换缓存键"
    assert fingerprint_of(other) != fingerprint_of(sample())


def test_fingerprint_is_prefixed_and_stable():
    inv = from_inventory(sample())

    assert inv.fingerprint == fingerprint_of(inv) == inv.cache_key
    assert len(inv.fingerprint) == 40, "sha1 hexdigest"
    canonical = canonical_dom(inv)
    assert FINGERPRINT_PREFIX == f"inventory/v{INVENTORY_VERSION}"
    assert canonical.startswith(FINGERPRINT_PREFIX)
    assert canonical_dom(inv) == canonical, "同一次结构必须给出逐字相同的规范串"


def test_fingerprint_changes_with_inventory_version(monkeypatch):
    """模型口径一变, 旧缓存必须整批失效 —— 版本在规范串里, 不在调用方手里"""
    before = fingerprint_of(sample())
    monkeypatch.setattr(inv_mod, "FINGERPRINT_PREFIX", "inventory/v99", raising=True)

    assert fingerprint_of(sample()) != before


# ---------------------------------------------------------------------------
# 4. 往返 / 冻结 / 降级
# ---------------------------------------------------------------------------

def test_to_dict_round_trips():
    first = from_inventory(sample())
    again = from_inventory(first.to_dict())

    assert again.fingerprint == first.fingerprint
    assert [e.eid for e in again.elements] == [e.eid for e in first.elements]
    assert [(e.name, e.primary, e.stability, e.risk) for e in again.elements] == \
           [(e.name, e.primary, e.stability, e.risk) for e in first.elements]
    assert again.stats == first.stats
    assert again.signals == first.signals
    assert again.forms == first.forms
    assert again.links == first.links
    assert json.loads(json.dumps(first.to_dict()))["fingerprint"] == first.fingerprint


def test_round_trip_keeps_truncated_flag():
    inv = from_inventory(sample(truncated=True))

    assert inv.is_truncated is True
    assert from_inventory(inv.to_dict()).is_truncated is True


def test_dataclasses_are_frozen():
    inv = from_inventory(sample())

    with pytest.raises(FrozenInstanceError):
        setattr(inv, "title", "改标题")          # 属性赋值故意违规: 验的就是它挡不挡
    with pytest.raises(FrozenInstanceError):
        setattr(inv.element("e1"), "risk", "safe")


def test_with_archetype_keeps_fingerprint():
    inv = from_inventory(sample())
    classified = inv.with_archetype("list")

    assert classified.archetype == "list"
    assert inv.archetype == "", "原对象不能被就地改"
    assert classified.fingerprint == inv.fingerprint, "原型是解读, 不是页面结构"


def test_archetype_can_be_seeded_from_payload_or_argument():
    assert from_inventory(sample(archetype="detail")).archetype == "detail"
    assert from_inventory(sample(), archetype="form").archetype == "form"
    assert from_inventory(from_inventory(sample()), archetype="dialog").archetype == "dialog"


def test_dirty_payloads_degrade_to_empty_inventory():
    for payload in (None, 42, "text", [1, 2, 3], {}, {"inputs": "not-a-list"},
                    {"elements": [{"eid": ""}, 7]}):
        inv = from_inventory(payload)

        assert isinstance(inv, PageInventory)
        assert inv.elements == () and inv.tables == () and inv.links == ()
        assert inv.stats["interactive_count"] == 0
        assert inv.stats["max_depth"] == 0
        assert len(inv.fingerprint) == 40, "空清单也要有指纹 (它是合法的'空页面')"


def test_empty_inventory_of_two_blank_pages_matches():
    assert fingerprint_of(None) == fingerprint_of({})


def test_notes_and_page_name_are_kept():
    inv = from_inventory(sample(page_name="order_list", notes=["超量截断"]))

    assert inv.page_name == "order_list"
    assert inv.notes == ("超量截断",)


# ---------------------------------------------------------------------------
# 5. 真浏览器: 采集 -> 定型
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def driver():
    from ai_kit.evaluation import driver_provider

    drv = driver_provider.get_driver()
    if drv is None:
        pytest.skip("本机拉不起无头 Edge/Chrome, 跳过真页面定型")
    yield drv
    driver_provider.quit_driver(drv)


@pytest.fixture(scope="module")
def open_page(driver):
    import os

    from ai_kit.evaluation import driver_provider

    fixtures = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "ai_kit", "evaluation", "golden", "fixtures")

    def _open(name):
        driver.get(driver_provider.to_file_url(os.path.join(fixtures, name)))
        return driver

    return _open


@pytest.fixture
def html_page(driver, tmp_path):
    from ai_kit.evaluation import driver_provider

    def _load(body: str):
        path = tmp_path / "page.html"
        path.write_text(
            "<!DOCTYPE html><html><head><meta charset='utf-8'><title>t</title></head>"
            f"<body>{body}</body></html>", encoding="utf-8")
        driver.get(driver_provider.to_file_url(str(path)))
        return driver

    return _load


def test_from_collect_on_real_page(open_page):
    inv = from_collect(open_page("el_message_box.html"), page_name="el_message_box")

    assert inv.page_name == "el_message_box"
    assert inv.elements, "真页面上必须定型出元素"
    assert all(element.eid for element in inv.elements)
    assert inv.stats["interactive_count"] == len(inv.elements)
    assert inv.stats["max_depth"] > 0, "深度由采集器给出, 不能是假的 0"
    assert len(inv.fingerprint) == 40
    assert inv.dialogs, "弹窗页夹具应该定型出弹窗"


def test_every_element_has_a_usable_locator(open_page):
    inv = from_collect(open_page("el_message_box.html"))

    for element in inv.elements:
        assert element.locators or element.selector == f'[data-ai-eid="{element.eid}"]'
        assert 0.0 <= element.stability <= 1.0


def test_fingerprint_is_stable_across_two_captures(open_page):
    first = from_collect(open_page("el_message_box.html"))
    second = from_collect(open_page("el_message_box.html"))

    assert first.fingerprint == second.fingerprint
    assert [e.eid for e in first.elements] == [e.eid for e in second.elements]


def test_table_from_real_page_keeps_row_actions_referenceable(html_page):
    inv = from_collect(html_page("""
      <div class="el-table">
        <div class="el-table__header-wrapper">
          <table class="el-table__header"><thead><tr><th>名称</th><th>状态</th></tr></thead></table>
        </div>
        <div class="el-table__body-wrapper">
          <table class="el-table__body"><tbody>
            <tr class="el-table__row"><td>记录A</td><td>正常</td>
              <td><button class="el-button">编辑</button><button class="el-button">删除</button></td></tr>
          </tbody></table>
        </div>
      </div>
    """))

    assert inv.tables, "夹具里就有表格"
    table = inv.tables[0]
    assert table["columns"] == ["名称", "状态"]
    assert table["row_count"] == 1
    assert [action["text"] for action in table["row_actions"]] == ["编辑", "删除"]
    for action in table["row_actions"]:
        assert action["eid"], "行内按钮必须带上 eid, 否则规划器指不到它"
        assert inv.lookup(action["eid"]) is not None
    assert inv.stats["risky_destructive"] == 1, "'删除' 命中风险词表"
