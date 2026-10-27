# -*- coding: utf-8 -*-
"""
任务 6.1: blame 分类 — 先定"该算谁的", 再决定"能不能改代码"

锁死这些事 (任务卡):
    失败 -> diagnose(context)
          ├─ blame=product      -> product_bug, 禁止改测试代码
          ├─ blame=environment  -> env_issue,   禁止改测试代码
          ├─ blame=test_code    -> 放行进方案生成
          └─ blame=unknown      -> 交人工 (本模块补的安全出口)

    规则信号: 断言不匹配且页面无异常 -> product
             session 断 / 连接拒    -> environment
             NoSuchElement / Timeout / Stale -> test_code
    规则置信度 < 0.8 才交 LLM, 复用 analyzer 的 Phase1 -> Phase2 机制。

测试里所有会走到 Phase 2 的用例都显式注入假的 llm —— 真跑一次网络调用既不确定,
也可能因为 .env 里配了 key 就把测试变成花钱跑。
"""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields

import pytest

from ai_kit import analyzer, client
from ai_kit.ledger import BudgetExceeded
from ai_kit.proposal import diagnoser as D
from ai_kit.proposal.diagnoser import (
    BLAME_ENVIRONMENT, BLAME_PRODUCT, BLAME_TEST_CODE, BLAME_UNKNOWN,
    PROPOSAL_ENV_ISSUE, PROPOSAL_PATCH, PROPOSAL_PRODUCT_BUG,
    FailureContext, PageObservation,
)

ASSERT_FAILED = "AssertionError: 期望表格有 3 行, 实际 0 行"


def ctx(traceback: str = "", observation=None, **kwargs) -> FailureContext:
    return FailureContext(traceback=traceback, observation=observation, **kwargs)


class _Recorder:
    """假的 LLM: 记下调用, 返回预设 payload (None = 没结论)"""

    def __init__(self, payload=None, error: Exception = None):
        self.payload = payload
        self.error = error
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.payload


def llm_says(category: str, confidence=0.9, **extra):
    payload = {"category": category, "confidence": confidence, "summary": "LLM 的根因"}
    payload.update(extra)
    return _Recorder(payload)


# ---------------------------------------------------------------------------
# 六类标签 -> blame: 投影表不许有漏项
# ---------------------------------------------------------------------------

def test_every_analyzer_category_has_a_blame():
    """analyzer 新增一类而这里没配, 线上就会静默判成 unknown"""
    assert set(D._BLAME_BY_CATEGORY) == set(analyzer.CATEGORIES)


def test_blames_are_exactly_the_three_the_card_asks_for():
    assert D.BLAMES == (BLAME_PRODUCT, BLAME_ENVIRONMENT, BLAME_TEST_CODE)
    assert BLAME_UNKNOWN not in D.BLAMES, "unknown 是安全出口, 不是业务分类"


# ---------------------------------------------------------------------------
# 规则信号: 断言不匹配且页面无异常 -> product
# ---------------------------------------------------------------------------

def test_assertion_with_a_normal_page_is_a_product_bug():
    diag = D.blame_by_rules(ctx(ASSERT_FAILED, PageObservation()))

    assert diag.blame == BLAME_PRODUCT
    assert diag.confidence == 0.9, "页面无异常就该直接定性, 不该再去问 LLM"
    assert diag.category == analyzer.CATEGORY_PRODUCT_BUG
    assert any("无弹窗" in item for item in diag.evidence)


def test_assertion_without_a_page_snapshot_is_not_certain():
    """没有页面证据就不敢定性: 置信度必须掉到闸门以下, 交给 LLM"""
    diag = D.blame_by_rules(ctx(ASSERT_FAILED))

    assert diag.blame == BLAME_PRODUCT
    assert diag.confidence < D.THRESHOLD
    assert any("未采集" in item for item in diag.evidence)


def test_an_explicit_error_message_on_the_page_confirms_the_product_bug():
    diag = D.blame_by_rules(ctx(ASSERT_FAILED, PageObservation(messages=1, text="新增失败")))

    assert diag.blame == BLAME_PRODUCT
    assert diag.confidence >= D.THRESHOLD
    assert any("明确错误提示" in item for item in diag.evidence)


