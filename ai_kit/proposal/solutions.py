# -*- coding: utf-8 -*-
"""方案层 ② 方案类型学 — 把"该算谁的"变成"能怎么改", 每条都说得出为什么

    diagnoser.diagnose()        -> FailureDiagnosis (blame / root_cause)
    solutions.propose()         -> 一组 Proposal (7 类方案, 每条带 reason)

七类方案与各自的来源 (卡片 6.3):

    方案类型           谁生成                    有没有 diff
    locator_patch      healer.rank_candidates()  top 候选 -> difflib 真 diff (必填)
    wait_adjust        规则模板                  参数级建议, 不编 diff
    data_fix           LLM (要读懂业务数据)      LLM 给的, 必须自洽
    assertion_update   LLM (要读懂断言意图)      LLM 给的, 必须自洽
    config_change      LLM (要读懂配置项)        LLM 给的, 必须自洽
    product_bug        diagnoser.decide()        无 (只提缺陷单)
    env_issue          diagnoser.decide()        无 (先修环境)

    product_bug / env_issue 的字符串在 diagnoser 里定死 (decide() 判的就是它), 这里只
    重新导出, 不另写一份 —— 两处各写一遍, 迟早有一处漂移。

四条硬约定 (都有测试锁着, 也都解释了代价):

1. auto_applicable 只有一个来源: risk=low + diff 非空 + 类型属于改代码, 且不能是
   assertion_update (6.5 硬约束: 断言变更就是预期变更, 必须人审)。这条在 __post_init__
   里强制, 不信任何调用方 —— 尤其是 LLM: 它的补丁没人审过, 一律压在 medium 以上,
   永远进不了自动应用。自动应用失败一次, 整条链路就没人敢用了。

2. diff 必须由原文算出来 (difflib), 不是拼出来的字符串。拿不到目标文件、文件里没有
   原定位器、或候选就是原定位器时, locator_patch 直接不产出并在 stdout 说明卡在哪 ——
   卡片要求它的 diff 必填, 编一行假上下文只会让 git apply 报错, 而现场要过很久才会
   发现"方案本身就是假的"。wait_adjust 同理: 等待值落在哪个调用点因项目而异, 不编。

3. 改测试代码的四类 (locator_patch / wait_adjust / data_fix / assertion_update) 生成前
   必须过 diagnoser.require_patch_allowed(): blame 不是 test_code 直接抛, 不是提醒。

4. 每条方案必须回答为什么 (reason), 空字符串在 __post_init__ 里就被拒。方案层最容易
   退化成"看起来像修复的文本", 而 reason 是唯一能让人一眼看出它是不是在复述 diff 的
   字段; LLM 没给理由的方案直接丢弃, 不给它留位置。

规则优先: 能用规则算出来的绝不问 LLM。propose() 默认只在规则层一条方案都没产出时才
调 LLM (use_llm=True 强制, False 关掉); LLM 也拿不到目标文件原文时, prompt 里明确
要求 diff 留空 —— 没有原文却给了 diff 的方案会被丢弃。

6.2 的 acceptable_categories 在这里有第二个用途 (不只给评测): "超时信息里带着定位串"
这种两条路都说得通时, 定位器补丁与等待调整都出, 报告里并排给人看 —— 规则结论不变,
只是把分歧摆到台面上。

LLM 给的 verify_cmd / rollback_cmd / auto_applicable 一律不用, 由本地按 target_file
拼出来: 那是后面要执行的命令, 不能让模型决定 (也没有理由让它决定)。
"""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from ai_kit import analyzer
from ai_kit.ledger import BudgetExceeded
from ai_kit.utils import sanitize
from ai_kit.proposal import diagnoser
from ai_kit.proposal.diagnoser import (
    PROPOSAL_ENV_ISSUE, PROPOSAL_PRODUCT_BUG,
    FailureContext, FailureDiagnosis,
)

__all__ = [
    "PROPOSAL_LOCATOR_PATCH", "PROPOSAL_WAIT_ADJUST", "PROPOSAL_DATA_FIX",
    "PROPOSAL_ASSERTION_UPDATE", "PROPOSAL_CONFIG_CHANGE",
    "PROPOSAL_PRODUCT_BUG", "PROPOSAL_ENV_ISSUE",
    "PROPOSAL_TYPES", "CODE_TYPES", "RISKS", "TEMPERATURE", "MAX_TOKENS",
    "Proposal",
    "propose", "locator_patch", "wait_adjust",
    "product_bug_proposal", "env_issue_proposal", "proposals_with_llm",
]


# ---------------------------------------------------------------------------
# 类型学: 七个类型 / 三档风险 / 自动应用的边界
# ---------------------------------------------------------------------------

