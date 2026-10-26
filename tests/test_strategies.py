# -*- coding: utf-8 -*-
"""
策略模板库: 覆盖面 / 绑定 / 展开 / 报缺 / 裁剪

重点不是"有 8 个组", 而是四件容易做错的事:
1. **覆盖面**: 卡片 5.5 写的每个点都要在模板里找得到 (靠 groups 和变体 key 的断言钉住)。
2. **绑定**: 模板用角色而不是 eid; 绑定要能在"没有 filter-bar 类名""行内按钮被折叠"
   这些真实情形下退化, 而且 eid 重编号不能让 cid 变。
3. **报缺**: 页面上没有的对象必须进 missing, 不能悄悄少测。
4. **不造假**: 没 maxlength 就不出"边界长度", 纯文本框不出"错格式"。
真浏览器那半验证"角色 -> 元素"这条链在真页面确实接得上。
"""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from ai_kit.perception.archetype import ARCHETYPES
from ai_kit.perception.inventory import from_inventory
from ai_kit.perception.strategies import (
    CHECKLISTS,
    DESTRUCTIVE,
    LONG_FALLBACK,
    MAX_PER_STRATEGY,
    MUST,
    SAFE,
    SHOULD,
    SLOW,
    STRATEGY_VERSION,
    TARGETS,
    WRITE,
    Strategy,
    Variant,
    checklist,
    describe,
    expand,
    find,
    groups,
)


# ---------------------------------------------------------------------------
# 造数据 (collector 的 dict 形状)
# ---------------------------------------------------------------------------

def field(eid="f1", *, input_type="text", container="form", text="", name="",
          required=False, **extra):
    constraints = {"type": input_type, "required": required}
    constraints.update(extra)
    return {"eid": eid, "kind": "input", "tag": "input", "type": input_type,
            "text": text, "name": name, "container": container,
            "constraints": constraints}


def checkbox(eid="k1", *, container="table-row"):
    return {"eid": eid, "kind": "input", "tag": "input", "type": "checkbox",
            "container": container, "constraints": {"type": "checkbox"}}


def select(eid="s1", *, container="form", text="城市", options=("北京", "上海")):
    return {"eid": eid, "kind": "select", "tag": "select", "type": "", "text": text,
            "container": container, "options": list(options),
            "constraints": {"type": "select"}}


def button(eid="b1", text="按钮", *, container="page", risk=None):
    return {"eid": eid, "kind": "button", "tag": "button", "text": text,
            "container": container, "risk": risk}


def table(eid="c1", *, columns=("名称", "创建时间"), row_actions=(), row_count=3):
    actions = [dict(action) if isinstance(action, dict) else {"text": action}
               for action in row_actions]
    return {"eid": eid, "kind": "table", "tag": "table", "columns": list(columns),
            "row_count": row_count, "sample_rows": [["记录A", "2024-01-01"]],
            "row_actions": actions}


def page(*, inputs=(), buttons=(), selects=(), tables=(), charts=0,
         url="https://shop.example.com/order/list", title="订单列表"):
    return {"url": url, "title": title, "signals": {"charts": charts},
            "inputs": list(inputs), "buttons": list(buttons),
            "selects": list(selects), "tables": list(tables)}


def list_page(**overrides):
    """标准列表页: 筛选 + 表格 + 行内按钮 + 复选 + 批量删除"""
    payload = page(
        inputs=[field("e1", container="filter-bar", text="关键字", maxlength=32),
                checkbox("e2")],
        buttons=[button("e3", "新增", container="toolbar", risk=WRITE),
                 button("e4", "重置", container="filter-bar", risk=DESTRUCTIVE),
                 button("e5", "编辑", container="table-row", risk=WRITE),
                 button("e6", "删除", container="table-row", risk=DESTRUCTIVE),
                 button("e7", "批量删除", container="toolbar", risk=DESTRUCTIVE)],
        tables=[table(row_actions=[{"text": "编辑", "eid": "e5"},
                                   {"text": "删除", "eid": "e6"}])],
    )
    payload.update(overrides)
    return payload


def renumber(payload, prefix="z"):
    """模拟"同一页面重采一遍, eid 全体重编号"""
    if isinstance(payload, dict):
        return {key: (prefix + value if key == "eid" and isinstance(value, str)
                      else renumber(value, prefix))
                for key, value in payload.items()}
    if isinstance(payload, list):
        return [renumber(item, prefix) for item in payload]
    return payload