def test_a_leftover_dialog_makes_the_assertion_verdict_uncertain():
    """弹窗是产品没关掉, 还是测试没清理? 规则分不出来 -> 别急着定性"""
    diag = D.blame_by_rules(ctx(ASSERT_FAILED, PageObservation(dialogs=1)))

    assert diag.blame == BLAME_PRODUCT
    assert diag.confidence < D.THRESHOLD
    assert any("残留弹窗" in item for item in diag.evidence)


def test_an_unreachable_page_turns_the_assertion_into_an_environment_issue():
    """页面都打不开了, 断言失败只是表象; category 也要跟着改, 不能自相矛盾"""
    diag = D.blame_by_rules(ctx(ASSERT_FAILED, PageObservation(reachable=False)))

    assert diag.blame == BLAME_ENVIRONMENT
    assert diag.category == analyzer.CATEGORY_ENV
    assert diag.confidence >= D.THRESHOLD
    assert "不可访问" in diag.root_cause


# ---------------------------------------------------------------------------
# 规则信号: NoSuchElement / Timeout / Stale -> test_code
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("traceback, category", [
    ("NoSuchElementException: Unable to locate element: #kw",
     analyzer.CATEGORY_SCRIPT_HEALER),
    ("TimeoutException: timed out waiting for element",
     analyzer.CATEGORY_SCRIPT_WAIT),
    ("StaleElementReferenceException: element is not attached to the page document",
     analyzer.CATEGORY_SCRIPT_WAIT),
])
def test_script_side_signals_are_test_code(traceback, category):
    diag = D.blame_by_rules(ctx(traceback))

    assert diag.blame == BLAME_TEST_CODE
    assert diag.category == category
    assert diag.confidence >= D.THRESHOLD, "脚本类信号够明确, 不该每条都去问 LLM"


def test_syntax_error_is_test_code_and_must_not_be_blamed_on_the_environment():
    """代码都没跑起来, 页面观测不到东西是必然的 —— 不能因此改判成环境问题"""
    diag = D.blame_by_rules(ctx("SyntaxError: invalid syntax", PageObservation(reachable=False)))

    assert diag.blame == BLAME_TEST_CODE
    assert diag.category == analyzer.CATEGORY_SYNTAX


# ---------------------------------------------------------------------------
# 规则信号: session 断 / 连接拒 -> environment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("traceback", [
    "WebDriverException: Connection refused",
    "InvalidSessionIdException: invalid session id",
    "no such window: target window already closed",
    "SessionNotCreatedException: session not created: This version of ChromeDriver "
    "only supports Chrome version 120",
])
def test_session_failures_are_environment(traceback):
    diag = D.blame_by_rules(ctx(traceback))

    assert diag.blame == BLAME_ENVIRONMENT
    assert diag.category == analyzer.CATEGORY_ENV
    assert diag.confidence >= D.THRESHOLD


def test_rule_confidence_is_not_float_noise():
    """analyzer 的 0.7 + 0.1*1 = 0.7999999999999999; 不收敛到两位小数, 单条命中
    永远过不了 0.8 闸门, 每条失败都要白花一次 LLM"""
    assert D.blame_by_rules(ctx("Connection refused")).confidence == 0.8


def test_nothing_recognisable_is_unknown():
    diag = D.blame_by_rules(ctx(""))

    assert diag.blame == BLAME_UNKNOWN
    assert diag.confidence == 0.0


# ---------------------------------------------------------------------------
# Phase 2: 规则不确定才问 LLM (复用 analyzer 的闸门)
# ---------------------------------------------------------------------------

def test_phase_one_never_calls_the_llm(monkeypatch):
    """Phase 1 是纯规则: 一旦它去调 LLM, 每条失败都要花一次钱"""
    def _boom(*args, **kwargs):
        raise AssertionError("Phase 1 不该调 LLM")

    monkeypatch.setattr(D, "blame_with_llm", _boom)

    diag = D.blame_by_rules(ctx(ASSERT_FAILED))

    assert diag.blame == BLAME_PRODUCT and diag.source == "rule"


def test_an_uncertain_rule_asks_the_llm():
    fake = llm_says(analyzer.CATEGORY_PRODUCT_BUG, 0.95)

    diag = D.diagnose(ctx(ASSERT_FAILED), llm=fake)

    assert len(fake.calls) == 1
    assert diag.blame == BLAME_PRODUCT
    assert diag.source == "llm", "LLM 结论更强时, 报告里要能看出来这条是谁下的"


