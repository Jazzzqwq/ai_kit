# -*- coding: utf-8 -*-
"""方案层 ① 失败归因 (blame) — 先定"该算谁的", 再决定"能不能改代码"

   失败 -> diagnoser.diagnose(context)
          ├─ blame=product      -> bug_draft(): 缺陷单草稿, 流程结束, 禁止改测试代码
          ├─ blame=environment  -> env_checklist(): 环境检查清单, 流程结束
          ├─ blame=test_code    -> decide() 放行, 进入方案生成 (locator_patch 等)
          └─ blame=unknown      -> 交人工

判定信号 (规则 + LLM):
    断言不匹配且页面无异常             -> product
    session 断 / 连接拒 / 页面打不开   -> environment
    NoSuchElement / Timeout / Stale    -> test_code

Phase 1 复用 ai_kit.analyzer 的六类规则表 (同一份信号, 不另立口径), 再把六类投影成
blame; Phase 2 沿用 analyzer 的机制: 规则置信度 < 0.8 才调 LLM, LLM 不可用、返回
认不出的分类、或置信度不高于规则, 一律降级回规则结果。

两处必须解释的取舍:

    为什么多一个 unknown: 卡片只给了三个方向, 但"判不出来"得有一条安全出路。把
    unknown 并进 test_code, 等于在根因不明时自动改测试代码 —— 那正是这张卡要修的东西。

    为什么断言类必须有页面快照才敢定性: 光有 AssertionError 说明不了是产品不符预期,
    还是测试跑在了半残的页面上。没有快照就把置信度压到 0.7 (低于闸门), 让 LLM 说话;
    快照显示页面正常/报错才给 0.9, 只剩残留弹窗则压到 0.6 —— 弹窗是产品没关掉还是
    测试没清理, 规则分不出来。

6.2 的 FailureDiagnosis v2 比 v1 多三件事 (都在 dataclass 里, 有测试锁着):

    root_cause            具体到能照着改 ("保存按钮文案由 确定 改为 提交")。v1 的
                          summary 只到"断言不匹配"这一级, 评测比对和缺陷单都用不上。
    rule_version          规则表 + blame 投影表的版本。评测结果里记它, 回归时才说得清
                          基线是哪一版; 改表必须和它一起 +1。
    acceptable_categories 另一个评审也会点头的分类 (定位器 / 等待 同时命中, 残留弹窗让
                          产品 / 脚本两头都可能)。它只给评测开容错, 不放宽规则结论, 也不
                          许放 unknown —— 猜不出来不能被宽容, 否则选 unknown 每次都算对。

账本: 要把这次判断记进成本就把 llm 传成 meter 包装过的调用, 例如
    llm = meter(partial(chat_json, max_retries=1, temperature=0.1, max_tokens=800),
                tag="blame")
BudgetExceeded 不会被降级路径吞掉 —— 预算没了要让人知道, 而不是悄悄用旧结论。

运行时接入: ai_kit.runtime 的失败钩子会调 diagnose(dict) 并把 str(结果) 打进失败
信息; 那条路默认不调 LLM (异常处理里不等网络), 但快照就地采 —— 失败那一刻 driver
还在手上, 是整条链路上唯一采得到页面快照的地方。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from ai_kit import analyzer
from ai_kit.ledger import BudgetExceeded
from ai_kit.utils import sanitize

__all__ = [
    "BLAME_PRODUCT", "BLAME_ENVIRONMENT", "BLAME_TEST_CODE", "BLAME_UNKNOWN",
    "BLAMES",
    "PROPOSAL_PRODUCT_BUG", "PROPOSAL_ENV_ISSUE", "PROPOSAL_PATCH",
    "THRESHOLD", "TEMPERATURE", "MAX_TOKENS", "BLAME_VERSION", "RULE_VERSION",
    "PageObservation", "FailureContext", "FailureDiagnosis", "Decision",
    "BugDraft", "EnvChecklist", "TestCodeEditRefused",
    "diagnose", "blame_by_rules", "blame_with_llm", "decide",
    "describe_observation", "bug_draft", "env_checklist", "require_patch_allowed",
]


# ---------------------------------------------------------------------------
# 三个 blame + 两种终态
# ---------------------------------------------------------------------------

BLAME_PRODUCT = "product"
BLAME_ENVIRONMENT = "environment"
BLAME_TEST_CODE = "test_code"
BLAME_UNKNOWN = "unknown"

# 卡片只承认前三个; unknown 是本模块补的安全出口, 不属于业务分类
BLAMES: Tuple[str, ...] = (BLAME_PRODUCT, BLAME_ENVIRONMENT, BLAME_TEST_CODE)

# 方案类型名 (6.2 的类型学从这里取值, 先在这层定死字符串, 免得两边各写一份)
PROPOSAL_PRODUCT_BUG = "product_bug"
PROPOSAL_ENV_ISSUE = "env_issue"
PROPOSAL_PATCH = "patch"

# 规则置信度低于这个值就交 LLM (与 analyzer 的闸门同一个数)
THRESHOLD = 0.8

# 分类任务要稳, 不要发散; 输出只有四个字段, 额度给多了只是给"跑飞"留空间
TEMPERATURE = 0.1
MAX_TOKENS = 800

# 版本: 评测结果里记 rule_version, 回归时才知道这条基线是哪一版判的。
# 规则表在 analyzer 里, blame 投影与歧义判据在本模块 —— 两段都写进去, 少一段就说不清。
BLAME_VERSION = "blame.v1"
RULE_VERSION = f"{analyzer.RULES_VERSION}+{BLAME_VERSION}"

# 六类标签 -> blame。页面打不开时才允许在这几类上改判成环境问题 (语法错误不在此列:
# 代码都没跑起来, 页面观测不到东西是必然的, 不能因此怪环境)
_BLAME_BY_CATEGORY: Dict[str, str] = {
    analyzer.CATEGORY_PRODUCT_BUG: BLAME_PRODUCT,
    analyzer.CATEGORY_ENV: BLAME_ENVIRONMENT,
    analyzer.CATEGORY_SCRIPT_HEALER: BLAME_TEST_CODE,
    analyzer.CATEGORY_SCRIPT_WAIT: BLAME_TEST_CODE,
    analyzer.CATEGORY_SYNTAX: BLAME_TEST_CODE,
    analyzer.CATEGORY_UNKNOWN: BLAME_UNKNOWN,
}

_EXECUTION_CATEGORIES = frozenset({
    analyzer.CATEGORY_PRODUCT_BUG,
    analyzer.CATEGORY_SCRIPT_HEALER,
    analyzer.CATEGORY_SCRIPT_WAIT,
})

# 定位器 vs 等待 是最常见的"两个都对"的歧义: 超时信息里往往也带着元素定位串
_SCRIPT_CATEGORIES = (analyzer.CATEGORY_SCRIPT_HEALER, analyzer.CATEGORY_SCRIPT_WAIT)

# 页面探针: 每个字段一条 CSS (逗号并成一次查询)。失败后每多查一次就多等一个
# implicit wait, 所以三条探针而不是九条。
_CSS_DIALOGS = ".el-message-box, .el-dialog, [role='dialog']"
_CSS_MESSAGES = ".el-message, .el-alert, [role='alert']"
_CSS_FIELD_ERRORS = ".el-form-item.is-error, [aria-invalid='true']"


# ---------------------------------------------------------------------------
# 页面快照 — 断言类失败定性的唯一证据来源
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PageObservation:
    """失败那一刻页面看到的东西 (纯数据, 不含 WebElement)

    None (未采集) 与 reachable=False (采了但打不开) 是两件事, 必须分开:
    前者是"不知道", 后者是"环境坏了"。
    """

    reachable: bool = True
    dialogs: int = 0            # 可见弹窗 / message-box (遮挡嫌疑)
    messages: int = 0           # 可见 toast / alert
    field_errors: int = 0       # 表单报错字段
    text: str = ""              # 可见提示文本摘录 (只给 LLM 当证据, 不参与规则)

    @property
    def looks_normal(self) -> bool:
        """页面什么都没表现出来 —— 断言失败就是断言失败, 产品行为确实不符预期"""
        return (self.reachable and not self.dialogs
                and not self.messages and not self.field_errors)

    @property
    def shows_error(self) -> bool:
        """页面给了明确错误提示 (校验失败 / 接口报错), 产品侧确实报错了"""
        return self.reachable and bool(self.messages or self.field_errors)

    @classmethod
    def from_driver(cls, driver: Any) -> Optional["PageObservation"]:
        """采一次页面快照; driver 为 None 返回 None, 任何异常都算"页面打不开了"

        只在失败之后调用 —— 它会真查 DOM, implicit wait 的代价由调用方承担。
        """
        if driver is None:
            return None
        try:
            from selenium.webdriver.common.by import By
        except Exception:           # noqa: BLE001 — 没装 selenium 就不采 (纯文本归因照跑)
            return None
        try:
            counts: Dict[str, int] = {}
            texts = []
            for field, selector in (("dialogs", _CSS_DIALOGS),
                                    ("messages", _CSS_MESSAGES),
                                    ("field_errors", _CSS_FIELD_ERRORS)):
                visible = [el for el in driver.find_elements(By.CSS_SELECTOR, selector)
                           if _is_visible(el)]
                counts[field] = len(visible)
                if field == "messages":
                    texts = [_text_of(el) for el in visible[:5]]
            return cls(text=" | ".join(t for t in texts if t)[:300], **counts)
        except Exception:           # noqa: BLE001 — session 断了 / driver 炸了
            return cls(reachable=False)


def _is_visible(element: Any) -> bool:
    try:
        return bool(element.is_displayed())
    except Exception:               # noqa: BLE001
        return False


def _text_of(element: Any) -> str:
    try:
        return str(element.text or "").strip()
    except Exception:               # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# 输入 / 输出
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FailureContext:
    """一次失败的全部材料 (纯数据; 从 conftest / allure 结果里攒出来)

    steps 是可选的执行步骤: 有它缺陷单才写得出真正的重现路径, 没有就明说没采集到,
    不替产品编一条。
    """

    traceback: str = ""
    log: str = ""
    case_id: str = ""
    test_name: str = ""
    steps: Tuple[str, ...] = ()
    observation: Optional[PageObservation] = None


@dataclass(frozen=True)
class FailureDiagnosis:
    """v2: category 之外多一个 blame, 并由 blame 决定下一步动作

    category 沿用 analyzer 的六类标签 (报告/统计不换口径), blame 才是决策依据。

    与 6.2 卡片 schema 的两处差异, 都是有意的:
      - evidence / acceptable_categories 用 tuple: 这个对象是 frozen 的, 可变字段会让
        "同一份诊断"被谁改过都查不出来; 要 list 的地方 (JSON / 报告) 走 as_dict()。
      - 多留 suggestion (v1 就有): 环境检查清单与报告要给它, 不是 v2 要删的东西。
    """

    category: str                        # 六类标签之一 (analyzer.CATEGORIES)
    blame: str                           # test_code | product | environment | unknown
    confidence: float
    root_cause: str = ""                 # 具体到能照着改, 不是"断言不匹配"
    evidence: Tuple[str, ...] = ()
    suggestion: str = ""
    source: str = "rule"                 # rule (Phase1) / llm (Phase2)
    rule_version: str = RULE_VERSION     # 规则表 + blame 投影的版本 (评测回归用)
    acceptable_categories: Tuple[str, ...] = ()     # 另一个评审也会点头的分类

    @property
    def is_certain(self) -> bool:
        """规则/LLM 自己有把握 (>= 闸门) —— 报告里可以当结论用"""
        return self.confidence >= THRESHOLD

    @property
    def is_ambiguous(self) -> bool:
        """还有别的分类也说得通: 报告要标出来, 别让"看起来确定"盖住分歧"""
        return bool(self.acceptable_categories)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "blame": self.blame,
            "confidence": round(self.confidence, 4),
            "root_cause": self.root_cause,
            "evidence": list(self.evidence),
            "suggestion": self.suggestion,
            "source": self.source,
            "rule_version": self.rule_version,
            "acceptable_categories": list(self.acceptable_categories),
        }

    def __str__(self) -> str:
        """一行流: 该算谁的 + 分类 + 置信度 + 具体根因

        runtime._diagnose() 会直接 str() 后打进失败信息 (归因: ...), 所以这一行
        必须自包含 —— 不能在报告里只剩下一个 blame 单词。
        """
        return f"{self.blame} ({self.category}, 置信 {self.confidence:.2f}): {self.root_cause}"


@dataclass(frozen=True)
class Decision:
    """blame -> 下一步做什么

    may_edit_test_code 是卡片那句"禁止改测试代码"的唯一开关: 只有 test_code 为 True,
    方案层 (6.2+) 生成补丁前必须先过 require_patch_allowed()。
    """

    proposal_type: Optional[str]
    may_edit_test_code: bool
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "proposal_type": self.proposal_type,
            "may_edit_test_code": self.may_edit_test_code,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BugDraft:
    """缺陷单草稿 (标题 / 重现步骤 / 证据)"""

    title: str
    steps: Tuple[str, ...]
    evidence: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "steps": list(self.steps),
                "evidence": list(self.evidence)}


@dataclass(frozen=True)
class EnvChecklist:
    """环境问题的人工检查清单 (浏览器版本 / 网络 / 服务状态)"""

    items: Tuple[str, ...]
    evidence: Tuple[str, ...]

    def as_dict(self) -> Dict[str, Any]:
        return {"items": list(self.items), "evidence": list(self.evidence)}


class TestCodeEditRefused(RuntimeError):
    """blame 不是 test_code 却要改测试代码 —— 方案层必须在这里被拦住"""


# ---------------------------------------------------------------------------
# Phase 1: 规则 (复用 analyzer 的六类规则表)
# ---------------------------------------------------------------------------

def _detail_line(text: str, limit: int = 160) -> str:
    """异常自己那句话: traceback / 日志里的最后一行非空内容

    栈帧全是同样的框架路径, 只有末行说得出"到底哪一步不对"。
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    last = lines[-1]
    return last if len(last) <= limit else last[:limit] + "..."