def login_page():
    return page(
        inputs=[field("u1", text="用户名", container="page"),
                field("p1", text="密码", input_type="password", container="page")],
        buttons=[button("b1", "登录")],
    )


def form_page():
    return page(
        inputs=[field("f1", text="用户名", required=True, maxlength=10),
                field("f2", text="备注", required=True)],
        selects=[select("f3", text="城市")],
        buttons=[button("f4", "保存", container="form", risk=WRITE),
                 button("f5", "取消", container="form"),
                 button("f6", "重置", container="form", risk=DESTRUCTIVE)],
    )


# ---------------------------------------------------------------------------
# 1. 模板自检: 数据本身别写错
# ---------------------------------------------------------------------------

def test_checklists_cover_every_archetype():
    assert set(CHECKLISTS) == set(ARCHETYPES)


def test_every_strategy_points_at_a_known_target():
    broken = [(strategy.key, strategy.target)
              for group in CHECKLISTS.values() for strategy in group
              if strategy.target not in TARGETS]

    assert broken == [], "模板里的 target 写错会静默少测, 必须钉住"


def test_strategy_keys_are_globally_unique():
    keys = [strategy.key for group in CHECKLISTS.values() for strategy in group]

    assert len(keys) == len(set(keys)), "key 撞了 -> cid 撞 -> 用例互相覆盖"


def test_priorities_and_risks_are_in_range():
    for group in CHECKLISTS.values():
        for strategy in group:
            assert strategy.priority in (MUST, SHOULD, SLOW), strategy.key
            assert strategy.risk in (SAFE, WRITE, DESTRUCTIVE), strategy.key


def test_variant_keys_are_unique_inside_a_strategy():
    for group in CHECKLISTS.values():
        for strategy in group:
            keys = [variant.key for variant in strategy.variants]
            assert len(keys) == len(set(keys)), strategy.key
            assert all(variant.title for variant in strategy.variants), strategy.key


def test_list_crud_covers_the_card_checklist():
    """卡片 5.5 第一张表的 8 组, 顺序也照抄 (报告里按这个顺序读)"""
    assert groups("list_crud") == ("query", "pagination", "sort", "row_actions",
                                   "batch", "state", "consistency", "security")


def test_list_crud_variants_match_the_card():
    assert [v.key for v in find("query.filter").variants] == \
           ["empty", "valid", "invalid", "boundary", "sqli", "xss", "combo"]
    assert [v.key for v in find("pagination.jump").variants] == \
           ["first", "last", "middle", "out_of_range", "page_size"]
    assert [v.key for v in find("sort.column").variants] == ["asc", "desc"]
    assert [v.key for v in find("row_actions.each").variants] == \
           ["normal", "no_permission", "double_click"]
    assert [v.key for v in find("batch.delete").variants] == ["confirm", "cancel"]
    assert [v.key for v in find("security.text_input").variants] == \
           ["too_long", "emoji", "spaces", "script"]


def test_form_covers_the_card_checklist():
    assert set(groups("form")) == {"required", "format", "dependency", "persist",
                                   "submit", "reset", "cancel"}


def test_other_archetypes_have_three_to_five_groups():
    for archetype in ("login", "detail", "dashboard"):
        assert 3 <= len(groups(archetype)) <= 5, archetype


def test_unknown_has_a_generic_smoke_list():
    """LLM 关掉时 unknown 也得有东西可跑 —— 空计划比冒烟更糟"""
    assert groups("unknown") == ("smoke", "security")
    assert find("smoke.load").target == "page"


def test_describe_mentions_every_group():
    for archetype in ARCHETYPES:
        text = describe(archetype)
        for group in groups(archetype):
            assert f"[{group}]" in text, archetype


def test_checklist_is_read_only_data():
    assert checklist("list_crud") is CHECKLISTS["list_crud"]
    assert checklist("no-such-archetype") == ()
    assert groups("no-such-archetype") == ()
    assert find("query.filter").group == "query"
    assert find("query.filter", "form") is None, "form 里没有这条"


# ---------------------------------------------------------------------------
# 2. 绑定: 角色 -> 页面对象
# ---------------------------------------------------------------------------

def test_filter_field_prefers_the_filter_bar():
    inv = from_inventory(page(inputs=[field("e1", container="filter-bar", text="关键字"),
                                      field("e2", container="page", text="备注")]))

    assert [binding.ref for binding in TARGETS["filter_field"](inv)] == ["e1"]


