# -*- coding: utf-8 -*-
"""
采集器: JS 契约 + 归一化 + 真页面行为

分两半:
1. 纯 Python 半: 假 driver 校验"执行的是什么脚本 / 传了什么参数 / 脏数据怎么降级",
   不需要浏览器, 任何 CI 都能跑。
2. 真浏览器半: 拿 evaluation 的 golden fixtures 当活页面, 验证 JS 真能采出东西。
   拉不起无头 Edge/Chrome 就整组 skip —— 与 evaluation 套件同一口径。

真浏览器这半不可省: 采集的关键逻辑全在 JS 里, 只测 Python 侧等于没测
(历史上 DOM_SCRAPE_JS 就是因为"从没真跑过"而静默返回空页)。
"""
from __future__ import annotations

import json
import os

import pytest

from ai_kit.perception import collector
from ai_kit.perception.collector import (
    COLLECT_JS,
    COLLECTOR_VERSION,
    build_options,
    collect,
    empty_inventory,
    normalize,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "ai_kit", "evaluation", "golden", "fixtures")


class FakeDriver:
    """只实现采集用到的 execute_script, 用来钉住"传给 JS 的是什么" """

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.script = None
        self.options = None
        self.calls = 0

    def execute_script(self, script, *args):
        self.calls += 1
        self.script = script
        self.options = args[0] if args else None
        if self.error:
            raise RuntimeError(self.error)
        return self.result


# ---------------------------------------------------------------------------
# 1. JS 契约: 必须是一个带 return 的表达式
# ---------------------------------------------------------------------------

def test_script_starts_as_an_expression_not_a_comment():
    """selenium 把脚本当函数体执行。

    老 DOM_SCRAPE_JS 死在这两条上: 开头一行 `//` 注释让 `"return " + js` 退化成
    `return;` (ASI), 结尾又是 `})();` 语句形式 —— 于是永远返回 undefined,
    而 Python 侧的 `str -> dict -> 兜底` 判断连错都不报, 静默采出空页面。
    """
    head = COLLECT_JS
    assert head.startswith("(function"), "必须是表达式, 不能是语句形式的 IIFE"
    assert head == head.lstrip(), "常量不能带前导空白/换行: return 后面换行会被 ASI 吃成 return;"
    assert not head.startswith("//"), "开头不能是行注释, 否则 return 会被 ASI 吃掉"
    assert not COLLECT_JS.rstrip().endswith(";"), "结尾不能带分号, 要能直接跟在 return 后面"


def test_script_returns_json_string():
    """任务卡要求输出 JSON.stringify(inventory), 不是 HTML"""
    assert "return JSON.stringify(" in COLLECT_JS
    assert "outerHTML" in COLLECT_JS, "单元素仍需带截断后的 outerHTML 供 LLM 参考"


def test_collect_executes_return_prefixed_script():
    drv = FakeDriver(result="{}")
    collect(drv)

    assert drv.script == "return " + COLLECT_JS.strip()
    assert drv.calls == 1, "采集只该有 1 次 JS 往返"
    assert drv.options["version"] == COLLECTOR_VERSION


# ---------------------------------------------------------------------------
# 2. 规则包 -> JS 参数
# ---------------------------------------------------------------------------

def test_default_options_carry_builtin_keywords_and_hints():
    opts = build_options()

    assert "删除" in opts["riskKeywords"]["destructive"]
    assert "保存" in opts["riskKeywords"]["write"]
    assert "[role=dialog]" in opts["containerHints"]["dialog"]
    assert opts["markEid"] is True


def test_site_profile_words_are_tried_first():
    site = {
        "risk_keywords": {"destructive": ["注销账号"]},
        "container_hints": {"dialog": [".shop-drawer"]},
    }
    opts = build_options(site)

    assert opts["riskKeywords"]["destructive"][0] == "注销账号"
    assert "删除" in opts["riskKeywords"]["destructive"], "内置词不能被站点包挤掉"
    assert opts["containerHints"]["dialog"][0] == ".shop-drawer"


