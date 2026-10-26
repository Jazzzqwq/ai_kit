# -*- coding: utf-8 -*-
"""
5.8 运行层: loc() 地址簿 + page 原语 (显式等待 / 失败截图 / 归因)

这一层的价值不在"能点到", 而在**失败时说得清**:
1. **loc()** 是 eid 与页面之间的地址簿。表没装、eid 不在表里, 都要给出「表里有几条、
   表从哪来」, 否则报错就是一句 NoSuchElement, 人得回头翻 locators.json。
2. **显式等待** 每个原语都等元素的真实状态 (visible / clickable / present), 不用
   sleep 赌时间; 找不到时按 alternatives 依次试, 采集器给的多策略要用上。
3. **失败三件套** 动作 + eid + 截图, 外加 proposal.diagnoser 的归因 —— 截图和归因
   都是附加证据, 它们自己失败不能把原始失败吃掉。
4. **ActionFailure 是 AssertionError** pytest 里就是一条干净的失败, 不是异常堆栈。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from ai_kit.runtime import (
    ActionFailure,
    Locator,
    LocatorNotKnown,
    Page,
    RuntimeNotBound,
    loc,
    locators,
    page,
    use_locators,
)
from test_planner import driver, html_page  # noqa: F401 — driver / html_page 是 fixture

# 一张「该有的都有」的页面: 输入框 / 原生 select / checkbox / 按钮 / 表格 / 弹窗。
# 只有表格与弹窗是纯前端可验证的, 所以断言都围绕它们 —— 这条用例不测后端。
TABLE_HTML = """
<div class="bar">
  <input name="kw">
  <select name="st">
    <option value="">全部</option>
    <option value="on">正常</option>
  </select>
  <input type="checkbox" id="cb">
  <button id="search">查询</button>
</div>
<table>
  <thead><tr><th>名称</th><th>数量</th></tr></thead>
  <tbody>
    <tr><td>甲</td><td>3</td></tr>
    <tr><td>乙</td><td>1</td></tr>
  </tbody>
</table>
<button id="del">删除</button>
<div class="el-dialog" id="dlg" style="display:none">
  <p>确定删除？</p>
  <button id="ok">确定</button><button id="no">取消</button>
</div>
<div id="msg"></div>
<script>
  document.getElementById('search').onclick = function () {
    document.getElementById('msg').textContent = '已查询';
  };
  document.getElementById('del').onclick = function () {
    document.getElementById('dlg').style.display = 'block';
  };
  document.getElementById('ok').onclick = function () {
    document.getElementById('dlg').style.display = 'none';
  };
  document.getElementById('no').onclick = function () {
    document.getElementById('dlg').style.display = 'none';
  };