def _root_cause(context: FailureContext, *, blame: str,
                observation: Optional[PageObservation], fallback: str) -> str:
    """具体根因: 异常末行 + 页面上的原文, 退化到规则根因

    卡片要的是"按钮文案由 X 改为 Y"这一级 —— 规则层给不出这么细, 但异常原句和页面
    提示文本都是事实, 拼起来就够开发照着查。真没线索时不硬编, 明说缺什么。
    """
    parts = []
    if blame == BLAME_PRODUCT and observation is not None and observation.text:
        parts.append(f"页面提示「{observation.text}」")
    if (blame == BLAME_ENVIRONMENT and observation is not None
            and not observation.reachable):
        parts.append("页面已不可访问 (driver 报错)")
    detail = _detail_line(context.traceback) or _detail_line(context.log)
    if detail:
        parts.append(detail)
    if parts:
        return "; ".join(parts)[:300]
    if fallback:
        return f"缺少具体线索 (只有笼统信号: {fallback})"
    return "缺少具体线索 (没有 traceback / 日志)"


def _acceptable_categories(text: str, category: str, blame: str,
                           observation: Optional[PageObservation]) -> Tuple[str, ...]:
    """规则层看得见的歧义 —— "另一个评审也会点头"的分类, 只给评测开容错

    两条红线: 不放 unknown (猜不出来不能被宽容, 否则选 unknown 每次都算对), 不放自己。
    歧义必须有证据, 不是"多列几个总能中一个"。
    """
    others = []
    if category in _SCRIPT_CATEGORIES:
        # 超时信息里常带着定位串 ("timeout: no such element"), 两个结论都成立
        hits = [c for c in _SCRIPT_CATEGORIES if c in analyzer.matched_categories(text)]
        if len(hits) == 2:
            others.extend(c for c in hits if c != category)
    if (blame == BLAME_PRODUCT and observation is not None
            and observation.dialogs
            and not observation.messages and not observation.field_errors):
        # 残留弹窗: 产品没关掉 / 脚本没清理或被遮挡, 两条路都说得通
        others.append(analyzer.CATEGORY_SCRIPT_HEALER)
    return tuple(c for c in dict.fromkeys(others)
                 if c != category and c != analyzer.CATEGORY_UNKNOWN)[:2]


