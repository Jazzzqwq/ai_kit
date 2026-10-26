# -*- coding: utf-8 -*-
"""5.9 计划缓存: plans/<fingerprint>.json 的命中 / 失效 / 状态还原

重点不是「存了能读回来」, 而是三件容易写错的事:
1. 键含 profile/model/prompt_version —— 少一个就会拿 A 的答案交 B 的卷;
2. needs_review 与 diagnostics 不进 to_dict —— 不额外存, 命中后计划会「悄悄变干净」;
3. 缓存目录是外部输入 (指纹/环境变量) —— 路径穿越与坏 JSON 都要能兜住。
"""
import json
from dataclasses import replace
from pathlib import Path

import pytest

import ai_kit.client as client
from ai_kit import ledger as L
from ai_kit.perception import cache
from ai_kit.perception.inventory import from_inventory
from ai_kit.perception.planner import PLANNER_VERSION, STRATEGY_VERSION, plan
from test_planner import list_payload


@pytest.fixture
def inv():
    return from_inventory(list_payload())


@pytest.fixture
def out(tmp_path):
    return tmp_path / "plans"


def marked(plan_, first_only=True):
    """造一份「第一条待人工确认」的计划 (needs_review 不进 JSON, 只能靠缓存额外存)"""
    return replace(plan_, cases=tuple(
        replace(case, needs_review=first_only and index == 0)
        for index, case in enumerate(plan_.cases)))


# ---------------------------------------------------------------------------
# 1. 文件名与目录
# ---------------------------------------------------------------------------

def test_cache_path_is_fingerprint_json_under_plans(tmp_path, inv):
    path = cache.plan_cache_path(inv.fingerprint, tmp_path)

    assert path.parent == tmp_path
    assert path.name == f"{inv.fingerprint}.json"
    assert path.is_absolute()


def test_default_plans_dir_can_be_overridden(tmp_path, monkeypatch):
    monkeypatch.delenv(cache.PLANS_DIR_ENV, raising=False)
    assert cache.plans_dir() == Path(cache.DEFAULT_PLANS_DIR)

    monkeypatch.setenv(cache.PLANS_DIR_ENV, str(tmp_path / "custom"))
    assert cache.plans_dir() == tmp_path / "custom"

    assert cache.plans_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_weird_fingerprints_cannot_escape_the_plans_dir(tmp_path):
    """指纹是外部输入: 直接拼进路径就是拿缓存键做路径穿越"""
    for evil in ("../../etc/passwd", "..\\..\\windows", "", "a/b", ".."):
        path = cache.plan_cache_path(evil, tmp_path)

        assert path.parent == tmp_path, f"{evil!r} 跑出目录了: {path}"
        assert "/" not in path.name and "\\" not in path.name
        assert ".." not in path.name


def test_short_or_odd_fingerprints_still_get_a_stable_name(tmp_path):
    first = cache.plan_cache_path("abc", tmp_path)
    second = cache.plan_cache_path("abc", tmp_path)

    assert first == second
    assert first.suffix == ".json"


# ---------------------------------------------------------------------------
# 2. 缓存键: 四成分
# ---------------------------------------------------------------------------

def test_cache_key_carries_fingerprint_profile_model_and_versions(inv):
    key = cache.cache_key(inv, profile="example_shop", model="qwen-plus",
                          prompt_version="planner.v9")

    assert key.startswith(inv.fingerprint)
    assert "profile=example_shop" in key
    assert "model=qwen-plus" in key
    assert f"strategy=v{STRATEGY_VERSION}" in key
    assert "planner=planner.v9" in key
    assert key.endswith("|list_crud")


def test_cache_key_defaults_match_the_planner(inv):
    """cache 只是转发, 不能自己另立一套口径"""
    from ai_kit.perception.planner import plan_cache_key

    assert cache.cache_key(inv) == plan_cache_key(inv)


@pytest.mark.parametrize("field,value", [
    ("profile", "example_shop"), ("model", "其他模型"), ("prompt_version", "planner.v9")])
def test_each_component_changes_the_key(inv, field, value):
    assert cache.cache_key(inv, **{field: value}) != cache.cache_key(inv)