def test_filter_field_falls_back_to_page_level_inputs():
    """站点不给 filter-bar 类名时不能一条筛选项都绑不上"""
    inv = from_inventory(page(inputs=[field("e1", container="page", text="关键字")]))

    assert [binding.ref for binding in TARGETS["filter_field"](inv)] == ["e1"]


def test_filter_field_keeps_selects_but_drops_checkboxes():
    inv = from_inventory(page(inputs=[field("e1", container="filter-bar", text="关键字"),
                                      checkbox("k1", container="filter-bar")],
                              selects=[select("s1", container="filter-bar")]))

    assert [binding.ref for binding in TARGETS["filter_field"](inv)] == ["e1", "s1"]


def test_columns_are_namespaced_by_table():
    """两张表都有"名称"列, ref 不能撞 (撞了就是给错表排序)"""
    inv = from_inventory(page(tables=[table("c1", columns=("名称",)),
                                      table("c2", columns=("名称",))]))

    refs = [binding.ref for binding in TARGETS["column"](inv)]
    assert refs == ["c1.column:名称", "c2.column:名称"]
    assert len(set(refs)) == 2


def test_row_action_keeps_eid_and_falls_back_to_text():
    inv = from_inventory(page(
        buttons=[button("e5", "编辑", container="table-row", risk=WRITE)],
        tables=[table(row_actions=[{"text": "编辑", "eid": "e5"},
                                   {"text": "删除"}])]))

    bindings = TARGETS["row_action"](inv)
    assert [binding.ref for binding in bindings] == ["e5", "c1.row:删除"]
    assert bindings[0].element is not None, "有 eid 就要拿到原件 (风险/约束从原件读)"


def test_delete_button_ignores_reset_even_though_reset_is_destructive():
    """采集器把'重置'也算 destructive, 批量删除不能因此绑到重置上"""
    inv = from_inventory(list_page())

    refs = [binding.ref for binding in TARGETS["delete_button"](inv)]
    assert refs == ["e7"], "只有批量删除, 不含重置, 也不含行内删除"


def test_delete_button_skips_row_level_buttons():
    inv = from_inventory(page(
        buttons=[button("e6", "删除", container="table-row", risk=DESTRUCTIVE),
                 button("e8", "删除", container="page", risk=DESTRUCTIVE)]))

    assert [binding.ref for binding in TARGETS["delete_button"](inv)] == ["e8"]


def test_checkbox_binding_only_takes_checks():
    inv = from_inventory(page(inputs=[checkbox("k1"), field("e1", text="关键字")]))

    assert [binding.ref for binding in TARGETS["checkbox"](inv)] == ["k1"]


def test_credential_pairs_the_username_field():
    inv = from_inventory(login_page())

    bindings = TARGETS["credential"](inv)
    assert [binding.ref for binding in bindings] == ["p1"]
    assert bindings[0].refs == ("u1",), "登录永远是一对输入, 用户名要一起带上"


def test_submit_prefers_save_words_and_falls_back_to_form_actions():
    named = from_inventory(page(buttons=[button("b1", "查询", container="toolbar"),
                                         button("b2", "保存", container="form", risk=WRITE)]))
    loose = from_inventory(page(buttons=[button("b3", "下一步", container="form")]))

    assert [binding.ref for binding in TARGETS["submit"](named)] == ["b2"]
    assert [binding.ref for binding in TARGETS["submit"](loose)] == ["b3"]


def test_chart_binding_follows_the_signal():
    inv = from_inventory(page(charts=2))

    assert [binding.ref for binding in TARGETS["chart"](inv)] == ["chart:1", "chart:2"]


def test_empty_page_binds_nothing():
    inv = from_inventory({})

    for name, target in TARGETS.items():
        if name == "page":
            continue
        assert target(inv) == (), name


# ---------------------------------------------------------------------------
# 3. 展开: 条数 / 去重 / 稳定
# ---------------------------------------------------------------------------

def test_expand_counts_add_up():
    """一页 44 条: query 9 / pagination 6 / sort 7 / row_actions 6 /
    batch 5 / state 4 / consistency 3 / security 4 —— "越详细越好"的量级"""
    result = expand(list_page())

    assert result.archetype == "list_crud"
    assert result.stats == {"query": 9, "pagination": 6, "sort": 7, "row_actions": 6,
                            "batch": 5, "state": 4, "consistency": 3, "security": 4}
    assert result.total == 44
    assert result.missing == ()


