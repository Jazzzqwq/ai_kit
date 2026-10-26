# -*- coding: utf-8 -*-
"""
5.8 渲染器: 计划 -> pytest 源码, 双模式 + locators.json + AST 准入

这一层的价值不在"能生成代码", 而在**生成的代码能信**:
1. **standalone 只写原语** `page.fill(loc("e1"), "x")`: 产物里没有站点选择器,
   换网站只换 profile 与计划。loc() 查的是随计划落盘的 locators.json。
2. **po 模式不猜方法名** 动作 -> 方法的映射由人给 (PoPage.mapping); 没映射就拒收,
   编一个方法名比不生成更坏 —— 它会让所有下游以为这条用例有救。
3. **AST 准入是硬门槛** 每条产物返回前过 ③; 过不了的用例不进 sources, 只进
   problems, 上层据此标 needs_review —— 卡一条, 不卡一页。
4. **落盘是配套的** 模块与 locators.json 必须同目录 (产物用 `Path(__file__)` 找它);
   po 模式不需要 locators.json。
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ai_kit.perception.inventory import from_inventory
from ai_kit.perception.planner import (
    P1,
    PlanCase,
    Step,
    lint_source,
    plan,
    plan_verified,
    verify,
)
from ai_kit.perception.renderer import (
    DEFAULT_DRIVER_FIXTURE,
    LOCATORS_FILENAME,
    MODE_PO,
    MODE_STANDALONE,
    PoPage,
    build_locators,
    make_renderer,
    render,
    write,
)
from ai_kit.runtime import loc, use_locators
from test_planner import (  # noqa: F401 — driver / html_page 是 fixture
    LIST_HTML,
    case_of,
    driver,
    html_page,
    list_payload,
)


def _locs(result):
    return result.locators.get("locators") or {}


# ---------------------------------------------------------------------------
# 1. locators.json: eid 与页面之间的地址簿
# ---------------------------------------------------------------------------

def test_build_locators_covers_elements_tables_columns_and_row_actions():
    doc = build_locators(from_inventory(list_payload()), page="list")
    entries = doc["locators"]

    for eid in ("e1", "c1", "c1.column:名称", "c1.row:删除", "page"):
        assert eid in entries, eid
    assert entries["c1.column:名称"]["kind"] == "column"
    assert entries["c1.column:名称"]["table"] == "c1"
    assert entries["c1.row:删除"]["kind"] == "row-action"
    assert doc["page"] == "list"


def test_column_locator_falls_back_to_the_eid_mark_when_nothing_was_collected():
    """手写清单没有定位串: 采集器打的 data-ai-eid 就是唯一锛点"""
    doc = build_locators(from_inventory(list_payload()))
    value = doc["locators"]["c1.column:名称"]["value"]

    assert value.startswith('//*[@data-ai-eid="c1"]'), value
    assert value.endswith('//th[normalize-space()="名称"]'), value


def test_column_locator_wraps_a_real_xpath_anchor_in_parentheses():
    """清单里有 xpath 时要用它: 不括起来, 后面的 //th 就变成「整个文档的任意 th」"""
    payload = list_payload(tables=[{"eid": "c1", "columns": ["名称"],
                                    "row_actions": [],
                                    "locator": {"by": "xpath", "value": "//table"}}])
    value = build_locators(from_inventory(payload))["locators"]["c1.column:名称"]["value"]

    assert value.startswith("(//table)//th["), value


def test_row_action_locator_accepts_button_and_link_and_role_button():
    entries = _locs(render(plan(from_inventory(list_payload())),
                           inventory=from_inventory(list_payload())))
    value = entries["c1.row:删除"]["value"]

    assert "//button[" in value and "//a[" in value and '@role="button"' in value
    assert value.endswith("[1]"), "union xpath 要取第一个, 否则 find_element 会报多个匹配"


def test_page_entry_exists_without_any_element():
    doc = build_locators(from_inventory(list_payload()))

    assert doc["locators"]["page"]["value"] == "html"
    assert doc["url"]