PROPOSAL_LOCATOR_PATCH = "locator_patch"
PROPOSAL_WAIT_ADJUST = "wait_adjust"
PROPOSAL_DATA_FIX = "data_fix"
PROPOSAL_ASSERTION_UPDATE = "assertion_update"
PROPOSAL_CONFIG_CHANGE = "config_change"

PROPOSAL_TYPES: Tuple[str, ...] = (
    PROPOSAL_LOCATOR_PATCH, PROPOSAL_WAIT_ADJUST, PROPOSAL_DATA_FIX,
    PROPOSAL_ASSERTION_UPDATE, PROPOSAL_CONFIG_CHANGE,
    PROPOSAL_PRODUCT_BUG, PROPOSAL_ENV_ISSUE,
)

# 改测试代码的几类: 生成前要过 blame 闸门, 也只有它们谈得上"应用一个补丁"
CODE_TYPES: Tuple[str, ...] = (PROPOSAL_LOCATOR_PATCH, PROPOSAL_WAIT_ADJUST,
                               PROPOSAL_DATA_FIX, PROPOSAL_ASSERTION_UPDATE)

# LLM 只能出这几类: 产品/环境是归因层的结论, 不是"想出来的方案"
_LLM_TYPES: Tuple[str, ...] = (PROPOSAL_LOCATOR_PATCH, PROPOSAL_WAIT_ADJUST,
                               PROPOSAL_DATA_FIX, PROPOSAL_ASSERTION_UPDATE,
                               PROPOSAL_CONFIG_CHANGE)

# 永不出现在"可自动应用"里的一类 (卡片 6.5 硬约束): 断言变更 = 预期变更, 必须人审
_AUTO_EXCLUDED: Tuple[str, ...] = (PROPOSAL_ASSERTION_UPDATE,)

RISKS: Tuple[str, ...] = ("low", "medium", "high")
_RISK_ORDER: Dict[str, int] = {"low": 0, "medium": 1, "high": 2}

# 候选来自哪一层决定风险: L1 是站点坑表的精确命中, L2 是语义推断, L3/L4 是猜
_RISK_BY_LAYER: Dict[str, str] = {"L1": "low", "L2": "medium", "L3": "high", "L4": "high"}

# diff 上下文行数: 三行是 git apply 的常规容忍度, 再多只是把 hunk 拉长
_CONTEXT = 3

# 单步等待上限: 超过 30s 之后继续加只是把失败拖长, 不是修复
_WAIT_CEILING = 30.0

# 一次最多收几条 LLM 方案: 给得多只会让"挑一条"变成新的负担
MAX_PROPOSALS = 3

# 生成代码的方案要稳, 不要发挥; 1500 tokens 够写一条带 diff 的方案
TEMPERATURE = 0.1
MAX_TOKENS = 1500