def test_a_certain_rule_does_not_spend_an_llm_call():
    fake = _Recorder({"category": analyzer.CATEGORY_ENV, "confidence": 0.99})

    diag = D.diagnose(ctx(ASSERT_FAILED, PageObservation()), llm=fake)

    assert fake.calls == [], "规则已经够确定, 不该花钱"
    assert diag.blame == BLAME_PRODUCT and diag.source == "rule"


def test_use_llm_false_stops_at_phase_one():
    fake = llm_says(analyzer.CATEGORY_ENV, 0.99)

    diag = D.diagnose(ctx(ASSERT_FAILED), use_llm=False, llm=fake)

    assert fake.calls == []
    assert diag.blame == BLAME_PRODUCT and diag.confidence == 0.7


def test_llm_without_a_verdict_keeps_the_rule_result():
    diag = D.diagnose(ctx(ASSERT_FAILED), llm=_Recorder(None))

    assert diag.blame == BLAME_PRODUCT and diag.source == "rule"
    assert any("没给出可用结论" in item for item in diag.evidence)


def test_llm_that_raises_does_not_break_the_flow():
    diag = D.diagnose(ctx(ASSERT_FAILED), llm=_Recorder(error=RuntimeError("网关 502")))

    assert diag.blame == BLAME_PRODUCT
    assert any("没给出可用结论" in item for item in diag.evidence)


def test_budget_exceeded_is_not_swallowed_by_the_fallback():
    """5.9: 预算没了必须让人知道, 不能被"降级回规则"悄悄吞掉"""
    boom = _Recorder(error=BudgetExceeded(1.0, 1.0, "blame"))

    with pytest.raises(BudgetExceeded):
        D.diagnose(ctx(ASSERT_FAILED), llm=boom)


def test_an_unrecognised_category_from_the_llm_falls_back_to_the_rule():
    diag = D.diagnose(ctx(ASSERT_FAILED), llm=llm_says("看不出来是什么问题", 0.99))

    assert diag.blame == BLAME_PRODUCT and diag.source == "rule"


@pytest.mark.parametrize("spoken, blame", [
    ("产品缺陷", BLAME_PRODUCT),
    ("环境问题", BLAME_ENVIRONMENT),
    ("脚本-定位器问题", BLAME_TEST_CODE),
    ("脚本-等待问题", BLAME_TEST_CODE),
    ("脚本-语法/导入", BLAME_TEST_CODE),
    ("未知-待人工分析", BLAME_UNKNOWN),
])
def test_llm_answers_are_normalised_through_the_shared_table(spoken, blame):
    """LLM 报的是六类标签 (与 analyzer 同一套), 由一张表投影成 blame"""
    diag = D.diagnose(ctx(""), llm=llm_says(spoken, 0.9))

    assert diag.category == spoken
    assert diag.blame == blame


def test_a_weaker_llm_verdict_does_not_override_the_rule():
    diag = D.diagnose(ctx(ASSERT_FAILED), llm=llm_says(analyzer.CATEGORY_ENV, 0.55))

    assert diag.blame == BLAME_PRODUCT
    assert any("不高于规则初判" in item for item in diag.evidence)


def test_a_missing_confidence_is_treated_as_weak():
    payload = {"category": analyzer.CATEGORY_ENV}
    diag = D.diagnose(ctx(ASSERT_FAILED), llm=_Recorder(payload))

    assert diag.blame == BLAME_PRODUCT, "0.5 的结论压不过 0.7 的规则初判"


def test_an_out_of_range_confidence_is_clamped():
    diag = D.diagnose(ctx(ASSERT_FAILED), llm=llm_says(analyzer.CATEGORY_PRODUCT_BUG, 7))

    assert diag.confidence == 1.0


def test_unknown_from_the_llm_still_ends_the_flow():
    diag = D.diagnose(ctx(""), llm=llm_says(analyzer.CATEGORY_UNKNOWN, 0.6))

    assert diag.blame == BLAME_UNKNOWN
    assert D.decide(diag).proposal_type is None


# ---------------------------------------------------------------------------
# prompt: 只贴事实, 不把规则初判塞给模型
# ---------------------------------------------------------------------------

def test_the_llm_prompt_lists_the_six_categories_and_the_consequences():
    for category in analyzer.CATEGORIES:
        assert category in D.LLM_SYSTEM

    assert "只有判成 脚本-* 才允许改测试代码" in D.LLM_SYSTEM
    assert "不许发明" in D.LLM_SYSTEM
    assert "未知-待人工分析, 不要猜" in D.LLM_SYSTEM