# ---------------------------------------------------------------------------
# 3. 存 / 取
# ---------------------------------------------------------------------------

def test_save_then_load_returns_an_equal_plan(tmp_path, inv):
    plan_ = cache.plan_cached(inv, root=tmp_path)
    path, note = cache.save_plan(plan_, inv, root=tmp_path)

    loaded, why = cache.load_plan(inv, root=tmp_path)

    assert path is not None and path.is_file()
    assert "已写入" in note
    assert loaded is not None and "命中" in why
    assert loaded.to_dict() == plan_.to_dict()


def test_payload_carries_the_key_version_and_review_list(inv):
    plan_ = marked(plan(inv))

    payload = cache.plan_payload(plan_, inv)

    assert payload["cache_version"] == cache.CACHE_VERSION
    assert payload["cache_key"].startswith(inv.fingerprint)
    assert payload["fingerprint"] == inv.fingerprint
    assert payload["review"] == [plan_.cases[0].case_id]
    assert "diagnostics" in payload
    assert set(payload["plan"]) == {"page", "archetype", "profile", "model",
                                    "prompt_version", "generated_at", "cases"}


def test_missing_file_is_a_miss_with_a_reason(tmp_path, inv):
    loaded, why = cache.load_plan(inv, root=tmp_path)

    assert loaded is None
    assert "没有缓存文件" in why


def test_broken_json_is_a_miss_and_does_not_raise(tmp_path, inv):
    path = cache.plan_cache_path(inv.fingerprint, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{半个文件", encoding="utf-8")

    loaded, why = cache.load_plan(inv, root=tmp_path)

    assert loaded is None and "读不出来" in why


def test_payload_that_is_not_an_object_is_a_miss(tmp_path, inv):
    path = cache.plan_cache_path(inv.fingerprint, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")

    assert cache.load_plan(inv, root=tmp_path)[0] is None


def test_changed_key_is_a_miss(tmp_path, inv):
    cache.save_plan(cache.plan_cached(inv, root=tmp_path), inv, root=tmp_path)

    loaded, why = cache.load_plan(inv, root=tmp_path, model="另一个模型")

    assert loaded is None
    assert "缓存键变了" in why


def test_old_cache_version_is_a_miss(tmp_path, inv):
    path = cache.plan_cache_path(inv.fingerprint, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"cache_version": "plan-cache.v0"}), encoding="utf-8")

    loaded, why = cache.load_plan(inv, root=tmp_path)

    assert loaded is None and "格式变了" in why


def test_a_plan_that_cannot_be_read_back_is_a_miss(tmp_path, inv):
    path = cache.plan_cache_path(inv.fingerprint, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "cache_version": cache.CACHE_VERSION,
        "cache_key": cache.cache_key(inv),
        "plan": {"cases": "不是数组"},
    }), encoding="utf-8")

    loaded, why = cache.load_plan(inv, root=tmp_path)

    assert loaded is None and "读不回来" in why


