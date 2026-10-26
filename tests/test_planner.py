# -*- coding: utf-8 -*-
"""
用例计划: schema 忠实 / ref 可解析 / 校验闭环 / 不编数据

重点不是"能生成 43 条", 而是四件容易做错的事:
1. **schema 忠实**: to_dict 只出卡片 5.6 的键; steps 里只有 action/eid/value,
   绝不出现 find_element/定位串 —— 那是 ⑥ 的事, 混进来就等于把自愈锁死在字符串里。
2. **ref 可解析**: 每个 eid 都能在清单里落地 (元素/列/行内按钮/图表/页), 列标题
   不假装是可交互元素。
3. **校验闭环**: 写错的步骤要报出来 (未知动作 / eid 编的 / fill 打到按钮 / value 类型错),
   但用例不能因此消失。
4. **不编数据**: @maxlength 算得出就填定长串, 算不出就留 token + 记 unresolved;
   页面没有导出按钮就跳过并说明, 不造一条永远绿的假用例。
真浏览器那半验证"合成清单上成立"的结论在真采集结果上也成立。
"""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from ai_kit.perception.archetype import ARCHETYPES
from ai_kit.perception.inventory import from_inventory
from ai_kit.perception.planner import (
    ACTIONS,
    CASE_PREFIX,
    DEMO_PASSWORD,
    DEMO_USERNAME,
    INVALID_FALLBACK,
    MARKER_PREFIX,
    P0,
    P1,
    P2,
    P3,
    PLANNER_VERSION,
    PRIORITIES,
    RESULT_CAP,
    TAG_ORDER,
    PlanCase,
    Step,
    build_cases,
    plan,
    plan_cache_key,
    plan_with_llm,
    resolve_ref,
    resolve_value,
    validate_case,
    validate_step,
)
from ai_kit.perception.strategies import (
    DESTRUCTIVE,
    SAFE,
    SLOW,
    SQLI_PAYLOAD,
    STRATEGY_VERSION,
    WRITE,
    XSS_PAYLOAD,
    expand,
)


# ---------------------------------------------------------------------------
# 造数据 (collector 的 dict 形状, 与 ④ 的测试同一套口径)
# ---------------------------------------------------------------------------

def field(eid="f1", *, container="form", text="", input_type="text",
          required=False, **extra):
    constraints = {"type": input_type, "required": required}
    constraints.update(extra)
    return {"eid": eid, "kind": "input", "tag": "input", "type": input_type,
            "text": text, "name": text, "container": container,
            "constraints": constraints}


def checkbox(eid="k1", *, container="table-row"):
    return {"eid": eid, "kind": "input", "tag": "input", "type": "checkbox",
            "container": container, "constraints": {"type": "checkbox"}}


def select(eid="s1", *, container="form", text="城市", options=("北京", "上海")):
    return {"eid": eid, "kind": "select", "tag": "select", "type": "", "text": text,
            "name": text, "container": container, "options": list(options),
            "constraints": {"type": "select"}}


def button(eid="b1", text="按钮", *, container="page", risk=None):
    return {"eid": eid, "kind": "button", "tag": "button", "text": text,
            "name": text, "container": container, "risk": risk}


def table(eid="c1", *, columns=("名称", "创建时间"), row_actions=(), row_count=3):
    actions = [dict(action) if isinstance(action, dict) else {"text": action}
               for action in row_actions]
    return {"eid": eid, "kind": "table", "tag": "table", "columns": list(columns),
            "row_count": row_count, "sample_rows": [["记录A", "2024-01-01"]],
            "row_actions": actions}


def dialog(eid="d1", *, text="确定要删除这条记录吗?", buttons=("确定", "取消")):
    return {"eid": eid, "selector": f"#{eid}", "text": text, "buttons": list(buttons)}


def page(*, inputs=(), buttons=(), selects=(), tables=(), charts=0, dialogs=(),
         url="https://shop.example.com/order/list", title="订单列表"):
    return {"url": url, "title": title, "signals": {"charts": charts},
            "inputs": list(inputs), "buttons": list(buttons),
            "selects": list(selects), "tables": list(tables),
            "dialogs": list(dialogs)}


def list_payload(**overrides):
    payload = page(
        inputs=[field("e1", container="filter-bar", text="关键字", maxlength=32),
                checkbox("e2")],
        buttons=[button("e3", "查询", container="filter-bar"),
                 button("e4", "重置", container="filter-bar", risk=DESTRUCTIVE),
                 button("e5", "新增", container="toolbar", risk=WRITE),
                 button("e6", "批量删除", container="toolbar", risk=DESTRUCTIVE),
                 button("e7", "编辑", container="table-row", risk=WRITE),
                 button("e8", "删除", container="table-row", risk=DESTRUCTIVE)],
        tables=[table(row_actions=[{"text": "编辑", "eid": "e7"},
                                   {"text": "删除", "eid": "e8"}])],
        url="https://shop.example.com/patient/list", title="患者列表",
    )
    payload.update(overrides)
    return payload