def test_the_user_message_carries_facts_not_the_rule_guess():
    fake = llm_says(analyzer.CATEGORY_PRODUCT_BUG, 0.95)
    # 只留残留弹窗: 规则只能给 0.6, 这样才能真的走到 LLM 那一段
    context = ctx(ASSERT_FAILED, PageObservation(dialogs=1, text="新增失败"),
                  case_id="LC-002", steps=("打开新增页", "填名称", "点保存"))

    D.diagnose(context, llm=fake)
    _, user = fake.calls[0]

    for fact in ("LC-002", "AssertionError", "新增失败", "打开新增页", "点保存"):
        assert fact in user, f"user 消息漏了事实: {fact}"
    assert "规则初判" not in user and "product" not in user, \
        "规则初判不该回灌给 LLM —— 那是在替它先选答案"


def test_the_user_message_says_so_when_there_is_no_page_snapshot():
    fake = llm_says(analyzer.CATEGORY_PRODUCT_BUG, 0.95)

    D.diagnose(ctx(ASSERT_FAILED), llm=fake)

    assert "未采集" in fake.calls[0][1]


def test_the_user_message_keeps_the_tail_of_a_long_traceback():
    """异常类型和原因都在末尾, 头部是大段无关栈帧"""
    fake = llm_says(analyzer.CATEGORY_PRODUCT_BUG, 0.95)
    traceback = "HEAD_MARK" + "x" * 4000 + "TAIL_MARK AssertionError"

    D.diagnose(ctx(traceback), llm=fake)

    user = fake.calls[0][1]
    assert "TAIL_MARK" in user and "HEAD_MARK" not in user


def test_the_default_channel_sets_temperature_and_max_tokens(monkeypatch):
    """5.10 第 3 条: temperature=0.1, max_tokens 显式给, 不交给服务端默认值"""
    captured = {}

    def fake_chat_json(system, user, **kwargs):
        captured["kwargs"] = kwargs
        return {"category": analyzer.CATEGORY_PRODUCT_BUG, "confidence": 0.9}

    monkeypatch.setattr(client, "chat_json", fake_chat_json)
    monkeypatch.setattr(client, "llm_available", lambda: True)

    diag = D.diagnose(ctx(ASSERT_FAILED))

    assert captured["kwargs"]["temperature"] == D.TEMPERATURE == 0.1
    assert captured["kwargs"]["max_tokens"] == D.MAX_TOKENS
    assert diag.source == "llm"


def test_no_configured_key_degrades_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(client, "llm_available", lambda: False)

    diag = D.diagnose(ctx(ASSERT_FAILED))

    assert diag.blame == BLAME_PRODUCT and diag.source == "rule"


# ---------------------------------------------------------------------------
# decide: 卡片里那三条路由
# ---------------------------------------------------------------------------

def diag_of(blame: str) -> D.FailureDiagnosis:
    return D.FailureDiagnosis(category=analyzer.CATEGORY_UNKNOWN, blame=blame, confidence=0.9)


def test_product_only_produces_a_bug_report():
    decision = D.decide(diag_of(BLAME_PRODUCT))

    assert decision.proposal_type == PROPOSAL_PRODUCT_BUG
    assert decision.may_edit_test_code is False


def test_environment_only_produces_a_checklist():
    decision = D.decide(diag_of(BLAME_ENVIRONMENT))

    assert decision.proposal_type == PROPOSAL_ENV_ISSUE
    assert decision.may_edit_test_code is False


def test_test_code_goes_on_to_solution_generation():
    decision = D.decide(diag_of(BLAME_TEST_CODE))

    assert decision.proposal_type == PROPOSAL_PATCH
    assert decision.may_edit_test_code is True


def test_unknown_ends_the_flow_for_a_human():
    decision = D.decide(diag_of(BLAME_UNKNOWN))

    assert decision.proposal_type is None
    assert decision.may_edit_test_code is False


@pytest.mark.parametrize("blame", [BLAME_PRODUCT, BLAME_ENVIRONMENT, BLAME_UNKNOWN])
def test_patching_is_refused_for_everything_but_test_code(blame):
    with pytest.raises(D.TestCodeEditRefused) as excinfo:
        D.require_patch_allowed(diag_of(blame))

    assert blame in str(excinfo.value)


