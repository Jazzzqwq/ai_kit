# -*- coding: utf-8 -*-
"""
原型分类 — 这一页是登录 / 列表CRUD / 表单 / 详情 / 看板, 纯规则, 零成本

为什么规则优先: 原型决定"该测什么"(④ strategies 按原型取 checklist), 判错一次会被
⑤⑥ 放大成一批错用例。规则分类可复现、可解释 (evidence)、可单测、不烧 token;
LLM 只在规则自己都没把握 (置信度 < 0.6) 时被请来兜底, 默认关闭。

判定顺序 (先中先算, 不是取分高者):
    login -> list_crud -> form -> dashboard -> detail -> unknown

与卡片 5.4 的一处偏离: 卡片把 dashboard 排在 detail 之后, 但 detail 的条件
(输入 <= 1 且无 destructive) 基本是 dashboard 的超集 —— 看板页往往就一个筛选框、
没有删除按钮, 照抄顺序等于 dashboard 永远不中。所以 dashboard 提到 detail 之前,
由 test_dashboard_beats_detail 钉住这个顺序。

阈值/关键词全在下面的常量里: 调分类行为只改数字, 不改逻辑。

用法:
    from ai_kit.perception import classify, annotate

    result = classify(inv)                 # ArchetypeResult (纯规则, 0 成本)
    result.archetype, result.confidence, result.evidence, result.needs_llm()
    inv2 = annotate(inv)                   # 回填 inv.archetype, 指纹不变
    result = classify(inv, allow_llm=True) # 低置信度时问 LLM 一次 (可注入 ask 便于测试)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from ai_kit.perception.inventory import ElementSpec, PageInventory, from_inventory
from ai_kit.utils import sanitize

# ---------------------------------------------------------------------------
# 原型名 (④ strategies 用这些字符串查 checklist, 别随手改)
# ---------------------------------------------------------------------------

LOGIN = "login"
LIST_CRUD = "list_crud"
FORM = "form"
DETAIL = "detail"
DASHBOARD = "dashboard"
UNKNOWN = "unknown"

ARCHETYPES: Tuple[str, ...] = (LOGIN, LIST_CRUD, FORM, DETAIL, DASHBOARD, UNKNOWN)

# --- 判定用的阈值 (改这里就是改分类行为) ---

LOGIN_KEYWORDS: Tuple[str, ...] = ("登录", "登陆", "登入", "sign in", "signin", "log in", "login")
LIST_ACTION_CONTAINERS: Tuple[str, ...] = ("toolbar", "filter-bar")
LIST_MIN_ACTIONS = 2                 # toolbar/filter-bar 里至少几个按钮才算"有操作区"
FORM_MIN_REQUIRED = 2                # 没有 <form> 时, 至少几个必填字段才认成表单页
DASHBOARD_MAX_INTERACTIVE = 12       # "交互元素少"
DASHBOARD_MIN_CONTAINERS = 2         # "分散": 元素摊在至少两类容器里
DETAIL_MAX_INPUTS = 1                # 详情页至多一个输入 (通常是搜索/备注)

LLM_CONFIDENCE_THRESHOLD = 0.6       # 低于它才值得花一次 LLM

# --- 各规则的置信度 ---
# 全部 >= LLM_CONFIDENCE_THRESHOLD: 规则一旦命中就是"敢用"的结论, 不会次次都去问 LLM。
CONF_LOGIN = 0.95                    # 密码框 + 登录按钮, 两条独立证据
CONF_LIST_CRUD = 0.85                # 表格 + 操作区 + 有行内按钮 (真的能增删改)
CONF_LIST_CRUD_NO_ROWS = 0.75        # 有表格 + 操作区, 但没找到行内按钮
CONF_FORM = 0.80                     # 有 <form> 容器
CONF_FORM_REQUIRED_ONLY = 0.72       # 没 <form> (组件库常自己渲染), 靠必填字段数推断
CONF_DASHBOARD = 0.75                # 有图表 + 元素少而分散
CONF_DETAIL = 0.65                   # 兜底型: 输入少 + 没有危险动作

_LOGIN_WORDS: Tuple[str, ...] = tuple(re.sub(r"\s+", "", word).lower() for word in LOGIN_KEYWORDS)


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArchetypeResult:
    """分类结果: 结论 + 置信度 + 证据 + 各规则得分。

    scores 里有每一条命中的规则及其置信度 —— 排查"为什么被分到 detail"时,
    看它就能发现"其实 login 也中了一部分, 只差一个登录按钮"。
    """

    archetype: str = UNKNOWN
    confidence: float = 0.0
    rule: str = ""                       # 命中的规则名; 没命中 -> "no-match"
    evidence: Tuple[str, ...] = ()
    scores: Dict[str, float] = field(default_factory=dict)
    source: str = "rule"                 # rule / llm

    @property
    def is_unknown(self) -> bool:
        return self.archetype == UNKNOWN

    def needs_llm(self, threshold: float = LLM_CONFIDENCE_THRESHOLD) -> bool:
        return self.confidence < threshold

    def to_dict(self) -> Dict[str, Any]:
        return {"archetype": self.archetype,
                "confidence": self.confidence,
                "rule": self.rule,
                "evidence": list(self.evidence),
                "scores": dict(self.scores),
                "source": self.source}


@dataclass(frozen=True)
class _Hit:
    """规则命中: 只是中间结果, 不进公开 API"""

    archetype: str
    confidence: float
    evidence: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 小助手
# ---------------------------------------------------------------------------

def _flat(text: str) -> str:
    """去掉所有空白再小写: "登 录" / "Sign In" 都要能匹配"""
    return re.sub(r"\s+", "", text or "").lower()


def _field_type(element: ElementSpec) -> str:
    """输入控件的 HTML type: 约束里最全, 其次是元素本身/属性"""
    for source in (element.constraints.get("type"), element.type, element.attrs.get("type")):
        if isinstance(source, str) and source.strip():
            return source.strip().lower()
    return ""


def _as_page(inventory: Any) -> PageInventory:
    return inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)


def _containers(inv: PageInventory) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for element in inv.elements:
        counts[element.container] = counts.get(element.container, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 五条规则 (纯函数, 不中返回 None)
# ---------------------------------------------------------------------------

def _rule_login(inv: PageInventory) -> Optional[_Hit]:
    """密码框 + 登录按钮。只有密码框没有登录按钮 -> 不中 (改密码页也是密码框)"""
    password = [e for e in inv.elements if e.is_field and _field_type(e) == "password"]
    if not password:
        return None
    for element in inv.elements:
        if not element.is_action:
            continue
        haystack = _flat(f"{element.name} {element.text}")
        if any(word in haystack for word in _LOGIN_WORDS):
            return _Hit(LOGIN, CONF_LOGIN,
                        (f"password-input:{password[0].eid}",
                         f"login-button:{element.eid}:{element.name}"))
    return None


def _rule_list_crud(inv: PageInventory) -> Optional[_Hit]:
    """表格 + 操作区至少 2 个按钮; 找到行内按钮则更像真的 CRUD"""
    if not inv.tables:
        return None
    actions = [e for e in inv.elements
               if e.is_action and e.container in LIST_ACTION_CONTAINERS]
    if len(actions) < LIST_MIN_ACTIONS:
        return None
    row_actions = [a for table in inv.tables for a in (table.get("row_actions") or ())]
    confidence = CONF_LIST_CRUD if row_actions else CONF_LIST_CRUD_NO_ROWS
    return _Hit(LIST_CRUD, confidence,
                (f"tables:{len(inv.tables)}",
                 f"toolbar-actions:{len(actions)}",
                 f"row-actions:{len(row_actions)}"))


def _rule_form(inv: PageInventory) -> Optional[_Hit]:
    """有 <form> 容器, 或 (没有表格且) 至少 2 个必填字段"""
    required = [e for e in inv.elements if e.is_field and e.constraints.get("required")]
    in_form = [e for e in inv.elements if e.container == "form"]
    if in_form:
        return _Hit(FORM, CONF_FORM,
                    (f"form-container-elements:{len(in_form)}", f"required:{len(required)}"))
    if len(required) >= FORM_MIN_REQUIRED and not inv.tables:
        return _Hit(FORM, CONF_FORM_REQUIRED_ONLY,
                    (f"required-inputs:{len(required)}", "no-table"))
    return None


def _rule_dashboard(inv: PageInventory) -> Optional[_Hit]:
    """有图表 + 交互元素少而分散。

    "分散"用容器种类数近似; 元素本来就 <= 2 个时无从判断分散, 不否决
    (一个只有图表和导出按钮的看板, 不该因为只有一个容器就被踢出去)。
    """
    charts = inv.signals.get("charts", 0)
    if charts < 1:
        return None
    interactive = inv.stats.get("interactive_count", len(inv.elements))
    if interactive > DASHBOARD_MAX_INTERACTIVE:
        return None
    containers = _containers(inv)
    if len(containers) < DASHBOARD_MIN_CONTAINERS and interactive > 2:
        return None
    return _Hit(DASHBOARD, CONF_DASHBOARD,
                (f"charts:{charts}", f"interactive:{interactive}",
                 f"containers:{len(containers)}"))


def _rule_detail(inv: PageInventory) -> Optional[_Hit]:
    """只读展示: 输入 <= 1 且没有 destructive 动作。

    空页面 (没元素也没表格) 不算详情页 —— 那是采集失败或白屏, 应该落到 unknown。
    """
    if not inv.elements and not inv.tables:
        return None
    inputs = [e for e in inv.elements if e.is_field]
    if len(inputs) > DETAIL_MAX_INPUTS:
        return None
    if any(e.risk == "destructive" for e in inv.elements):
        return None
    return _Hit(DETAIL, CONF_DETAIL,
                (f"inputs:{len(inputs)}", "no-destructive",
                 f"interactive:{len(inv.elements)}"))


# 顺序即优先级: 先中先算。dashboard 提到 detail 前的原因见文件头。
_RULES: Tuple[Tuple[str, Callable[[PageInventory], Optional[_Hit]]], ...] = (
    (LOGIN, _rule_login),
    (LIST_CRUD, _rule_list_crud),
    (FORM, _rule_form),
    (DASHBOARD, _rule_dashboard),
    (DETAIL, _rule_detail),
)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def classify(inventory: Any, *, allow_llm: bool = False,
             ask: Optional[Callable[[str], Any]] = None) -> ArchetypeResult:
    """原型分类。默认纯规则 (0 成本); allow_llm=True 时低置信度才问一次 LLM。

    inventory 可以是 PageInventory, 也可以是 collector 的 dict。
    """
    inv = _as_page(inventory)
    scores: Dict[str, float] = {}
    winner: Optional[_Hit] = None

    for name, rule in _RULES:
        hit = rule(inv)
        if hit is None:
            continue
        scores[name] = round(hit.confidence, 2)
        if winner is None:
            winner = hit

    if winner is None:
        result = ArchetypeResult(UNKNOWN, 0.0, "no-match",
                                 (f"elements:{len(inv.elements)}",
                                  f"tables:{len(inv.tables)}",
                                  f"charts:{inv.signals.get('charts', 0)}"), scores)
    else:
        result = ArchetypeResult(winner.archetype, winner.confidence, winner.archetype,
                                 winner.evidence, scores)

    if allow_llm and result.needs_llm():
        result = confirm_with_llm(result, inv, ask=ask)
    return result


def annotate(inventory: Any, result: Optional[ArchetypeResult] = None, *,
             allow_llm: bool = False,
             ask: Optional[Callable[[str], Any]] = None) -> PageInventory:
    """把分类结论回填进清单 (返回新对象, 指纹不变)。

    unknown 也会被写进去 —— 空着和"明确不知道"对下游是两件事: 前者像忘了分类,
    后者可以安心退回通用 checklist。
    """
    inv = _as_page(inventory)
    outcome = result if result is not None else classify(inv, allow_llm=allow_llm, ask=ask)
    return inv.with_archetype(outcome.archetype)


# ---------------------------------------------------------------------------
# 可选: LLM 兜底
# ---------------------------------------------------------------------------

CONFIRM_SYSTEM = (
    "你是 Web 页面原型分类器。候选原型只有: login(登录页) / list_crud(列表增删改查页) / "
    "form(表单填写页) / detail(只读详情页) / dashboard(数据看板) / unknown(看不出来)。"
    "根据给定的元素清单摘要判断, 不要臆造页面上没有的控件。"
)

_CONFIRM_TEMPLATE = """页面标题: {title}
统计: 可交互 {interactive} (输入 {inputs} / 按钮 {buttons} / 下拉 {selects}) · 表格 {tables} · 图表 {charts} · 弹窗 {dialogs}
容器分布: {containers}
规则结论: {archetype} (置信度 {confidence}); 各规则得分: {scores}
代表性元素:
{elements}
候选原型: {candidates}
只输出 JSON: {{"archetype": "候选原型之一", "confidence": 0.0-1.0, "reason": "一句话理由"}}"""


def _confirm_prompt(inv: PageInventory, result: ArchetypeResult) -> str:
    """拼确认提示词; 出境前过 sanitize

    送出去的是标题 + 元素摘要 (name 是占位符 / 可见文案)。详情页标题常写成
    "患者详情 - 张三", 列表页元素名可能带"病历号：MR001", 这段文本的唯一去向
    就是 LLM, 所以在出口统一抹一遍。eid / kind / container 不带 PII 形态, 抹不到 ——
    而分类判断恰恰靠它们, 骨架必须原样留着。
    """
    stats = inv.stats
    lines = []
    for element in inv.elements[:20]:
        bits = [element.eid, element.kind or element.tag]
        if element.name:
            bits.append(f"name={element.name[:20]}")
        bits.append(f"container={element.container}")
        if element.risk != "safe":
            bits.append(f"risk={element.risk}")
        if element.constraints.get("required"):
            bits.append("required")
        lines.append("- " + " ".join(bits))
    return sanitize(_CONFIRM_TEMPLATE.format(
        title=inv.title or inv.page_name or "(无标题)",
        interactive=stats.get("interactive_count", len(inv.elements)),
        inputs=stats.get("inputs", 0),
        buttons=stats.get("buttons", 0),
        selects=stats.get("selects", 0),
        tables=stats.get("tables", 0),
        charts=inv.signals.get("charts", 0),
        dialogs=stats.get("dialogs", 0),
        containers=json.dumps(_containers(inv), ensure_ascii=False),
        archetype=result.archetype,
        confidence=result.confidence,
        scores=json.dumps(result.scores, ensure_ascii=False),
        elements="\n".join(lines) or "(无)",
        candidates=" / ".join(ARCHETYPES),
    ))


def _parse_answer(answer: Any) -> Dict[str, Any]:
    if isinstance(answer, Mapping):
        return dict(answer)
    if isinstance(answer, str) and answer.strip():
        try:
            parsed = json.loads(answer)
        except ValueError:                       # 模型爱套 ```json, 交给 client 那层处理
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _ask_llm(prompt: str) -> Any:
    """默认问法: 惰性 import client —— 本模块的默认路径不碰网络也不碰密钥"""
    from ai_kit import client

    return client.chat_json(CONFIRM_SYSTEM, prompt, max_retries=0)


def confirm_with_llm(result: ArchetypeResult, inventory: Any, *,
                     ask: Optional[Callable[[str], Any]] = None) -> ArchetypeResult:
    """低置信度时请 LLM 看一眼; 任何异常/胡话都退回原结论, 绝不抛。

    返回值来源用 source 标出来 (rule / llm), 便于统计"LLM 到底改没改分类"。
    """
    inv = _as_page(inventory)
    try:
        answer = (ask or _ask_llm)(_confirm_prompt(inv, result))
    except Exception:                            # noqa: BLE001 — 兜底环节不能自己变成故障点
        return result

    parsed = _parse_answer(answer)
    name = str(parsed.get("archetype") or "").strip().lower()
    if name not in ARCHETYPES:
        return result
    try:
        confidence = float(parsed.get("confidence"))
    except (TypeError, ValueError):
        confidence = LLM_CONFIDENCE_THRESHOLD
    confidence = round(max(0.0, min(1.0, confidence)), 2)
    reason = str(parsed.get("reason") or "").strip()[:60]
    evidence = result.evidence + ((f"llm:{reason}",) if reason else ())
    return ArchetypeResult(name, confidence, "llm", evidence, result.scores, "llm")
