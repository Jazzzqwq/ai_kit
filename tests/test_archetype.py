# -*- coding: utf-8 -*-
"""
原型分类: 五条规则 + 顺序 + 置信度 + LLM 兜底

本文件的重点不是"能分出五类", 而是三件容易做错的事:
1. **顺序**: 先中先算。dashboard 必须排在 detail 前面, 否则它永远不中 (见 dashboard 那组)。
2. **置信度**: 规则命中就该 >= 0.6, 否则每页都白烧一次 LLM; 只有 unknown 才该去问。
3. **兜底不抛**: LLM 胡说 / 抛异常 / 返回 None, 都必须退回规则结论。
真浏览器那半用活页面验证"字段确实接得上"(特别是 input[type=password] 和图表信号)。
"""
from __future__ import annotations

import os

import pytest

from ai_kit.perception.archetype import (
    ARCHETYPES,
    LLM_CONFIDENCE_THRESHOLD,
    annotate,
    classify,
    confirm_with_llm,
)
from ai_kit.perception.inventory import fingerprint_of, from_inventory

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ai_kit", "evaluation", "golden", "fixtures")


# ---------------------------------------------------------------------------
# 造数据 (就是 collector 的 dict 形状)
# ---------------------------------------------------------------------------

def field(eid="e1", *, input_type="text", required=False, container="form", text="", name=""):
    return {"eid": eid, "kind": "input", "tag": "input", "type": input_type,
            "attrs": {"type": input_type, "name": eid}, "text": text, "name": name,
            "container": container,
            "constraints": {"type": input_type, "required": required}}


def button(eid="e2", text="按钮", *, container="page", risk=None, href=""):
    attrs = {"href": href} if href else {}
    return {"eid": eid, "kind": "button", "tag": "a" if href else "button", "text": text,
            "attrs": attrs, "container": container, "risk": risk}


def table(eid="c1", *, row_actions=(), columns=("名称", "状态")):
    return {"eid": eid, "kind": "table", "tag": "table", "columns": list(columns),
            "row_count": 3, "sample_rows": [["记录A", "正常"]],
            "row_actions": [{"text": text} for text in row_actions]}


def page(*, inputs=(), buttons=(), selects=(), tables=(), charts=0, title="t"):
    return {"url": "https://shop.example.com/order/list", "title": title,
            "signals": {"charts": charts},
            "inputs": list(inputs), "buttons": list(buttons),
            "selects": list(selects), "tables": list(tables)}


class Spy:
    """假 LLM: 记下提示词, 按剧本作答 (异常/None/字符串/字典都支持)"""

    def __init__(self, answer=None):
        self.answer = answer
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# ---------------------------------------------------------------------------
# 1. login
# ---------------------------------------------------------------------------

def test_login_needs_password_and_login_button():
    result = classify(page(inputs=[field(input_type="password")],
                           buttons=[button(text="登录")]))

    assert result.archetype == "login"
    assert result.confidence >= 0.9
    assert result.source == "rule"
    assert any("password" in item for item in result.evidence)
    assert any("login-button" in item for item in result.evidence)


@pytest.mark.parametrize("label", ["登 录", "Sign In", "SIGNIN", "登录系统"])
def test_login_label_is_whitespace_and_case_insensitive(label):
    result = classify(page(inputs=[field(input_type="password")],
                           buttons=[button(text=label)]))

    assert result.archetype == "login"


def test_password_without_login_button_is_not_login():
    """改密码页也有密码框, 但没有登录按钮就不该判成登录页"""
    result = classify(page(inputs=[field(input_type="password")],
                           buttons=[button(text="保存")]))

    assert result.archetype != "login"
    assert "login" not in result.scores


def test_login_wins_over_form_even_though_both_hit():
    result = classify(page(inputs=[field(input_type="password"), field("e9")],
                           buttons=[button(text="登录")]))

    assert result.archetype == "login", "先中先算: login 在 form 之前"
    assert result.scores["form"] == 0.8, "没中的也要记分, 方便排查近失"
    assert result.scores["login"] > result.scores["form"]


# ---------------------------------------------------------------------------
# 2. list_crud
# ---------------------------------------------------------------------------

def test_list_crud_needs_table_and_two_toolbar_buttons():
    result = classify(page(tables=[table()],
                           buttons=[button("e2", "新增", container="toolbar"),
                                    button("e3", "导出", container="toolbar")]))

    assert result.archetype == "list_crud"
    assert result.confidence == 0.75
    assert any("toolbar-actions:2" in item for item in result.evidence)