def blame_by_rules(context: FailureContext) -> FailureDiagnosis:
    """纯规则归因, 不调 LLM

    六类标签来自 analyzer (信号表只维护一份), 本函数只做四件事: 投影成 blame、
    用页面快照修正断言类结论、把置信度拉回 [0, 1]、给出具体根因与可容错分类。
    所有判断都能追到一条 evidence —— 报告里每一句都得查得到出处。
    """
    text = f"{context.traceback}\n{context.log}"
    rule = analyzer.diagnose(text, use_llm=False)
    blame = _BLAME_BY_CATEGORY.get(rule.category, BLAME_UNKNOWN)
    category = rule.category
    # analyzer 的置信度是 0.7 + 0.1 * 命中数, 单条命中原样算出来是 0.7999999999999999
    # —— 不收敛到两位小数, 单条命中的规则永远过不了 0.8 闸门, 每条失败都要白花一次 LLM。
    confidence = round(float(rule.confidence), 2)
    summary = rule.summary
    evidence = list(rule.evidence)
    observation = context.observation

    if (observation is not None and not observation.reachable
            and rule.category in _EXECUTION_CATEGORIES):
        # 页面都打不开了: 断言失败 / 找不到元素都只是表象。category 也要跟着改,
        # 否则报告里会出现"分类=产品缺陷, blame=环境问题"这种自相矛盾的一行。
        blame = BLAME_ENVIRONMENT
        category = analyzer.CATEGORY_ENV
        confidence = max(confidence, 0.9)
        summary = "页面已不可访问 (失败是表象, 先修环境)"
        evidence.append("页面快照: 不可访问 (driver 报错)")

    elif blame == BLAME_PRODUCT:
        if observation is None:
            confidence = min(confidence, 0.7)       # 没证据就不定性, 交 LLM
            evidence.append("页面快照: 未采集 (无法确认页面是否异常)")
        elif observation.shows_error:
            confidence = max(confidence, 0.9)
            evidence.append(
                f"页面快照: 有明确错误提示 (提示 {observation.messages} / "
                f"报错字段 {observation.field_errors})")
        elif observation.looks_normal:
            confidence = max(confidence, 0.9)
            evidence.append("页面快照: 无弹窗 / 无提示 / 无报错字段")
        else:
            confidence = min(confidence, 0.6)       # 只剩残留弹窗, 规则分不出来
            evidence.append(
                f"页面快照: {observation.dialogs} 个残留弹窗 "
                f"(可能是产品没关掉, 也可能是测试没清理)")

    return FailureDiagnosis(
        category=category,
        blame=blame,
        confidence=min(max(confidence, 0.0), 1.0),
        root_cause=_root_cause(context, blame=blame, observation=observation,
                               fallback=summary),
        evidence=tuple(evidence),
        suggestion=rule.suggestion,
        source="rule",
        rule_version=RULE_VERSION,
        acceptable_categories=_acceptable_categories(text, category, blame, observation),
    )