def test_patching_is_allowed_for_test_code():
    D.require_patch_allowed(diag_of(BLAME_TEST_CODE))     # 不抛就是通过


# ---------------------------------------------------------------------------
# 两个终态产物
# ---------------------------------------------------------------------------

def test_bug_draft_has_a_title_and_the_repro_steps():
    context = ctx(ASSERT_FAILED, case_id="LC-002", steps=("打开新增页", "填名称", "点保存"))
    draft = D.bug_draft(context, D.diagnose(context, use_llm=False))

    assert draft.title.startswith("[LC-002]")
    assert "实际 0 行" in draft.title, "标题要用异常自己那句话, 而不是笼统的「断言不匹配」"
    assert draft.steps == ("打开新增页", "填名称", "点保存")


def test_bug_draft_falls_back_to_the_rule_summary_without_a_traceback():
    context = ctx("", case_id="LC-009")
    diagnosis = D.FailureDiagnosis(category=analyzer.CATEGORY_PRODUCT_BUG,
                                   blame=BLAME_PRODUCT, confidence=0.9,
                                   root_cause="断言不匹配 (功能行为与预期不一致)")

    draft = D.bug_draft(context, diagnosis)

    assert draft.title == "[LC-009] 断言不匹配 (功能行为与预期不一致)"


def test_bug_draft_admits_when_the_steps_are_unknown():
    """没采集到步骤就明说 —— 编一条会让开发照着走一遍再打回来"""
    draft = D.bug_draft(ctx(ASSERT_FAILED), D.diagnose(ctx(ASSERT_FAILED), use_llm=False))

    assert "未采集到执行步骤" in draft.steps[0]


def test_bug_draft_keeps_the_page_snapshot_and_the_traceback_tail():
    context = ctx(ASSERT_FAILED, PageObservation(), case_id="LC-002")
    draft = D.bug_draft(context, D.diagnose(context, use_llm=False))

    assert any(item.startswith("可访问: ") for item in draft.evidence)
    assert any("traceback 末段" in item for item in draft.evidence)


def test_bug_draft_writes_the_snapshot_once_with_the_visible_error_text():
    """证据要能直接粘进工单: 快照只写一行, 但要带上页面上那句错误原文"""
    context = ctx(ASSERT_FAILED, PageObservation(messages=1, text="查询失败: 服务暂不可用"),
                  case_id="LC-002")
    draft = D.bug_draft(context, D.diagnose(context, use_llm=False))

    snapshots = [item for item in draft.evidence if item.startswith("可访问: ")]
    assert len(snapshots) == 1
    assert "查询失败: 服务暂不可用" in snapshots[0]
    assert not any(item.startswith("页面快照:") for item in draft.evidence)


def test_evidence_lines_do_not_contain_escaped_newlines():
    """traceback 折成一行才能直接贴进工单"""
    context = ctx("Traceback (most recent call last):\n  File x.py, line 1\n" + ASSERT_FAILED)
    draft = D.bug_draft(context, D.diagnose(context, use_llm=False))

    tail = [item for item in draft.evidence if item.startswith("traceback 末段")][0]
    assert "\n" not in tail and " / " in tail


def test_bug_draft_is_json_serialisable():
    draft = D.bug_draft(ctx(ASSERT_FAILED), D.diagnose(ctx(ASSERT_FAILED), use_llm=False))

    assert json.loads(json.dumps(draft.as_dict(), ensure_ascii=False))["title"]


def test_env_checklist_covers_browser_network_and_service():
    checklist = D.env_checklist(ctx("Connection refused"), D.blame_by_rules(ctx("Connection refused")))

    joined = " ".join(checklist.items)
    for topic in ("浏览器", "网络", "服务"):
        assert topic in joined
    assert len(checklist.items) == 3


def test_env_checklist_keeps_the_rule_suggestion():
    context = ctx("Connection refused")
    checklist = D.env_checklist(context, D.blame_by_rules(context))

    assert any("规则建议" in item for item in checklist.evidence)


# ---------------------------------------------------------------------------
# 页面快照
# ---------------------------------------------------------------------------

def test_looks_normal_and_shows_error_are_different_questions():
    assert PageObservation().looks_normal
    assert not PageObservation().shows_error

    assert PageObservation(messages=1).shows_error
    assert not PageObservation(messages=1).looks_normal

    assert not PageObservation(dialogs=1).looks_normal
    assert not PageObservation(dialogs=1).shows_error, "弹窗不等于错误提示"

    assert not PageObservation(reachable=False).looks_normal