def test_cids_are_unique_and_readable():
    result = expand(list_page(), "list_crud")

    cids = [case.cid for case in result.cases]
    assert len(cids) == len(set(cids))
    assert "list_crud.query.filter.关键字.empty" in cids
    assert "list_crud.sort.column.创建时间.asc" in cids


def test_cids_survive_eid_renumbering():
    """用例 id 不能跟着 eid 走: 同一页面两次采集 eid 可能重编号"""
    first = expand(list_page())
    second = expand(renumber(list_page()))

    assert [case.cid for case in first] == [case.cid for case in second]
    assert [case.ref for case in first] != [case.ref for case in second], \
        "ref 该跟着 eid 变, cid 不该"


def test_each_case_carries_a_target_and_an_expectation():
    result = expand(list_page())

    for case in result.cases:
        assert case.ref, case.cid
        assert case.label, case.cid
        assert case.expect, f"{case.cid} 没有期望就是一条永远绿的假用例"
        assert case.group in groups("list_crud")


def test_duplicate_labels_do_not_collide():
    inv = from_inventory(page(
        buttons=[button("e5", "编辑", container="table-row", risk=WRITE),
                 button("e6", "编辑", container="table-row", risk=WRITE)],
        tables=[table(row_actions=[{"text": "编辑", "eid": "e5"},
                                   {"text": "编辑", "eid": "e6"}])]))

    cids = [case.cid for case in expand(inv, "list_crud")]
    assert len(cids) == len(set(cids))


def test_expand_accepts_a_collector_dict():
    assert [case.cid for case in expand(list_page())] == \
           [case.cid for case in expand(from_inventory(list_page()))]


def test_archetype_comes_from_the_inventory_when_present():
    inv = from_inventory(list_page(), archetype="form")

    assert expand(inv).archetype == "form", "清单说自己是表单页, 就别再去猜"


def test_nonsense_archetype_falls_back_to_classification():
    assert expand(list_page(), "no_such_archetype").archetype == "list_crud"


def test_unknown_page_still_gets_smoke_cases():
    result = expand(from_inventory({}))

    assert result.archetype == "unknown"
    assert [case.strategy for case in result.cases] == ["smoke.load"]
    assert any("smoke.interactive" in item for item in result.missing), \
        "缺的要说出来, 不是悄悄不出"


# ---------------------------------------------------------------------------
# 4. 报缺 / 裁剪
# ---------------------------------------------------------------------------

def test_missing_targets_are_reported():
    """只有筛选框的页面上, 分页/排序/状态都无从测 —— 必须进 missing"""
    result = expand(page(inputs=[field("e1", container="filter-bar", text="关键字",
                                       maxlength=32)]),
                    "list_crud")

    missing = " ".join(result.missing)
    for key in ("query.reset", "pagination.jump", "sort.column", "row_actions.each",
                "batch.select", "state.empty", "consistency.three_way"):
        assert key in missing, missing
    assert result.stats["query"] == 7, "报缺的同时该出的还得照出"


def test_limit_keeps_group_breadth():
    """不能 cases[:12] —— 那会把 security/consistency 整组砍掉"""
    result = expand(list_page(), limit=12)

    assert result.total == 12
    assert set(result.stats) == set(groups("list_crud")), "每组至少留一条"
    assert max(result.stats.values()) == 2
    assert any("轮转" in note for note in result.notes)


def test_max_per_strategy_caps_the_fan_out():
    inv = from_inventory(page(
        inputs=[field("e1", container="filter-bar", text="关键字", maxlength=32),
                field("e2", container="filter-bar", text="状态", maxlength=16),
                field("e3", container="filter-bar", text="客户", maxlength=64)]))

    capped = expand(inv, "list_crud", max_per_strategy=2)
    assert capped.stats["query"] == 2 * 7, "只展开前 2 个筛选项"
    assert any("只展开前 2 个" in note for note in capped.notes)

    full = expand(inv, "list_crud", max_per_strategy=MAX_PER_STRATEGY)
    assert full.stats["query"] == 3 * 7


def test_include_slow_drops_the_perf_baseline():
    fast = expand(list_page(), include_slow=False)

    assert fast.total == 43
    assert all(case.priority != SLOW for case in fast)


def test_groups_filter_limits_the_checklist():
    result = expand(list_page(), groups=["batch"])

    assert set(result.stats) == {"batch"}
    assert all(case.group == "batch" for case in result.cases)