def test_list_crud_is_more_confident_when_rows_have_actions():
    result = classify(page(tables=[table(row_actions=("编辑", "删除"))],
                           buttons=[button("e2", "新增", container="toolbar"),
                                    button("e3", "导出", container="filter-bar")]))

    assert result.archetype == "list_crud"
    assert result.confidence == 0.85, "找到行内按钮 = 真的能增删改, 置信度拉高"
    assert any("row-actions:2" in item for item in result.evidence)


def test_list_crud_needs_two_buttons():
    result = classify(page(tables=[table()],
                           buttons=[button("e2", "新增", container="toolbar")]))

    assert result.archetype != "list_crud", "操作区只有一个按钮, 证据不够"


def test_buttons_outside_action_containers_do_not_count():
    result = classify(page(tables=[table()],
                           buttons=[button("e2", "新增", container="page"),
                                    button("e3", "导出", container="page")]))

    assert result.archetype != "list_crud"


# ---------------------------------------------------------------------------
# 3. form
# ---------------------------------------------------------------------------

def test_form_container_is_enough():
    result = classify(page(inputs=[field(container="form")],
                           buttons=[button(text="保存", container="form")]))

    assert result.archetype == "form"
    assert result.confidence == 0.8


def test_two_required_fields_without_form_tag_and_without_table():
    """组件库常自己渲染出 div 而不是 form, 靠必填字段数兜住"""
    result = classify(page(inputs=[field("e1", required=True, container="page"),
                                   field("e2", required=True, container="page")],
                           buttons=[button(text="提交")]))

    assert result.archetype == "form"
    assert result.confidence == 0.72
    assert any("required-inputs:2" in item for item in result.evidence)


def test_two_required_fields_with_table_is_not_form():
    result = classify(page(inputs=[field("e1", required=True, container="page"),
                                   field("e2", required=True, container="page")],
                           tables=[table()]))

    assert result.archetype != "form", "有表格时那多半是列表页的筛选区"


# ---------------------------------------------------------------------------
# 4. dashboard / 5. detail
# ---------------------------------------------------------------------------

def test_dashboard_from_charts_and_few_scattered_elements():
    result = classify(page(charts=1,
                           inputs=[field(container="filter-bar")],
                           buttons=[button("e2", "导出", container="toolbar"),
                                    button("e3", "刷新", container="toolbar")]))

    assert result.archetype == "dashboard"
    assert result.confidence == 0.75
    assert any("charts:1" in item for item in result.evidence)


def test_dashboard_beats_detail():
    """卡片把 dashboard 排在 detail 后, 但 detail 的条件是它的超集 —— 顺序必须调过来"""
    payload = page(charts=2, inputs=[field(container="filter-bar")])
    result = classify(payload)

    assert result.archetype == "dashboard", "只有一个筛选框的看板不能被判成详情页"
    assert "detail" in result.scores


def test_dashboard_needs_few_interactive_elements():
    many = [button(f"e{i}", f"按钮{i}", container="toolbar")
            for i in range(1, 20)]
    result = classify(page(charts=1, buttons=many))

    assert result.archetype != "dashboard", "元素一多就是操作台, 不是看板"


def test_dashboard_needs_spread_unless_there_is_barely_anything():
    clustered = [button(f"e{i}", f"按钮{i}", container="page") for i in range(1, 6)]
    result = classify(page(charts=1, buttons=clustered))

    assert result.archetype != "dashboard", "5 个按钮挤在一个容器里, 不算分散"

    almost_empty = classify(page(charts=1, buttons=[button("e1", "导出", container="page")]))
    assert almost_empty.archetype == "dashboard", "只有图和一个按钮时无从不分散, 不该否决"


def test_detail_from_few_inputs_and_no_destructive():
    result = classify(page(inputs=[field(container="page")],
                           buttons=[button("e2", "返回"), button("e3", "打印")]))

    assert result.archetype == "detail"
    assert result.confidence == 0.65
    assert "no-destructive" in result.evidence


def test_destructive_action_blocks_detail():
    result = classify(page(inputs=[field(container="page")],
                           buttons=[button("e2", "删除", risk="destructive")]))

    assert result.archetype != "detail"


def test_two_inputs_block_detail():
    result = classify(page(inputs=[field("e1", container="page"),
                                   field("e2", container="page")],
                           buttons=[button("e3", "返回")]))

    assert result.archetype != "detail"


def test_table_only_page_is_detail():
    """只读列表 (有表格没有按钮) 也是展示页, 不该掉进 unknown"""
    result = classify(page(tables=[table()]))

    assert result.archetype == "detail"


