# -*- coding: utf-8 -*-
"""
5.10 的 prompt 五条约束 —— 每条一个测试, 改 prompt 时这些断言就是回归网

1. system 只写硬约束: 只能用清单里的 eid / 只能用列出的 PO 方法 / 不许写代码
2. user 只含五块事实 (原型 / checklist / 清单 JSON / PO 签名 / schema)
3. temperature=0.1, max_tokens 显式给
4. 两步法: 模型只产用例清单, 输出 schema 与校验器同源
5. 重试时把校验错误原文 + 归类句一起回填

关键点在于: 这里断言的是「真实校验器产出的文案能被 prompt 认出来」, 不是我自己
编一条错误再自己认出来。改 planner 的报错措辞而没回来改 prompts 的正则,
下面几条会直接红。
"""
import json
import re
from dataclasses import replace

from test_planner import list_payload

from ai_kit.perception import planner as P
from ai_kit.perception import prompts as PR
from ai_kit.perception.inventory import from_inventory

FACT_HEADS = ("页面原型", "策略 checklist", "元素清单 (JSON)", "输出 schema")


# ---------------------------------------------------------------------------
# 造数据
# ---------------------------------------------------------------------------

def heads(text):
    return [line[3:] for line in text.splitlines() if line.startswith("## ")]


def prompt_for(payload=None, **kwargs):
    """(清单, 计划, 本轮要改的用例的 prompt)"""
    inv = from_inventory(payload if payload is not None else list_payload())
    base = P.plan(inv)
    return inv, base, P._llm_prompt(inv, base, base.cases[:2], **kwargs)


# ---------------------------------------------------------------------------
# 1. system: 约束写死在 system, 事实一条都不许有
# ---------------------------------------------------------------------------

def test_system_forbids_inventing_eids_and_methods():
    text = PR.PLANNER_SYSTEM

    assert "只输出 JSON, 不写代码" in text
    assert "只能使用 user 消息「元素清单」里出现过的 eid" in text
    assert "清单里没有的元素一律不许使用" in text
    assert "没列出的方法名一律不许出现" in text
    assert "不许发明元素、方法、定位串" in text


def test_system_carries_no_page_facts_and_no_concrete_eid():
    """system 里出现具体 eid, 模型就会把「示例里的」当成「清单里的」"""
    text = PR.PLANNER_SYSTEM

    assert not re.search(r"\be\d+\b", text), "system 里不许出现具体的 eid, 示例也不行"
    assert "患者列表" not in text
    assert "https://" not in text


def test_system_says_less_is_fine():
    """宁可少写一条, 也不许为凑数编一条永远红的用例"""
    assert "少一条用例" in PR.PLANNER_SYSTEM


# ---------------------------------------------------------------------------
# 2. user: 五块事实, 顺序固定, 不夹带别的东西
# ---------------------------------------------------------------------------

def test_user_prompt_has_the_four_fact_blocks_in_order_without_po():
    _, _, text = prompt_for()

    assert heads(text)[:4] == list(FACT_HEADS)
    assert heads(text)[4].startswith("本轮要改的用例")
    assert not any("PO 方法签名" in head for head in heads(text)), \
        "非 po 模式不该出现 PO 段 (写「不适用」也是噪音)"


def test_po_mode_inserts_the_signature_block_before_the_schema():
    _, _, text = prompt_for(po_methods=("search_by_name",),
                            po_signatures=("search_by_name(name, size=...)",))

    assert heads(text)[:4] == list(FACT_HEADS[:3]) + ["PO 方法签名 (只能用这些)"]
    assert heads(text)[4] == "输出 schema"
    assert "- search_by_name(name, size=...)" in text


def test_po_block_without_signatures_does_not_claim_zero_arguments():
    """只给方法名就写 name(...): ③ 拦得住不存在的方法, 拦不住参数个数传错"""
    assert "- search_by_name(...)" in PR.po_block(("search_by_name",))


def test_facts_are_ordered_the_same_way_every_time():
    """顺序变了 diff 会整段变红, 缓存与人工审阅都靠这个稳定性"""
    first, second = prompt_for()[2], prompt_for()[2]

    assert first == second


def test_the_model_never_sees_a_locator():
    """定位串只属于 locators.json; 给了模型, 它就会把 xpath 抄进 value"""
    inv = from_inventory(list_payload())
    element = inv.element("e1")
    leaked = replace(element, locators={"css": "#kw"}, locator_by={"css": "css"},
                     html="<input id='kw'>", attrs={"id": "kw"})
    inv = replace(inv, elements=(leaked,) + inv.elements[1:])

    text = json.dumps(PR.slim_inventory(inv), ensure_ascii=False)

    for secret in ("#kw", "<input", "data-ai-eid", "css", "xpath"):
        assert secret not in text, f"清单摘要里漏了 {secret!r}"


