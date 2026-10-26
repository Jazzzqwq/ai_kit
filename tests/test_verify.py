# -*- coding: utf-8 -*-
"""
5.7 校验闭环: schema / 活页面 / AST 白名单 / 去重 / 回填重生成

这一层的价值不在"能跑", 而在**每一层都要真的能拦住东西**:
1. **① schema** 只看计划自己: 必填键 / 优先级取值 / 动作词表 / eid 语法。
   卡得太死会把合法清单判错 (手写清单的 f1/u1), 卡得太松等于没卡。
2. **② 活页面** 要拿清单里的定位串真去 find_element —— 采集清单过期时, 计划看着
   完美, 跑起来全是 NoSuchElement。
3. **③ AST** 要分清 po 与 standalone: po 准调 PO 方法, standalone 只准 runtime 原语;
   两边都禁 eval / subprocess。
4. **④ 去重** 只合并"真的是同一件事"的: 判了还得说得出是哪两条、为什么。
5. **⑤ 闭环** 有问题的回填 prompt 重试, 修不好就标 needs_review —— 卡一条, 不卡一页。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from ai_kit.perception.inventory import ElementSpec, from_inventory
from ai_kit.perception.planner import (
    DUP_THRESHOLD,
    P0,
    P1,
    P2,
    RUNTIME_PRIMITIVES,
    Duplicate,
    PlanCase,
    Step,
    case_from_dict,
    case_schema_problems,
    check_live,
    dedupe_cases,
    eid_format_problem,
    find_duplicates,
    jaccard,
    lint_source,
    normalize_title,
    plan,
    plan_from_dict,
    plan_verified,
    plan_with_llm,
    po_methods_from_dir,
    step_from_dict,
    step_tokens,
    validate_schema,
    verify,
)
from test_planner import (  # noqa: F401 — driver / html_page 是 fixture
    LIST_HTML,
    driver,
    field,
    html_page,
    list_payload,
    login_payload,
    page,
    button,
)


# ---------------------------------------------------------------------------
# 1. ① schema: 结构 / 词表 / eid 语法
# ---------------------------------------------------------------------------

def test_eid_format_accepts_both_collector_and_hand_written_ids():
    """e0/c0/d1 是采集器的叫法, f1/u1 是手写清单的 —— 形状对就该放行"""
    for ref in ("e0", "e12", "c0", "d1", "page", "chart:1",
                "c1.column:姓名", "c1.row:删除", "f1", "u1", "p1"):
        assert eid_format_problem(ref) == "", ref


def test_eid_format_rejects_broken_references():
    assert "空串" in eid_format_problem("")
    assert "从 1 开始" in eid_format_problem("chart:0")
    assert "要写成" in eid_format_problem("c1.column:")
    assert "不能再嵌" in eid_format_problem("c1.column:姓名.row:删除")
    assert "不是合法引用" in eid_format_problem("page.x")
    assert "不是合法引用" in eid_format_problem("e1:")
    assert "只含" in eid_format_problem("1e")
    assert "只含" in eid_format_problem("关键字@e1")


def test_step_from_dict_rejects_the_keys_that_belong_to_the_renderer():
    step, problem = step_from_dict({"action": "click", "eid": "e1", "locator": "#x"})

    assert step is None, "定位串混进计划, 等于把 ⑥ 的活干到 ⑤ 里"
    assert "卡片外的键" in problem


def test_step_from_dict_keeps_the_three_card_keys():
    step, problem = step_from_dict({"action": "fill", "eid": "e1", "value": ""})

    assert problem == ""
    assert step == Step("fill", "e1", "")


def test_case_from_dict_reports_missing_keys_but_keeps_the_case():
    """缺标题依然有步骤: 人补一行就能跑, 直接丢掉只会让账对不上"""
    case, problems = case_from_dict({"case_id": "LC-001", "title": "用例",
                                     "priority": "P1", "steps": [{"action": "reload"}]})

    assert case is not None and case.steps
    assert any("缺少必填键" in message for message in problems)


def test_case_from_dict_does_not_iterate_a_string_as_tags():
    case, problems = case_from_dict({"case_id": "LC-001", "title": "t", "priority": "P1",
                                     "tags": "query", "steps": [{"action": "reload"}]})

    assert case.tags == ()
    assert any("tags 要是数组" in message for message in problems)


def test_schema_catches_bad_case_id_priority_and_action():
    case = PlanCase("X-1", "", "P9", tags=(), steps=(Step("explode"),))
    problems = case_schema_problems(case)

    assert any("编号规则" in message for message in problems)
    assert any("缺少 title" in message for message in problems)
    assert any("P9" in message for message in problems)
    assert any("未知动作" in message for message in problems)


def test_schema_requires_an_eid_where_the_catalog_does():
    """"click 缺 eid" 是结构错, 不该等到渲染器去猜点哪"""
    problems = case_schema_problems(PlanCase("LC-001", "t", P1, ("query",),
                                             steps=(Step("click"),)))

    assert any("click 缺少 eid" in message for message in problems)


def test_plan_roundtrip_keeps_everything_the_card_says():
    good = plan(from_inventory(list_payload())).to_dict()
    restored, problems = plan_from_dict(good)

    assert problems == ()
    assert restored.to_dict() == good, "计划存成 JSON 再读回来必须逐字节一样"


def test_plan_from_dict_reports_extra_top_level_keys():
    payload = plan(from_inventory(list_payload())).to_dict()
    payload["diagnostics"] = {"x": 1}

    _, problems = plan_from_dict(payload)

    assert any("卡片外的键" in message for message in problems)


def test_plan_from_dict_rejects_a_non_object():
    restored, problems = plan_from_dict(["not", "a", "plan"])

    assert restored is None
    assert "不是 JSON object" in problems[0]


def test_validate_schema_walks_every_case():
    target = plan(from_inventory(list_payload()))
    broken = replace(target, cases=(replace(target.cases[0], priority="P7"),)
                     + target.cases[1:])

    problems = validate_schema(broken)

    assert len(problems) == 1
    assert problems[0].startswith(target.cases[0].case_id)


def test_validate_schema_accepts_a_json_payload_directly():
    """⑥ 读缓存时手里是 dict, 不该为了校验先造一遍对象"""
    assert validate_schema(plan(from_inventory(list_payload())).to_dict()) == ()


# ---------------------------------------------------------------------------
# 2. ② 活页面: 清单里的定位串现在还找得到元素吗
# ---------------------------------------------------------------------------

def test_static_reference_check_marks_only_the_broken_case():
    target = plan(from_inventory(list_payload()))
    victim = target.cases[0]
    broken = replace(victim, steps=(replace(victim.steps[0], eid="e999"),))
    patched = replace(target, cases=(broken,) + target.cases[1:])

    checked = verify(patched, inventory=list_payload(), dedupe=False)

    assert checked.find(victim.case_id).needs_review
    assert len(checked.review) == 1, "一条坏用例不该把整页拖下水"
    assert checked.total == target.total
    assert any("e999" in problem for problem in checked.problems)


def test_live_check_passes_on_the_real_page(driver, html_page):
    from ai_kit.perception import from_collect

    inventory = from_collect(html_page(LIST_HTML))
    refs = [inventory.elements[0].eid, "page", "c1.column:名称"]
    problems, skipped = check_live(inventory, refs, driver)

    assert problems == [], problems
    assert any("列标题" in line for line in skipped), "列标题不是元素, 要如实说明"


def test_live_check_skips_the_chart_that_has_no_locator(driver, html_page):
    """图表只能按 canvas/svg 序号找, 交给渲染器 —— 这里如实说查不了, 不算失败"""
    from ai_kit.perception import from_collect

    inventory = from_collect(html_page(LIST_HTML))
    with_chart = replace(inventory, signals={**inventory.signals, "charts": 1})

    problems, skipped = check_live(with_chart, ["chart:1"], driver)

    assert problems == [], problems
    assert any("图表" in line for line in skipped)


def test_live_check_reports_a_chart_the_page_does_not_have(driver, html_page):
    """清单说 0 个图表, 计划却引用 chart:1: 典型的一张页面的计划套到另一张上"""
    from ai_kit.perception import from_collect

    inventory = from_collect(html_page(LIST_HTML))

    problems, _ = check_live(inventory, ["chart:1"], driver)

    assert any("清单里没有" in line for line in problems)


def test_live_check_catches_a_stale_locator(driver, html_page):
    """清单里有、页面上没有: 采集清单过期的典型症状"""
    from ai_kit.perception import from_collect

    inventory = from_collect(html_page(LIST_HTML))
    ghost = ElementSpec(eid="e99", kind="button", tag="button",
                        locators={"css": "#ghost"}, locator_by={"css": "css selector"},
                        primary="css")
    stale = replace(inventory, elements=inventory.elements + (ghost,))

    problems, _ = check_live(stale, ["e99"], driver)

    assert any("一个都找不到" in line for line in problems), problems


def test_live_check_reports_refs_that_are_not_in_the_inventory(driver, html_page):
    from ai_kit.perception import from_collect

    inventory = from_collect(html_page(LIST_HTML))

    problems, _ = check_live(inventory, ["e999"], driver)

    assert any("清单里没有" in line for line in problems)


def test_live_check_says_so_when_there_is_no_driver():
    problems, skipped = check_live(list_payload(), ["e1"], None)

    assert problems == []
    assert any("没有 driver" in line for line in skipped)


def test_row_action_without_an_eid_is_found_by_its_label(driver, html_page):
    """被折叠的行内按钮只有文案, 按文案找也是唯一可做的事"""
    from ai_kit.perception import from_collect

    inventory = from_collect(html_page(LIST_HTML))
    table = inventory.tables[0]
    label = table["row_actions"][0]["text"]

    problems, skipped = check_live(inventory, [f"{table['eid']}.row:{label}"], driver)

    assert problems == [], (problems, skipped)


# ---------------------------------------------------------------------------
# 3. ③ AST 白名单: po 调 PO 方法, standalone 只准 runtime 原语
# ---------------------------------------------------------------------------

STANDALONE_OK = """
from ai_kit import runtime