def form_payload():
    return page(
        inputs=[field("f1", text="用户名", required=True, maxlength=10),
                field("f2", text="邮箱", input_type="email", required=True)],
        selects=[select("f3", text="城市")],
        buttons=[button("f4", "保存", container="form", risk=WRITE),
                 button("f5", "取消", container="form"),
                 button("f6", "重置", container="form", risk=DESTRUCTIVE)],
        url="https://shop.example.com/user/new", title="新建用户",
    )


def login_payload():
    return page(
        inputs=[field("u1", text="用户名", container="page"),
                field("p1", text="密码", input_type="password", container="page")],
        buttons=[button("b1", "登录")],
        url="https://shop.example.com/login", title="登录",
    )


def case_of(target_plan, *parts):
    """按 cid 片段找用例 (case_id 会随用例增删变, cid 才是稳定身份)"""
    for case in target_plan:
        if all(part in case.cid for part in parts):
            return case
    raise AssertionError(f"计划里没有 cid 含 {parts} 的用例: "
                         f"{[case.cid for case in target_plan]}")


# ---------------------------------------------------------------------------
# 1. 动作词表: 计划的词汇表本身别写错
# ---------------------------------------------------------------------------

def test_action_catalog_is_keyed_by_name():
    assert ACTIONS
    assert all(name == action.name for name, action in ACTIONS.items())


def test_action_metadata_is_in_range():
    for action in ACTIONS.values():
        assert action.eid in ("req", "opt", "none"), action.name
        assert action.value in ("req", "opt", ""), action.name
        assert set(action.on) <= {"element", "table", "column", "row-action",
                                  "chart", "dialog"}, action.name
        assert action.kind in ("", "text", "int", "page", "direction", "key",
                               "url", "ms", "label", "relation"), action.name


def test_every_action_that_needs_an_eid_says_where_it_lands():
    """eid 必填却不声明 on -> 校验时挡不住『把 fill 打在按钮上』这类错"""
    leaked = [action.name for action in ACTIONS.values()
              if action.eid == "req" and not action.on]

    assert leaked == []


def test_catalog_covers_the_card_example():
    """卡片 5.6 的例子逐字可用 (sqli 那条用例全靠这四个动作)"""
    for name in ("fill", "click", "assert_no_error_toast", "assert_row_count_lte"):
        assert name in ACTIONS, name


def test_step_serializes_only_action_eid_value():
    assert Step("fill", "e12", "' OR 1=1 --").to_dict() == {
        "action": "fill", "eid": "e12", "value": "' OR 1=1 --"}


def test_step_omits_empty_eid_and_value():
    assert Step("assert_no_error_toast").to_dict() == {"action": "assert_no_error_toast"}
    assert Step("reload").to_dict() == {"action": "reload"}


def test_step_describe_is_human_readable():
    assert "@e12" in Step("fill", "e12", "x").describe()
    assert "fill" in Step("fill", "e12", "x").describe()


# ---------------------------------------------------------------------------
# 2. ref 解析: eid 是清单引用, 不是 DOM id
# ---------------------------------------------------------------------------

def test_ref_resolves_to_an_element():
    ref = resolve_ref(from_inventory(list_payload()), "e1")

    assert (ref.kind, ref.eid) == ("element", "e1")
    assert ref.text == "关键字"


def test_ref_resolves_to_a_table():
    ref = resolve_ref(from_inventory(list_payload()), "c1")

    assert (ref.kind, ref.eid) == ("table", "c1")


def test_ref_resolves_to_a_column_by_visible_name():
    ref = resolve_ref(from_inventory(list_payload()), "c1.column:名称")

    assert (ref.kind, ref.table, ref.column) == ("column", "c1", "名称")


def test_unknown_column_is_none_not_a_guess():
    assert resolve_ref(from_inventory(list_payload()), "c1.column:不存在的列") is None


def test_ref_resolves_to_a_row_action_by_label():
    ref = resolve_ref(from_inventory(list_payload()), "c1.row:删除")

    assert (ref.kind, ref.table, ref.text) == ("row-action", "c1", "删除")


def test_unknown_row_action_is_none():
    assert resolve_ref(from_inventory(list_payload()), "c1.row:归档") is None


def test_ref_resolves_to_a_chart_index():
    inventory = from_inventory(page(charts=2))

    assert resolve_ref(inventory, "chart:1").kind == "chart"
    assert resolve_ref(inventory, "chart:2").index == 2
    assert resolve_ref(inventory, "chart:3") is None, "清单里只有 2 个图表"