def test_site_pack_reaches_the_js_through_effective_rules():
    """端到端: 站点 JSON 里的词/选择器要真能出现在 arguments[0] 里"""
    opts = build_options("example_shop")

    assert "作废订单" in opts["riskKeywords"]["destructive"]
    assert ".shop-dialog" in opts["containerHints"]["dialog"]
    assert "[role=dialog]" in opts["containerHints"]["dialog"], "框架/通用提示应仍在"


def test_options_clamp_absurd_budgets():
    opts = build_options(max_elements=0, max_html=10)
    assert opts["maxElements"] >= 1
    assert opts["maxHtml"] >= 200


def test_missing_risk_keywords_warns_but_still_collects(monkeypatch):
    monkeypatch.setattr(collector, "effective_rules", lambda profile=None: {})
    with pytest.warns(RuntimeWarning, match="risk_keywords"):
        opts = build_options()

    assert opts["riskKeywords"] == {}
    assert opts["containerHints"] == {}


# ---------------------------------------------------------------------------
# 3. 归一化: 脏数据不进下游
# ---------------------------------------------------------------------------

SAMPLE_PAYLOAD = {
    "url": "file:///tmp/p.html",
    "title": "示例页",
    "ready_state": "complete",
    "viewport": {"width": 1280, "height": 800},
    "counts": {"discovered": 9, "interactive": 9, "inputs": 9, "buttons": 9},
    "inputs": [
        {"eid": "e1", "kind": "input", "tag": "input", "text": "",
         "attrs": {"name": "user", "value": "admin"},
         "locator": {"by": "css selector", "value": "[name=user]", "stability": 0.85,
                     "strategy": "name"},
         "container": "form", "actions": ["fill", "clear"],
         "constraints": {"required": True}},
        {"eid": "e1", "kind": "input", "tag": "input"},          # eid 重复
        {"kind": "input", "tag": "input"},                        # 没 eid
        "not-a-dict",                                             # 脏条目
    ],
    "buttons": [
        {"eid": "e2", "kind": "button", "tag": "button", "text": "保存",
         "locator": None,
         "alternatives": [{"by": "css selector", "value": "#save", "stability": 1.0,
                           "strategy": "id"}],
         "risk": "write"},
        {"eid": "e3", "kind": "button", "tag": "button", "text": "取消",
         "locator": {"by": "", "value": ""}},                     # 坏 locator
    ],
    "selects": [],
    "tables": [{
        "eid": "c1", "kind": "table", "tag": "table",
        "columns": ["名称", "状态"], "row_count": 3,
        "sample_rows": [["记录A", "正常"]],
        "row_actions": [{"text": "编辑", "count": 3,
                         "locator": {"by": "css selector", "value": "td .edit",
                                     "stability": 0.7}}],
    }],
    "dialogs": [{"eid": "d1", "selector": ".modal", "text": "确认删除", "buttons": ["确定"]}],
    "notes": ["一行说明"],
}


def test_elements_are_deduped_and_bad_entries_dropped():
    inv = normalize(SAMPLE_PAYLOAD)

    assert [e["eid"] for e in inv["inputs"]] == ["e1"], "重复/缺 eid/非 dict 的条目都要丢掉"
    assert [e["eid"] for e in inv["buttons"]] == ["e2", "e3"]


def test_input_value_is_never_kept():
    """attrs.value 是用户数据, 采集结果要进 LLM, 一律不留"""
    inv = normalize(SAMPLE_PAYLOAD)

    assert "value" not in inv["inputs"][0]["attrs"]
    assert "admin" not in json.dumps(inv, ensure_ascii=False)
    assert inv["inputs"][0]["attrs"]["name"] == "user", "其余属性要保留"


def test_locator_promoted_from_alternatives():
    inv = normalize(SAMPLE_PAYLOAD)
    save = [e for e in inv["buttons"] if e["eid"] == "e2"][0]

    assert save["locator"]["value"] == "#save", "locator 缺失时用第一个备选顶上"
    assert save["alternatives"] == []


def test_broken_locator_degrades_to_none():
    inv = normalize(SAMPLE_PAYLOAD)
    cancel = [e for e in inv["buttons"] if e["eid"] == "e3"][0]

    assert cancel["locator"] is None, "缺 by/value 的定位串不能进下游"