# 超时值只认两种写法: timeout=10 / timeout: 10, 或者带时间单位的 "retried 10s"。
# 裸数字不认 —— 日志里的 10.000 可能是渲染进程的消息超时, 也可能是版本号; 猜错就会给出
# 一条看起来很像真的建议, 而它改的不是那个值。
_TIMEOUT_ASSIGN_RE = re.compile(r"timeout\s*[:=]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_TIMEOUT_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|secs\b|seconds?\b|秒)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Proposal: 一条可执行的方案
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Proposal:
    """一条方案 (纯数据; 层间传递, 不包含 WebElement / driver)

    字段顺序与卡片一致, 多一个 reason: 卡片正文要求"每条方案必须能回答为什么", 而
    schema 里没有它 —— 一个只描述改什么的方案, 人没法判断该不该采纳。

    frozen 的理由与 FailureDiagnosis 相同: 方案会被报告/应用/回滚三处读, 中间任何一处
    改一下字段, 事后就说不清"最后应用的是哪一版"。要 list/dict 的地方走 as_dict()。
    """

    proposal_id: str        # "S001"
    type: str               # PROPOSAL_TYPES 之一
    title: str              # "text='新增患者' → data-testid='patient-add'"
    target_file: str        # 相对路径 (posix 分隔符)
    diff: str               # unified diff (locator_patch 必填)
    risk: str               # low | medium | high
    auto_applicable: bool   # 仅 risk=low 且 diff 非空且类型改代码, 且非 assertion_update
    confidence: float
    verify_cmd: str         # "pytest tests/test_x.py::test_add -q"
    rollback_cmd: str       # "git checkout -- <target_file>"
    reason: str             # 为什么这么改能修好 (卡片正文要求, 空字符串直接拒)

    def __post_init__(self) -> None:
        if self.type not in PROPOSAL_TYPES:
            raise ValueError(f"未知方案类型 {self.type!r}; 只能是 {PROPOSAL_TYPES}")
        if self.risk not in RISKS:
            raise ValueError(f"未知风险档 {self.risk!r}; 只能是 {RISKS}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence 必须在 [0, 1]: {self.confidence}")
        if not str(self.reason).strip():
            raise ValueError("每条方案都要回答为什么: reason 不能为空")
        if self.type == PROPOSAL_LOCATOR_PATCH and not self.diff.strip():
            raise ValueError("locator_patch 必须带 diff (卡片: diff 必填); 生成不出 diff 就别产它")
        if self.diff.strip() and not _looks_like_unified_diff(self.diff):
            raise ValueError("diff 不像 unified diff (要 --- / +++ 头 + @@ hunk)")
        if self.auto_applicable and not _can_auto_apply(self.risk, self.diff, self.type):
            raise ValueError(
                "auto_applicable 只在 risk=low 且 diff 非空且类型属于改代码时成立, "
                f"且 assertion_update 恒为 False: type={self.type} risk={self.risk} "
                f"diff={'有' if self.diff else '空'}")

    @property
    def is_code_change(self) -> bool:
        """这条方案改的是测试代码 (要过 blame 闸门、能被 apply)"""
        return self.type in CODE_TYPES

    @property
    def needs_review(self) -> bool:
        """不能自动应用 —— 报告里要标出来, 别让人以为没标注就是安全的"""
        return not self.auto_applicable

    def as_dict(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "type": self.type,
            "title": self.title,
            "target_file": self.target_file,
            "diff": self.diff,
            "risk": self.risk,
            "auto_applicable": self.auto_applicable,
            "confidence": round(self.confidence, 4),
            "verify_cmd": self.verify_cmd,
            "rollback_cmd": self.rollback_cmd,
            "reason": self.reason,
        }

    def __str__(self) -> str:
        """一行流: 编号 + 类型/风险 + 改什么 + 能不能自动应用"""
        tail = "可自动应用" if self.auto_applicable else "需人工确认"
        return f"{self.proposal_id} [{self.type}/{self.risk}] {self.title} ({tail})"


# ---------------------------------------------------------------------------
# 公共小工具: 判据 / 归一 / 组装
# ---------------------------------------------------------------------------

def _can_auto_apply(risk: str, diff: str, kind: str) -> bool:
    """卡片那句"仅 low 且 diff 非空", 再加两条:

    - 只有改代码的类型才有东西可应用 (product_bug / env_issue 没有补丁);
    - assertion_update 恒为 False (卡片 6.5 硬约束): 断言变更就是预期变更, 必须人审。
    """
    return (risk == "low" and bool(str(diff or "").strip())
            and kind in CODE_TYPES and kind not in _AUTO_EXCLUDED)


def _looks_like_unified_diff(diff: str) -> bool:
    """至少得像一个 hunk: --- / +++ 头 + 至少一行 @@ (不含边界噪声)"""
    lines = str(diff).splitlines()
    return (len(lines) >= 4
            and lines[0].startswith("--- ")
            and lines[1].startswith("+++ ")
            and any(line.startswith("@@ ") for line in lines[2:]))


def _clamp(value: Any) -> float:
    """LLM 的置信度: 缺失/脏值当 0.5 (弱结论), 越界夹回 [0, 1]"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    return round(min(max(number, 0.0), 1.0), 4)


def _cap_risk(raw: str, ceiling: str = "medium") -> str:
    """风险档只能往上抬, 不能低于 ceiling —— LLM 说 low 也不等于可以自动应用"""
    if raw not in RISKS:
        return "high"
    return raw if _RISK_ORDER[raw] >= _RISK_ORDER[ceiling] else ceiling


def _raise_risk(risk: str) -> str:
    """抬一档: 一次替换多处、或者方案不够具体时用"""
    order = list(RISKS)
    return order[min(order.index(risk) + 1, len(order) - 1)] if risk in order else "high"


def _posix(path: Any) -> str:
    """相对路径统一成 posix: 报告/补丁头/git 命令在 Windows 上都得是 /"""
    return str(path or "").strip().replace("\\", "/")


def _short(text: Any, limit: int = 80) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _verify_cmd(target_file: str, test_name: str = "") -> str:
    """验证命令: 同一个用例重跑一遍, 不猜项目里怎么跑测试"""
    rel = _posix(target_file)
    if not rel:
        return ""
    node = f"{rel}::{test_name}" if test_name else rel
    return f"pytest {node} -q"


def _rollback_cmd(diff: str, target_file: str) -> str:
    """回滚命令: 只有真的改了文件才有得回滚"""
    rel = _posix(target_file)
    return f"git checkout -- {rel}" if diff and rel else ""


def _make(*, kind: str, title: str, target_file: str, diff: str, risk: str,
          confidence: Any, verify_cmd: str, reason: str) -> Proposal:
    """统一出口: auto_applicable / rollback_cmd 由本地算, 不接受调用方指定"""
    return Proposal(
        proposal_id="",                      # 编号交给 _numbered(), 生成器不关心第几条
        type=kind,
        title=_short(title),
        target_file=_posix(target_file),
        diff=str(diff or ""),
        risk=risk,
        auto_applicable=_can_auto_apply(risk, diff, kind),
        confidence=_clamp(confidence),
        verify_cmd=verify_cmd,
        rollback_cmd=_rollback_cmd(diff, target_file),
        reason=str(reason).strip(),
    )


def _numbered(items: Sequence[Proposal]) -> Tuple[Proposal, ...]:
    """按生成顺序编号 S001..: 同样的输入给同样的编号, 报告里才引用得起来"""
    return tuple(replace(item, proposal_id=f"S{index:03d}")
                 for index, item in enumerate(items, 1))


# ---------------------------------------------------------------------------
# 真 diff: 读原文 -> 替换 -> difflib
# ---------------------------------------------------------------------------

def _read_source(target_file: str, root: Optional[str],
                 source_text: Optional[str]) -> Optional[str]:
    """目标文件原文: 显式给的 source_text 优先, 否则按 root 读 (只读, 从不写文件)

    newline="" 是必须的: 默认的换行翻译会把 CRLF 变成 LF, 生成的 diff 与磁盘上的文件
    对不上, git apply 会报 "patch does not apply"。
    """
    if source_text is not None:
        return str(source_text)
    rel = _posix(target_file)
    if not rel:
        return None
    path = rel if os.path.isabs(rel) else os.path.join(root or os.getcwd(), rel)
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _unified_diff(source: str, patched: str, target_file: str) -> str:
    """(原文, 改后) -> git apply 能吃的 unified diff; 路径带 a/ b/ 前缀"""
    rel = _posix(target_file)
    return "".join(difflib.unified_diff(
        source.splitlines(keepends=True), patched.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", n=_CONTEXT))


# ---------------------------------------------------------------------------
# locator_patch: 规则层 top 候选 -> 真 diff (确定性, 不调 LLM)
# ---------------------------------------------------------------------------

def _default_ranker() -> Callable[..., Any]:
    """懒导入 healer: 只想看类型/拼命令的调用方不必先拉起 selenium"""
    from ai_kit.healer import rank_candidates
    return rank_candidates


def _rank(*, by: str, locator: str, intent: str, profile: Any,
          driver: Any, ranker: Optional[Callable[..., Any]]) -> Tuple[Any, ...]:
    """候选排序; 规则包/驱动出问题只降级成"没有候选", 不让方案层中断"""
    rank = ranker if ranker is not None else _default_ranker()
    try:
        found = rank(by=by or None, locator=locator, intent=intent,
                     profile=profile, driver=driver)
        return tuple(found or ())
    except Exception as exc:        # noqa: BLE001 — 规则包/驱动是外部输入
        print(f"  [solutions] 候选排序失败, 跳过 locator_patch ({exc})")
        return ()


def _better_candidate(candidates: Sequence[Any], broken: str) -> Optional[Any]:
    """候选里第一个"真的换了写法"的

    rank_candidates 永远把原定位器按 0.1 兜底放进候选表, 所以 top 可能就是它自己;
    只比 locator 不比 by: 本层只替换表达式字面量, 换 by (css->xpath) 得改调用代码,
    那种方案写进 reason 让人看, 不冒充成自动补丁。
    """
    for cand in candidates:
        text = str(getattr(cand, "locator", "") or "").strip()
        if text and text != broken:
            return cand
    return None


def locator_patch(diagnosis: FailureDiagnosis, *,
                  locator: str = "",
                  candidates: Optional[Sequence[Any]] = None,
                  target_file: str = "",
                  test_name: str = "",
                  root: Optional[str] = None,
                  source_text: Optional[str] = None,
                  by: str = "",
                  intent: str = "",
                  ranker: Optional[Callable[..., Any]] = None,
                  profile: Any = None,
                  driver: Any = None) -> Optional[Proposal]:
    """定位器补丁: top 候选 + 真 diff, 返回 None 表示这条方案产不出来

    五种产不出来的情况都打一行说明 (没有坏定位器 / 没有可落的相对文件 / 没有更好的
    候选 / 读不到原文 / 原文里没有原定位器) —— 卡片要求 locator_patch 必带 diff,
    拿不到原文就只能放弃, 编出来的 diff 比没有方案更糟。
    """
    broken = str(locator or "").strip()
    if not broken:
        print("  [solutions] 没有坏定位器, 跳过 locator_patch")
        return None
    diagnoser.require_patch_allowed(diagnosis)
    rel = _posix(target_file)
    if not rel or os.path.isabs(rel):
        # 补丁要落到一个相对仓库根的文件上: 没有它就没法写 a/ b/ 头, 也没法回滚
        print("  [solutions] 没有可用的相对 target_file, 补丁无处可落, 跳过 locator_patch")
        return None

    found = candidates if candidates is not None else _rank(
        by=by, locator=broken, intent=intent, profile=profile,
        driver=driver, ranker=ranker)
    cand = _better_candidate(found, broken)
    if cand is None:
        print(f"  [solutions] 没有比 {broken!r} 更好的候选, 跳过 locator_patch")
        return None

    source = _read_source(target_file, root, source_text)
    if source is None:
        print(f"  [solutions] 读不到 {target_file or '(未给目标文件)'} 原文, "
              "生成不出 diff, 跳过 locator_patch")
        return None
    occurrences = source.count(broken)
    if not occurrences:
        print(f"  [solutions] {_posix(target_file) or '原文'} 里找不到 {broken!r}, "
              "生成不出 diff, 跳过 locator_patch")
        return None

    new_locator = str(cand.locator).strip()
    diff = _unified_diff(source, source.replace(broken, new_locator), target_file)
    layer = str(getattr(cand, "layer", "") or "")
    risk = _RISK_BY_LAYER.get(layer, "high")
    if occurrences > 1:
        risk = _raise_risk(risk)            # 一次换多处, 得有人看一眼

    reason = (f"{layer or '候选表'} 给出 {new_locator} "
              f"(权重 {float(getattr(cand, 'confidence', 0.0)):.2f})")
    layer_reason = str(getattr(cand, "reason", "") or "").strip()
    if layer_reason:
        reason += f": {layer_reason}"
    reason += f"; 本次根因是 {diagnosis.root_cause}"
    if occurrences > 1:
        reason += f"; 原定位器在文件里出现 {occurrences} 次, 一次全换"

    return _make(kind=PROPOSAL_LOCATOR_PATCH,
                 title=f"{broken} → {new_locator}",
                 target_file=target_file, diff=diff, risk=risk,
                 confidence=getattr(cand, "confidence", 0.5),
                 verify_cmd=_verify_cmd(target_file, test_name),
                 reason=reason)


# ---------------------------------------------------------------------------
# wait_adjust: 规则模板 (不调 LLM, 也不编 diff)
# ---------------------------------------------------------------------------

def _wait_numbers(text: str) -> Optional[Tuple[float, float]]:
    """从失败文本里取现有超时值 -> (现在, 建议值); 取不到返回 None

    建议值 = 翻倍且不超过上限; 已经到上限时返回 (old, old), 由调用方改说辞 ——
    "30s -> 60s" 不是修复, 只是把失败拖长一倍。
    """
    value = str(text or "")
    match = _TIMEOUT_ASSIGN_RE.search(value) or _TIMEOUT_UNIT_RE.search(value)
    if not match:
        return None
    try:
        old = float(match.group(1))
    except ValueError:
        return None
    if old <= 0:
        return None
    new = round(min(old * 2, _WAIT_CEILING), 1)
    # 已经超过上限时不能"往下调": 40s -> 30s 是把超时改小, 比不改更糟
    return old, new if new > old else old


def wait_adjust(diagnosis: FailureDiagnosis, *,
                target_file: str = "",
                test_name: str = "",
                text: str = "") -> Proposal:
    """等待调整: 超时类失败的兜底改法, 只改等待值 / 等待条件

    为什么只给参数不给 diff: 等待值写在哪个调用点 (page object / 用例 / 全局配置)
    因项目而异, 这里没有原文就没法定位到行; 编一个 hunk 还不如告诉人改什么。
    """
    diagnoser.require_patch_allowed(diagnosis)
    numbers = _wait_numbers(text or diagnosis.root_cause)
    root = diagnosis.root_cause or "超时"

    if numbers is None:
        title = "显式等待: 等具体条件 (元素可见/可点击) 而不是盲等固定时长"
        risk, confidence = "high", 0.4
        reason = (f"规则模板: 根因是 {root}; 日志里没有可用的超时值, 所以只给方向 —— "
                  "把盲等换成等具体条件, 别加一个更大的魔法数字")
    elif numbers[0] == numbers[1]:
        title = f"等待已 {numbers[0]:g}s 仍超时 → 不加时长, 改为等待具体条件"
        risk, confidence = "medium", 0.5
        reason = (f"规则模板: 根因是 {root}; 已经等 {numbers[0]:g}s 还超时, 继续加只是把"
                  f"失败拖长 (上限 {_WAIT_CEILING:g}s) —— 该查的是元素到底在不在, "
                  "那属于 locator_patch / 产品缺陷")
    else:
        old, new = numbers
        title = f"显式等待 {old:g}s → {new:g}s"
        risk, confidence = "medium", 0.6
        reason = (f"规则模板: 根因是 {root}; 超时类失败里加等待是代价最小的改法 "
                  f"(不动断言、不动定位器), 代价是这条用例失败时要多等 {new - old:g}s; "
                  f"若 {new:g}s 后仍找不到元素, 说明元素根本不在, 该走 locator_patch")

    return _make(kind=PROPOSAL_WAIT_ADJUST, title=title, target_file=target_file,
                 diff="", risk=risk, confidence=min(diagnosis.confidence, confidence),
                 verify_cmd=_verify_cmd(target_file, test_name), reason=reason)


# ---------------------------------------------------------------------------
# 两个终态方案: 提缺陷 / 修环境 (没有 diff, 也永远不自动应用)
# ---------------------------------------------------------------------------

def product_bug_proposal(diagnosis: FailureDiagnosis, *,
                         target_file: str = "", test_name: str = "") -> Proposal:
    """产品缺陷: 只给缺陷单方向, 改任何代码都是掩盖 bug (风险最高)"""
    return _make(kind=PROPOSAL_PRODUCT_BUG,
                 title=f"提缺陷: {diagnosis.root_cause or '页面行为与预期不符'}",
                 target_file=target_file, diff="", risk="high",
                 confidence=diagnosis.confidence,
                 verify_cmd=_verify_cmd(target_file, test_name),
                 reason=(f"{diagnoser.decide(diagnosis).reason}; 根因: {diagnosis.root_cause}; "
                         f"证据: {'; '.join(diagnosis.evidence[:3]) or '无'}"))


def env_issue_proposal(diagnosis: FailureDiagnosis, *,
                       target_file: str = "", test_name: str = "") -> Proposal:
    """环境问题: 先修环境再重跑; 此时改脚本只是把环境噪声写进用例"""
    return _make(kind=PROPOSAL_ENV_ISSUE,
                 title=f"先修环境再重跑: {diagnosis.root_cause or '页面已不可访问'}",
                 target_file=target_file, diff="", risk="medium",
                 confidence=diagnosis.confidence,
                 verify_cmd=_verify_cmd(target_file, test_name),
                 reason=(f"{diagnoser.decide(diagnosis).reason}; "
                         f"检查清单见 diagnoser.env_checklist(); 根因: {diagnosis.root_cause}"))


# ---------------------------------------------------------------------------
# 复杂类型: LLM 出草稿 (带失败上下文 + 候选定位器 + DOM 差异)
# ---------------------------------------------------------------------------

LLM_SYSTEM = """你在为一次已经归因为"脚本问题"的 UI 自动化失败写修复方案。只输出 JSON。

硬约束:
1. type 只能取这五个之一, 原样抄写: locator_patch / wait_adjust / data_fix /
   assertion_update / config_change。产品缺陷与环境问题由归因层判定, 不归你写。
2. diff 必须是能直接 `git apply` 的 unified diff: --- / +++ 头, 至少一行 @@, 三行上下文;
   只改必要的行, 不许重写整个文件, 不许顺手做别的优化。
3. 没有给出目标文件原文时, diff 必须留空字符串 —— 不许凭空写上下文和行号,
   编出来的 diff 会让 git apply 报错, 比没有方案更糟。
4. reason 必填, 要说清"为什么这样改能修好这次失败", 不许复述 diff, 不许写
   "提高健壮性" "增加稳定性" 这类空话。
5. 不许发明文件路径、元素、接口、断言值或步骤; 缺证据就把缺的那条写进 reason。
6. target_file 只能取给定的那一个路径, 不许猜别的文件。
7. risk 只能 low / medium / high, 拿不准写 high。
8. 最多 3 条, 按可能性从高到低排。

输出 schema:
{"proposals": [{"type": "wait_adjust", "title": "给确定按钮加显式等待",
  "target_file": "tests/test_user.py", "diff": "", "risk": "medium",
  "confidence": 0.6, "reason": "点确定前没有等待弹窗动画结束, 元素还没可点击就点了"}]}
"""


def _default_llm() -> Optional[Callable[..., Any]]:
    """默认通道: chat_json + 显式 temperature/max_tokens (5.10 的额度约束)"""
    try:
        from ai_kit.client import chat_json, llm_available
    except Exception:               # noqa: BLE001
        return None
    if not llm_available():
        return None
    return partial(chat_json, max_retries=1,
                   temperature=TEMPERATURE, max_tokens=MAX_TOKENS)


def _describe_candidates(candidates: Sequence[Any]) -> str:
    """候选定位器 -> 给 LLM 的证据行 (没有候选就直说, 不让它自己编一个)"""
    lines = []
    for cand in candidates[:5]:
        lines.append(f"- {getattr(cand, 'layer', '?')} "
                     f"{getattr(cand, 'by', '?')}={getattr(cand, 'locator', '?')} "
                     f"(权重 {float(getattr(cand, 'confidence', 0.0)):.2f}) "
                     f"{getattr(cand, 'reason', '') or ''}".rstrip())
    return "\n".join(lines) if lines else "没有候选 (规则层没给出可用定位器)"


def _llm_user(diagnosis: FailureDiagnosis, context: Optional[FailureContext], *,
              candidates: Sequence[Any], dom_diff: str,
              target_file: str, source_available: bool) -> str:
    """只贴事实: 用例 / traceback / 日志 / 步骤 / 页面快照 / 归因 / 候选 / DOM 差异

    出口过 sanitize: traceback、快照、dom_diff 里都可能有患者数据, 而这段文本
    只给 LLM 看 (改目标文件 / 候选定位串 / 归因文字一律不碰)。
    """
    parts = []
    label = ""
    if context is not None:
        label = context.case_id or context.test_name
    if label:
        parts.append(f"=== 用例 ===\n{label}")
    if context is not None:
        parts.append(f"=== Traceback ===\n{diagnoser._tail(context.traceback, 2000)}")
        parts.append(f"=== Log ===\n{diagnoser._tail(context.log, 1000)}")
        if context.steps:
            numbered = "\n".join(f"{i}. {step}"
                                 for i, step in enumerate(context.steps[:20], 1))
            parts.append(f"=== 执行步骤 ===\n{numbered}")
        parts.append("=== 页面快照 ===\n"
                     + diagnoser.describe_observation(context.observation))
    parts.append(f"=== 归因 ===\n分类 {diagnosis.category} / blame {diagnosis.blame}\n"
                 f"根因 {diagnosis.root_cause}")
    parts.append("=== 候选定位器 ===\n" + _describe_candidates(candidates))
    parts.append("=== DOM 差异 ===\n"
                 + (dom_diff or "未采集 (没有失败前后的 DOM diff, 不许臆测页面结构)"))
    parts.append(f"=== 目标文件 ===\n{_posix(target_file) or '未提供'}"
                 + (": 已给原文, 可以写 diff" if source_available
                    else ": 没有原文, diff 必须留空"))
    return sanitize("\n\n".join(parts))


def _from_llm(item: Any, *,
              target_file: str, test_name: str, source_available: bool
              ) -> Optional[Proposal]:
    """一条 LLM 方案 -> Proposal; 任何一条不自洽就丢弃 (返回 None)"""
    if not isinstance(item, dict):
        return None
    kind = str(item.get("type") or "").strip()
    if kind not in _LLM_TYPES:
        print(f"  [solutions] LLM 方案类型 {kind!r} 不在白名单, 丢弃")
        return None
    reason = str(item.get("reason") or "").strip()
    if not reason:
        print(f"  [solutions] LLM 的 {kind} 没给 reason, 丢弃")
        return None
    diff = str(item.get("diff") or "")
    if diff.strip() and not _looks_like_unified_diff(diff):
        print("  [solutions] LLM 给的 diff 不是 unified diff, 丢弃")
        return None
    if diff.strip() and not source_available:
        print("  [solutions] 没有目标文件原文却给了 diff, 丢弃 (不许编上下文)")
        return None
    wanted = _posix(item.get("target_file") or target_file)
    if target_file and wanted != _posix(target_file):
        print(f"  [solutions] LLM 想把方案写到 {wanted}, 但我们只给了 "
              f"{_posix(target_file)}, 丢弃")
        return None
    if diff.strip() and not wanted:
        print("  [solutions] LLM 给了 diff 却没给目标文件, 补丁无处可落, 丢弃")
        return None
    if kind == PROPOSAL_LOCATOR_PATCH and not diff.strip():
        print("  [solutions] LLM 的 locator_patch 没带 diff, 丢弃 (卡片: diff 必填)")
        return None

    title = str(item.get("title") or "").strip() or f"{kind} (LLM 草稿)"
    return _make(kind=kind, title=title, target_file=wanted, diff=diff,
                 risk=_cap_risk(str(item.get("risk") or "").strip().lower()),
                 confidence=item.get("confidence"),
                 verify_cmd=_verify_cmd(wanted, test_name),
                 reason=reason)


def proposals_with_llm(diagnosis: FailureDiagnosis,
                       context: Optional[FailureContext] = None, *,
                       llm: Optional[Callable[..., Any]] = None,
                       target_file: str = "",
                       test_name: str = "",
                       dom_diff: str = "",
                       candidates: Sequence[Any] = (),
                       source_available: bool = False) -> Tuple[Proposal, ...]:
    """复杂类型的草稿: 只有规则层算不出来的才走这里 (要给 API 就把 llm 接上)

    llm: (system, user) -> dict|None。抛错/返回怪数据一律降级成 () —— 方案层不能
    因为模型抽风就中断; BudgetExceeded 除外, 预算没了要让人知道。
    """
    diagnoser.require_patch_allowed(diagnosis)
    ask = llm if llm is not None else _default_llm()
    if ask is None:
        return ()
    try:
        result = ask(LLM_SYSTEM, _llm_user(diagnosis, context, candidates=candidates,
                                           dom_diff=dom_diff, target_file=target_file,
                                           source_available=source_available))
    except Exception as exc:        # noqa: BLE001 — 注入的客户端可能任意抛
        if isinstance(exc, BudgetExceeded):
            raise
        print(f"  [solutions] LLM 生成方案失败, 跳过 ({exc})")
        return ()
    if not isinstance(result, dict):
        return ()
    raw = result.get("proposals")
    if not isinstance(raw, list):
        return ()

    out = []
    for item in raw[:MAX_PROPOSALS]:
        proposal = _from_llm(item, target_file=target_file, test_name=test_name,
                             source_available=source_available)
        if proposal is not None:
            out.append(proposal)
    return tuple(out)


# ---------------------------------------------------------------------------
# 主入口: 一次失败 -> 一组方案 (规则优先, 拿不到才问 LLM)
# ---------------------------------------------------------------------------

def _failure_text(context: Optional[FailureContext]) -> str:
    if context is None:
        return ""
    return f"{context.traceback}\n{context.log}"


def propose(diagnosis: FailureDiagnosis, *,
            context: Optional[FailureContext] = None,
            by: str = "",
            locator: str = "",
            intent: str = "",
            target_file: str = "",
            test_name: str = "",
            root: Optional[str] = None,
            source_text: Optional[str] = None,
            ranker: Optional[Callable[..., Any]] = None,
            profile: Any = None,
            driver: Any = None,
            dom_diff: str = "",
            use_llm: Optional[bool] = None,
            llm: Optional[Callable[..., Any]] = None) -> Tuple[Proposal, ...]:
    """失败 -> 一组方案, 编号从 S001 开始

    分流完全跟着 blame 走 (卡片 6.1 的闸门):
        blame=product      -> product_bug (一条, 无 diff)
        blame=environment  -> env_issue  (一条, 无 diff)
        blame=test_code    -> locator_patch (定位器类) / wait_adjust (等待类)
        blame=unknown      -> 空 (交人工, 不猜)

    use_llm 三态 (只对 blame=test_code 生效):
        None (默认) 规则层一条都没产出时才问 LLM —— 规则能算的绝不多花一次钱
        True        强制要草稿 (人工点了"再给几条")
        False       只走规则 (评测/离线可复现, 一次网络都不发)

    source_text: 目标文件原文; 不给就按 root 读。读不到时 locator_patch 不产出,
    LLM 也会被要求 diff 留空 —— 没有原文的 diff 一定是编的。
    """
    decision = diagnoser.decide(diagnosis)
    items = []
    # 先给默认值: 下面只有 test_code 分支才会真的算候选/读原文, 但两条路径之后
    # 要一起交给 LLM 当证据 —— 不预置, 读代码的人会以为这里可能拿到未定义变量
    source: Optional[str] = None
    candidates: Tuple[Any, ...] = ()

    if decision.proposal_type == PROPOSAL_PRODUCT_BUG:
        items.append(product_bug_proposal(diagnosis, target_file=target_file,
                                          test_name=test_name))
    elif decision.proposal_type == PROPOSAL_ENV_ISSUE:
        items.append(env_issue_proposal(diagnosis, target_file=target_file,
                                        test_name=test_name))
    elif decision.may_edit_test_code:
        # 这里不用再调 require_patch_allowed: 三个子生成器各自入门就查 (真正的闸门在
        # 子生成器上, 直接调它们的人也不会绕过去)
        source = _read_source(target_file, root, source_text)
        if locator:
            candidates = _rank(by=by, locator=locator, intent=intent,
                               profile=profile, driver=driver, ranker=ranker)

        # 主结论先出; 6.2 说"另一个评审也会点头"时把那条也出 (定位器 / 等待 同时命中)
        plan = []
        if diagnosis.category == analyzer.CATEGORY_SCRIPT_HEALER:
            plan.append("patch")
        elif diagnosis.category == analyzer.CATEGORY_SCRIPT_WAIT:
            plan.append("wait")
        if (analyzer.CATEGORY_SCRIPT_HEALER in diagnosis.acceptable_categories
                and "patch" not in plan):
            plan.append("patch")
        if (analyzer.CATEGORY_SCRIPT_WAIT in diagnosis.acceptable_categories
                and "wait" not in plan):
            plan.append("wait")
        # 语法/导入类: 规则无从下手, 只能等 LLM 或人工

        for kind in plan:
            if kind == "patch":
                patch = locator_patch(diagnosis, locator=locator, candidates=candidates,
                                      target_file=target_file, test_name=test_name,
                                      source_text=source)
                if patch is not None:
                    items.append(patch)
            else:
                items.append(wait_adjust(diagnosis, target_file=target_file,
                                         test_name=test_name, text=_failure_text(context)))
    else:
        print("  [solutions] 归因不明 (unknown), 不出方案, 交人工")

    if decision.may_edit_test_code and (use_llm is True or (use_llm is None and not items)):
        items.extend(proposals_with_llm(
            diagnosis, context, llm=llm, target_file=target_file, test_name=test_name,
            dom_diff=dom_diff, candidates=candidates,
            source_available=source is not None))

    return _numbered(items)
