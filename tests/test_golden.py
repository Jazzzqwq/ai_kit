# -*- coding: utf-8 -*-
"""
黄金集 schema (7.2) 自检 — 归一化 / 校验 / 规模底线 / 真数据干净

跑: pytest tests/test_golden.py
"""
from __future__ import annotations

import json

import pytest

from ai_kit import analyzer
from ai_kit.evaluation import golden as G


# ---------------------------------------------------------------------------
# 归一化: 新 schema 直接用, 老形状搬字段
# ---------------------------------------------------------------------------

def test_a_legacy_flat_analyzer_case_is_moved_into_the_envelope():
    old = {"id": "A1", "traceback": "NoSuchElementException: nope",
           "log": "retry", "expected_category": "脚本-定位器问题", "note": "元素找不到"}

    case = G.normalize(old, "analyzer")

    assert case["input"] == {"traceback": "NoSuchElementException: nope", "log": "retry"}
    assert case["expected"]["category"] == "脚本-定位器问题"
    assert case["expected"]["acceptable_categories"] == []
    assert case["note"] == "元素找不到"


def test_the_envelope_defaults_are_filled_not_left_empty():
    """手写 case 不必每条都重复写 module/rule_version/source"""
    case = G.normalize({"id": "A1", "expected_category": "产品缺陷"}, "analyzer")

    assert case["module"] == "analyzer"
    assert case["rule_version"] == analyzer.RULES_VERSION, "analyzer 要跟着规则版本走"
    assert case["source"] == "manual"


def test_a_case_already_in_the_new_shape_is_untouched():
    new = {"id": "A1", "module": "analyzer", "rule_version": "rules.v9",
           "source": "flywheel", "input": {"traceback": "x", "log": ""},
           "expected": {"category": "环境问题", "acceptable_categories": []}}

    case = G.normalize(new, "analyzer")

    assert case["rule_version"] == "rules.v9" and case["source"] == "flywheel"


def test_the_provenance_fields_survive_normalization():
    """飞轮的 category_source / failure_id 不在 schema 里, 丢了就回不到那条决策"""
    case = G.normalize({"id": "FLY-1", "traceback": "x", "expected_category": "产品缺陷",
                        "category_source": "diagnosis@rules.v1", "failure_id": "LC-002"},
                       "analyzer")

    assert case["category_source"] == "diagnosis@rules.v1"
    assert case["failure_id"] == "LC-002"


def test_healer_and_generator_each_have_their_own_field_map():
    healer = G.normalize({"id": "H1", "fixture": "el_dialog.html", "broken_by": "css",
                          "broken_locator": ".el-dialog", "intent": "关闭",
                          "expected_locator": ".el-dialog__headerbtn",
                          "expected_target": "dialog-close", "layer_hint": "L1"}, "healer")
    gen = G.normalize({"id": "G1", "page_name": "用户管理", "po_module": "m",
                       "po_class": "C", "fixture_name": "f", "po_methods": "def a()",
                       "page_structure": {"title": "x"},
                       "expected_categories": ["add"], "expected_keywords": ["add"]},
                      "generator")

    assert healer["expected"]["target"] == "dialog-close"
    assert healer["input"]["broken_locator"] == ".el-dialog"
    assert gen["expected"]["keywords"] == ["add"]
    assert gen["input"]["page_structure"] == {"title": "x"}


def test_an_unknown_module_is_refused():
    with pytest.raises(G.GoldenError, match="未知 module"):
        G.normalize({"id": "x"}, "writer")


# ---------------------------------------------------------------------------
# 校验: 脏数据一次列清
# ---------------------------------------------------------------------------

def _case(**kw):
    base = {"id": "A1", "module": "analyzer", "rule_version": "rules.v1",
            "source": "manual", "input": {"traceback": "x", "log": ""},
            "expected": {"category": "产品缺陷", "acceptable_categories": []}}
    base.update(kw)
    return base


def test_clean_cases_have_no_problems():
    assert G.check([_case()], "analyzer") == []