def test_ref_resolves_to_a_dialog():
    inventory = from_inventory(page(dialogs=[dialog("d1")]))

    assert resolve_ref(inventory, "d1").kind == "dialog"


def test_page_ref_and_empty_ref():
    inventory = from_inventory(list_payload())

    assert resolve_ref(inventory, "page").kind == "page"
    assert resolve_ref(inventory, "") is None
    assert resolve_ref(inventory, "不存在的eid") is None


# ---------------------------------------------------------------------------
# 3. 校验: 写错的步骤要报出来
# ---------------------------------------------------------------------------

def test_valid_step_has_no_problem():
    inventory = from_inventory(list_payload())

    assert validate_step(inventory, Step("fill", "e1", "abc")) == ""
    assert validate_step(inventory, Step("click", "e3")) == ""
    assert validate_step(inventory, Step("assert_no_error_toast")) == ""


def test_unknown_action_is_a_problem():
    problem = validate_step(from_inventory(list_payload()),
                            Step("send_keys", "e1", "abc"))

    assert "未知动作" in problem


def test_missing_eid_is_a_problem():
    assert "缺少 eid" in validate_step(from_inventory(list_payload()), Step("click"))


def test_page_level_action_must_not_carry_an_eid():
    problem = validate_step(from_inventory(list_payload()), Step("reload", "e1"))

    assert "页级动作" in problem


def test_invented_eid_is_a_problem():
    problem = validate_step(from_inventory(list_payload()),
                            Step("click", "e999"))

    assert "不在元素清单里" in problem


def test_fill_on_a_button_is_a_problem():
    problem = validate_step(from_inventory(list_payload()),
                            Step("fill", "e3", "abc"))

    assert "只能打在输入控件上" in problem


def test_select_on_a_text_box_is_a_problem():
    problem = validate_step(from_inventory(list_payload()),
                            Step("select", "e1", "北京"))

    assert "select 只能打在下拉框上" in problem


def test_check_on_a_text_box_is_a_problem():
    problem = validate_step(from_inventory(list_payload()),
                            Step("check", "e1"))

    assert "checkbox/radio" in problem


def test_value_type_is_checked():
    inventory = from_inventory(list_payload())

    assert "整数" in validate_step(inventory, Step("assert_row_count_lte", "c1", "十条"))
    assert "asc/desc" in validate_step(inventory, Step("sort", "c1.column:名称", "up"))
    assert "detail/list" in validate_step(inventory, Step("assert_consistent", value="pdf"))
    assert "缺少 value" in validate_step(inventory, Step("paginate", "c1"))


def test_case_reports_step_numbers():
    inventory = from_inventory(list_payload())
    case = PlanCase(case_id="LC-900", title="坏的", steps=(
        Step("click", "e999"), Step("fill", "e3", "abc")))

    problems = validate_case(inventory, case)

    assert len(problems) == 2
    assert problems[0].startswith("步骤 1 (click)")
    assert problems[1].startswith("步骤 2 (fill)")


def test_case_reports_bad_priority_and_empty_steps():
    problems = validate_case(from_inventory(list_payload()),
                             PlanCase(case_id="LC-901", title="t", priority="P9"))

    assert any("优先级" in problem for problem in problems)
    assert any("没有步骤" in problem for problem in problems)


# ---------------------------------------------------------------------------
# 4. 取值: 算得出就算, 算不出就留 token
# ---------------------------------------------------------------------------

def test_maxlength_builds_an_exact_length_string():
    inventory = from_inventory(list_payload())

    value, ok = resolve_value("@maxlength", inventory.element("e1"), {"length": 32})

    assert ok is True
    assert value == "测" * 32


def test_maxlength_on_a_number_box_uses_digits():
    inventory = from_inventory(page(inputs=[field("n1", input_type="number")]))

    assert resolve_value("@maxlength", inventory.element("n1"), {"length": 3}) == ("999", True)


def test_maxlength_without_length_is_unresolved():
    assert resolve_value("@maxlength", None, {}) == ("@maxlength", False)


def test_invalid_format_uses_the_control_type():
    inventory = from_inventory(form_payload())

    assert resolve_value("@invalid_format", inventory.element("f2"))[0] == "not-an-email"


def test_invalid_format_falls_back_when_only_pattern_is_known():
    """只给了 pattern 的文本框: 给一串绝不合法的东西, 但别假装知道格式"""
    inventory = from_inventory(page(inputs=[field("p1", pattern="[A-Z]{3}")]))

    assert resolve_value("@invalid_format", inventory.element("p1")) == \
        (INVALID_FALLBACK, True)


def test_invalid_format_without_any_signal_is_unresolved():
    """既没类型也没 pattern: 连『错格式』长什么样都不知道, 不许编"""
    assert resolve_value("@invalid_format", None) == ("@invalid_format", False)


def test_boundary_value_comes_from_seed_data():
    assert resolve_value("@max", None, {"number": 5}) == (5, True)