def test_counts_and_risk_summary_are_recomputed():
    """JS 传上来的 counts 是 9, 实际只有 1 个 input + 2 个 button —— 以列表为准"""
    inv = normalize(SAMPLE_PAYLOAD)

    assert inv["counts"]["inputs"] == 1
    assert inv["counts"]["buttons"] == 2
    assert inv["counts"]["tables"] == 1
    assert inv["counts"]["interactive"] == 3
    assert inv["risks"] == {"write": 1}
    assert inv["container_summary"] == {"form": 1, "page": 2}


def test_table_structure_is_kept():
    table = normalize(SAMPLE_PAYLOAD)["tables"][0]

    assert table["columns"] == ["名称", "状态"]
    assert table["row_count"] == 3
    assert table["sample_rows"] == [["记录A", "正常"]]
    assert table["row_actions"][0]["text"] == "编辑"
    assert table["row_actions"][0]["count"] == 3


def test_garbage_payloads_degrade_to_empty_inventory():
    for payload in (None, 42, "text", [1, 2, 3], {"inputs": "not-a-list"}):
        inv = normalize(payload)
        assert set(inv) == set(empty_inventory())
        assert inv["counts"]["buttons"] == 0
        assert inv["inputs"] == []


def test_no_driver_returns_empty_inventory():
    inv = collect(None)

    assert inv["notes"], "降级原因要写进 notes"
    assert inv["counts"]["interactive"] == 0


def test_js_failure_degrades_with_warning():
    with pytest.warns(RuntimeWarning, match="采集 JS 执行失败"):
        inv = collect(FakeDriver(error="page crashed"))

    assert inv["buttons"] == [] and "page crashed" in inv["notes"][0]


def test_bad_json_degrades_with_warning():
    with pytest.warns(RuntimeWarning, match="不是合法 JSON"):
        inv = collect(FakeDriver(result="{not json"))

    assert inv["inputs"] == []


def test_payload_as_dict_is_accepted():
    """有的 driver 会把 JSON 反序列化成 dict, 两种都要能吃"""
    inv = collect(FakeDriver(result=SAMPLE_PAYLOAD))
    assert inv["counts"]["inputs"] == 1


# ---------------------------------------------------------------------------
# 4. 真浏览器: fixtures 当活页面
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def driver():
    from ai_kit.evaluation import driver_provider

    drv = driver_provider.get_driver()
    if drv is None:
        pytest.skip("本机拉不起无头 Edge/Chrome, 跳过真页面采集")
    yield drv
    driver_provider.quit_driver(drv)


@pytest.fixture(scope="module")
def open_page(driver):
    from ai_kit.evaluation import driver_provider

    def _open(name):
        driver.get(driver_provider.to_file_url(os.path.join(FIXTURES, name)))
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


def test_collect_returns_non_empty_inventory(open_page):
    inv = collect(open_page("el_message_box.html"))

    assert inv["title"] == "el-message-box fixture"
    assert inv["url"].startswith("file://")
    assert inv["ready_state"] == "complete"
    assert inv["counts"]["buttons"] >= 3
    assert inv["counts"]["discovered"] > 0


def test_message_box_buttons_get_container_and_risk(open_page):
    inv = collect(open_page("el_message_box.html"))
    by_text = {b["text"]: b for b in inv["buttons"]}

    assert by_text["确定"]["container"] == "dialog"
    assert by_text["确定"]["risk"] == "write"
    assert by_text["取消"]["risk"] is None
    assert inv["container_summary"]["dialog"] >= 3
    assert inv["dialogs"], "可见 .el-message-box 必须被认成弹窗"


def test_dialog_is_reported_once_per_subtree(open_page):
    """`.el-dialog__wrapper` 和 `.el-dialog` 会同时命中同一棵子树, 不能报两次"""
    inv = collect(open_page("el_dialog.html"))
    assert len(inv["dialogs"]) == 1
    assert "编辑" in inv["dialogs"][0]["text"] or inv["dialogs"][0]["buttons"]