# ---------------------------------------------------------------------------
# 2. standalone: 动作 -> page 原语
# ---------------------------------------------------------------------------

def test_standalone_source_uses_loc_and_the_runtime_only():
    inventory = from_inventory(list_payload())
    target = plan(inventory)
    case = case_of(target, "query.filter", "关键字", "valid")

    result = render(target, inventory=inventory)
    source = result[case.case_id]

    assert 'page.fill(loc("e1"), "测试数据")' in source
    assert 'page.click(loc("e3"))' in source
    assert "page.assert_no_error_toast()" in source
    assert "page.assert_rows_match(loc(\"c1\"), \"测试数据\")" in source


def test_standalone_header_binds_the_page_and_finds_locators_next_to_itself():
    inventory = from_inventory(list_payload())
    source = render(plan(inventory), inventory=inventory)[
        plan(inventory).cases[0].case_id]

    assert "from ai_kit.runtime import loc, page, use_locators" in source
    assert f'use_locators(Path(__file__).with_name("{LOCATORS_FILENAME}"))' in source
    assert "@pytest.fixture(autouse=True)" in source
    assert f"def _ai_kit_page({DEFAULT_DRIVER_FIXTURE})" in source
    assert "page.bind(" in source and "page.unbind()" in source


def test_every_rendered_source_passes_the_ast_gate():
    """③ 的门槛必须在渲染器内部先过一遍: 脏代码不该走到落盘"""
    inventory = from_inventory(list_payload())
    target = plan(inventory)

    result = render(target, inventory=inventory)

    assert result, "一条都渲染不出来, 后面的断言就没有意义"
    for case_id, source in result.items():
        assert lint_source(source, mode=MODE_STANDALONE) == (), case_id


def test_unsupported_action_is_reported_instead_of_faked():
    """throttle 需要 CDP: 生成一条假用例比不生成更费人的时间"""
    case = PlanCase("LC-900", "限速", P1, ("perf",),
                    steps=(Step("throttle", value="3s"), Step("reload")))
    target = replace(plan(from_inventory(list_payload())), cases=(case,))

    result = render(target, inventory=from_inventory(list_payload()))

    assert case.case_id not in result
    assert case.case_id in result.skipped
    assert any("CDP" in problem.message for problem in result.problems)


def test_a_ref_missing_from_locators_is_refused_before_it_becomes_a_red_run():
    case = PlanCase("LC-901", "点到不存在的元素", P1, ("query",),
                    steps=(Step("click", "e999"),))
    target = replace(plan(from_inventory(list_payload())), cases=(case,))

    result = render(target, inventory=from_inventory(list_payload()))

    assert case.case_id not in result
    assert any("locators 里没有 e999" in problem.message for problem in result.problems)


def test_standalone_without_an_inventory_refuses_every_case():
    target = plan(from_inventory(list_payload()))

    result = render(target, inventory=None)

    assert not result
    assert len(result.skipped) == target.total
    assert all("没有元素清单" in problem.message for problem in result.problems)


# ---------------------------------------------------------------------------
# 3. po 模式: 映射表由人给, 不猜
# ---------------------------------------------------------------------------

PO_MAPPING = {"goto": "open()", "fill": "search_by_name($value)",
              "click": "click_filter()", "assert_no_error_toast": "assert_clean()"}


def _po_case() -> PlanCase:
    return PlanCase("LC-001", "按关键字查询", P1, ("query",),
                    steps=(Step("goto", value="https://shop.example.com/list"),
                           Step("fill", "e1", "甲"),
                           Step("click", "e3"),
                           Step("assert_no_error_toast")))


def test_po_mode_maps_every_action_through_the_given_table():
    po = PoPage(module="pages.patient_list", cls="PatientListPage",
                mapping=PO_MAPPING,
                methods=("open", "search_by_name", "click_filter", "assert_clean"))
    target = replace(plan(from_inventory(list_payload())), cases=(_po_case(),))

    result = render(target, inventory=from_inventory(list_payload()), mode=MODE_PO, po=po)
    source = result["LC-001"]

    assert "from pages.patient_list import PatientListPage" in source
    assert "page = PatientListPage(driver)" in source
    assert "page.open()" in source
    assert 'page.search_by_name("甲")' in source
    assert "page.click_filter()" in source
    assert "page.assert_clean()" in source
    assert "loc(" not in source, "po 模式不该出现 loc(): 定位是 PO 的职责"