class _El:
    def __init__(self, text=""):
        self.text = text

    def is_displayed(self) -> bool:
        return True


class _Driver:
    """find_elements 只认白名单 selector, 其余返回空; explode=True 时全抛"""

    def __init__(self, visible=None, explode: bool = False):
        self._visible = visible or {}
        self._explode = explode

    def find_elements(self, by, selector):
        if self._explode:
            raise RuntimeError("invalid session id")
        return [_El(text) for text in self._visible.get(selector, [])]


def test_no_driver_means_no_observation():
    assert PageObservation.from_driver(None) is None


def test_from_driver_counts_visible_elements_and_keeps_the_message_text():
    driver = _Driver({D._CSS_MESSAGES: ["新增失败"], D._CSS_FIELD_ERRORS: ["名称必填"]})

    observation = PageObservation.from_driver(driver)

    assert observation.messages == 1
    assert observation.field_errors == 1
    assert observation.dialogs == 0
    assert "新增失败" in observation.text


def test_a_dead_driver_is_unreachable_not_unknown():
    """session 断了是"采到了, 页面打不开"; 与"没采过"必须分开, 两者结论不同"""
    observation = PageObservation.from_driver(_Driver(explode=True))

    assert observation is not None
    assert observation.reachable is False
    assert D.blame_by_rules(ctx(ASSERT_FAILED, observation)).blame == BLAME_ENVIRONMENT


def test_a_diagnosis_is_immutable():
    diag = D.blame_by_rules(ctx(ASSERT_FAILED))

    with pytest.raises(FrozenInstanceError):
        diag.blame = BLAME_TEST_CODE


def test_the_same_context_gives_the_same_diagnosis():
    context = ctx(ASSERT_FAILED, PageObservation(dialogs=1), case_id="LC-002")
    first = D.diagnose(context, use_llm=False).as_dict()

    for _ in range(3):
        assert D.diagnose(context, use_llm=False).as_dict() == first


# ---------------------------------------------------------------------------
# 运行时钩子: ai_kit.runtime 传的是 dict, 要的是一行 str()
# ---------------------------------------------------------------------------

RUNTIME_CONTEXT = {
    "action": "click", "eid": "e9", "locator": "e9 (css selector=#nope)",
    "error": "_Missing: 找不到元素: e9 (css selector=#nope) (试了 1 个定位串, 共等 10s)",
    "screenshot": "/tmp/x.png", "url": "http://localhost/admin", "driver": None,
}


def test_the_runtime_dict_is_accepted():
    """契约是 diagnose(context: dict); 找不到元素要归到脚本侧"""
    diag = D.diagnose(dict(RUNTIME_CONTEXT))

    assert diag.blame == BLAME_TEST_CODE
    assert diag.category == analyzer.CATEGORY_SCRIPT_HEALER


def test_the_runtime_path_does_not_spend_an_llm_call_by_default():
    """钩子在异常处理里同步跑, 不能停下来等几秒网络"""
    fake = llm_says(analyzer.CATEGORY_PRODUCT_BUG, 0.99)
    payload = dict(RUNTIME_CONTEXT, error="完全看不懂的一段输出")

    diag = D.diagnose(payload, llm=fake)

    assert fake.calls == []
    assert diag.blame == BLAME_UNKNOWN


def test_the_runtime_path_can_still_ask_for_a_review():
    fake = llm_says(analyzer.CATEGORY_PRODUCT_BUG, 0.99)
    payload = dict(RUNTIME_CONTEXT, error="完全看不懂的一段输出")

    diag = D.diagnose(payload, use_llm=True, llm=fake)

    assert len(fake.calls) == 1
    assert diag.blame == BLAME_PRODUCT


def test_the_runtime_path_collects_the_page_snapshot_on_the_spot():
    """失败那一刻 driver 还在手上, 是整条链路上唯一采得到快照的地方"""
    error = "AssertionError: 期望 3 行, 实际 0 行"
    driver = _Driver({D._CSS_MESSAGES: ["查询失败: 服务暂不可用"]})

    with_snapshot = D.diagnose(dict(RUNTIME_CONTEXT, error=error, driver=driver))
    without = D.diagnose(dict(RUNTIME_CONTEXT, error=error, driver=None))

    assert with_snapshot.blame == BLAME_PRODUCT
    assert with_snapshot.confidence == 0.9
    assert any("明确错误提示" in item for item in with_snapshot.evidence)
    assert without.confidence < D.THRESHOLD, "拿不到快照就不该有这份底气"