def test_slim_inventory_keeps_only_decidable_fields():
    inv = from_inventory(list_payload())
    row = PR.slim_inventory(inv)["elements"][0]

    assert set(row) <= {"eid", "kind", "type", "name", "container", "risk",
                        "options", "constraints"}, row


def test_checklist_comes_from_the_strategy_table():
    """策略清单是事实也是护栏: 空着就等于让模型自己发明测试点"""
    _, base, text = prompt_for()

    strategies = P.CHECKLISTS[base.archetype]
    assert strategies
    for strategy in strategies:
        assert strategy.title in text, strategy.title


def test_empty_checklist_says_so_instead_of_leaving_a_blank_section():
    assert "按元素清单里的可交互控件保守生成" in PR.checklist_block(())


# ---------------------------------------------------------------------------
# 3. temperature / max_tokens 显式给
# ---------------------------------------------------------------------------

def test_temperature_is_low():
    assert PR.TEMPERATURE == 0.1, "代码生成要保守, 高温度会冒出『灵机一动』的步骤"


def test_max_tokens_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.delenv(PR.MAX_TOKENS_ENV, raising=False)
    assert PR.max_tokens() == PR.DEFAULT_MAX_TOKENS

    for broken in ("", "abc", "0", "-5", " "):
        monkeypatch.setenv(PR.MAX_TOKENS_ENV, broken)
        assert PR.max_tokens() == PR.DEFAULT_MAX_TOKENS, broken


def test_max_tokens_can_be_overridden(monkeypatch):
    monkeypatch.setenv(PR.MAX_TOKENS_ENV, "512")

    assert PR.max_tokens() == 512


def test_the_real_call_always_sends_both_parameters(monkeypatch):
    """不显式给上限 = 把这次调用的花费交给服务端默认值"""
    from ai_kit import client

    seen = {}

    def spy(system_prompt, user_prompt, max_retries=2, *, temperature=1.0, max_tokens=None):
        seen.update(system=system_prompt, user=user_prompt, max_retries=max_retries,
                    temperature=temperature, max_tokens=max_tokens)
        return {"cases": []}

    monkeypatch.setattr(client, "chat_json", spy)
    monkeypatch.delenv(PR.MAX_TOKENS_ENV, raising=False)

    assert P._ask_llm("user 消息") == {"cases": []}
    assert seen["system"] == PR.PLANNER_SYSTEM
    assert seen["temperature"] == 0.1
    assert seen["max_tokens"] == PR.DEFAULT_MAX_TOKENS
    assert seen["max_retries"] == 0, "重试归 planner 的闭环管, client 别再自己重试一轮"


# ---------------------------------------------------------------------------
# 4. 两步法: 模型只产清单, schema 与校验器同源
# ---------------------------------------------------------------------------

def test_output_schema_is_the_subset_the_validator_accepts():
    case = PR.output_schema()["cases"][0]

    assert set(case) <= set(P.CASE_KEYS) | {"reason"}, "schema 里出现了校验器不认的键"
    assert list(case["steps"][0]) == list(P.STEP_KEYS)


def test_the_prompt_never_fences_anything_but_json():
    _, _, text = prompt_for()

    assert "```json" in text
    assert "```python" not in text, "给模型一个 python 代码块, 它就真的开始写代码了"


def test_every_case_block_example_uses_an_eid_placeholder():
    """schema 里的例子必须写 <清单里的 eid>: 写 e12 会被当成合法值照抄"""
    assert '"eid": "<清单里的 eid>"' in PR.PLANNER_SYSTEM


# ---------------------------------------------------------------------------
# 5. 重试回填: 真实校验文案 + 归类句
# ---------------------------------------------------------------------------

def real_eid_problem():
    """真实来源: validate_step 对不存在的 eid 说的话"""
    inv = from_inventory(list_payload())
    return P.validate_step(inv, P.Step("click", "e99"))


def test_a_real_validator_message_is_summarised_as_a_missing_eid():
    block = PR.errors_block({"LC-002": [f"步骤 2 (click): {real_eid_problem()}"]})

    assert "以下 eid 不存在: e99" in block


def test_a_real_lint_message_is_summarised_as_a_missing_po_method():
    problems = P.lint_source("def test_x():\n    page.foo_bar()\n",
                             mode="po", po_methods=("search_by_name",))

    block = PR.errors_block({"LC-003": [str(problem) for problem in problems]})

    assert "以下 PO 方法不存在: foo_bar" in block


def test_a_bare_unknown_call_is_also_caught():
    problems = P.lint_source("def test_x():\n    search_by_magic()\n", mode="po")

    block = PR.errors_block({"LC-003": [str(problem) for problem in problems]})

    assert "以下 PO 方法不存在: search_by_magic" in block