def test_po_mode_refuses_to_guess_a_method_that_was_not_mapped():
    po = PoPage(module="pages.patient_list", cls="PatientListPage",
                mapping={"goto": "open()"}, methods=("open",))
    target = replace(plan(from_inventory(list_payload())), cases=(_po_case(),))

    result = render(target, inventory=from_inventory(list_payload()), mode=MODE_PO, po=po)

    assert "LC-001" not in result
    assert any("不猜方法名" in problem.message for problem in result.problems)


def test_po_mode_lets_the_ast_gate_catch_a_method_nobody_wrote():
    """映射表写错方法名: 语法没问题, 只有 ③ 的 PO 方法清单能拦住"""
    po = PoPage(module="pages.patient_list", cls="PatientListPage",
                mapping=dict(PO_MAPPING, fill="search_by_magic($value)"),
                methods=("open", "click_filter", "assert_clean"))
    target = replace(plan(from_inventory(list_payload())), cases=(_po_case(),))

    result = render(target, inventory=from_inventory(list_payload()), mode=MODE_PO, po=po)

    assert "LC-001" not in result
    assert any("search_by_magic" in problem.message for problem in result.problems)


def test_po_mode_without_a_mapping_refuses_everything():
    target = replace(plan(from_inventory(list_payload())), cases=(_po_case(),))

    result = render(target, inventory=None, mode=MODE_PO,
                    po=PoPage(module="pages.patient_list", cls="PatientListPage"))

    assert not result
    assert any("mapping 是空的" in problem.message for problem in result.problems)


def test_po_mode_can_use_a_conftest_fixture_instead_of_constructing_the_page():
    po = PoPage(module="pages.patient_list", cls="PatientListPage",
                mapping=PO_MAPPING, methods=("open", "search_by_name", "click_filter",
                                             "assert_clean"),
                fixture="patient_page")
    target = replace(plan(from_inventory(list_payload())), cases=(_po_case(),))

    source = render(target, inventory=from_inventory(list_payload()), mode=MODE_PO,
                    po=po)["LC-001"]

    assert "def test_lc_001(patient_page):" in source
    assert "    page = patient_page" in source
    assert "PatientListPage(driver)" not in source


# ---------------------------------------------------------------------------
# 4. 参数校验: 错就是错, 不要静默降级
# ---------------------------------------------------------------------------

def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        render(plan(from_inventory(list_payload())), mode="hybrid")


def test_render_requires_a_plan():
    with pytest.raises(TypeError):
        render({"cases": []})


def test_render_result_is_a_mapping_so_plan_verified_can_eat_it():
    inventory = from_inventory(list_payload())
    target = plan(inventory)
    result = render(target, inventory=inventory)

    assert isinstance(result, dict)
    assert set(result) <= {case.case_id for case in target.cases}
    assert not (set(result) & set(result.skipped)), "出了代码的用例不该同时在拒收名单里"
    assert result.report().startswith("渲染 (standalone)")


# ---------------------------------------------------------------------------
# 5. 落盘: 模块 + locators.json 必须配套
# ---------------------------------------------------------------------------

def test_write_puts_the_module_next_to_its_locators(tmp_path):
    inventory = from_inventory(list_payload())
    folder = tmp_path / "generated"

    written = write(plan(inventory), inventory=inventory, out_dir=folder)

    assert written.test_path and Path(written.test_path).is_file()
    assert written.locators_path and Path(written.locators_path).is_file()
    assert Path(written.test_path).parent == Path(written.locators_path).parent
    source = Path(written.test_path).read_text(encoding="utf-8")
    assert source.count("use_locators(Path(__file__)") == 1, "一个模块只留一份头"
    compile(source, written.test_path, "exec")