# ---------------------------------------------------------------------------
# 6. unknown / 置信度契约
# ---------------------------------------------------------------------------

def test_blank_page_is_unknown_and_asks_for_llm():
    result = classify(page())

    assert result.archetype == "unknown"
    assert result.confidence == 0.0
    assert result.rule == "no-match"
    assert result.needs_llm() is True
    assert any("elements:0" in item for item in result.evidence)


@pytest.mark.parametrize("payload", [
    page(inputs=[field(input_type="password")], buttons=[button(text="登录")]),
    page(tables=[table(row_actions=("编辑",))],
         buttons=[button("e2", "新增", container="toolbar"),
                  button("e3", "导出", container="toolbar")]),
    page(inputs=[field(container="form")]),
    page(charts=1, inputs=[field(container="filter-bar")]),
    page(inputs=[field(container="page")]),
])
def test_rule_hits_are_confident_enough_to_skip_llm(payload):
    """规则命中却低于阈值 = 每页都白烧一次 LLM; 这条钉住所有规则的置信度下限"""
    result = classify(payload)

    assert result.archetype != "unknown"
    assert result.confidence >= LLM_CONFIDENCE_THRESHOLD
    assert result.needs_llm() is False


def test_scores_only_contain_hits():
    result = classify(page(tables=[table()]))

    assert set(result.scores) == {"detail"}, "没命中的规则不进 scores"


def test_classify_accepts_inventory_or_dict():
    payload = page(tables=[table()])

    assert classify(payload).archetype == classify(from_inventory(payload)).archetype


def test_result_is_frozen_and_jsonable():
    result = classify(page(tables=[table()]))

    with pytest.raises(Exception):
        setattr(result, "archetype", "login")
    assert result.to_dict()["archetype"] == "detail"


def test_archetype_names_are_the_published_set():
    assert ARCHETYPES == ("login", "list_crud", "form", "detail", "dashboard", "unknown")


# ---------------------------------------------------------------------------
# 7. LLM 兜底 (默认关闭, 低置信度才开)
# ---------------------------------------------------------------------------

def test_llm_is_not_called_by_default():
    spy = Spy({"archetype": "form"})
    result = classify(page(), ask=spy)

    assert result.archetype == "unknown"
    assert spy.calls == []


def test_llm_is_not_called_when_rules_are_confident():
    spy = Spy({"archetype": "login"})
    result = classify(page(tables=[table()]), allow_llm=True, ask=spy)

    assert result.archetype == "detail"
    assert spy.calls == [], "规则已经 >= 0.6, 没必要花钱"


def test_llm_can_resolve_unknown():
    spy = Spy({"archetype": "form", "confidence": 0.8, "reason": "两个必填校验块"})
    result = classify(page(), allow_llm=True, ask=spy)

    assert len(spy.calls) == 1
    assert result.archetype == "form"
    assert result.confidence == 0.8
    assert result.source == "llm"
    assert any(item.startswith("llm:") for item in result.evidence)


def test_llm_answer_as_json_string_is_accepted():
    spy = Spy('{"archetype": "dashboard", "confidence": 0.7}')
    result = classify(page(), allow_llm=True, ask=spy)

    assert result.archetype == "dashboard"


@pytest.mark.parametrize("answer", [
    {"archetype": "上帝视角"},
    {"archetype": ""},
    {"nope": 1},
    "not json at all",
    None,
    42,
])
def test_bad_llm_answers_fall_back_to_rules(answer):
    result = classify(page(), allow_llm=True, ask=Spy(answer))

    assert result.archetype == "unknown"
    assert result.source == "rule"


def test_llm_exception_never_escapes():
    result = classify(page(), allow_llm=True, ask=Spy(RuntimeError("boom")))

    assert result.archetype == "unknown"
    assert result.source == "rule"


def test_llm_confidence_is_clamped_and_defaulted():
    high = classify(page(), allow_llm=True,
                    ask=Spy({"archetype": "detail", "confidence": 9}))
    missing = classify(page(), allow_llm=True, ask=Spy({"archetype": "detail"}))

    assert high.confidence == 1.0
    assert missing.confidence == LLM_CONFIDENCE_THRESHOLD