# ---------------------------------------------------------------------------
# 5. 不造假: 约束不够就不出那条用例
# ---------------------------------------------------------------------------

def test_maxlength_gives_a_boundary_case_with_real_length():
    case = next(case for case in expand(list_page()) if case.cid.endswith(".boundary"))

    assert case.data["token"] == "@maxlength"
    assert case.data["length"] == 32 and case.data["limit"] == 32
    assert case.data["type"] == "text"


def test_no_maxlength_means_no_boundary_case():
    payload = list_page(inputs=[field("e1", container="filter-bar", text="关键字")])

    cids = [case.cid for case in expand(payload)]
    assert not any(cid.endswith(".boundary") for cid in cids)


def test_too_long_still_runs_without_maxlength():
    """超长测的是服务端健壮性, 没有 maxlength 也要测 —— 长度用兜底值"""
    payload = list_page(inputs=[field("e1", container="filter-bar", text="关键字")])
    case = next(case for case in expand(payload) if case.cid.endswith(".too_long"))

    assert case.data["length"] == LONG_FALLBACK
    assert "limit" not in case.data


def test_plain_text_field_gets_no_format_case():
    """备注框没有 pattern/min/max, 配"错格式"只会永远绿"""
    inv = from_inventory(form_page())

    format_cids = [case.cid for case in expand(inv) if case.group == "format"]
    assert "form.format.each.用户名.wrong" in format_cids
    assert not any("备注" in cid for cid in format_cids if ".each." in cid)


def test_min_and_max_constraints_produce_both_bounds():
    inv = from_inventory(page(inputs=[field("n1", text="数量", input_type="number",
                                            min=1, max=10)],
                              buttons=[button("b1", "保存", container="form", risk=WRITE)]))

    cases = {case.cid.rsplit(".", 1)[-1]: case for case in expand(inv)
             if case.strategy == "format.each"}
    assert cases["min_minus_1"].data["number"] == 0
    assert cases["max_plus_1"].data["number"] == 11
    assert cases["wrong"].data["token"] == "@invalid_format"


def test_required_field_case_targets_only_required_fields():
    inv = from_inventory(page(
        inputs=[field("f1", text="用户名", required=True),
                field("f2", text="备注")],
        buttons=[button("b1", "保存", container="form", risk=WRITE)]))

    refs = {case.ref for case in expand(inv) if case.strategy == "required.each"}
    assert refs == {"f1"}


# ---------------------------------------------------------------------------
# 6. 风险 / 优先级 / 结构
# ---------------------------------------------------------------------------

def test_batch_delete_is_destructive():
    cases = [case for case in expand(list_page()) if case.strategy == "batch.delete"]

    assert len(cases) == 2
    assert all(case.risk == DESTRUCTIVE for case in cases)
    assert {case.data["confirm"] for case in cases} == {True, False}


def test_row_level_delete_is_destructive_even_though_template_is_safe():
    """模板写 safe, 但目标是行内"删除" —— 必须按元素的风险标高"""
    cases = [case for case in expand(list_page())
             if case.strategy == "row_actions.each" and case.label == "删除"]

    assert cases and all(case.risk == DESTRUCTIVE for case in cases)


def test_variant_priority_overrides_strategy_priority():
    slow = [case for case in expand(list_page()) if case.strategy == "state.perf"]
    must = [case for case in expand(list_page()) if case.strategy == "query.filter"]
    should = [case for case in must if case.cid.endswith(".boundary")]

    assert all(case.priority == SLOW for case in slow)
    assert should[0].priority == SHOULD, "变体自己的优先级要盖住策略的"


def test_login_expansion_is_small_but_complete():
    result = expand(login_page())

    assert result.archetype == "login"
    assert set(result.stats) == {"credentials", "lockout", "session", "security"}
    assert result.missing == ()

    for case in result.cases:
        if case.strategy == "credential.negative":
            assert case.refs == ("u1",), "错误凭证用例要同时动用户名和密码"


def test_detail_expansion_flags_the_missing_delete_path():
    inv = from_inventory(page(buttons=[button("d1", "编辑", container="toolbar", risk=WRITE),
                                       button("d2", "返回", container="toolbar")]))

    result = expand(inv)
    assert result.archetype == "detail"
    assert any("delete_confirm" in item for item in result.missing)

    forced = expand(inv, "detail", groups=["actions"])
    assert forced.stats["actions"] == 4, "编辑 × 2 变体 + 返回 × 2 变体"
    assert any("delete_confirm" in item for item in forced.missing)