def test_existing_value_is_kept_verbatim():
    """@existing 要真去库里捞; 编一个假名字比留 token 更坏"""
    assert resolve_value("@existing", None) == ("@existing", False)


def test_plain_value_is_not_a_token():
    assert resolve_value("abc", None) == ("abc", True)


# ---------------------------------------------------------------------------
# 5. 计划 schema: 就是卡片 5.6
# ---------------------------------------------------------------------------

def test_plan_top_level_keys_match_the_card():
    payload = plan(list_payload()).to_dict()

    assert list(payload) == ["page", "archetype", "profile", "model",
                             "prompt_version", "generated_at", "cases"]


def test_case_keys_match_the_card_plus_two():
    payload = plan(list_payload()).to_dict()["cases"][0]

    assert list(payload) == ["case_id", "title", "priority", "tags", "preconditions",
                             "test_data", "steps", "cleanup", "cid", "expect"]


def test_every_step_has_only_allowed_keys():
    for case in plan(list_payload()):
        for step in (*case.steps, *case.cleanup):
            assert set(step.to_dict()) <= {"action", "eid", "value"}, step


def test_plan_is_json_serializable():
    payload = plan(form_payload(), profile="medical_web", model="qwen-plus")

    text = payload.to_json()
    assert json.loads(text) == payload.to_dict()


def test_page_is_a_path_not_a_full_url():
    """域名换个环境不该换计划"""
    assert plan(list_payload()).page == "/patient/list"
    assert plan(list_payload(), page="/custom").page == "/custom"


def test_prompt_version_and_generated_at():
    assert PLANNER_VERSION == "planner.v3"
    assert plan(list_payload(), generated_at="2026-01-01T00:00:00+00:00").generated_at \
        == "2026-01-01T00:00:00+00:00"
    assert plan(list_payload()).generated_at


def test_profile_and_model_are_passed_through():
    payload = plan(list_payload(), profile="medical_web", model="qwen-plus")

    assert (payload.profile, payload.model) == ("medical_web", "qwen-plus")
    assert payload.prompt_version == PLANNER_VERSION


# ---------------------------------------------------------------------------
# 6. 用例编号: 稳定身份与可读编号分开
# ---------------------------------------------------------------------------

def test_case_id_prefixes_follow_the_archetype():
    assert plan(list_payload()).cases[0].case_id.startswith("LC-")
    assert plan(form_payload()).cases[0].case_id.startswith("FM-")
    assert plan(login_payload()).cases[0].case_id.startswith("LG-")


def test_case_id_prefix_table_covers_every_archetype():
    assert set(ARCHETYPES) <= set(CASE_PREFIX)


def test_case_ids_are_unique_and_sequential():
    cases = plan(list_payload()).cases
    ids = [case.case_id for case in cases]

    assert len(ids) == len(set(ids))
    assert ids == [f"LC-{index:03d}" for index in range(1, len(ids) + 1)]


def test_titles_start_with_the_page_name():
    for case in plan(list_payload()):
        assert case.title.startswith("患者列表-"), case.title
        assert " · " not in case.title, "标题里的分隔符会影响用例管理系统解析"


def test_no_seed_is_dropped_silently():
    """④ 的每条种子要么进计划, 要么进 skipped —— 不许凭空消失"""
    payload = list_payload()
    seeds = expand(payload)
    target_plan = plan(payload)
    skipped = {line.split(": ", 1)[0] for line in target_plan.diagnostics["skipped"]}

    assert [case.cid for case in target_plan] == \
           [seed.cid for seed in seeds if seed.cid not in skipped]


def test_case_expect_comes_from_the_seed():
    seeds = {seed.cid: seed.expect for seed in expand(list_payload())}

    for case in plan(list_payload()):
        assert case.expect == seeds[case.cid], case.cid


# ---------------------------------------------------------------------------
# 7. 优先级 / 标签 / 前置条件
# ---------------------------------------------------------------------------

def test_priorities_are_in_range():
    for payload in (list_payload(), form_payload(), login_payload()):
        for case in plan(payload):
            assert case.priority in PRIORITIES, case


def test_destructive_cases_are_always_p0():
    for case in plan(list_payload()):
        if case.is_destructive:
            assert case.priority == P0, case.cid


def test_priority_follows_the_strategy_level():
    target_plan = plan(list_payload())

    assert case_of(target_plan, "sort.null").priority == P2, "SHOULD -> P2"
    assert case_of(target_plan, "state.perf").priority == P3, "SLOW -> P3"
    assert case_of(target_plan, "query.filter", ".valid").priority == P1, "MUST -> P1"