# ---------------------------------------------------------------------------
# Phase 2: LLM 复核 (与 analyzer 同一套六类标签, 多给一份页面快照)
# ---------------------------------------------------------------------------

LLM_SYSTEM = """你在判断一次 UI 自动化失败到底该算谁的。只输出 JSON, 不写代码。

硬约束:
1. category 只能取下面六个之一, 原样抄写, 不许改写措辞:
   脚本-定位器问题 / 脚本-等待问题 / 脚本-语法/导入 / 环境问题 / 产品缺陷 / 未知-待人工分析
2. 不许发明 traceback 里没有的异常类型、页面元素、步骤或接口。
3. confidence 要诚实: 0.5 上下表示两边都像; 判不出来就选 未知-待人工分析, 不要猜。
4. 你的结论会直接决定下一步动作: 判成 产品缺陷 就只能出缺陷单, 判成 环境问题 就只能
   先修环境, 只有判成 脚本-* 才允许改测试代码。所以没有证据别往脚本问题上倒。
5. root_cause 要具体到能照着改: 点名元素 / 文案 / 接口 / 异常原句 (例如「保存按钮文案由
   确定 改为 提交」), 不许写「断言不匹配」这种笼统话; 信息不够就说清缺哪条证据。
6. acceptable_categories 最多 2 个, 只填「另一个评审也会点头」的分类; 不许放
   未知-待人工分析, 也不许重复 category。没有把握并列就留空数组。

判断口径:
- 产品缺陷: 断言不匹配, 但页面能开、没有崩溃或遮罩残留, 也就是页面行为确实和预期不符;
  或者页面给了明确的错误提示 (校验失败 / 接口报错)。
- 环境问题: session 断开 / 连接被拒 / 浏览器或驱动起不来 / 网络不通 / 页面打不开。
- 脚本-定位器问题: NoSuchElement / 元素被遮挡 / 点击被拦截。
- 脚本-等待问题: 超时 / Stale / 元素还没渲染出来。
- 脚本-语法/导入: SyntaxError / ImportError / 依赖缺失。
- 未知-待人工分析: 以上都不像, 或信息不够。

输出 schema:
{"category": "产品缺陷", "confidence": 0.8,
 "root_cause": "保存接口返回 200 但列表无新记录, 页面也没有任何提示文案",
 "suggestion": "检查后端字段校验与前端提示", "acceptable_categories": []}
"""