def test_the_original_lines_are_kept_next_to_the_summary():
    """归纳句让模型看到类别, 原文让它看到位置 —— 少了原文就不知道改哪一步"""
    errors = {"LC-002": ["步骤 2 (click): click 的 eid 'e99' 不在元素清单里"]}

    block = PR.errors_block(errors)

    assert "- LC-002: 步骤 2 (click): click 的 eid 'e99' 不在元素清单里" in block
    assert block.index("以下 eid 不存在") < block.index("原文:")


def test_unrecognised_problems_are_not_force_fitted_into_a_category():
    """认不出来就不归类: 硬凑一句「只有 eid 有问题」会让模型漏改"""
    block = PR.errors_block({"LC-004": ["优先级 'P9' 不在 P0..P3 里"]})

    assert "以下 eid 不存在" not in block
    assert "以下 PO 方法不存在" not in block
    assert "优先级 'P9' 不在 P0..P3 里" in block


def test_nothing_to_repair_means_no_block_at_all():
    assert PR.errors_block(None) == ""
    assert PR.errors_block({}) == ""


def test_retry_prompt_carries_the_previous_round_errors():
    inv, base, _ = prompt_for()
    errors = {"LC-001": [f"步骤 2 (click): {real_eid_problem()}"]}

    text = P._llm_prompt(inv, base, base.cases[:1], errors=errors)

    assert heads(text)[-1].startswith("上次校验没通过的地方")
    assert "以下 eid 不存在: e99" in text


def test_a_clean_round_has_no_errors_block():
    _, _, text = prompt_for()

    assert not any("校验没通过" in head for head in heads(text))


# ---------------------------------------------------------------------------
# PO 签名: 扫 pages/ 只读 AST
# ---------------------------------------------------------------------------

def write_pages(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "patient.py").write_text(
        "class PatientListPage:\n"
        "    def __init__(self, driver):\n"
        "        self.driver = driver\n"
        "\n"
        "    def search_by_name(self, name, size=10):\n"
        "        pass\n"
        "\n"
        "    @staticmethod\n"
        "    def open_page():\n"
        "        pass\n"
        "\n"
        "    def _private(self):\n"
        "        pass\n",
        encoding="utf-8")
    return pages


def test_signatures_are_collected_with_parameter_names(tmp_path):
    signatures = P.po_signatures_from_dir(write_pages(tmp_path))

    assert "search_by_name(name, size=...)" in signatures
    assert "open_page()" in signatures


def test_private_helpers_are_not_offered_to_the_model(tmp_path):
    signatures = P.po_signatures_from_dir(write_pages(tmp_path))

    assert not [row for row in signatures if row.startswith("_")], signatures


def test_a_missing_pages_dir_is_empty_not_an_error(tmp_path):
    assert P.po_signatures_from_dir(tmp_path / "nope") == ()


def test_signatures_land_in_the_po_block(tmp_path):
    signatures = P.po_signatures_from_dir(write_pages(tmp_path))

    _, _, text = prompt_for(po_methods=("search_by_name",), po_signatures=signatures)

    assert "search_by_name(name, size=...)" in text
    assert "没列出的方法一律不许使用" in text


# ---------------------------------------------------------------------------
# 原型兜底: 计划的 archetype 为空/unknown 时, checklist 也不许空
# ---------------------------------------------------------------------------

def test_an_unknown_page_still_gets_its_generic_checklist():
    """unknown 也是 CHECKLISTS 的一格 (「页面能打开」这类底线), 不是空"""
    inv = from_inventory(list_payload())
    base = replace(P.plan(inv), archetype=P.UNKNOWN)

    text = P._llm_prompt(inv, base, base.cases[:1])
    lines = text.splitlines()

    assert lines[lines.index("## 页面原型") + 1] == P.UNKNOWN
    assert "按元素清单里的可交互控件保守生成" not in text
    for strategy in P.CHECKLISTS[P.UNKNOWN]:
        assert strategy.title in text, strategy.title


def test_a_plan_without_an_archetype_falls_back_to_the_rules():
    """计划里 archetype 是空串时不能把空段丢给模型: 现场分类一次就是了"""
    inv = from_inventory(list_payload())
    base = replace(P.plan(inv), archetype="")

    text = P._llm_prompt(inv, base, base.cases[:1])
    lines = text.splitlines()

    assert lines[lines.index("## 页面原型") + 1] == P.classify(inv).archetype
    for strategy in P.CHECKLISTS[P.classify(inv).archetype]:
        assert strategy.title in text, strategy.title


def test_the_archetype_block_never_renders_empty():
    assert PR.archetype_block("") == "## 页面原型\n(未分类)"