def test_str_is_one_self_contained_line():
    """运行时把它直接拼进失败信息 (归因: ...), 这一行必须自己说清楚"""
    diag = D.blame_by_rules(ctx(ASSERT_FAILED, PageObservation()))
    line = str(diag)

    assert "\n" not in line
    for part in (BLAME_PRODUCT, analyzer.CATEGORY_PRODUCT_BUG, "0.90", "实际 0 行"):
        assert part in line


# ---------------------------------------------------------------------------
# 6.2: FailureDiagnosis v2 —— root_cause / rule_version / acceptable_categories
# ---------------------------------------------------------------------------

def test_the_schema_carries_the_fields_the_card_asks_for():
    """卡片要求的最小字段集: 少一个, 评测/报告就得自己再拼一遍"""
    names = {f.name for f in fields(D.FailureDiagnosis)}

    assert {"category", "blame", "confidence", "root_cause", "evidence",
            "source", "rule_version", "acceptable_categories"} <= names


def test_rule_version_says_which_two_tables_produced_the_verdict():
    """规则表在 analyzer, blame 投影在这层; 只记一半, 回归就对不上号"""
    assert D.RULE_VERSION == f"{analyzer.RULES_VERSION}+{D.BLAME_VERSION}"

    diag = D.blame_by_rules(ctx(ASSERT_FAILED))

    assert diag.rule_version == D.RULE_VERSION


def test_rule_version_is_recorded_even_when_the_llm_decides():
    """LLM 结论也要能追到基线规则版本 —— 评测比的正是"它比规则好在哪"""
    diag = D.diagnose(ctx(ASSERT_FAILED), llm=llm_says(analyzer.CATEGORY_PRODUCT_BUG, 0.95))

    assert diag.source == "llm"
    assert diag.rule_version == D.RULE_VERSION


def test_root_cause_names_what_actually_differs():
    """卡片要"按钮文案由 X 改为 Y"那一级: 规则根因"断言不匹配"看不出差在哪"""
    diag = D.blame_by_rules(ctx(ASSERT_FAILED, PageObservation()))

    assert "实际 0 行" in diag.root_cause
    assert diag.root_cause != "断言不匹配 (功能行为与预期不一致)"


def test_root_cause_carries_the_visible_page_text():
    """一个是现象 (页面提示), 一个是差异 (断言原句), 两样都要"""
    diag = D.blame_by_rules(
        ctx(ASSERT_FAILED, PageObservation(messages=1, text="新增失败: 字段校验未通过")))

    assert "新增失败: 字段校验未通过" in diag.root_cause
    assert "实际 0 行" in diag.root_cause


def test_root_cause_admits_when_there_is_no_clue():
    """真拿不到线索就别写一句看起来确定的根因"""
    diag = D.blame_by_rules(ctx(""))

    assert "缺少具体线索" in diag.root_cause


def test_root_cause_falls_back_to_the_log_when_the_traceback_is_empty():
    context = FailureContext(log="[runtime] 找不到元素: e9 (css selector=#nope)")

    diag = D.blame_by_rules(context)

    assert "e9 (css selector=#nope)" in diag.root_cause


def test_root_cause_on_the_runtime_path_names_the_element():
    """运行时钩子那份 dict 只有 error 字符串, 根因也要能到元素一级"""
    diag = D.diagnose(dict(RUNTIME_CONTEXT))

    assert "e9 (css selector=#nope)" in diag.root_cause


def test_an_ambiguous_pair_of_script_categories_is_declared():
    """超时信息里带着定位串时, "等待" 和 "定位器" 两个结论评审都得接受"""
    traceback = ("selenium.common.exceptions.TimeoutException: Message: timeout: "
                 "no such element: Unable to locate element: #kw")

    diag = D.blame_by_rules(ctx(traceback))

    assert diag.category == analyzer.CATEGORY_SCRIPT_WAIT
    assert diag.acceptable_categories == (analyzer.CATEGORY_SCRIPT_HEALER,)
    assert diag.is_ambiguous


def test_a_single_script_signal_is_not_ambiguous():
    diag = D.blame_by_rules(ctx("NoSuchElementException: Unable to locate element: #kw"))

    assert diag.acceptable_categories == ()
    assert not diag.is_ambiguous