def test_save_failure_is_reported_not_raised(tmp_path, inv):
    """root 指向一个文件: 建目录必然失败, 但缓存坏掉不该让整条流水线停"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")

    path, note = cache.save_plan(cache.plan_cached(inv, root=tmp_path), inv, root=blocker)

    assert path is None
    assert "写不进去" in note


# ---------------------------------------------------------------------------
# 4. needs_review / diagnostics 必须还原 (5.8 那类「悄悄变干净」的回归)
# ---------------------------------------------------------------------------

def test_review_flags_survive_the_round_trip(tmp_path, inv):
    plan_ = cache.plan_cached(inv, root=tmp_path)
    cache.save_plan(marked(plan_), inv, root=tmp_path)

    loaded, _ = cache.load_plan(inv, root=tmp_path)

    assert [case.case_id for case in loaded.review] == [plan_.cases[0].case_id], \
        "命中缓存后待确认的用例不能变干净"


def test_review_flags_are_restored_from_diagnostics_too(tmp_path, inv):
    """兜底: 有人手工编辑过 payload, 只留了 diagnostics["needs_review"]"""
    plan_ = cache.plan_cached(inv, root=tmp_path)
    cache.save_plan(marked(plan_), inv, root=tmp_path)
    case_id = plan_.cases[0].case_id
    path = cache.plan_cache_path(inv.fingerprint, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["review"] = []
    payload["diagnostics"] = {"needs_review": {case_id: ["e999: 清单里没有"]}}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded, _ = cache.load_plan(inv, root=tmp_path)

    assert [case.case_id for case in loaded.review] == [case_id]


def test_diagnostics_are_kept_across_the_round_trip(tmp_path, inv):
    plan_ = cache.plan_cached(inv, root=tmp_path)
    payload = cache.plan_payload(plan_, inv)
    payload["diagnostics"] = {"problems": ["LC-002: 缺字段"], "verify": "未跑"}
    path = cache.plan_cache_path(inv.fingerprint, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    loaded, _ = cache.load_plan(inv, root=tmp_path)

    assert loaded.diagnostics["problems"] == ["LC-002: 缺字段"]
    assert loaded.diagnostics["verify"] == "未跑"


# ---------------------------------------------------------------------------
# 5. plan_cached: 命中不花钱, 未命中才记账
# ---------------------------------------------------------------------------

def test_first_call_misses_and_writes(tmp_path, inv):
    under_test = cache.plan_cached(inv, root=tmp_path)

    assert "未命中" in under_test.diagnostics["cache"]
    assert "已写入" in under_test.diagnostics["cache"]
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_second_call_hits_and_does_not_call_the_model(tmp_path, inv):
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return {"cases": []}

    cache.plan_cached(inv, ask=ask, root=tmp_path)
    spy = cache.plan_cached(inv, ask=ask, root=tmp_path)

    assert calls == [], "命中缓存还去调模型, 缓存就白做了"
    assert "命中" in spy.diagnostics["cache"]


def test_cache_hit_does_not_create_budget_entries(tmp_path, inv):
    budget = L.Budget(0.0001)
    ledger = L.RunLedger(tmp_path / "runs.jsonl")
    cache.plan_cached(inv, root=tmp_path)

    cache.plan_cached(inv, root=tmp_path, ask=lambda prompt: {"cases": []},
                      budget=budget, ledger=ledger)

    assert budget.calls == 0
    assert ledger.read() == []


def test_miss_with_ask_writes_one_ledger_row(tmp_path, inv, monkeypatch):
    state = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
    monkeypatch.setattr(client, "get_cumulative_usage", lambda: dict(state))

    def ask(prompt):
        state["prompt_tokens"] += 600
        state["completion_tokens"] += 400
        state["calls"] += 1
        return {"cases": []}

    ledger = L.RunLedger(tmp_path / "runs.jsonl")
    cache.plan_cached(inv, root=tmp_path / "plans", ask=ask, ledger=ledger,
                      model="qwen-plus", max_retries=1,
                      render=lambda plan_: {case.case_id: "import subprocess"
                                            for case in plan_.cases})

    rows = ledger.read()
    assert len(rows) == 1
    assert rows[0]["tag"] == "plan"
    assert rows[0]["prompt_version"] == PLANNER_VERSION
    assert rows[0]["total_tokens"] == 1000


def test_budget_exhaustion_pierces_plan_cached(tmp_path, inv):
    """预算用完之后不能再悄悄退回确定性计划 —— 必须让人看到「是钱没了」"""
    budget = L.Budget(0.001)
    budget.add(0.002)

    with pytest.raises(L.BudgetExceeded):
        cache.plan_cached(inv, root=tmp_path, ask=lambda prompt: {"cases": []},
                          budget=budget, max_retries=1,
                          render=lambda plan_: {case.case_id: "import subprocess"
                                                for case in plan_.cases})


def test_plan_cached_reports_a_failed_write(tmp_path, inv):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")

    under_test = cache.plan_cached(inv, root=blocker)

    assert "未命中" in under_test.diagnostics["cache"]
    assert "写不进去" in under_test.diagnostics["cache"]


def test_two_misses_on_the_same_page_produce_identical_plans(tmp_path, inv):
    first = cache.plan_cached(inv, root=tmp_path / "a")
    second = cache.plan_cached(inv, root=tmp_path / "b")

    assert first.to_dict() == second.to_dict()