def test_written_locators_can_be_loaded_by_the_runtime(tmp_path):
    inventory = from_inventory(list_payload())
    written = write(plan(inventory), inventory=inventory, out_dir=tmp_path / "gen")

    use_locators(written.locators_path)
    try:
        assert loc("e1").by == "css selector"
        assert loc("c1.column:名称").table == "c1"
        assert loc("page").value == "html"
    finally:
        use_locators(None)


def test_write_skips_the_cases_that_did_not_render(tmp_path):
    case = PlanCase("LC-902", "限速", P1, ("perf",), steps=(Step("throttle", value="3s"),))
    target = replace(plan(from_inventory(list_payload())), cases=(case,))

    written = write(target, inventory=from_inventory(list_payload()), out_dir=tmp_path / "g")

    assert written.test_path == "", "一条都没生成出来, 就不该留下一个空模块"
    assert written.result.problems


def test_po_mode_writes_no_locators_file(tmp_path):
    po = PoPage(module="pages.patient_list", cls="PatientListPage",
                mapping=PO_MAPPING, methods=("open", "search_by_name", "click_filter",
                                             "assert_clean"))
    target = replace(plan(from_inventory(list_payload())), cases=(_po_case(),))

    written = write(target, inventory=from_inventory(list_payload()), out_dir=tmp_path / "p",
                    mode=MODE_PO, po=po)

    assert written.test_path
    assert written.locators_path == ""
    assert not (tmp_path / "p" / LOCATORS_FILENAME).exists()


# ---------------------------------------------------------------------------
# 6. 与 5.7 闭环的咬合: 渲染不出来的用例不能静默「通过」
# ---------------------------------------------------------------------------

def test_plan_verified_keeps_every_case_the_renderer_refused():
    inventory = from_inventory(list_payload())
    direct = render(plan(inventory), inventory=inventory)
    checked = plan_verified(inventory, render=make_renderer(inventory),
                            mode=MODE_STANDALONE, dedupe=False)

    assert len(checked.review) == len(direct.skipped)
    for case_id in direct.skipped:
        assert checked.find(case_id).needs_review, case_id


def test_verify_counts_render_problems_as_ast_problems():
    inventory = from_inventory(list_payload())
    base = plan(inventory)
    victim = base.cases[0]

    checked = verify(base, inventory=inventory, dedupe=False,
                     render_problems={victim.case_id: ["没有原语: throttle"]})

    assert checked.find(victim.case_id).needs_review
    assert any("没有原语" in line for line in checked.diagnostics["ast"])


# ---------------------------------------------------------------------------
# 7. 端到端: 落盘的代码真的能在页面上跑
# ---------------------------------------------------------------------------

CONFTEST = '''# -*- coding: utf-8 -*-
import pytest

from ai_kit.evaluation import driver_provider

URL = {url!r}


@pytest.fixture
def driver():
    drv = driver_provider.get_driver()
    if drv is None:
        pytest.skip("本机拉不起无头浏览器")
    drv.get(URL)
    yield drv
'''


def test_generated_module_runs_against_the_real_page(driver, html_page, tmp_path):
    """整条链路: 真页面采集 -> 计划 -> 渲染 -> 落盘 -> pytest 子进程跑产物"""
    from ai_kit.evaluation import driver_provider
    from ai_kit.perception import from_collect

    driver = html_page(LIST_HTML)
    inventory = from_collect(driver)

    target = plan(inventory)
    picked = (case_of(target, "query.filter", "关键字", "empty"),
              case_of(target, "sort.column", "名称", "asc"))
    out = tmp_path / "run"
    written = write(replace(target, cases=picked), inventory=inventory, out_dir=out)
    assert written.test_path and written.locators_path

    (out / "conftest.py").write_text(
        CONFTEST.format(url=driver_provider.to_file_url(str(tmp_path / "page.html"))),
        encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(out), "-q", "-p", "no:cacheprovider",
         "--tb=short"],
        cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 passed" in proc.stdout, proc.stdout