def test_a_leftover_dialog_makes_the_locator_category_acceptable():
    """弹窗残留既可能是产品没关, 也可能是脚本没清理/被遮挡"""
    diag = D.blame_by_rules(ctx(ASSERT_FAILED, PageObservation(dialogs=1)))

    assert diag.blame == BLAME_PRODUCT
    assert diag.acceptable_categories == (analyzer.CATEGORY_SCRIPT_HEALER,)


def test_a_clear_product_bug_is_not_ambiguous():
    diag = D.blame_by_rules(ctx(ASSERT_FAILED, PageObservation()))

    assert diag.acceptable_categories == (), "证据清楚就不要给自己留后路"


def test_unknown_is_never_offered_as_an_acceptable_answer():
    """把 unknown 放进容错, 等于每次猜不出来都算对"""
    diag = D.blame_by_rules(ctx(""))

    assert diag.blame == BLAME_UNKNOWN
    assert analyzer.CATEGORY_UNKNOWN not in diag.acceptable_categories


def test_the_llm_acceptable_categories_are_normalised_and_bounded():
    """模型偶尔会把整张表列一遍: 只收能归一到六类的, 去掉自己, 最多两个"""
    fake = llm_says(analyzer.CATEGORY_SCRIPT_WAIT, 0.9,
                    acceptable_categories=["需要等", "胡说八道", "脚本-等待问题",
                                           "环境问题", "脚本-定位器问题"])

    diag = D.diagnose(ctx(ASSERT_FAILED), llm=fake)

    assert diag.acceptable_categories == (analyzer.CATEGORY_ENV,
                                          analyzer.CATEGORY_SCRIPT_HEALER)


def test_the_llm_root_cause_is_used_and_the_old_summary_key_still_works():
    new = D.diagnose(ctx(ASSERT_FAILED),
                     llm=llm_says(analyzer.CATEGORY_PRODUCT_BUG, 0.95,
                                  root_cause="保存按钮文案由 确定 改为 提交"))
    legacy = D.diagnose(ctx(ASSERT_FAILED),
                        llm=_Recorder({"category": analyzer.CATEGORY_PRODUCT_BUG,
                                       "confidence": 0.95, "summary": "旧键根因"}))

    assert new.root_cause == "保存按钮文案由 确定 改为 提交"
    assert legacy.root_cause == "旧键根因"


def test_the_llm_without_a_root_cause_still_gets_a_specific_one():
    """模型漏了根因字段, 也不能在报告里剩一个空字符串"""
    diag = D.diagnose(ctx(ASSERT_FAILED),
                      llm=_Recorder({"category": analyzer.CATEGORY_PRODUCT_BUG,
                                     "confidence": 0.95}))

    assert "实际 0 行" in diag.root_cause


def test_as_dict_carries_the_new_fields_for_reports():
    diag = D.blame_by_rules(ctx(ASSERT_FAILED, PageObservation(dialogs=1)))

    row = json.loads(json.dumps(diag.as_dict(), ensure_ascii=False))

    assert row["root_cause"] and row["rule_version"] == D.RULE_VERSION
    assert row["acceptable_categories"] == [analyzer.CATEGORY_SCRIPT_HEALER]
    assert isinstance(row["evidence"], list), "冻结对象内部用 tuple, 出去必须是 JSON 列表"


def test_the_prompt_asks_for_a_specific_root_cause_and_a_bounded_tolerance():
    for phrase in ("root_cause", "acceptable_categories", "具体到能照着改", "最多 2 个"):
        assert phrase in D.LLM_SYSTEM


def test_the_eval_suite_records_the_version_and_uses_the_tolerance():
    """6.2 的两个字段不能是死字段: 评测汇报告里要真用上"""
    from ai_kit.evaluation import eval_analyzer

    blame = eval_analyzer._eval_blame(eval_analyzer._load_cases())

    assert blame["rule_version"] == D.RULE_VERSION
    assert blame["tolerant_accuracy"] >= blame["accuracy"]
    assert blame["n_tolerated"] == (sum(row["tolerant"] for row in blame["cases"])
                                     - sum(row["correct"] for row in blame["cases"]))
    assert all(row["root_cause"] for row in blame["cases"]), "每行都要有具体根因"
    assert all(row["rule_version"] == D.RULE_VERSION for row in blame["cases"])