def _tail(text: str, limit: int) -> str:
    """取尾部: 异常链和报错原因都在末尾, 头部都是无关的栈帧"""
    value = str(text or "")
    return value[-limit:] if len(value) > limit else value


def _one_line(text: str, limit: int = 300) -> str:
    """取尾部并折成一行 —— 证据要能直接粘进缺陷单/工单, 而不是带一堆转义换行"""
    tail = _tail(str(text or "").strip(), limit)
    return " / ".join(line.strip() for line in tail.splitlines() if line.strip())


def describe_observation(observation: Optional[PageObservation]) -> str:
    """页面快照 -> 给 LLM 看的事实描述 (没有推断, 推断单独一行标出来)"""
    if observation is None:
        return "未采集 (没有 driver) —— 页面是否异常未知, 判不出来就选 未知-待人工分析"
    if not observation.reachable:
        return "不可访问 (driver 报错, 页面已经打不开)"
    lines = [
        "可访问: 是",
        f"可见弹窗: {observation.dialogs}",
        f"提示 / 错误消息: {observation.messages}",
        f"表单报错字段: {observation.field_errors}",
    ]
    if observation.text:
        lines.append(f"可见提示文本: {observation.text}")
    if observation.looks_normal:
        lines.append("推断: 页面看起来正常 (断言失败不像是页面出了异常)")
    elif observation.shows_error:
        lines.append("推断: 页面给了明确错误提示 (产品侧确实报错了)")
    else:
        lines.append(f"推断: 有 {observation.dialogs} 个残留弹窗, 可能遮挡, 也可能测试没清理")
    return "\n".join(lines)