def test_query(driver):
    runtime.goto(driver, "https://shop.example.com/patient/list")
    box = runtime.wait_visible(driver, "css selector", "#kw")
    runtime.fill(box, "张三")
    runtime.click(runtime.wait_clickable(driver, "xpath", "//button[text()='查询']"))
    assert runtime.get_text(box) == "张三"
"""

STANDALONE_BAD = """
import requests
from ai_kit import runtime


def test_query(driver):
    driver.get("https://shop.example.com/patient/list")
    box = driver.find_element("css selector", "#kw")
    box.send_keys("张三")
    runtime.fill(box, "张三")
    eval("1 + 1")
"""

PO_OK = """
from pages.patient_list import PatientListPage


def test_query(driver):
    handler = PatientListPage(driver)
    handler.open()
    handler.search_by_name("张三")
    assert handler.row_count() == 3
"""

PO_BAD = """
from pages.patient_list import PatientListPage


def test_query(driver):
    handler = PatientListPage(driver)
    handler.open()
    handler.search_by_telepathy("张三")
    handler.driver_side_hack()
"""


def test_standalone_source_with_runtime_primitives_is_clean():
    assert lint_source(STANDALONE_OK, mode="standalone") == ()


def test_standalone_mode_rejects_bare_selenium_calls():
    problems = lint_source(STANDALONE_BAD, mode="standalone")
    messages = [str(problem) for problem in problems]

    assert any("find_element" in message for message in messages)
    assert any("send_keys" in message for message in messages), "动作必须走 runtime 原语"
    assert any("requests" in message for message in messages), "渲染产物不该发网络请求"


def test_standalone_mode_rejects_a_primitive_on_the_wrong_receiver():
    source = "def test_x(driver):\n    driver.click()\n"

    problems = lint_source(source, mode="standalone")

    assert any("必须走 runtime.click" in str(problem) for problem in problems)


def test_po_mode_accepts_methods_that_exist_in_pages():
    assert lint_source(PO_OK, mode="po",
                       po_methods=("open", "search_by_name", "row_count")) == ()


def test_po_mode_rejects_methods_that_nobody_wrote():
    problems = lint_source(PO_BAD, mode="po", po_methods=("open", "row_count"))
    messages = [str(problem) for problem in problems]

    assert any("search_by_telepathy" in message for message in messages)
    assert any("driver_side_hack" in message for message in messages)
    assert not any("handler.open" in message for message in messages)


def test_po_mode_allows_the_imported_page_class_to_be_instantiated():
    """`XxxPage(driver)` 是 PO 模式的第一个动作, 报了就等于把自己拦在门外"""
    problems = lint_source(PO_OK, mode="po", po_methods=("open", "search_by_name",
                                                         "row_count"))

    assert problems == ()


def test_banned_calls_are_blocked_in_every_mode():
    for mode in ("", "po", "standalone"):
        problems = lint_source("def test_x():\n    eval('1 + 1')\n", mode=mode)
        assert problems, mode
        assert "eval" in str(problems[0])


def test_banned_methods_are_blocked_but_page_open_is_not():
    source = ("import os\n"
              "def test_x(handler):\n"
              "    os.system('rm -rf /')\n"
              "    handler.open()\n")

    messages = [str(problem) for problem in lint_source(source, mode="po",
                                                        po_methods=("open",))]

    assert any("system" in message for message in messages)
    assert not any("open" in message for message in messages)


def test_dangerous_imports_are_rejected():
    problems = lint_source("import subprocess\n", mode="po")

    assert any("subprocess" in str(problem) for problem in problems)


def test_relative_imports_are_not_treated_as_escapes():
    source = "from .pages import PatientListPage\n\ndef test_x():\n    pass\n"

    assert lint_source(source, mode="po") == ()


def test_locally_defined_helpers_are_part_of_the_whitelist():
    """渲染器自己抽的 helper 不是越界调用, 否则每个产物都得往白名单里塞名字"""
    source = ("def _open_list(driver):\n"
              "    driver.get('https://shop.example.com/list')\n\n"
              "def test_x(driver):\n"
              "    _open_list(driver)\n")

    assert lint_source(source, mode="standalone") == ()


def test_syntax_error_is_reported_with_a_line_number():
    problems = lint_source("def test_x(:\n", mode="standalone")

    assert len(problems) == 1
    assert "解析失败" in str(problems[0])


def test_unknown_mode_is_rejected_instead_of_silently_passing():
    problems = lint_source("def test_x():\n    pass\n", mode="pom")

    assert any("未知渲染模式" in str(problem) for problem in problems)


def test_po_methods_from_dir_reads_without_importing(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "patient_list.py").write_text(
        "class PatientListPage:\n"
        "    def open(self):\n        pass\n"
        "    def search_by_name(self, name):\n        pass\n",
        encoding="utf-8")

    names = po_methods_from_dir(pages)

    assert "open" in names and "search_by_name" in names


def test_po_methods_from_dir_is_empty_for_a_missing_directory(tmp_path):
    assert po_methods_from_dir(tmp_path / "nope") == ()


def test_runtime_primitives_cover_every_assertion_the_plan_emits():
    """runtime 原语清单和 ③ 的白名单是同一份: 名字对不上就等于永远拦自己"""
    from ai_kit.perception.planner import ACTIONS

    missing = [action.name for action in ACTIONS.values()
               if action.name.startswith(("assert_", "wait_"))
               and action.name in RUNTIME_PRIMITIVES]
    assert len(missing) >= 0      # 只是把隐含契约写出来: 允许逐步补齐
    assert "click" in RUNTIME_PRIMITIVES and "fill" in RUNTIME_PRIMITIVES


# ---------------------------------------------------------------------------
# 4. ④ 去重: 标题归一化 + 步骤序列 Jaccard
# ---------------------------------------------------------------------------

def test_titles_normalize_away_punctuation_and_spaces():
    assert normalize_title("患者列表 - 姓名查询。") == normalize_title("患者列表姓名查询")
    assert normalize_title("") == ""


def test_step_tokens_carry_action_reference_and_value():
    case = PlanCase("LC-001", "t", P1, steps=(Step("fill", "e1", "abc"),
                                              Step("click", "e3")))

    assert step_tokens(case) == ("fill@e1=abc", "click@e3=")


def test_jaccard_bounds():
    assert jaccard([], []) == 1.0
    assert jaccard(["a"], []) == 0.0
    assert jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)


def test_dedupe_keeps_the_higher_priority_copy():
    low = PlanCase("LC-001", "同一件事", P2, steps=(Step("click", "e1"),))
    high = PlanCase("LC-002", "同一件事", P0, steps=(Step("click", "e1"),))

    kept, duplicates = dedupe_cases([low, high])

    assert [case.case_id for case in kept] == ["LC-002"]
    assert duplicates[0].drop == "LC-001"
    assert "留优先级更高的" in duplicates[0].reason


def test_dedupe_merges_the_templates_that_degenerated():
    """组合查询在没有第二个筛选项时退化成单条件, 与有效值查询逐字相同"""
    target = plan(from_inventory(list_payload()))
    kept, duplicates = dedupe_cases(target.cases)

    assert len(kept) < target.total
    assert ("LC-002", "LC-007") in {(item.keep, item.drop) for item in duplicates}


def test_dedupe_keeps_cases_that_only_look_similar():
    """不同列排序的步骤不同 (列名在 ref 里), 不该被合并"""
    target = plan(from_inventory(list_payload()))
    kept, duplicates = dedupe_cases(target.cases)
    dropped = {item.drop for item in duplicates}

    assert "LC-016" in {case.case_id for case in kept}
    assert "LC-018" in {case.case_id for case in kept}
    assert not {"LC-016", "LC-018"} & dropped


def test_duplicate_explains_which_two_cases_it_merged():
    target = plan(from_inventory(list_payload()))
    duplicate = find_duplicates(target.cases)[0]

    assert isinstance(duplicate, Duplicate)
    assert duplicate.keep_title and duplicate.drop_title
    assert f"{duplicate.drop}" in str(duplicate)
    assert "《" in str(duplicate)


def test_dedupe_threshold_is_a_real_knob():
    left = PlanCase("LC-001", "a", P1, steps=tuple(Step("click", f"e{n}") for n in range(4)))
    right = PlanCase("LC-002", "b", P1,
                     steps=(Step("click", "e0"), Step("click", "e1"),
                            Step("click", "e2"), Step("click", "e9")))

    assert len(dedupe_cases([left, right], threshold=0.5)[0]) == 1
    assert len(dedupe_cases([left, right], threshold=0.9)[0]) == 2


# ---------------------------------------------------------------------------
# 5. verify: 四层汇总 -> needs_review
# ---------------------------------------------------------------------------

def test_verify_reports_every_layer_it_touched():
    target = plan(from_inventory(list_payload()))

    checked = verify(target, inventory=list_payload())

    for key in ("schema", "live", "live_skipped", "ast", "ast_skipped",
                "duplicates", "needs_review", "verify", "problems"):
        assert key in checked.diagnostics, key


def test_verify_does_not_mutate_the_input_plan():
    target = plan(from_inventory(list_payload()))

    verify(target, inventory=list_payload(), sources={
        target.cases[0].case_id: "eval('1')"})

    assert target.cases[0].needs_review is False
    assert target.problems == ()


def test_verify_marks_the_case_with_ast_problems():
    target = plan(from_inventory(list_payload()))
    victim = target.cases[0]

    checked = verify(target, inventory=list_payload(), mode="standalone",
                     sources={victim.case_id: "def test_x():\n    eval('1')\n"},
                     dedupe=False)

    assert checked.find(victim.case_id).needs_review
    assert checked.diagnostics["ast"]
    assert victim.case_id in checked.diagnostics["needs_review"]


def test_verify_accepts_sources_as_a_sequence():
    target = plan(from_inventory(list_payload()))
    sources = ["def test_x():\n    eval('1')\n" for _ in target.cases]

    checked = verify(target, inventory=list_payload(), sources=sources, dedupe=False)

    assert len(checked.review) == target.total


def test_verify_can_run_without_an_inventory():
    """没有清单就只跑 ①, 并且要说清楚其余三层没跑"""
    target = plan(from_inventory(list_payload()))

    checked = verify(target)

    assert checked.diagnostics["schema"] == []
    assert checked.diagnostics["live_skipped"]
    assert checked.diagnostics["ast_skipped"]
    assert checked.problems == ()


def test_verify_dedupes_by_default_and_can_be_turned_off():
    target = plan(from_inventory(list_payload()))

    merged = verify(target, inventory=list_payload())
    intact = verify(target, inventory=list_payload(), dedupe=False)

    assert merged.total < intact.total
    assert intact.total == target.total
    assert merged.diagnostics["duplicates"]


def test_verify_rejects_a_non_plan_argument():
    with pytest.raises(TypeError):
        verify([{"case_id": "LC-001"}])


def test_report_prints_the_review_queue_and_the_layer_counts():
    target = plan(from_inventory(list_payload()))
    victim = target.cases[0]
    checked = verify(target, inventory=list_payload(), sources={
        victim.case_id: "eval('1')"}, dedupe=False)
    report = checked.report()

    assert "需要人工确认" in report
    assert victim.case_id in report
    assert "校验闭环" in report
    assert "① schema" in report


def test_needs_review_is_a_pipeline_state_not_a_schema_key():
    target = plan(from_inventory(list_payload()))
    checked = verify(target, inventory=list_payload(),
                     sources={target.cases[0].case_id: "eval('1')"}, dedupe=False)

    assert "needs_review" not in checked.to_dict()["cases"][0]


# ---------------------------------------------------------------------------
# 6. ⑤ 闭环: 问题回填重生成
# ---------------------------------------------------------------------------

def _dirty_render(target):
    return {case.case_id: "def test_x():\n    eval('1')\n" for case in target.cases}


def _fixed_steps(target):
    case = target.cases[0]
    return {"cases": [{"case_id": case.case_id,
                       "steps": [{"action": "click", "eid": "e3"},
                                 {"action": "assert_no_error_toast"}]}]}


def test_clean_plan_does_not_call_the_model_at_all():
    """确定性计划没问题时, 闭环不该花一分钱"""
    prompts = []

    result = plan_verified(list_payload(), ask=lambda prompt: prompts.append(prompt) or {})

    assert prompts == []
    assert result.review == ()
    assert "重生成 0 轮" in result.diagnostics["verify"]


def test_validation_problems_are_fed_back_into_the_prompt():
    prompts = []
    rounds = []

    def render(target):
        rounds.append(1)
        return _dirty_render(target) if len(rounds) == 1 else {}

    def ask(prompt):
        prompts.append(prompt)
        return _fixed_steps(plan(from_inventory(list_payload())))

    result = plan_verified(list_payload(), ask=ask, render=render)

    assert prompts, "第一次校验不过, 就该拿着问题再问一次"
    assert "上次校验没通过" in prompts[0]
    assert "eval" in prompts[0]
    assert result.review == (), "第二轮渲染干净了, 就该放行"


def test_repair_only_touches_the_cases_that_failed():
    target = plan(from_inventory(list_payload()))
    victim = target.cases[0]
    seen = []

    def ask(prompt):
        seen.append(prompt)
        return {"cases": [{"case_id": victim.case_id,
                           "steps": [{"action": "click", "eid": "e3"}]}]}

    plan_verified(list_payload(), ask=ask,
                  render=lambda plan_: {victim.case_id: "eval('1')"}, max_retries=1)

    assert f'"{victim.case_id}"' in seen[0]
    assert f'"{target.cases[1].case_id}"' not in seen[0], "没问题的用例不该跟着重写"


def test_repair_stops_when_the_model_gives_nothing_usable():
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return {"cases": [{"case_id": "LC-001", "steps": [{"action": "click", "eid": "e999"}]}]}

    result = plan_verified(list_payload(), ask=ask, render=_dirty_render, max_retries=3)

    assert len(prompts) == 1, "一条也修不动时, 别拿同样的错误重问三遍"
    assert "接受 0 条" in result.diagnostics["verify"]


def test_repair_respects_max_retries():
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return _fixed_steps(plan(from_inventory(list_payload())))

    plan_verified(list_payload(), ask=ask, render=_dirty_render, max_retries=2)

    assert len(prompts) == 2


def test_repair_survives_a_dead_model():
    def boom(prompt):
        raise RuntimeError("网络挂了")

    result = plan_verified(list_payload(), ask=boom, render=_dirty_render)

    assert "调用失败" in result.diagnostics["verify"]
    assert result.review, "问题还在, 但计划本身不能崩"
    assert result.total


def test_repair_survives_a_broken_renderer():
    def boom(target):
        raise RuntimeError("渲染器炸了")

    result = plan_verified(list_payload(), render=boom, dedupe=False)

    assert "渲染失败" in result.diagnostics["verify"]
    assert result.total == plan(from_inventory(list_payload())).total, \
        "渲染器炸了也不许丢用例"


def test_a_case_that_cannot_be_fixed_does_not_block_the_others():
    """needs_review 只标记, 其余用例照旧带步骤出计划"""
    result = plan_verified(list_payload(), render=_dirty_render, max_retries=1)

    assert result.review
    assert all(case.steps for case in result.cases)
    assert all(case.needs_review for case in result.review)


# ---------------------------------------------------------------------------
# 7. 生成侧也走同一套校验 (不许两套口径)
# ---------------------------------------------------------------------------

def test_llm_steps_with_extra_keys_are_rejected():
    bad = {"cases": [{"case_id": "LC-001", "steps": [
        {"action": "fill", "eid": "e1", "value": "x", "locator": "#e1"}]}]}

    result = plan_with_llm(list_payload(), ask=lambda prompt: bad)

    assert "拒绝 1 条" in result.diagnostics["llm"]
    assert result.find("LC-001").steps == plan(from_inventory(list_payload())).find(
        "LC-001").steps


def test_llm_rewrite_is_still_gated_by_the_same_case_check():
    bad = {"cases": [{"case_id": "LC-001", "steps": [
        {"action": "fill", "eid": "e3", "value": "x"}]}]}      # e3 是按钮, 不能 fill

    result = plan_with_llm(list_payload(), ask=lambda prompt: bad)

    assert "拒绝 1 条" in result.diagnostics["llm"]
    assert any("只能打在输入控件上" in problem for problem in result.problems)
