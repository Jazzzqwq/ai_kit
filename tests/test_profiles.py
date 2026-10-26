# -*- coding: utf-8 -*-
"""
任务 A-6: 规则外置 — profiles/loader.py + builtin/*.json 契约

锁死三件事:
    1. 内置包齐全、schema 干净; element_plus 与改造前 healer.py 里硬编码的
       L1 坑表 / 语义提示与候选 / 意图词表 / L3-L4 容器逐条一致 —— 外置不能改行为
    2. 合并语义: 越靠后越优先; 脏规则永不覆盖好规则; 不修改入参
    3. 加载/校验: load_profile 严格 (写错要能发现), resolve_profile 宽松 (不拖垮自愈)

运行: pytest tests/test_profiles.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_kit.profiles import (
    DEFAULT_BUILTIN,
    RULE_KEYS,
    ProfileError,
    builtin_names,
    default_rules,
    effective_rules,
    load_profile,
    merge_profiles,
    resolve_profile,
    site_names,
    validate_profile,
)
from ai_kit.profiles.loader import TABLE_KEYS

# ---------------------------------------------------------------------------
# 改造前 ai_kit/healer.py 里的硬编码 — 逐字抄下来当基准
# ---------------------------------------------------------------------------

LEGACY_L1 = [
    ("body > div.el-message-box", "css selector", ".el-message-box",
     "Element Plus 弹窗 DOM: body > __wrapper > .el-message-box"),
    ("body > div.el-dialog", "css selector", ".el-dialog",
     "Element Plus dialog DOM: body > __wrapper > .el-dialog"),
    ("body > .el-message-box", "css selector", ".el-message-box",
     "body 子元素不是 .el-message-box"),
    ("body > .el-dialog", "css selector", ".el-dialog",
     "body 子元素不是 .el-dialog"),
    (".el-message-box__btns button.el-button--primary", "css selector",
     ".el-message-box__btns button.el-button--danger, "
     ".el-message-box__btns button.el-button--primary",
     "删除确认弹窗用 danger 按钮, 保存用 primary, 都试试"),
]

LEGACY_HINTS = [
    (r"message[-_]?box|el-message-box", "message_box"),
    (r"\.el-dialog", "dialog"),
    (r"el-table", "table"),
    (r"el-select|el-dropdown", "select"),
]

LEGACY_SEMANTIC = {
    "message_box": [
        ".el-message-box",
        ".el-message-box__wrapper .el-message-box",
        "body > .el-message-box__wrapper > .el-message-box",
    ],
    "dialog": [
        ".el-dialog",
        ".el-dialog__wrapper .el-dialog",
    ],
    "table": [
        ".el-table__body-wrapper .el-table__body",
        ".el-table .el-table__body",
        "table.el-table__body",
    ],
    "select": [
        ".el-select__wrapper",
        ".el-select .el-input__inner",
    ],
}

LEGACY_INTENT_KEYWORDS = {
    "confirm": ["确定", "confirm", "delete"],
    "cancel": ["取消", "cancel"],
    "close": ["关闭", "close"],
}

LEGACY_INTENT_SELECTORS = {
    "confirm": (".el-message-box__btns button.el-button--danger, "
                ".el-message-box__btns button.el-button--primary, "
                ".el-message-box__btns button:last-child"),
    "cancel": ".el-message-box__btns button.el-button--default",
    "close": ".el-message-box__headerbtn, .el-dialog__close",
}

LEGACY_L3_CONTAINERS = [".el-message-box", ".el-dialog"]


# ---------------------------------------------------------------------------
# 内置包: 齐全 / 干净 / 与原硬编码一致
# ---------------------------------------------------------------------------

def test_builtin_names_cover_expected_frameworks():
    assert set(DEFAULT_BUILTIN) == {"generic", "element_plus"}
    assert set(builtin_names()) >= {"generic", "element_plus", "antd"}


@pytest.mark.parametrize("name", builtin_names())
def test_every_builtin_pack_is_clean(name):
    pack = load_profile(name)
    assert validate_profile(pack) == [], f"内置包 {name} 自带 schema 问题"
    assert pack["name"] == name, "name 必须和文件名一致, 否则日志里对不上"


def test_element_plus_l1_rules_match_legacy_hardcode():
    rules = load_profile("element_plus")["l1_rules"]

    assert set(rules) == {broken for broken, *_ in LEGACY_L1}
    for broken, by, locator, reason in LEGACY_L1:
        assert rules[broken] == {"by": by, "locator": locator, "reason": reason}


def test_element_plus_semantic_hints_match_legacy_order():
    hints = load_profile("element_plus")["semantic_hints"]

    assert [tuple(h) for h in hints] == LEGACY_HINTS, "顺序参与匹配优先级, 不能换"


def test_element_plus_semantic_candidates_match_legacy():
    assert load_profile("element_plus")["semantic_candidates"] == LEGACY_SEMANTIC


def test_element_plus_intent_tables_match_legacy():
    pack = load_profile("element_plus")

    assert pack["intent_keywords"] == LEGACY_INTENT_KEYWORDS
    assert pack["intent_selectors"] == LEGACY_INTENT_SELECTORS


def test_element_plus_containers_match_legacy():
    pack = load_profile("element_plus")

    assert pack["l3_containers"] == LEGACY_L3_CONTAINERS
    # 原来第 4 个是业务类名 .filter-card, 已搬到 sites/example_shop.json
    assert pack["l4_containers"] == [".el-message-box", ".el-dialog",
                                     ".el-message-box__wrapper", "table.el-table__body"]


def test_generic_is_framework_agnostic():
    pack = load_profile("generic")

    for key in ("l1_rules", "semantic_candidates", "intent_keywords",
                "intent_selectors", "semantic_hints", "l3_containers"):
        assert not pack[key], f"generic 的 {key} 不该有内容, 否则会污染具体框架包"


@pytest.mark.parametrize("selector", ["[role=dialog]", ".modal", "[class*=dialog]"])
def test_generic_l4_containers_cover_universal_patterns(selector):
    """任务卡点名的三种通用容器模式"""
    assert selector in load_profile("generic")["l4_containers"]


# ---------------------------------------------------------------------------
# 默认规则包
# ---------------------------------------------------------------------------

def test_default_rules_fill_every_key():
    rules = default_rules()

    assert set(rules) == set(RULE_KEYS)
    for key in TABLE_KEYS:
        assert isinstance(rules[key], dict), f"{key} 应该是 dict"
    for key in ("semantic_hints", "l3_containers", "l4_containers"):
        assert isinstance(rules[key], list), f"{key} 应该是 list"


def test_default_l1_comes_from_element_plus_only():
    assert default_rules()["l1_rules"] == load_profile("element_plus")["l1_rules"]


def test_default_containers_put_framework_patterns_before_generic():
    containers = default_rules()["l4_containers"]
    framework = load_profile("element_plus")["l4_containers"]
    generic = load_profile("generic")["l4_containers"]

    assert containers[:len(framework)] == framework, "框架容器要先试, 才不改变老行为"
    assert containers[len(framework):] == generic


def test_default_rules_is_cached():
    assert default_rules() is default_rules()


def test_effective_rules_does_not_touch_default_pack():
    before = json.dumps(default_rules(), sort_keys=True, ensure_ascii=False)

    merged = effective_rules(load_profile("antd"))
    merged["l1_rules"]["injected.by.test"] = {"by": "css selector", "locator": ".x"}
    merged["l1_rules"]["body > div.el-message-box"]["locator"] = ".tampered"
    merged["semantic_candidates"]["dialog"].append(".tampered")

    after = json.dumps(default_rules(), sort_keys=True, ensure_ascii=False)
    assert after == before, "合并结果必须和缓存的内置包深层脱钩"


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------

def test_merge_later_pack_wins_for_scalar_table():
    merged = merge_profiles({"intent_selectors": {"confirm": ".old"}},
                            {"intent_selectors": {"confirm": ".new"}})
    assert merged["intent_selectors"]["confirm"] == ".new"


def test_merge_puts_new_pack_semantic_candidates_first():
    merged = merge_profiles({"semantic_candidates": {"dialog": [".builtin"]}},
                            {"semantic_candidates": {"dialog": [".site"]}})
    assert merged["semantic_candidates"]["dialog"] == [".site", ".builtin"]


def test_merge_appends_intent_keywords_and_dedupes():
    merged = merge_profiles({"intent_keywords": {"confirm": ["确定", "ok"]}},
                            {"intent_keywords": {"confirm": ["yes", "确定"]}})
    assert merged["intent_keywords"]["confirm"] == ["yes", "确定", "ok"]


@pytest.mark.parametrize("dirty", [
    "just-a-string",
    {"by": "css selector"},
    {"locator": "   "},
    ["only-one-element"],
    None,
    42,
])
def test_merge_dirty_l1_rule_never_overrides_valid_one(dirty):
    merged = merge_profiles({"l1_rules": {"broken.sel": {"locator": ".good"}}},
                            {"l1_rules": {"broken.sel": dirty}})
    assert merged["l1_rules"]["broken.sel"] == {"locator": ".good"}


def test_merge_dirty_intent_keywords_never_wipes_builtin():
    merged = merge_profiles({"intent_keywords": {"confirm": ["确定"]}},
                            {"intent_keywords": {"confirm": "not-a-list"}})
    assert merged["intent_keywords"]["confirm"] == ["确定"]


def test_merge_does_not_mutate_inputs():
    left = {"l1_rules": {"a": {"locator": ".a"}}, "l4_containers": [".a"]}
    right = {"l4_containers": [".b"], "semantic_hints": [["\\.z", "dialog"]]}
    left_snapshot = json.loads(json.dumps(left))
    right_snapshot = json.loads(json.dumps(right))

    merged = merge_profiles(left, right)
    merged["l1_rules"]["a"]["locator"] = ".tampered"
    merged["semantic_hints"][0][0] = "tampered"

    assert left == left_snapshot and right == right_snapshot


def test_merge_ignores_non_mapping_packs():
    merged = merge_profiles(42, "oops", None, {"l4_containers": [".a"]})
    assert merged["l4_containers"] == [".a"]


# ---------------------------------------------------------------------------
# 加载: 严格 vs 宽松
# ---------------------------------------------------------------------------

def test_load_profile_returns_copy_of_dict():
    src = {"l1_rules": {}}
    out = load_profile(src)
    out["l1_rules"]["x"] = 1
    assert src == {"l1_rules": {}}


def test_load_profile_by_path(tmp_path):
    path = tmp_path / "my_site.json"
    path.write_text(json.dumps({"name": "my_site", "l4_containers": [".x"]}),
                    encoding="utf-8")

    assert load_profile(str(path))["l4_containers"] == [".x"]
    assert load_profile(path)["name"] == "my_site"


def test_load_profile_unknown_name_raises():
    with pytest.raises(ProfileError):
        load_profile("no-such-pack")


def test_load_profile_bad_json_raises(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ProfileError):
        load_profile(str(path))


def test_load_profile_non_object_raises(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(ProfileError):
        load_profile(str(path))


@pytest.mark.parametrize("garbage", [42, object(), ["a", "b"], None])
def test_load_profile_non_pathlike_raises(garbage):
    with pytest.raises(ProfileError):
        load_profile(garbage)


def test_resolve_profile_swallows_errors():
    assert resolve_profile(None) == {}
    assert resolve_profile(42) == {}
    assert resolve_profile({"l4_containers": [".x"]}) == {"l4_containers": [".x"]}


def test_resolve_profile_warns_on_missing_pack():
    with pytest.warns(RuntimeWarning):
        assert resolve_profile("no-such-pack") == {}


def test_effective_rules_returns_default_for_garbage():
    assert effective_rules(42) is default_rules()
    assert effective_rules(None) is default_rules()
    assert effective_rules({}) is default_rules()


def test_effective_rules_honours_env_var(monkeypatch):
    """不改代码就能加站点知识: AI_KIT_PROFILE=包名或路径"""
    monkeypatch.setenv("AI_KIT_PROFILE", "antd")

    rules = effective_rules()

    assert ".ant-modal" in rules["l4_containers"]
    assert default_rules()["l4_containers"][0] == ".el-message-box", \
        "内置包不受环境变量影响"


def test_explicit_profile_beats_env_var(monkeypatch):
    monkeypatch.setenv("AI_KIT_PROFILE", "antd")

    rules = effective_rules(load_profile("example_shop"))

    assert ".filter-card" in rules["l4_containers"]
    assert ".ant-modal" not in rules["l4_containers"]


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dirty", [
    ["not", "an", "object"],
    {"l1_rules": "oops"},
    {"l1_rules": {"sel": "just-a-string"}},
    {"l1_rules": {"sel": {"by": "css selector"}}},
    {"semantic_candidates": {"dialog": ".not-a-list"}},
    {"semantic_candidates": {"dialog": [1, 2]}},
    {"intent_selectors": {"confirm": 42}},
    {"semantic_hints": [["[unclosed", "dialog"]]},
    {"l4_containers": [None]},
    {"schema": "1"},
])
def test_validate_profile_reports_problems(dirty):
    assert validate_profile(dirty), f"{dirty!r} 应该被报出问题"


def test_validate_profile_reports_unknown_keys():
    issues = validate_profile({"l4_containers": [], "l4_container": []})
    assert any("未知键" in issue for issue in issues)


def test_validate_profile_allows_comment_keys():
    """以 _ 开头的键是注释位, 不该被当成未知键抱怨"""
    assert validate_profile({"_comment": "说明", "l4_containers": []}) == []


# ---------------------------------------------------------------------------
# 站点示例包
# ---------------------------------------------------------------------------

def test_example_site_pack_is_available_and_clean():
    assert "example_shop" in site_names()
    assert validate_profile(load_profile("example_shop")) == []


def test_example_site_overrides_builtin_rules():
    rules = effective_rules(load_profile("example_shop"))

    assert rules["l1_rules"]["body > div.el-message-box"]["locator"] == ".shop-message-box"
    assert rules["semantic_candidates"]["table"][0] == ".shop-grid .el-table__body"
    assert rules["l4_containers"][:2] == [".filter-card", ".shop-grid"]
    assert ".el-dialog" in rules["l4_containers"], "站点包只该加, 不该把框架容器挤掉"


def test_business_selectors_live_in_site_pack_not_builtin():
    """框架包不该含具体业务类名: .filter-card 已从 healer 硬编码搬进 sites/"""
    assert ".filter-card" in load_profile("example_shop")["l4_containers"]

    for name in builtin_names():
        pack = load_profile(name)
        for key in ("l3_containers", "l4_containers"):
            assert ".filter-card" not in pack.get(key, []), f"{name} 里混进了业务选择器"


def test_pyproject_ships_profile_json_in_package_data():
    """规则包是运行期数据: 打包配置漏了它, 装完 healer 会静默退化成只剩原始定位器"""
    tomllib = pytest.importorskip("tomllib")
    root = Path(__file__).resolve().parents[1]

    with open(root / "pyproject.toml", "rb") as f:
        config = tomllib.load(f)

    patterns = " ".join(config["tool"]["setuptools"]["package-data"]["ai_kit.profiles"])
    assert "builtin/*.json" in patterns
    assert "sites/*.json" in patterns