def test_expansion_helpers_agree_with_the_cases():
    result = expand(list_page())

    assert sum(result.stats.values()) == result.total == len(result.cases) == len(result)
    assert set(result.by_group()) == set(groups("list_crud"))
    assert 0 < len(result.of_priority(MUST)) < result.total, "既有必测也有建议项"


def test_to_dict_is_json_serializable():
    result = expand(list_page())
    payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))

    assert payload["archetype"] == "list_crud"
    assert payload["version"] == STRATEGY_VERSION, "缓存键要带模板版本, 否则改模板不生效"
    assert payload["total"] == result.total
    assert payload["cases"][0]["cid"] == result.cases[0].cid
    assert set(payload["cases"][0]) >= {"cid", "group", "ref", "data", "expect", "risk"}


def test_strategy_and_variant_serialise_together():
    payload = find("query.filter").to_dict()

    assert payload["target"] == "filter_field"
    assert payload["variants"][0]["key"] == "empty"


def test_result_objects_are_frozen():
    result = expand(list_page())

    with pytest.raises(FrozenInstanceError):
        setattr(result, "archetype", "form")
    with pytest.raises(FrozenInstanceError):
        setattr(result.cases[0], "risk", SAFE)


def test_templates_are_plain_data():
    """模板里不能混行为 (回调/正则), 否则"只改数据"这句话就不成立了"""
    for group in CHECKLISTS.values():
        for strategy in group:
            assert isinstance(strategy, Strategy)
            for variant in strategy.variants:
                assert isinstance(variant, Variant)
                assert isinstance(dict(variant.data), dict)


# ---------------------------------------------------------------------------
# 7. 真浏览器: 角色 -> 元素 这条链要接得上
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def driver():
    from ai_kit.evaluation import driver_provider

    drv = driver_provider.get_driver()
    if drv is None:
        pytest.skip("本机拉不起无头 Edge/Chrome, 跳过真页面展开")
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


def test_expand_on_a_real_list_page(html_page):
    from ai_kit.perception import from_collect

    inv = from_collect(html_page(LIST_HTML))
    result = expand(inv)

    assert inv.archetype == "", "采集还没分类, 展开时就地分类"
    assert result.archetype == "list_crud"
    assert result.missing == (), result.missing
    assert result.total > 30
    assert result.stats["batch"] == 5, "两个复选 × 3 + 批量删除 × 2"


def test_every_real_case_points_at_a_real_object(html_page):
    from ai_kit.perception import from_collect

    inv = from_collect(html_page(LIST_HTML))
    result = expand(inv)

    for case in result.cases:
        assert case.ref
        if case.ref in ("page",) or case.ref.startswith(("chart:", "column:", "row:")) \
                or ".column:" in case.ref or ".row:" in case.ref:
            continue
        assert inv.lookup(case.ref) is not None, f"{case.cid} 指向了不存在的 {case.ref}"


def test_real_row_actions_are_referenceable(html_page):
    from ai_kit.perception import from_collect

    inv = from_collect(html_page(LIST_HTML))
    cases = [case for case in expand(inv) if case.kind == "row-action"]

    assert cases, "行内按钮是 CRUD 用例的抓手, 一条都绑不上就白采了"
    assert all(inv.lookup(case.ref) is not None for case in cases)
    assert {case.label for case in cases} == {"编辑", "删除"}


def test_real_login_page_expands_credentials(html_page):
    from ai_kit.perception import from_collect

    inv = from_collect(html_page(
        "<form><input name='username' placeholder='用户名'>"
        "<input type='password' name='password'><button>登录</button></form>"))
    result = expand(inv)

    assert result.archetype == "login"
    assert result.stats["credentials"] == 7
    for case in result.cases:
        for ref in (case.ref, *case.refs):
            if ref == "page":
                continue
            assert inv.lookup(ref) is not None, ref


def test_real_form_page_expands_required_fields(html_page):
    from ai_kit.perception import from_collect

    inv = from_collect(html_page(
        "<form><input name='user' placeholder='用户名' required>"
        "<input name='mail' placeholder='邮箱' required>"
        "<button>保存</button><button>取消</button></form>"))
    result = expand(inv)

    assert result.archetype == "form"
    assert result.stats["required"] == 2 * 2 + 1, "两个必填 × 2 + 全空提交 1 条"
    assert result.stats["cancel"] == 1