def _llm_user(context: FailureContext) -> str:
    """只贴事实: 用例标识 / traceback / 日志 / 页面快照 / 执行步骤

    出境前过 sanitize: 失败断言里常带着被测页面填的值 (手机号 / 病历号),
    而这段文本的唯一去向就是 LLM。用例标识和步骤序号都在行首, 抹不到。
    """
    parts = []
    label = context.case_id or context.test_name
    if label:
        parts.append(f"=== 用例 ===\n{label}")
    parts.append(f"=== Traceback ===\n{_tail(context.traceback, 2000)}")
    parts.append(f"=== Log ===\n{_tail(context.log, 1000)}")
    parts.append("=== 页面快照 ===\n" + describe_observation(context.observation))
    if context.steps:
        numbered = "\n".join(f"{i}. {step}"
                             for i, step in enumerate(context.steps[:20], 1))
        parts.append(f"=== 执行步骤 ===\n{numbered}")
    return sanitize("\n\n".join(parts))


def _default_llm() -> Optional[Callable[..., Any]]:
    """默认通道: chat_json + 显式 temperature/max_tokens (5.10 的额度约束)

    没配 key / 没装 requests 直接 None —— 归因不能因为 LLM 不可用就中断。
    """
    try:
        from ai_kit.client import chat_json, llm_available
    except Exception:               # noqa: BLE001
        return None
    if not llm_available():
        return None
    return partial(chat_json, max_retries=1,
                   temperature=TEMPERATURE, max_tokens=MAX_TOKENS)


def _as_confidence(value: Any) -> float:
    """LLM 的置信度: 缺失/脏值当 0.5 (弱结论), 越界夹回 [0, 1]"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return min(max(number, 0.0), 1.0)


def _llm_categories(raw: Any, own: str) -> Tuple[str, ...]:
    """LLM 报的容错分类: 归一 -> 只留六类 -> 去掉自己和 unknown -> 最多两个

    模型偶尔会把整张表都列一遍, 那种容错等于没有评测; 所以只收能归一到六类的, 并且
    只留前两个。unknown 同样不收 —— 猜不出来不能被宽容。
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list = []
    for item in raw:
        category = analyzer.normalize_category(item)
        if (category in analyzer.CATEGORIES
                and category not in (own, analyzer.CATEGORY_UNKNOWN)
                and category not in out):
            out.append(category)
    return tuple(out[:2])