def test_read_only_variant_is_not_treated_as_destructive():
    """『没权限时删除入口不可用』只断言不点, 不该背 P0 和备份前置条件"""
    case = case_of(plan(list_payload()), "row_actions", "删除", "no_permission")

    assert case.priority == P2
    assert "destructive" not in case.tags
    assert "permission" in case.tags
    assert not case.cleanup


def test_tags_start_with_the_group_and_keep_a_stable_order():
    for case in plan(list_payload()):
        assert case.tags, case.cid
        assert len(case.tags) == len(set(case.tags)), case.cid
        rest = case.tags[1:]
        assert list(rest) == [tag for tag in TAG_ORDER if tag in rest], case.cid


def test_sqli_and_xss_are_tagged_security():
    target_plan = plan(list_payload())

    assert case_of(target_plan, "query.filter", "sqli").tags == \
           ("query", "negative", "security")
    assert case_of(target_plan, "security.text_input", "script").tags == \
           ("security", "negative")


def test_write_and_performance_tags():
    target_plan = plan(list_payload())

    assert "write" in case_of(target_plan, "row_actions", "编辑", "normal").tags
    assert "performance" in case_of(target_plan, "state.perf").tags


def test_preconditions_start_from_the_archetype_base():
    target_plan = plan(list_payload())

    assert case_of(target_plan, "query.filter", "sqli").preconditions[:3] == \
           ("已登录", "拥有该模块的查看权限", "列表中至少有 1 条测试数据")
    assert plan(login_payload()).cases[0].preconditions[:2] == \
           ("未登录", "已准备一组可用的测试账号")


def test_preconditions_are_deduplicated():
    for case in plan(list_payload()):
        assert len(case.preconditions) == len(set(case.preconditions)), case.cid


def test_preconditions_add_the_risky_bits():
    target_plan = plan(list_payload())
    delete = case_of(target_plan, "batch.delete", "confirm")
    readonly = case_of(target_plan, "row_actions", "删除", "no_permission")

    assert "已备份或可回滚的测试数据" in delete.preconditions
    assert "使用只读账号登录" in readonly.preconditions
    assert "已备份或可回滚的测试数据" not in readonly.preconditions


def test_slow_network_case_declares_the_ability_to_simulate_it():
    case = case_of(plan(list_payload()), "state.loading")

    assert "可以模拟弱网/断网" in case.preconditions


# ---------------------------------------------------------------------------
# 8. test_data: 记真填下去的值, 不记模板内部键
# ---------------------------------------------------------------------------

def test_test_data_records_the_fields_actually_filled():
    target_plan = plan(login_payload())
    case = case_of(target_plan, "credential.submit", "success")

    assert case.test_data == {"用户名": DEMO_USERNAME, "密码": DEMO_PASSWORD}


def test_test_data_records_clearing_too():
    case = case_of(plan(login_payload()), "credential.negative", "empty_both")

    assert case.test_data == {"用户名": "", "密码": ""}


def test_test_data_never_leaks_template_internals():
    for payload in (list_payload(), form_payload(), login_payload()):
        for case in plan(payload):
            assert not set(case.test_data) & {"value", "type", "token",
                                              "length", "filter"}, case.cid


def test_test_data_has_no_duplicate_meaning():
    """seed 里的 filter 与真正填下去的字段值相同时, 只留字段名 (『筛选值』太含混)"""
    case = case_of(plan(list_payload()), "query.count")

    assert list(case.test_data) == ["关键字"]


def test_test_data_shows_the_marker_used_for_persistence():
    case = case_of(plan(form_payload()), "persist.after_save")

    marker = case.test_data["用户名"]
    assert marker.startswith(MARKER_PREFIX)
    assert any(step.value == marker for step in case.steps if step.action == "assert_persisted")


# ---------------------------------------------------------------------------
# 9. 结构化步骤: 语义对得上, 且不含代码
# ---------------------------------------------------------------------------

def test_every_case_has_steps_and_at_least_one_assertion():
    for payload in (list_payload(), form_payload(), login_payload()):
        for case in plan(payload):
            assert case.steps, case.cid
            assert any(ACTIONS[step.action].asserts for step in case.steps), case.cid


def test_sqli_case_looks_like_the_card_example():
    case = case_of(plan(list_payload()), "query.filter", "sqli")

    assert [step.to_dict() for step in case.steps] == [
        {"action": "fill", "eid": "e1", "value": SQLI_PAYLOAD},
        {"action": "click", "eid": "e3"},
        {"action": "assert_no_error_toast"},
        {"action": "assert_row_count_lte", "eid": "c1", "value": RESULT_CAP},
    ]
    assert case.test_data == {"关键字": SQLI_PAYLOAD}


def test_inline_delete_opens_the_dialog_and_stops():
    """行内删除只验到『确认弹窗出现』, 不替用户真删"""
    case = case_of(plan(list_payload()), "row_actions", "删除", "normal")

    assert case.actions() == ("click", "assert_feedback", "cancel_dialog")
    assert case.steps[-1].value == "取消"