def test_prompt_carries_evidence_for_the_model():
    spy = Spy({"archetype": "detail"})
    # 两个输入 + 一个删除键: detail 不中 (输入多了/有 destructive), 又没有表格图表 -> unknown
    classify(page(inputs=[field("e1", container="filter-bar"), field("e2", container="page")],
                  buttons=[button("e3", "删除", risk="destructive")]),
             allow_llm=True, ask=spy)

    prompt = spy.calls[0]
    assert "e1" in prompt, "要让模型看到具体元素, 不能只给一句'帮我分类'"
    assert "detail" in prompt and "unknown" in prompt, "候选原型要列全"
    assert "只输出 JSON" in prompt


def test_prompt_carries_no_pii_because_it_goes_to_the_llm():
    """标题和元素名都可能带着被测页面的真数据: 出境前必须抹掉, 骨架不能抹"""
    spy = Spy({"archetype": "detail"})
    classify(page(title="患者详情 13812345678",
                  inputs=[field("e1", container="filter-bar", name="姓名：张三")],
                  buttons=[button("e3", "删除", risk="destructive")]),
             allow_llm=True, ask=spy)

    prompt = spy.calls[0]
    for probe in ("13812345678", "张三"):
        assert probe not in prompt, f"分类提示词里还在泄 PII: {probe}"
    assert "e1" in prompt, "eid 是模型回话时对元素的唯一指认, 不能抹"
    assert "filter-bar" in prompt and "risk=destructive" in prompt


def test_confirm_with_llm_keeps_rule_scores():
    base = classify(page())
    confirmed = confirm_with_llm(base, page(), ask=Spy({"archetype": "list_crud", "confidence": 0.7}))

    assert confirmed.archetype == "list_crud"
    assert confirmed.scores == base.scores


# ---------------------------------------------------------------------------
# 8. annotate: 回填但不改指纹
# ---------------------------------------------------------------------------

def test_annotate_backfills_without_touching_fingerprint():
    inv = from_inventory(page(tables=[table()]))
    annotated = annotate(inv)

    assert annotated.archetype == "detail"
    assert inv.archetype == "", "原对象不能被就地改"
    assert annotated.fingerprint == inv.fingerprint


def test_annotate_writes_unknown_explicitly():
    inv = annotate(from_inventory(page()))

    assert inv.archetype == "unknown", "空着和'明确不知道'不是一回事"
    assert inv.page_name == ""


def test_annotate_accepts_a_ready_result():
    payload = page(tables=[table()])
    inv = annotate(payload, classify(payload))

    assert inv.archetype == "detail"
    assert inv.fingerprint == fingerprint_of(payload)


# ---------------------------------------------------------------------------
# 9. 真浏览器: 活页面上规则接得上吗
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def driver():
    from ai_kit.evaluation import driver_provider

    drv = driver_provider.get_driver()
    if drv is None:
        pytest.skip("本机拉不起无头 Edge/Chrome, 跳过真页面分类")
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


def test_real_login_page(html_page):
    from ai_kit.perception import from_collect

    inv = from_collect(html_page("""
      <form class="el-form">
        <input class="el-input__inner" type="password" name="pwd" placeholder="密码">
        <button class="el-button el-button--primary">登 录</button>
      </form>
    """))
    result = classify(inv)

    assert result.archetype == "login"
    assert result.confidence >= 0.9


def test_real_crud_list_page(html_page):
    from ai_kit.perception import from_collect

    inv = from_collect(html_page("""
      <div class="toolbar"><button>新增</button><button>导出</button></div>
      <table><thead><tr><th>名称</th><th>状态</th></tr></thead><tbody>
        <tr><td>记录A</td><td>正常</td>
            <td><button>编辑</button><button>删除</button></td></tr>
      </tbody></table>
    """))
    result = classify(inv)

    assert result.archetype == "list_crud"
    assert result.confidence == 0.85, "行内按钮采到了, 置信度该拉高"


def test_real_form_page(open_page):
    from ai_kit.perception import from_collect

    result = classify(from_collect(open_page("form_error.html")))

    assert result.archetype == "form"


def test_real_dashboard_page(html_page):
    from ai_kit.perception import from_collect

    inv = from_collect(html_page("""
      <div class="filter-bar"><input placeholder="时间范围"></div>
      <div class="chart-box"><canvas width="300" height="150"></canvas></div>
      <div class="toolbar"><button>导出</button></div>
    """))
    result = classify(inv)

    assert inv.signals["charts"] >= 1, "canvas 必须被算成图表信号"
    assert result.archetype == "dashboard"


def test_real_readonly_table_page(open_page):
    from ai_kit.perception import from_collect

    result = classify(from_collect(open_page("el_table.html")))

    assert result.archetype == "detail", "只有表格没有操作区, 是只读展示页"