def test_a_duplicate_id_is_reported():
    """去重全靠 id, 重复就分不清谁是谁"""
    problems = G.check([_case(), _case()], "analyzer")

    assert any("重复" in p for p in problems)


def test_a_case_without_a_category_is_reported():
    problems = G.check([_case(expected={"category": "", "acceptable_categories": []})],
                       "analyzer")

    assert any("expected.category 为空" in p for p in problems)


def test_tolerance_must_not_repeat_the_answer():
    problems = G.check([_case(expected={"category": "产品缺陷",
                                        "acceptable_categories": ["产品缺陷"]})],
                       "analyzer")

    assert any("不该再写一遍自己" in p for p in problems)


def test_a_module_mismatch_is_reported():
    problems = G.check([_case(module="healer")], "analyzer")

    assert any("与所在套件" in p for p in problems)


def test_an_unknown_source_is_reported():
    problems = G.check([_case(source="неизвестно")], "analyzer")

    assert any("source=" in p for p in problems)


# ---------------------------------------------------------------------------
# 真数据: 规模底线 + 全部干净
# ---------------------------------------------------------------------------

def test_the_analyzer_golden_set_meets_its_floor():
    cases = G.load("analyzer")

    stat = G.scale(cases, "analyzer")
    assert stat["n"] >= 30 and stat["ok"], stat
    assert G.check(cases, "analyzer") == []


def test_the_healer_golden_set_meets_its_floor():
    cases = G.load("healer")

    stat = G.scale(cases, "healer")
    assert stat["n"] >= 20 and stat["ok"], stat
    assert G.check(cases, "healer") == []


def test_every_analyzer_case_expects_a_known_category():
    cases = G.load("analyzer")

    unknown = [c["id"] for c in cases if c["expected"]["category"] not in analyzer.CATEGORIES]
    assert unknown == [], f"期望值必须是 analyzer 的六类之一: {unknown}"


def test_the_ambiguous_case_carries_its_tolerance():
    """A25: 规则先命中超时, 人判定位器 —— 容错写在黄金集里, 不是规则自己说的"""
    cases = {c["id"]: c for c in G.load("analyzer")}

    case = cases["A25_ambiguous_timeout_locator"]
    assert case["expected"]["category"] == "脚本-定位器问题"
    assert case["expected"]["acceptable_categories"] == ["脚本-等待问题"]


def test_extra_files_are_merged_in(tmp_path):
    (tmp_path / "analyzer_cases.json").write_text(json.dumps({"cases": [
        {"id": "M1", "traceback": "x", "expected_category": "产品缺陷"}]},
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "flywheel_positive.json").write_text(json.dumps({"cases": [
        {"id": "FLY-1", "module": "analyzer", "source": "flywheel",
         "input": {"traceback": "y", "log": ""},
         "expected": {"category": "产品缺陷"}}]}, ensure_ascii=False), encoding="utf-8")

    cases = G.load("analyzer", directory=str(tmp_path), extra=("flywheel_positive.json",))

    assert [c["id"] for c in cases] == ["M1", "FLY-1"]
    assert {c["source"] for c in cases} == {"manual", "flywheel"}


def test_a_missing_optional_file_is_just_empty(tmp_path):
    assert G.load("proposal", directory=str(tmp_path)) == []


def test_the_cli_passes_on_the_real_golden_set(capsys):
    assert G.main([]) == 0
    out = capsys.readouterr().out
    assert "analyzer" in out and "schema: 0 个问题" in out


def test_the_cli_fails_when_the_set_is_too_small(tmp_path, capsys):
    (tmp_path / "analyzer_cases.json").write_text(json.dumps({"cases": [
        {"id": "M1", "traceback": "x", "expected_category": "产品缺陷"}]},
        ensure_ascii=False), encoding="utf-8")

    code = G.main(["--dir", str(tmp_path)])

    assert code == 1, "样本不够就该让 CI 红, 而不是照样出数"
    assert "样本不足" in capsys.readouterr().out