def test_batch_delete_confirm_and_cancel_differ():
    target_plan = plan(list_payload())
    confirm = case_of(target_plan, "batch.delete", "confirm")
    cancel = case_of(target_plan, "batch.delete", "cancel")

    assert confirm.actions() == ("check", "click", "confirm_dialog",
                                 "assert_no_error_toast", "assert_row_count_lte")
    assert cancel.actions() == ("check", "click", "cancel_dialog", "assert_row_count_gte")
    assert cancel.steps[-1].value == 3, "取消后行数不该变"


def test_dialog_labels_come_from_the_inventory():
    payload = list_payload(dialogs=[dialog("d1", buttons=("是, 删除", "再想想"))])

    confirm = case_of(plan(payload), "batch.delete", "confirm")
    cancel = case_of(plan(payload), "batch.delete", "cancel")

    assert confirm.steps[2].value == "是, 删除"
    assert cancel.steps[2].value == "再想想"


def test_invert_unchecks_after_checking():
    """反选覆盖『取消选中』; 两次 check 等于没测取消"""
    case = case_of(plan(list_payload()), "batch.select", "invert")

    assert case.actions() == ("check", "assert_selection_count_gte",
                              "uncheck", "assert_selection_count")
    assert case.steps[-1].value == 0


def test_selection_counts_are_scoped_to_the_table():
    case = case_of(plan(list_payload()), "batch.select", "select_all")

    assert case.steps[-1].eid == "c1"


def test_login_button_is_clicked_even_without_a_form():
    """④ 的 submit 别名只认『保存』, 纯 div 壳里的『登录』按钮要靠登录词兜底

    enter_key 变体是故意的例外: 它用回车提交, 不点按钮。
    """
    for case in plan(login_payload()):
        if "credential.submit" not in case.cid:
            continue
        if any(step.action == "press" for step in case.steps):
            continue
        assert "click" in case.actions(), case.cid


def test_columns_are_referenced_not_faked():
    case = case_of(plan(list_payload()), "sort.column", "asc")
    inventory = from_inventory(list_payload())

    for step in case.steps:
        if step.eid:
            assert resolve_ref(inventory, step.eid) is not None, step


def test_every_step_eid_resolves_on_the_inventory():
    for payload in (list_payload(), form_payload(), login_payload()):
        inventory = from_inventory(payload)
        for case in plan(payload):
            for step in (*case.steps, *case.cleanup):
                if step.eid:
                    assert resolve_ref(inventory, step.eid) is not None, \
                        f"{case.cid}: {step.eid}"


def test_steps_never_contain_code_or_locators():
    forbidden = ("find_element(", "By.", "css=", "xpath", "//input", "send_keys")

    for payload in (list_payload(), form_payload(), login_payload()):
        for case in plan(payload):
            for step in (*case.steps, *case.cleanup):
                blob = json.dumps(step.to_dict(), ensure_ascii=False)
                assert not any(token in blob for token in forbidden), f"{case.cid}: {blob}"


def test_xss_payload_is_not_stripped():
    case = case_of(plan(list_payload()), "query.filter", "xss")

    assert case.steps[0].value == XSS_PAYLOAD
    assert "assert_no_alert" in case.actions()


# ---------------------------------------------------------------------------
# 10. 诊断: 少测了要说, 但 diagnostic 不进 JSON
# ---------------------------------------------------------------------------

def test_missing_export_is_reported_not_faked():
    target_plan = plan(list_payload())

    assert any("导出" in line for line in target_plan.diagnostics["skipped"])
    assert all("export" not in case.cid for case in target_plan)


def test_missing_remember_me_and_deep_link_are_reported():
    skipped = plan(login_payload()).diagnostics["skipped"]

    assert any("记住我" in line for line in skipped)
    assert any("深链" in line for line in skipped)


def test_existing_token_is_reported_as_unresolved():
    unresolved = plan(form_payload()).diagnostics["unresolved"]

    assert any("@existing" in line for line in unresolved)
    assert any("@invalid_format" in line for line in unresolved)


def test_missing_table_is_reported_and_skips_table_steps():
    """没有表格就少测, 但不许产出 eid 为空的 paginate"""
    payload = page(inputs=[field("e1", container="filter-bar", text="关键字")],
                   buttons=[button("e3", "查询", container="filter-bar")],
                   url="https://shop.example.com/patient/list", title="患者列表")
    target_plan = plan(payload, archetype="list_crud")

    assert any("没有表格" in line for line in target_plan.diagnostics["missing"])
    for case in target_plan:
        assert all(step.eid for step in case.steps if step.action == "paginate"), case.cid


def test_clean_synthetic_pages_have_no_problems():
    for payload in (list_payload(), form_payload(), login_payload()):
        target_plan = plan(payload)
        assert target_plan.problems == (), target_plan.report()
        assert target_plan.is_valid