</script>
"""

# eid -> 定位串: 平表形状, 与 locators.json 的 "locators" 段等价
TABLE_LOCATORS = {
    "e1": {"by": "css selector", "value": "[name=kw]", "label": "关键字"},
    "e2": {"by": "css selector", "value": "select[name=st]", "label": "状态"},
    "e3": {"by": "css selector", "value": "#cb", "label": "启用"},
    "e4": {"by": "css selector", "value": "#search", "label": "查询"},
    "e5": {"by": "css selector", "value": "table", "label": "列表"},
    "e5.column:名称": {"by": "xpath", "value": '(//table)[1]//th[normalize-space()="名称"]',
                     "kind": "column", "label": "名称", "table": "e5"},
    "e5.column:数量": {"by": "xpath", "value": '(//table)[1]//th[normalize-space()="数量"]',
                     "kind": "column", "label": "数量", "table": "e5"},
    "e6": {"by": "css selector", "value": "#del", "label": "删除"},
    "d1": {"by": "css selector", "value": ".el-dialog", "kind": "dialog", "label": "确认"},
}


@pytest.fixture
def live(driver, html_page, tmp_path, monkeypatch):
    """装载定位表 + 绑定 driver + 把截图赶到 tmp_path (不往仓库里丢 png)"""
    monkeypatch.setenv("AI_KIT_ARTIFACTS", str(tmp_path / "artifacts"))
    monkeypatch.setenv("AI_KIT_WAIT", "2")
    use_locators(TABLE_LOCATORS)
    drv = html_page(TABLE_HTML)
    page.bind(drv)
    try:
        yield drv
    finally:
        page.unbind()
        use_locators(None)


# ---------------------------------------------------------------------------
# 1. locators 表: 装载 / 查询 / 报错要说得清
# ---------------------------------------------------------------------------

def test_use_locators_accepts_a_flat_table():
    table = use_locators({"e1": {"by": "css selector", "value": "#a"}})

    assert table["e1"].value == "#a"
    assert loc("e1").by == "css selector"
    use_locators(None)


def test_use_locators_accepts_the_whole_renderer_document():
    """build_locators 的产物是带 "locators" 键的整份文档, 不该要求调用方先拆开"""
    use_locators({"version": 1, "locators": {"e1": {"by": "css selector", "value": "#a"}}})

    assert loc("e1").value == "#a"
    use_locators(None)


def test_use_locators_reads_a_json_file_and_remembers_where_it_came_from(tmp_path):
    path = tmp_path / "locators.json"
    path.write_text('{"locators": {"e1": {"by": "css selector", "value": "#a"}}}',
                    encoding="utf-8")

    use_locators(path)

    assert loc("e1").value == "#a"
    assert locators()["e1"].kind == "element"
    use_locators(None)


def test_use_locators_explains_a_missing_file(tmp_path):
    missing = tmp_path / "nope.json"

    with pytest.raises(LocatorNotKnown) as excinfo:
        use_locators(missing)

    assert "locators 文件不存在" in str(excinfo.value)
    assert "AI_KIT_LOCATORS" in str(excinfo.value)


def test_loc_hint_says_how_many_entries_the_table_has():
    use_locators({"e1": {"by": "css selector", "value": "#a"}})

    with pytest.raises(LocatorNotKnown) as excinfo:
        loc("e99")

    assert "表里有 1 条" in str(excinfo.value)
    assert "同一份元素清单" in str(excinfo.value)
    use_locators(None)


def test_loc_rejects_an_empty_eid():
    with pytest.raises(LocatorNotKnown):
        loc("")


def test_page_is_a_locator_without_any_table():
    """整页动作 (assert_no_error_toast) 不该要求 locators.json 里有一条 page"""
    use_locators(None)

    assert loc("page").value == "html"


def test_pairs_put_the_primary_first_and_keep_the_alternatives():
    wanted = Locator("e1", "css selector", "#a",
                     (("xpath", "//div[@id='a']"), ("id", "a")))

    assert wanted.pairs() == (("css selector", "#a"), ("xpath", "//div[@id='a']"),
                              ("id", "a"))


def test_a_locator_without_a_value_contributes_nothing():
    """脏记录 (没有定位串) 不该被当成「找得到」"""
    assert Locator("e1").pairs() == ()


# ---------------------------------------------------------------------------
# 2. 绑定: 没 bind 就调原语, 要报「怎么绑」而不是 AttributeError
# ---------------------------------------------------------------------------

def test_primitives_refuse_to_run_without_a_driver():
    fresh = Page()

    with pytest.raises(RuntimeNotBound) as excinfo:
        fresh.click(Locator("e1", "css selector", "#a"))

    assert "page.bind(driver)" in str(excinfo.value)


def test_bind_and_unbind_are_idempotent(driver):
    fresh = Page()
    fresh.bind(driver).bind(driver)
    assert fresh.bound

    fresh.unbind()
    fresh.unbind()
    assert not fresh.bound


# ---------------------------------------------------------------------------
# 3. 元素动作: 每条都走显式等待, 不靠 sleep 赌时间
# ---------------------------------------------------------------------------

def test_fill_then_read_back(live):
    page.fill(loc("e1"), "关键字")
    page.assert_value(loc("e1"), "关键字")

    page.clear(loc("e1"))
    page.assert_value(loc("e1"), "")


def test_click_changes_the_dom_and_feedback_notices(live):
    page.click(loc("e4"))
    page.assert_no_error_toast()
    page.assert_feedback()


def test_select_option_uses_the_visible_text(live):
    page.select_option(loc("e2"), "正常")

    assert page.get_value(loc("e2")) == "on"


def test_check_and_uncheck(live):
    page.check(loc("e3"))
    assert live.find_element("css selector", "#cb").is_selected()

    page.uncheck(loc("e3"))
    assert not live.find_element("css selector", "#cb").is_selected()


def test_assert_visible_and_hidden(live):
    page.assert_visible(loc("e5"))
    page.assert_hidden(loc("d1"))


def test_press_key_reaches_the_element(live):
    """Enter 在搜索框上不该报错 (页面没有监听, 但按键必须真的送到元素上)"""
    page.fill(loc("e1"), "甲")
    page.press_key(loc("e1"), "enter")

    page.assert_value(loc("e1"), "甲")


# ---------------------------------------------------------------------------
# 4. 表格: 行数 / 列定位 / 排序
# ---------------------------------------------------------------------------

def test_row_count_reads_the_data_rows_not_the_header(live):
    assert page.row_count(loc("e5")) == 2


def test_assert_row_count_and_its_bounds(live):
    page.assert_row_count(loc("e5"), 2)
    page.assert_row_count_lte(loc("e5"), 3)
    page.assert_row_count_gte(loc("e5"), 1)


def test_column_locator_is_derived_from_the_table_anchor(live):
    """c1.column:X 在清单里没有独立定位串, 靠「表格锚点 + 文案」派生 —— 要真找得到"""
    page.assert_sorted(loc("e5.column:数量"), "desc")


def test_assert_sorted_says_what_the_column_actually_holds(live):
    with pytest.raises(ActionFailure) as excinfo:
        page.assert_sorted(loc("e5.column:数量"), "asc")

    assert "没有按 asc 排序" in str(excinfo.value)
    assert "'3', '1'" in str(excinfo.value)


def test_rows_match_checks_every_row(live):
    """甲 只在第一行: 1/2 行不含 —— 逐行检查才报得出这个比例"""
    with pytest.raises(ActionFailure) as excinfo:
        page.assert_rows_match(loc("e5"), "甲")

    assert "1/2 行不含" in str(excinfo.value)
    assert "乙 1" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5. 弹窗: 按文案点, 点完要真的关掉
# ---------------------------------------------------------------------------

def test_confirm_dialog_closes_the_dialog(live):
    page.click(loc("e6"))
    page.assert_visible(loc("d1"))

    page.confirm_dialog()
    page.assert_hidden(loc("d1"))


def test_cancel_dialog_accepts_an_explicit_label(live):
    page.click(loc("e6"))

    page.cancel_dialog("取消")
    page.assert_hidden(loc("d1"))


def test_confirm_without_a_dialog_fails_with_a_reason(live):
    with pytest.raises(ActionFailure) as excinfo:
        page.confirm_dialog()

    assert "没有可见弹窗" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 6. 失败上下文: 动作 / eid / 截图 / 归因
# ---------------------------------------------------------------------------

def test_failure_carries_action_eid_and_a_real_screenshot(live, tmp_path):
    with pytest.raises(ActionFailure) as excinfo:
        page.assert_text(loc("e5"), "不存在的文案")

    failure = excinfo.value
    assert failure.action == "assert_text"
    assert failure.eid == "e5"
    assert failure.screenshot, "失败必须留截图, 否则报告里只有一句话"
    assert (tmp_path / "artifacts") in Path(failure.screenshot).parents
    assert "e5 (css selector=table)" in str(failure)
    assert isinstance(failure, AssertionError), "pytest 里要是一条失败, 不是异常堆栈"


def test_diagnoser_answer_lands_in_the_failure(live, monkeypatch):
    """proposal.diagnoser 还没落地: 这里塞一个假的, 证明钩子是活的"""
    fake = types.ModuleType("ai_kit.proposal.diagnoser")

    def diagnose(context):
        assert context["action"] == "click"
        assert context["eid"] == "e9"
        return "按钮被遮罩层挡住"

    fake.diagnose = diagnose
    monkeypatch.setitem(sys.modules, "ai_kit.proposal.diagnoser", fake)

    with pytest.raises(ActionFailure) as excinfo:
        page.click(Locator("e9", "css selector", "#nope"))

    assert excinfo.value.diagnosis == "按钮被遮罩层挡住"
    assert "归因: 按钮被遮罩层挡住" in str(excinfo.value)


def test_a_broken_diagnoser_does_not_eat_the_failure(live, monkeypatch):
    fake = types.ModuleType("ai_kit.proposal.diagnoser")

    def diagnose(context):
        raise RuntimeError("归因自己挂了")

    fake.diagnose = diagnose
    monkeypatch.setitem(sys.modules, "ai_kit.proposal.diagnoser", fake)

    with pytest.raises(ActionFailure) as excinfo:
        page.assert_text(loc("e5"), "不存在的文案")

    assert "diagnoser 调用失败" in excinfo.value.diagnosis
    assert "assert_text 失败" in str(excinfo.value)


def test_the_primary_locator_can_be_wrong_and_the_alternative_still_works(live):
    """采集器给了多策略定位: 第一条过期了, 第二条要顶上"""
    use_locators({"e1": {"by": "css selector", "value": "#gone",
                         "alternatives": [["css selector", "[name=kw]"]]}})

    page.fill(loc("e1"), "备选")
    page.assert_value(loc("e1"), "备选")