def test_table_columns_rows_and_row_actions(html_page):
    inv = collect(html_page("""
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

    assert len(inv["tables"]) == 1, "嵌套的 table.el-table__body 不能重复计数"
    table = inv["tables"][0]
    assert table["columns"] == ["名称", "状态"]
    assert table["row_count"] == 1
    assert table["sample_rows"][0][:2] == ["记录A", "正常"]
    assert [a["text"] for a in table["row_actions"]] == ["编辑", "删除"]
    assert table["row_actions"][0]["locator"]["value"]

    known = {e["eid"] for e in inv["buttons"] + inv["inputs"] + inv["selects"]}
    assert table["row_actions"][0]["eid"] in known, "行内按钮必须能被 eid 引用, 否则规划器用不了它"


def test_row_actions_fold_repeated_rows(html_page):
    rows = "".join(
        f'<tr><td>记录{i}</td><td><button class="el-button">编辑</button></td></tr>'
        for i in range(5))
    inv = collect(html_page(f"<table><tbody>{rows}</tbody></table>"))

    assert inv["counts"]["folded_rows"] == 3, "5 行同一种按钮, 留 2 个折叠 3 个"
    action = inv["tables"][0]["row_actions"][0]
    assert action["count"] == 5
    known = {e["eid"] for e in inv["buttons"]}
    assert action["eid"] in known, "代表元素要留在清单里, eid 才能被引用"


def test_risk_levels_from_profile_keywords(html_page):
    inv = collect(html_page(
        "<button>删除</button><button>保存</button><button>取消</button>"
        "<button aria-label='remove row'></button>"))
    risk = {b["text"]: b["risk"] for b in inv["buttons"]} if inv["buttons"] else {}

    assert risk.get("删除") == "destructive"
    assert risk.get("保存") == "write"
    assert risk.get("取消") is None
    assert [b["risk"] for b in inv["buttons"] if b["aria"].get("label") == "remove row"] \
        == ["destructive"], "aria-label 也算文本"


def test_container_attribution(html_page):
    inv = collect(html_page("""
      <div class="toolbar"><button>批量删除</button></div>
      <div class="filter-bar"><input placeholder="搜索"><button>查询</button></div>
      <form class="el-form"><input name="a"></form>
      <table><tbody><tr><td><button>编辑</button></td></tr></tbody></table>
      <div role="dialog"><button>确定</button></div>
    """))
    where = {}
    for el in inv["inputs"] + inv["buttons"]:
        where[el["text"] or el["attrs"].get("placeholder") or el["name"]] = el["container"]

    assert where["批量删除"] == "toolbar"
    assert where["搜索"] == "filter-bar"
    assert where["查询"] == "filter-bar"
    assert where["a"] == "form"
    assert where["编辑"] == "table-row"
    assert where["确定"] == "dialog"


def test_locator_strategy_order_and_stability(html_page):
    inv = collect(html_page(
        '<input data-testid="user-input" id="uid" name="user" aria-label="用户名"'
        ' class="el-input__inner">'
        "<button>保存</button>"))
    field = inv["inputs"][0]
    strategies = [field["locator"]["strategy"]] + [a["strategy"] for a in field["alternatives"]]

    assert strategies[0] == "data-testid"
    assert field["locator"]["stability"] == 1.0
    assert strategies[:5] == ["data-testid", "id", "aria-label", "name", "semantic-css"]
    assert "relative-xpath" in strategies

    button = [b for b in inv["buttons"] if b["text"] == "保存"][0]
    assert button["locator"]["strategy"] == "relative-xpath"
    assert button["locator"]["stability"] == 0.4
    assert "text" in [a["strategy"] for a in button["alternatives"]]


def test_dynamic_id_is_demoted(html_page):
    inv = collect(html_page('<input id="el-id-1234-5">'))
    locator = inv["inputs"][0]["locator"]

    assert locator["strategy"] == "id(疑似动态)"
    assert locator["stability"] < 1.0, "框架生成的 id 每次渲染都变, 不能当最稳的定位串"


def test_form_constraints_are_captured(html_page):
    inv = collect(html_page(
        '<form><input name="code" required maxlength="8" pattern="\\d{8}" placeholder="编号">'
        '<input name="ro" readonly><select name="s"><option>甲</option><option>乙</option></select>'
        "</form>"))
    by_name = {e["name"]: e for e in inv["inputs"]}

    assert by_name["code"]["constraints"]["required"] is True
    assert by_name["code"]["constraints"]["maxlength"] == 8
    assert by_name["code"]["constraints"]["pattern"] == "\\d{8}"
    assert by_name["code"]["actions"] == ["fill", "clear"]
    assert by_name["ro"]["constraints"]["readonly"] is True

    select = inv["selects"][0]
    assert select["name"] == "s"
    assert select["constraints"]["type"] == "select"


def test_select_options_are_listed(html_page):
    inv = collect(html_page(
        '<select name="city"><option>北京</option><option>上海</option></select>'))
    assert inv["selects"][0]["options"] == ["北京", "上海"]
    assert inv["selects"][0]["actions"] == ["select"]


def test_element_html_is_truncated(html_page):
    long_text = "x" * 5000
    inv = collect(html_page(f"<button>{long_text}</button>"), max_html=300)
    html = inv["buttons"][0]["html"]

    assert len(html) < 500, "单元素 outerHTML 必须截断"
    assert html.endswith("...[TRUNCATED]")


def test_input_value_does_not_leak_through_outer_html(open_page):
    """form_error.html 里有一个 <input value="admin">, 这是用户数据"""
    inv = collect(open_page("form_error.html"))

    assert inv["inputs"], "夹具里至少有一个输入框"
    assert "admin" not in json.dumps(inv, ensure_ascii=False)


def test_eids_are_resolvable_back_to_elements(open_page, driver):
    inv = collect(open_page("el_message_box.html"))

    for el in (inv["buttons"] + inv["inputs"])[:6]:
        found = driver.find_element("css selector", f'[data-ai-eid="{el["eid"]}"]')
        assert found.tag_name == el["tag"]


def test_second_run_reuses_the_same_eids(open_page):
    first = collect(open_page("el_message_box.html"))
    second = collect(open_page("el_message_box.html"))

    assert [b["eid"] for b in first["buttons"]] == [b["eid"] for b in second["buttons"]]


def test_mark_eid_can_be_disabled(open_page, driver):
    collect(open_page("el_table.html"), mark_eid=False)

    assert driver.find_elements("css selector", "[data-ai-eid]") == [], \
        "mark_eid=False 时不能往页面里写任何东西"


def test_over_budget_collection_is_grouped(html_page):
    buttons = "".join(f"<button>按钮{i}</button>" for i in range(40))
    inv = collect(html_page(f"<div>{buttons}</div>"), max_elements=10)

    assert inv["truncated"] is True
    assert len(inv["buttons"]) == 10
    assert inv["groups"][0]["container"] == "page"
    assert inv["groups"][0]["count"] == 40
    assert len(inv["groups"][0]["samples"]) == 3
    assert any("超过上限" in n for n in inv["notes"])


def test_chart_signal_is_counted_once(html_page):
    """图表不进元素清单 (canvas 不可交互), 但要给原型分类留个信号; 嵌套只算一次"""
    inv = collect(html_page(
        '<div class="chart-box"><canvas width="300" height="150"></canvas></div>'
        '<canvas width="100" height="100"></canvas>'))

    assert inv["signals"]["charts"] == 2, '.chart-box > canvas 是同一张图, 不能数两次'


def test_plain_page_has_no_chart_signal(html_page):
    assert collect(html_page("<button>保存</button>"))["signals"]["charts"] == 0


def test_dom_depth_is_recorded(html_page):
    """清单里唯一能反映页面复杂度的原始信号; 定型层拿它算 stats.max_depth"""
    inv = collect(html_page(
        '<button>浅</button><div><div><div><button>深</button></div></div></div>'))
    depth = {b["text"]: b["depth"] for b in inv["buttons"]}

    assert depth["深"] > depth["浅"]
    assert depth["浅"] >= 1, "body 的子节点必须在 1 层以上"


def test_invisible_content_is_ignored(html_page):
    inv = collect(html_page("""
      <template><button>模板里的</button></template>
      <button style="display:none">display-none</button>
      <button style="visibility:hidden">visibility-hidden</button>
      <div hidden><button>父级隐藏</button></div>
      <button>看得见</button>
    """))

    assert [b["text"] for b in inv["buttons"]] == ["看得见"]