def blame_with_llm(context: FailureContext,
                   llm: Optional[Callable[..., Any]] = None
                   ) -> Optional[FailureDiagnosis]:
    """Phase 2: 让 LLM 复核, 返回诊断或 None (None = 没有可用结论, 调用方降级)

    llm: (system, user) -> dict|None。默认走 client.chat_json; 注入时由调用方保证
    可用性, 抛错/返回怪数据都只降级成 None (BudgetExceeded 除外, 见下)。
    """
    ask = llm if llm is not None else _default_llm()
    if ask is None:
        return None
    try:
        result = ask(LLM_SYSTEM, _llm_user(context))
    except Exception as exc:        # noqa: BLE001 — 注入的客户端可能任意抛
        if isinstance(exc, BudgetExceeded):
            # 5.9: 预算没了必须往上抛, 被降级吞掉就成了"钱花光了却没人知道"
            raise
        print(f"  [blame] LLM 复核失败, 降级回规则结果 ({exc})")
        return None
    if not isinstance(result, dict):
        return None

    category = analyzer.normalize_category(
        result.get("category", analyzer.CATEGORY_UNKNOWN))
    if category not in _BLAME_BY_CATEGORY:
        print(f"  [blame] LLM 给了认不出的分类 {category!r}, 降级回规则结果")
        return None
    # v1 叫 summary, v2 叫 root_cause; 两个键都认, 免得换个 prompt 就丢根因
    root_cause = str(result.get("root_cause") or result.get("summary") or "").strip()
    if not root_cause:
        # 模型没给根因也不留空: 异常末行至少是事实, 报告里不能剩一个空字符串
        root_cause = _detail_line(context.traceback) or "LLM 未给出根因"
    return FailureDiagnosis(
        category=category,
        blame=_BLAME_BY_CATEGORY[category],
        confidence=_as_confidence(result.get("confidence")),
        root_cause=root_cause,
        evidence=(f"LLM 判为 {category}",),
        suggestion=str(result.get("suggestion") or ""),
        source="llm",
        rule_version=RULE_VERSION,
        acceptable_categories=_llm_categories(result.get("acceptable_categories"), category),
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _context_from_runtime(payload: Mapping[str, Any]) -> FailureContext:
    """运行时 _failure() 传的 dict -> FailureContext

    形状见 ai_kit.runtime: action / eid / locator / error / screenshot / url / driver。
    用到的只有两样: error (规则要看的那段文字) 和 driver (失败那一刻的页面还在它手上,
    是整个链路里唯一采得到快照的地方 —— 事后分析 allure 结果时已经没有 driver 了)。
    """
    bits = [f"动作: {payload.get('action')}" if payload.get("action") else "",
            f"元素: {payload.get('eid')}" if payload.get("eid") else "",
            f"定位: {payload.get('locator')}" if payload.get("locator") else "",
            f"页面: {payload.get('url')}" if payload.get("url") else ""]
    return FailureContext(
        traceback=str(payload.get("error") or ""),
        log="; ".join(bit for bit in bits if bit),
        observation=PageObservation.from_driver(payload.get("driver")),
    )


def diagnose(context: Any, *,
             use_llm: Optional[bool] = None,
             llm: Optional[Callable[..., Any]] = None) -> FailureDiagnosis:
    """对一次失败做 blame 归因: Phase 1 规则 -> 不够确定才 Phase 2 LLM

    context 两种形状都收:
        FailureContext            完整材料 (报告/方案层用这个)
        dict                      运行时钩子那份 context, 见 _context_from_runtime

    use_llm 默认值跟形状走: FailureContext 默认 True; dict 默认 False —— 运行时钩子
    是在异常处理里同步调用的, 不能因为一次失败再停下来等几秒网络。要复核就显式传。

    规则置信度 >= 0.8 直接返回 (不花一分钱); LLM 没结论或结论更弱 -> 回规则结果,
    并在 evidence 里记一笔"为什么最后还是规则", 免得事后看不出这一条到底信谁。
    """
    if isinstance(context, Mapping):
        context = _context_from_runtime(context)
        if use_llm is None:
            use_llm = False
    elif use_llm is None:
        use_llm = True

    rule = blame_by_rules(context)
    if not use_llm or rule.confidence >= THRESHOLD:
        return rule

    print(f"  [blame] 规则初判 {rule.blame} ({rule.confidence:.2f}) 不够确定, 调 LLM 复核...")
    llm_diag = blame_with_llm(context, llm=llm)
    if llm_diag is None:
        return replace(rule, evidence=rule.evidence + (
            "LLM 没给出可用结论, 沿用规则初判",))
    if llm_diag.confidence >= rule.confidence:
        print(f"  [blame] 采用 LLM 结论 {llm_diag.blame} ({llm_diag.confidence:.2f} "
              f"vs 规则 {rule.confidence:.2f})")
        return llm_diag
    return replace(rule, evidence=rule.evidence + (
        f"LLM 判为 {llm_diag.category} ({llm_diag.confidence:.2f}), 不高于规则初判",))


def decide(diagnosis: FailureDiagnosis) -> Decision:
    """blame -> 方案类型 / 能不能改测试代码"""
    if diagnosis.blame == BLAME_PRODUCT:
        return Decision(PROPOSAL_PRODUCT_BUG, False,
                        "产品缺陷: 只出缺陷单, 禁止改测试代码 (改了就掩盖了产品 bug)")
    if diagnosis.blame == BLAME_ENVIRONMENT:
        return Decision(PROPOSAL_ENV_ISSUE, False,
                        "环境问题: 先修环境再重跑, 此时改测试代码只是把环境噪声写进脚本")
    if diagnosis.blame == BLAME_TEST_CODE:
        return Decision(PROPOSAL_PATCH, True, "脚本问题: 进入方案生成 (locator_patch 等)")
    return Decision(None, False, "判不出来: 交人工, 不许自动改代码")


def require_patch_allowed(diagnosis: FailureDiagnosis) -> None:
    """方案层生成补丁前的闸门: blame 不是 test_code 就抛

    这条不是提醒而是拦截 —— "禁止改测试代码" 如果只是 Decision 里的一个字段,
    谁忘了看就没人看得见。
    """
    if diagnosis.blame == BLAME_TEST_CODE:
        return
    raise TestCodeEditRefused(
        f"blame={diagnosis.blame} 不允许改测试代码: {decide(diagnosis).reason}")


# ---------------------------------------------------------------------------
# 两个终态产物: 缺陷单草稿 / 环境检查清单
# ---------------------------------------------------------------------------

_ENV_CHECKS: Tuple[str, ...] = (
    "浏览器与驱动: 确认 Chrome/Edge 版本与 driver 匹配 (Selenium Manager 是否可用), "
    "必要时重装 driver",
    "网络: 浏览器手开一次被测站点, 确认没有代理 / VPN 拦截, 接口域名解析正常",
    "服务状态: 确认后端进程与端口在跑, 服务端日志没有 5xx 或重启记录",
)


def _headline(context: FailureContext, diagnosis: FailureDiagnosis) -> str:
    """缺陷单标题: 优先用异常自己那句话, 退化到规则根因

    "断言不匹配 (功能行为与预期不一致)" 这种标题开发看了还是不知道差在哪;
    "AssertionError: 期望表格有 3 行, 实际 0 行" 才能直接进缺陷单标题。
    """
    lines = [line.strip() for line in str(context.traceback or "").splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    if last and len(last) <= 120:
        return last
    return diagnosis.root_cause or "页面行为与预期不符"


def bug_draft(context: FailureContext, diagnosis: FailureDiagnosis) -> BugDraft:
    """产品缺陷单草稿: 只写查得到的东西

    步骤没采集到就写明"没采集到", 不替产品编重现路径 —— 一条编出来的步骤会让开发
    照着走一遍然后打回来。
    """
    label = context.case_id or context.test_name or "失败用例"
    title = f"[{label}] {_headline(context, diagnosis)}"
    steps = tuple(context.steps) or ("(未采集到执行步骤, 需按实际用例补全)",)
    evidence = [f"blame=product (置信 {diagnosis.confidence:.2f}, 来源 {diagnosis.source})"]
    if context.observation is not None:
        # 快照只写一行: 有采集就用详细版 (带可见提示原文), 不再重复规则那句摘要
        evidence.extend(item for item in diagnosis.evidence
                        if not item.startswith("页面快照:"))
        evidence.append(describe_observation(context.observation).replace("\n", " / "))
    else:
        evidence.extend(diagnosis.evidence)
    tail = _one_line(context.traceback)
    if tail:
        evidence.append(f"traceback 末段: {tail}")
    return BugDraft(title=title, steps=steps, evidence=tuple(evidence))


def env_checklist(context: FailureContext, diagnosis: FailureDiagnosis) -> EnvChecklist:
    """环境检查清单 + 这次失败留下的证据 (含规则给的处理建议)"""
    evidence = [f"blame=environment (置信 {diagnosis.confidence:.2f}, "
                f"来源 {diagnosis.source})"]
    evidence.extend(diagnosis.evidence)
    tail = _one_line(context.traceback)
    if tail:
        evidence.append(f"traceback 末段: {tail}")
    if diagnosis.suggestion:
        evidence.append(f"规则建议: {diagnosis.suggestion}")
    return EnvChecklist(items=_ENV_CHECKS, evidence=tuple(evidence))