def test_diagnostics_do_not_leak_into_the_json():
    target_plan = plan(list_payload())
    payload = target_plan.to_dict()

    assert "diagnostics" not in payload
    assert all("skipped" not in case for case in payload["cases"])
    assert "跳过" in target_plan.report()


def test_report_counts_priorities():
    target_plan = plan(list_payload())
    counts = target_plan.by_priority()

    assert sum(counts.values()) == target_plan.total
    for name in PRIORITIES:
        assert f"{name} {counts[name]}" in target_plan.report()


# ---------------------------------------------------------------------------
# 11. 缓存键 / 参数开关
# ---------------------------------------------------------------------------

def test_cache_key_carries_all_three_versions():
    key = plan_cache_key(from_inventory(list_payload()))

    assert f"strategy=v{STRATEGY_VERSION}" in key
    assert f"planner={PLANNER_VERSION}" in key
    assert "list_crud" in key


def test_cache_key_uses_the_same_archetype_as_the_plan():
    """键里不能写 unknown 而计划按 list_crud 生成, 否则缓存会把两份当成一份"""
    inventory = from_inventory(list_payload())

    assert inventory.archetype == "", "采集结果还没分类"
    assert plan_cache_key(inventory).endswith("|list_crud")
    assert plan_cache_key(inventory, "form").endswith("|form")


def test_cache_key_is_stable_for_the_same_inventory():
    inventory = from_inventory(list_payload())

    assert plan_cache_key(inventory) == plan_cache_key(inventory)


def test_cache_key_changes_when_the_page_changes():
    before = plan_cache_key(from_inventory(list_payload()))
    after = plan_cache_key(from_inventory(page(
        inputs=[field("e1", container="filter-bar", text="关键字", maxlength=64)],
        url="https://shop.example.com/patient/list", title="患者列表")))

    assert before != after


def test_limit_trims_and_says_so():
    target_plan = plan(list_payload(), limit=8)

    assert target_plan.total <= 8
    assert any("裁剪" in line for line in target_plan.diagnostics["notes"])


def test_include_slow_false_drops_p3():
    target_plan = plan(list_payload(), include_slow=False)

    assert target_plan.by_priority()[P3] == 0
    assert target_plan.total < plan(list_payload()).total


def test_credentials_can_be_injected():
    case = case_of(plan(login_payload(), credentials=("my_user", "MyPw1!")),
                   "credential.submit", "success")

    assert case.test_data == {"用户名": "my_user", "密码": "MyPw1!"}


def test_deep_link_turns_the_skipped_case_into_a_real_one():
    payload = login_payload()
    default = plan(payload)
    linked = plan(payload, deep_link="https://shop.example.com/order/list")

    assert not any("deep_link" in case.cid for case in default)
    assert linked.total == default.total + 1
    assert any("goto" in case.actions() for case in linked)


def test_archetype_can_be_forced():
    payload = page(inputs=[field("e1", container="filter-bar", text="关键字")],
                   url="https://shop.example.com/x", title="怪页")

    assert plan(payload, archetype="list_crud").archetype == "list_crud"


# ---------------------------------------------------------------------------
# 12. 确定性
# ---------------------------------------------------------------------------

def test_same_inventory_yields_the_same_plan():
    payload = list_payload()
    stamp = "2026-01-01T00:00:00+00:00"

    first = plan(payload, generated_at=stamp).to_dict()
    second = plan(payload, generated_at=stamp).to_dict()

    assert first == second, "计划要能 diff, 否则回归比没有还吵"


def test_plans_are_value_objects():
    target_plan = plan(list_payload())

    with pytest.raises(FrozenInstanceError):
        setattr(target_plan, "page", "/other")
    with pytest.raises(FrozenInstanceError):
        setattr(target_plan.cases[0], "title", "改不动")
    with pytest.raises(FrozenInstanceError):
        setattr(target_plan.cases[0].steps[0], "action", "click")


def test_plan_supports_lookup_helpers():
    target_plan = plan(list_payload())

    assert len(target_plan) == target_plan.total
    assert [case.case_id for case in target_plan] == \
           [case.case_id for case in target_plan.cases]
    assert target_plan.find("LC-001").case_id == "LC-001"
    assert target_plan.find("LC-999") is None
    assert all(case.priority in (P0, P1) for case in target_plan.of_priority(P0, P1))


def test_build_cases_returns_the_bundle():
    payload = list_payload()
    bundle = build_cases(from_inventory(payload), expand(payload).cases, archetype="list_crud")

    assert bundle.cases
    assert isinstance(bundle.skipped, list)
    assert bundle.problems == []


# ---------------------------------------------------------------------------
# 13. LLM 只是增强: 过了校验才准进计划
# ---------------------------------------------------------------------------

def test_llm_rewrite_is_accepted_when_it_validates():
    rewritten = {"cases": [{"case_id": "LC-001", "steps": [
        {"action": "fill", "eid": "e1", "value": "abc"},
        {"action": "click", "eid": "e3"},
        {"action": "assert_no_error_toast"}]}]}

    target_plan = plan_with_llm(list_payload(), ask=lambda prompt: rewritten)

    assert [step.to_dict() for step in target_plan.find("LC-001").steps] == \
           rewritten["cases"][0]["steps"]
    assert "接受 1 条" in target_plan.diagnostics["llm"]
    assert target_plan.is_valid


def test_llm_step_with_an_invented_eid_is_rejected():
    bad = {"cases": [{"case_id": "LC-001", "steps": [
        {"action": "click", "eid": "e999"}]}]}

    target_plan = plan_with_llm(list_payload(), ask=lambda prompt: bad)
    original = plan(list_payload()).find("LC-001")

    assert target_plan.find("LC-001").steps == original.steps, "校验不过就保留确定性步骤"
    assert "拒绝 1 条" in target_plan.diagnostics["llm"]
    assert any("e999" in problem for problem in target_plan.problems)


def test_llm_cannot_invent_cases():
    invented = {"cases": [{"case_id": "LC-999", "steps": [
        {"action": "reload"}]}]}

    target_plan = plan_with_llm(list_payload(), ask=lambda prompt: invented)

    assert target_plan.find("LC-999") is None
    assert any("LC-999" in problem for problem in target_plan.problems)


def test_llm_steps_with_broken_structure_are_rejected():
    broken = {"cases": [{"case_id": "LC-001", "steps": "先填再点"}]}

    target_plan = plan_with_llm(list_payload(), ask=lambda prompt: broken)

    assert "拒绝 1 条" in target_plan.diagnostics["llm"]
    assert any("结构不对" in problem for problem in target_plan.problems)


def test_llm_failure_falls_back_to_the_deterministic_plan():
    def boom(prompt):
        raise RuntimeError("网络挂了")

    target_plan = plan_with_llm(list_payload(), ask=boom)

    assert target_plan.to_dict()["cases"] == plan(list_payload()).to_dict()["cases"]
    assert "调用失败" in target_plan.diagnostics["llm"]


def test_llm_empty_answer_falls_back():
    target_plan = plan_with_llm(list_payload(), ask=lambda prompt: {})

    assert "没有可用的返回" in target_plan.diagnostics["llm"]
    assert target_plan.total == plan(list_payload()).total


# ---------------------------------------------------------------------------
# 14. 真浏览器: 合成清单上的结论在真采集结果上也成立
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def driver():
    from ai_kit.evaluation import driver_provider

    drv = driver_provider.get_driver()
    if drv is None:
        pytest.skip("本机拉不起无头 Edge/Chrome, 跳过真页面计划")
    yield drv
    driver_provider.quit_driver(drv)


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


LIST_HTML = """
<div class="filter-bar">
  <input name="kw" placeholder="关键字" maxlength="32">
  <button>查询</button><button>重置</button>
</div>
<div class="toolbar"><button>新增</button><button>批量删除</button></div>
<table>
  <thead><tr><th>名称</th><th>状态</th></tr></thead>
  <tbody>
    <tr><td>记录A</td><td>正常</td>
      <td><input type="checkbox"><button>编辑</button><button>删除</button></td></tr>
    <tr><td>记录B</td><td>正常</td>
      <td><input type="checkbox"><button>编辑</button><button>删除</button></td></tr>
  </tbody>
</table>
"""


def test_real_list_page_plans_without_problems(html_page):
    from ai_kit.perception import from_collect

    inventory = from_collect(html_page(LIST_HTML))
    target_plan = plan(inventory)

    assert target_plan.archetype == "list_crud"
    assert target_plan.total > 30
    assert target_plan.problems == (), target_plan.report()
    for case in target_plan:
        for step in (*case.steps, *case.cleanup):
            if step.eid:
                assert resolve_ref(inventory, step.eid) is not None, f"{case.cid}: {step.eid}"


def test_real_login_page_plans_a_submit_case(html_page):
    from ai_kit.perception import from_collect

    inventory = from_collect(html_page(
        "<form><input name='username' placeholder='用户名'>"
        "<input type='password' name='password'><button>登录</button></form>"))
    target_plan = plan(inventory)

    assert target_plan.archetype == "login"
    success = [case for case in target_plan if "assert_logged_in" in case.actions()]
    assert success, "登录页连一条『能登进去』的用例都出不来, 计划就没有意义"
    assert DEMO_PASSWORD in success[0].test_data.values()
    assert DEMO_USERNAME in success[0].test_data.values()
    assert target_plan.problems == (), target_plan.report()
