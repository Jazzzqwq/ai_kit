# -*- coding: utf-8 -*-
"""
用例计划 — ④ 的种子 -> 卡片 5.6 的 plan JSON (结构化动作, 不是代码)

这一步是从「TODO 注释骨架」到「可执行用例」的分界线:
    steps = [{"action": "fill", "eid": "e12", "value": "' OR 1=1 --"},
             {"action": "click", "eid": "e15"},
             {"action": "assert_no_error_toast"}]
渲染器 (⑥) 才把它翻译成 driver.find_element(...).send_keys(...)。计划里出现代码,
就等于把定位漂移的自愈、失败归因、缓存复用全部锁死在一坨字符串里。

四条规矩:

1. **确定性优先**。expand() 已经给出「测什么」, 这里按策略 key 查步骤模板补齐
   「怎么测」。同一份清单 + 同一个页面 -> 逐字节相同的计划 (generated_at 除外),
   回归 diff 才有意义。LLM 只是可选增强, 而且它的输出必须过校验才准进计划。

2. **ref 是清单引用, 不是 DOM id**。步骤里的 eid 一律走 resolve_ref():
   eid / "c1.column:姓名" / "c1.row:删除" / "chart:1" / "page" 都能解析。
   列标题本来就不是可交互元素, 硬塞一个假 eid 才是自欺欺人。

3. **校验闭环, 不静默丢**。每个步骤都对着动作词表 + 元素清单校验 (动作存在 /
   eid 解析得出 / 动作打在该控件上 / value 类型对)。校验不过的用例留在计划里,
   问题进 diagnostics —— 宁可报告上多一行红字, 也不要少一条用例还看不出来。

4. **不编数据**。@maxlength 算得出长度就填长度, 算不出就原样留 token 并记
   unresolved; 页面没有导出按钮就跳过「列表 vs 导出」并说明原因。计划是给人执行
   和给机器渲染的, 不是把覆盖率画满的涂色板。

卡片之外只有两个键 (都在 case 里, ⑥ 可以无视):
- `cid`: 稳定身份 (④ 的 cid), case_id 是按顺序编的号, 换页/增删用例就会变;
- `expect`: 自然语言期望, 断言步骤编不出来的那部分 (如「提示不区分用户名还是密码错」)。
diagnostics (missing / skipped / unresolved / problems) 不进 to_dict: 卡片 schema
是 ⑥ 的输入契约, 不该为未知键做兼容; 要看诊断请用 plan.report()。

缓存键: plan_cache_key() = 指纹 + inventory/strategy/planner 三个版本 + 原型。
改了步骤模板却复用旧计划, 比重新生成一次贵得多。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from ai_kit.perception.archetype import classify
from ai_kit.perception.inventory import ElementSpec, PageInventory, from_inventory
from ai_kit.perception.strategies import (
    CHECKLISTS,
    DESTRUCTIVE,
    MAX_PER_STRATEGY,
    PAGE_REF,
    SAFE,
    SLOW,
    SQLI_PAYLOAD,
    STRATEGY_VERSION,
    TARGETS,
    UNKNOWN,
    WRITE,
    XSS_PAYLOAD,
    CaseSeed,
    Expansion,
    _hit,
    _is_check,
    _is_text,
    _type_of,
    expand,
)

# 步骤模板/动作词表改了就 +1: 缓存键要带它, 否则旧计划会盖住新模板
PLANNER_VERSION = "planner.v3"

# 卡片 5.6 的优先级: P0 blocker / P1 critical / P2 normal / P3 minor
P0, P1, P2, P3 = "P0", "P1", "P2", "P3"
PRIORITIES: Tuple[str, ...] = (P0, P1, P2, P3)
_PRIORITY_OF: Dict[int, str] = {1: P1, 2: P2, 3: P3}      # MUST / SHOULD / SLOW

# 用例形态的编号前缀 (卡片示例: LC-017)
CASE_PREFIX: Dict[str, str] = {
    "list_crud": "LC", "form": "FM", "login": "LG",
    "detail": "DT", "dashboard": "DB", UNKNOWN: "GN",
}

# 演示账号: 计划要能直接跑, 但真站点请用 plan(credentials=...) 传自己的
DEMO_USERNAME = "qa_user"
DEMO_PASSWORD = "Pa55word!"
MARKER_PREFIX = "QA-"                  # 每次写入的唯一标记, 用来验「没落库」

RESULT_CAP = 50                        # 注入串不许把全表倒出来: 结果行数上限
MAX_FIELDS = 6                         # 一次计划里最多驱动几个字段 (再多就拆用例)

QUERY_WORDS: Tuple[str, ...] = ("查询", "搜索", "检索", "查找", "筛选", "过滤",
                                "search", "query", "filter", "apply")
CONFIRM_WORDS: Tuple[str, ...] = ("确定", "确认", "删除", "提交", "ok", "confirm")
CANCEL_WORDS: Tuple[str, ...] = ("取消", "关闭", "cancel", "close")

# 标签顺序 (报告里按这个顺序读; 也保证同一 case 每次生成的顺序一致)
TAG_ORDER: Tuple[str, ...] = ("negative", "security", "permission", "destructive",
                              "write", "performance", "data")

# 这些变体本身就是「喂脏数据/走异常路径」, 打 negative 标签
NEGATIVE_VARIANTS = frozenset((
    "empty", "invalid", "boundary", "sqli", "xss", "too_long", "spaces", "script",
    "wrong", "min_minus_1", "max_plus_1", "duplicate", "no_result", "out_of_range",
    "wrong_password", "wrong_user", "empty_both", "empty_password", "not_editable",
    "no_permission", "readonly", "offline", "double_click", "leave", "no_data",
))

# 只断言「入口不可用」的变体: 一下都不点, 风险不该跟着目标元素往上抬
_NO_TOUCH_VARIANTS = frozenset(("no_permission", "readonly", "not_editable"))

# test_data 的键名: 计划给人看, 能用中文就用中文 (字段值仍以字段名为键)
DATA_LABELS: Dict[str, str] = {
    "page": "页码", "size": "每页条数", "direction": "排序方向",
    "click_times": "点击次数", "confirm": "确认路径", "role": "账号角色",
    "attempts": "连续尝试次数", "rows": "数据行数", "network": "网络条件",
    "throttle": "网络延迟", "combine": "组合查询", "filter": "筛选值",
    "export": "导出", "key": "按键", "remember": "记住我", "pattern": "格式规则",
    "limit": "长度上限", "bound": "边界值", "number": "边界值",
    "password": "密码", "username": "用户名",
}

_SAMPLE_BY_TYPE: Dict[str, str] = {
    "email": "qa@example.com", "tel": "13800138000", "url": "https://example.com/qa",
    "number": "1", "date": "2026-01-15", "datetime-local": "2026-01-15T10:00",
    "month": "2026-01", "week": "2026-W03", "time": "10:00",
    "password": DEMO_PASSWORD, "search": "测试关键字",
}
_INVALID_BY_TYPE: Dict[str, str] = {
    "email": "not-an-email", "tel": "12345abc", "url": "ht!tp://bad url",
    "number": "abc", "date": "2026-13-45", "datetime-local": "2026-13-45T99:99",
    "month": "2026-13", "week": "2026-W99", "time": "99:99",
}
# 只给了 pattern 没给类型时用它: 任何真实格式规则都极不可能是这串
INVALID_FALLBACK = "!!!invalid@#$"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path_of(url: str) -> str:
    """计划里的 page 只要 path (卡片示例 /patient/list): 域名换个环境不该换计划"""
    if not url:
        return ""
    try:
        return urlsplit(url).path or ""
    except ValueError:
        return ""


def _flat(text: Any) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


# ---------------------------------------------------------------------------
# 动作词表: 计划的词汇表, 渲染器逐个实现一次
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Action:
    """一个结构化动作的定义 (校验用; 不含实现, 实现属于 ⑥ renderer)

    eid: "req" 必须带 / "opt" 可带 / "none" 不带
    on:  允许的 ref 种类 (resolve_ref 的 kind); 空 = 页级动作
    value: "req" / "opt" / "" (不带)
    kind: 值类型, 校验时用来挡住 "assert_row_count('十条')" 这类错
    """

    name: str
    eid: str = "none"
    on: Tuple[str, ...] = ()
    value: str = ""
    kind: str = ""
    asserts: bool = False
    doc: str = ""


def _catalog() -> Tuple[Action, ...]:
    return (
        # --- 流程 ---
        Action("goto", value="req", kind="url", doc="打开 URL"),
        Action("reload", doc="刷新当前页"),
        Action("back", doc="浏览器后退"),
        Action("fill", eid="req", on=("element",), value="req", kind="text",
               doc="输入文本 (空串也可)"),
        Action("clear", eid="req", on=("element",), doc="清空输入"),
        Action("select", eid="req", on=("element",), value="req", kind="text",
               doc="选择下拉项 (按可见文案)"),
        Action("check", eid="req", on=("element",), doc="勾选"),
        Action("uncheck", eid="req", on=("element",), doc="取消勾选"),
        Action("click", eid="req", on=("element", "row-action"), doc="点击"),
        Action("double_click", eid="req", on=("element", "row-action"), doc="双击"),
        Action("press", eid="req", on=("element",), value="req", kind="key",
               doc="按键 (Enter / Tab ...)"),
        # --- 环境 / 数据准备 ---
        Action("throttle", value="req", kind="text", doc="限速 (3s / 200kb/s)"),
        Action("set_network", value="req", kind="text", doc="模拟网络 (offline / online)"),
        Action("seed_rows", value="req", kind="int", doc="灌 N 行数据"),
        # --- 数据视图 ---
        Action("paginate", eid="req", on=("table",), value="req", kind="page",
               doc="翻到第 N 页 / first / last / next / prev"),
        Action("set_page_size", eid="req", on=("table",), value="req", kind="int",
               doc="切换每页条数"),
        Action("sort", eid="req", on=("column",), value="req", kind="direction",
               doc="点列头排序"),
        Action("export", eid="req", on=("element",), doc="触发导出并等下载完成"),
        # --- 弹窗 ---
        Action("confirm_dialog", value="opt", kind="label", doc="在弹窗里点确认"),
        Action("cancel_dialog", value="opt", kind="label", doc="在弹窗里点取消"),
        # --- 导航类断言 ---
        Action("assert_visible", eid="req", on=("element", "table", "row-action",
                                                "chart", "dialog"), asserts=True,
               doc="可见"),
        Action("assert_inactive", eid="req", on=("element", "row-action"), asserts=True,
               doc="不可用 (隐藏或置灰)"),
        Action("assert_text", eid="req", on=("element",), value="req", kind="text",
               asserts=True, doc="元素文本等于/包含 value"),
        Action("assert_value", eid="req", on=("element",), value="req", kind="text",
               asserts=True, doc="输入框当前值"),
        Action("assert_field_error", eid="req", on=("element",), value="opt", kind="text",
               asserts=True, doc="该字段下有校验提示 (默认查『必填』)"),
        Action("assert_masked", eid="opt", on=("element", "table"), asserts=True,
               doc="敏感值已脱敏 (不给 eid 则整页扫)"),
        Action("assert_autocomplete", eid="req", on=("element",), value="req", kind="text",
               asserts=True, doc="autocomplete 属性"),
        Action("assert_no_error_toast", asserts=True, doc="页面没有报错提示/toast"),
        Action("assert_no_alert", asserts=True, doc="没有弹原生 alert (XSS 串)"),
        Action("assert_loading", asserts=True, doc="出现 loading, 且结束后消失"),
        Action("assert_page_ready_within", value="req", kind="int", asserts=True,
               doc="页面可用耗时 < value 毫秒"),
        # --- 列表类断言 ---
        Action("assert_row_count", eid="opt", on=("table",), value="req", kind="int",
               asserts=True, doc="行数等于 value"),
        Action("assert_row_count_lte", eid="opt", on=("table",), value="req", kind="int",
               asserts=True, doc="行数 <= value"),
        Action("assert_row_count_gte", eid="opt", on=("table",), value="req", kind="int",
               asserts=True, doc="行数 >= value"),
        Action("assert_count_matches_rows", eid="opt", on=("table",), asserts=True,
               doc="『共 N 条』与实际行数一致"),
        Action("assert_rows_match", eid="opt", on=("table", "element"), value="req",
               kind="text", asserts=True, doc="每一行都包含 value"),
        Action("assert_sorted", eid="req", on=("column",), value="req", kind="direction",
               asserts=True, doc="该列已按方向排好"),
        Action("assert_no_duplicate_rows", eid="opt", on=("table",), asserts=True,
               doc="没有重复/串行数据"),
        Action("assert_empty_state", eid="opt", on=("table", "element"), asserts=True,
               doc="显示空态文案"),
        Action("assert_selection_count", eid="opt", on=("table",), value="req", kind="int",
               asserts=True, doc="选中行数等于 value"),
        Action("assert_selection_count_gte", eid="opt", on=("table",), value="req",
               kind="int", asserts=True, doc="选中行数 >= value"),
        # --- 交互 / 持久化 / 登录 ---
        Action("assert_feedback", value="opt", kind="text", asserts=True,
               doc="点击有反馈 (跳转/弹窗/提示)"),
        Action("assert_persisted", value="req", kind="text", asserts=True,
               doc="重新加载后 value 仍在 (value 是唯一标记)"),
        Action("assert_not_persisted", value="req", kind="text", asserts=True,
               doc="重新加载后查不到 value"),
        Action("assert_login_error", asserts=True, doc="登录失败且有提示"),
        Action("assert_logged_in", asserts=True, doc="已跳出登录页且会话可用"),
        Action("assert_locked", value="opt", kind="int", asserts=True,
               doc="账号被锁定 / 要求验证码"),
        Action("assert_submit_failed", asserts=True, doc="提交失败有明确提示且可重试"),
        Action("assert_consistent", value="req", kind="relation", asserts=True,
               doc="与 detail/list 的同一条记录字段一致"),
        Action("assert_export_matches_list", eid="opt", on=("table",), asserts=True,
               doc="导出内容与列表一致"),
        Action("assert_chart_matches_source", eid="req", on=("chart",), asserts=True,
               doc="图上数值与数据源一致"),
        Action("assert_chart_empty", eid="req", on=("chart",), asserts=True,
               doc="图表显示空态"),
        Action("toggle_legend", eid="req", on=("chart",), doc="点图例切换系列"),
    )


ACTIONS: Dict[str, Action] = {action.name: action for action in _catalog()}


# ---------------------------------------------------------------------------
# 计划数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """一个结构化动作。只允许 action / eid / value 三个键 (卡片 5.6)"""

    action: str
    eid: str = ""
    value: Any = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"action": self.action}
        if self.eid:
            out["eid"] = self.eid
        if self.value is not None:
            out["value"] = self.value
        return out

    def describe(self) -> str:
        """人读的一行 (报告/日志用; 渲染器不要拿它当输入)"""
        action = ACTIONS.get(self.action)
        label = action.doc if action is not None else "未知动作"
        parts = [f"{self.action}({label})"]
        if self.eid:
            parts.append(f"@{self.eid}")
        if self.value is not None:
            text = str(self.value)
            parts.append(f"= {text[:40] + '…' if len(text) > 40 else text}")
        return " ".join(parts)


@dataclass(frozen=True)
class PlanCase:
    """卡片 5.6 的一条用例 (cid / expect 是卡片之外的补充)"""

    case_id: str
    title: str
    priority: str = P1
    tags: Tuple[str, ...] = ()
    preconditions: Tuple[str, ...] = ()
    test_data: Dict[str, Any] = field(default_factory=dict)
    steps: Tuple[Step, ...] = ()
    cleanup: Tuple[Step, ...] = ()
    cid: str = ""
    expect: Tuple[str, ...] = ()

    @property
    def is_destructive(self) -> bool:
        return "destructive" in self.tags

    def actions(self) -> Tuple[str, ...]:
        return tuple(step.action for step in self.steps)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "priority": self.priority,
            "tags": list(self.tags),
            "preconditions": list(self.preconditions),
            "test_data": dict(self.test_data),
            "steps": [step.to_dict() for step in self.steps],
            "cleanup": [step.to_dict() for step in self.cleanup],
            "cid": self.cid,
            "expect": list(self.expect),
        }


@dataclass(frozen=True)
class TestPlan:
    """一页的用例计划。to_dict() 就是卡片 5.6 的 JSON; diagnostics 不进 JSON"""

    page: str = ""
    archetype: str = UNKNOWN
    profile: str = ""
    model: str = ""
    prompt_version: str = PLANNER_VERSION
    generated_at: str = ""
    cases: Tuple[PlanCase, ...] = ()
    # 不静默: 缺项/跳过/未解析/校验问题都留在这里, report() 打出来
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    # --- 统计 ---

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def problems(self) -> Tuple[str, ...]:
        return tuple(self.diagnostics.get("problems") or ())

    @property
    def is_valid(self) -> bool:
        return not self.problems

    def by_priority(self) -> Dict[str, int]:
        counts = {name: 0 for name in PRIORITIES}
        for case in self.cases:
            counts[case.priority] = counts.get(case.priority, 0) + 1
        return counts

    def by_tag(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for case in self.cases:
            for tag in case.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return counts

    def of_priority(self, *wanted: str) -> Tuple[PlanCase, ...]:
        return tuple(case for case in self.cases if case.priority in wanted)

    def find(self, case_id: str) -> Optional[PlanCase]:
        return next((case for case in self.cases if case.case_id == case_id), None)

    def require_all(self, priority: str = P0) -> Tuple[PlanCase, ...]:
        return tuple(case for case in self.cases if case.priority == priority)

    # --- 序列化 ---

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page": self.page,
            "archetype": self.archetype,
            "profile": self.profile,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "generated_at": self.generated_at,
            "cases": [case.to_dict() for case in self.cases],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def report(self) -> str:
        """控制台/CI 用的一页诊断 (不是计划本体)"""
        counts = self.by_priority()
        lines = [f"计划: {self.page or '(未知页面)'} · {self.archetype} · {self.total} 条",
                 "  " + " / ".join(f"{name} {counts.get(name, 0)}" for name in PRIORITIES)]
        tags = self.by_tag()
        if tags:
            lines.append("  标签: " + " · ".join(f"{k} {v}" for k, v in sorted(tags.items())))
        for key, title in (("missing", "缺项 (页面上没有的对象, 少测了)"),
                           ("skipped", "跳过 (模板出不了可执行步骤)"),
                           ("unresolved", "未解析取值 (需要人/数据源补)"),
                           ("notes", "说明"),
                           ("problems", "校验问题")):
            items = list(self.diagnostics.get(key) or ())
            if not items:
                continue
            lines.append(f"  {title}: {len(items)}")
            lines.extend(f"    - {item}" for item in items)
        llm = self.diagnostics.get("llm")
        if llm:
            lines.append(f"  LLM: {llm}")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self):
        return iter(self.cases)


# ---------------------------------------------------------------------------
# ref -> 页面对象 (步骤里的 eid 口径)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ref:
    """resolve_ref 的结果: 一个 ref 指向哪类对象"""

    ref: str
    kind: str = ""          # page / element / table / column / row-action / chart / dialog
    eid: str = ""           # 元素/表格/弹窗的 eid (column/chart/page 为空)
    table: str = ""         # column / row-action 所属表格
    column: str = ""
    text: str = ""
    index: int = 0


def resolve_ref(inventory: Any, ref: str) -> Optional[Ref]:
    """把计划里的引用解析成页面对象; 解析不出来返回 None (校验层据此报错)

    引用语法全部来自 ④ 的 Binding:
      e1                        -> element / table / dialog (按清单查找)
      c1.column:姓名            -> 表格 c1 的『姓名』列
      c1.row:删除               -> 表格 c1 的行内『删除』(被折叠时只有文案)
      chart:1                   -> 第 1 个图表 (signals.charts)
      page                      -> 整页
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    text = str(ref or "").strip()
    if not text:
        return None
    if text == PAGE_REF:
        return Ref(text, "page")

    if text.startswith("chart:"):
        index = _as_int(text.split(":", 1)[1])
        if 1 <= index <= int(inv.signals.get("charts", 0) or 0):
            return Ref(text, "chart", index=index)
        return None

    if ".column:" in text:
        table_id, column = text.split(".column:", 1)
        for table in inv.tables:
            if table.get("eid") == table_id and column in (table.get("columns") or ()):
                return Ref(text, "column", table=table_id, column=column)
        return None

    if ".row:" in text:
        table_id, label = text.split(".row:", 1)
        for table in inv.tables:
            if table.get("eid") != table_id:
                continue
            texts = [str(action.get("text") or "") for action in table.get("row_actions") or ()]
            if label in texts:
                return Ref(text, "row-action", table=table_id, text=label)
        return None

    element = inv.element(text)
    if element is not None:
        return Ref(text, "element", eid=element.eid, text=element.text or element.name)
    for table in inv.tables:
        if table.get("eid") == text:
            return Ref(text, "table", eid=text)
    for dialog in inv.dialogs:
        if dialog.get("eid") == text:
            return Ref(text, "dialog", eid=text, text=str(dialog.get("text") or ""))
    return None


# ---------------------------------------------------------------------------
# 取值: @token -> 具体值
# ---------------------------------------------------------------------------

def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _repeat(element: Optional[ElementSpec], length: int) -> str:
    """按控件类型造定长串: 数字框用数字, 其余用汉字 (maxlength 数的通常是字符)"""
    if length <= 0:
        return ""
    return ("9" if _type_of(element) == "number" else "测") * length


def valid_value(element: Optional[ElementSpec]) -> str:
    """该字段的「一个合法值」。够具体才跑得动, 又不假装懂业务数据"""
    if element is None:
        return "测试数据"
    if _type_of(element) == "select":
        options = element.options
        return str(options[0]) if options else ""
    haystack = _flat(f"{element.name} {element.text} {element.attrs.get('placeholder', '')}")
    if any(word in haystack for word in ("邮箱", "email", "mail")):
        return _SAMPLE_BY_TYPE["email"]
    if any(word in haystack for word in ("手机", "电话", "phone", "mobile", "tel")):
        return _SAMPLE_BY_TYPE["tel"]
    if any(word in haystack for word in ("用户", "账号", "帐号", "user", "account", "login")):
        return DEMO_USERNAME
    return _SAMPLE_BY_TYPE.get(_type_of(element), "测试数据")


def resolve_value(raw: Any, element: Optional[ElementSpec] = None,
                  data: Optional[Mapping[str, Any]] = None) -> Tuple[Any, bool]:
    """把 @token 变成具体值; 返回 (值, 是否解析成功)

    解析不了就原样返回 token —— 计划里留一个 @existing 比编一个假名字诚实:
    执行的人知道这里要先去库里捞一个已存在的值。
    """
    if not isinstance(raw, str) or not raw.startswith("@"):
        return raw, True
    payload = dict(data or {})
    token = raw.strip()

    if token.startswith("@maxlength"):
        length = _as_int(payload.get("length"))
        if length <= 0:
            return token, False
        return _repeat(element, length), True

    if token in ("@min-1", "@min", "@max+1", "@max"):
        number = payload.get("number")
        if number is None:
            return token, False
        return number, True

    if token == "@invalid_format":
        field_type = _type_of(element) if element is not None else ""
        if field_type in _INVALID_BY_TYPE:
            return _INVALID_BY_TYPE[field_type], True
        # pattern 可能来自种子 (④ 的变体数据) 也可能来自控件约束本身
        pattern = payload.get("pattern") or (element.constraints.get("pattern")
                                             if element is not None else "")
        if pattern:
            return INVALID_FALLBACK, True
        return token, False

    if token == "@existing":
        # 唯一性用例需要「库里已存在的值」: 规划器拿不到数据库, 不编
        return token, False

    if token == "@valid":
        value = valid_value(element)
        return (value, True) if value else (token, False)

    return raw, True


# ---------------------------------------------------------------------------
# 上下文: 一条种子的协作者 (表格 / 查询按钮 / 提交按钮 / 弹窗 ...)
# ---------------------------------------------------------------------------

def _hit_words(text: Any, words: Iterable[str]) -> bool:
    haystack = _flat(text)
    return any(_flat(word) in haystack for word in words if word)


class _Ctx:
    """一条种子编译成步骤时需要的页面协作者。

    这些东西不属于种子 (④ 只说『测哪个筛选框』), 也不属于渲染器 (⑥ 只执行):
    『填完之后点哪个按钮』恰好是计划该定下来的事。全部从清单派生, 派生不出来
    就 warn, 绝不瞎猜一个 eid。
    """

    def __init__(self, inventory: PageInventory, seed: CaseSeed, *, case_id: str,
                 page: str = "", credentials: Tuple[str, str] = (DEMO_USERNAME, DEMO_PASSWORD),
                 deep_link: str = "", max_fields: int = MAX_FIELDS,
                 result_cap: int = RESULT_CAP):
        self.inv = inventory
        self.seed = seed
        self.case_id = case_id
        self.page = page
        self.credentials = credentials
        self.deep_link = deep_link
        self.max_fields = max_fields
        self.result_cap = result_cap
        self.notes: List[str] = []
        self.unresolved: List[str] = []
        self.skipped = ""
        self.used: Dict[str, Any] = {}          # 实际填过的 字段名 -> 值 (test_data 从这来)
        self._seen_unresolved: set = set()

    # --- 基础 ---

    @property
    def variant(self) -> str:
        """变体 key (④ 的 CaseSeed.variant; 老数据退回 cid 末段)"""
        return self.seed.variant or self.seed.cid.rsplit(".", 1)[-1].split("#")[0]

    @property
    def marker(self) -> str:
        """唯一标记: 用来验『这次写入到底落库没有』"""
        return f"{MARKER_PREFIX}{self.case_id}"

    def step(self, action: str, eid: str = "", value: Any = None) -> Step:
        return Step(action, eid or "", value)

    def warn(self, message: str) -> None:
        self.notes.append(f"{self.seed.cid}: {message}")

    def skip(self, reason: str) -> None:
        self.skipped = reason

    def value(self, element: Optional[ElementSpec] = None) -> Any:
        """种子里的 @token -> 具体值; 解析不了就记 unresolved 并留下 token"""
        target = element if element is not None else self.inv.element(self.seed.ref)
        value, ok = resolve_value(self.seed.data.get("value", ""), target, self.seed.data)
        if not ok and value not in self._seen_unresolved:
            self._seen_unresolved.add(value)
            self.unresolved.append(
                f"{self.seed.cid}: {self.seed.strategy} 需要具体的取值 {value!r} "
                f"(库里捞一个已有值, 或手工补)")
        return value

    # --- 协作者 (全部从清单派生) ---

    def _first_ref(self, role: str) -> str:
        bindings = TARGETS[role](self.inv) if role in TARGETS else ()
        for binding in bindings:
            if binding.ref and binding.ref != PAGE_REF:
                return binding.ref
        return ""

    @property
    def table_eid(self) -> str:
        if self.seed.kind == "table" and resolve_ref(self.inv, self.seed.ref) is not None:
            return self.seed.ref
        return self.first_ref("table")

    def first_ref(self, role: str) -> str:
        return self._first_ref(role)

    @property
    def row_count(self) -> int:
        for table in self.inv.tables:
            if table.get("eid") == self.table_eid:
                return _as_int(table.get("row_count"))
        return 0

    @property
    def query_eid(self) -> str:
        """查询/搜索按钮: 先看筛选区, 再看工具栏, 最后全页兜底"""
        actions = [element for element in self.inv.elements if element.is_action]
        pool = [element for element in actions
                if element.container in ("filter-bar", "toolbar")
                and _hit(element, QUERY_WORDS)]
        pool = pool or [element for element in actions
                        if _hit(element, QUERY_WORDS) and element.container != "table-row"]
        return pool[0].eid if pool else ""

    @property
    def submit_eid(self) -> str:
        """提交按钮: ④ 的 submit 角色优先

        登录页/无 <form> 壳的组件库里, submit 别名只认『保存/提交』且只回落到
        form/dialog 容器, 于是一个孤零零的『登录』按钮就找不到 —— 这里补一层登录词。
        """
        ref = self.first_ref("submit")
        if ref:
            return ref
        for element in self.inv.elements:
            if element.is_action and _hit_words(
                    f"{element.name} {element.text} {element.attrs.get('value', '')}",
                    ("登录", "登陆", "signin", "login", "进入")):
                return element.eid
        return ""

    @property
    def first_filter(self) -> str:
        return self.first_ref("filter_field")

    @property
    def first_field(self) -> str:
        return self.first_ref("form_field")

    @property
    def select_all(self) -> str:
        return self.first_ref("checkbox")

    @property
    def export_eid(self) -> str:
        return self.first_ref("export_button")

    @property
    def user_eid(self) -> str:
        """登录配对的用户名框: ④ 已经放进 refs"""
        for ref in self.seed.refs:
            if self.inv.element(ref) is not None:
                return ref
        return ""

    @property
    def dialog_buttons(self) -> Tuple[str, ...]:
        return tuple(str(button) for dialog in self.inv.dialogs
                     for button in (dialog.get("buttons") or ()))

    @property
    def confirm_label(self) -> str:
        """弹窗确认按钮的文案: 认不出确认词就取『不是取消的那个』"""
        found = next((label for label in self.dialog_buttons
                      if _hit_words(label, CONFIRM_WORDS)), "")
        if found:
            return found
        others = [label for label in self.dialog_buttons
                  if not _hit_words(label, CANCEL_WORDS)]
        return others[0] if others else "确定"

    @property
    def cancel_label(self) -> str:
        """弹窗取消按钮的文案: 站点会写成『再想想』『返回』, 那就取另一个"""
        found = next((label for label in self.dialog_buttons
                      if _hit_words(label, CANCEL_WORDS)), "")
        if found:
            return found
        others = [label for label in self.dialog_buttons
                  if not _hit_words(label, CONFIRM_WORDS)]
        return others[0] if others else "取消"

    def sibling_filter(self) -> str:
        for binding in TARGETS["filter_field"](self.inv):
            if binding.ref != self.seed.ref:
                return binding.ref
        return ""

    def first_column(self) -> str:
        for table in self.inv.tables:
            if table.get("eid") != self.table_eid:
                continue
            for column in table.get("columns") or ():
                if str(column or "").strip():
                    return f"{self.table_eid}.column:{str(column).strip()}"
        return ""

    def required_fields(self) -> List[str]:
        return [binding.ref for binding in TARGETS["required_field"](self.inv)]

    # --- 动作小件 ---

    def input(self, eid: str, value: Any) -> List[Step]:
        """按控件类型自动选 fill / select / check —— 计划里只写意图"""
        element = self.inv.element(eid)
        if element is None:
            self.warn(f"{eid} 不在元素清单里, 输入这一步跳过")
            return []
        label = self.seed.label if eid == self.seed.ref else (element.name or element.text or eid)
        if label and label not in self.used:
            self.used[label] = value           # 给 test_data 留一份真实用到的值
        if _is_check(element):
            return [self.step("check" if value else "uncheck", eid)]
        if _type_of(element) == "select":
            return [self.step("select", eid, str(value))]
        if not element.is_field:
            self.warn(f"{eid} 不是输入控件, 输入这一步跳过")
            return []
        return [self.step("fill", eid, value)]

    def clear(self, eid: str) -> List[Step]:
        """清空输入; 同样记一笔 test_data —— 『登录时两个框都留空』也是测试数据"""
        element = self.inv.element(eid)
        if element is None:
            self.warn(f"{eid} 不在元素清单里, 清空这一步跳过")
            return []
        label = self.seed.label if eid == self.seed.ref else (element.name or element.text or eid)
        if label and label not in self.used:
            self.used[label] = ""
        return [self.step("clear", eid)]

    def apply(self) -> List[Step]:
        """让筛选条件生效: 点查询按钮, 没有按钮就回车"""
        if self.query_eid:
            return [self.step("click", self.query_eid)]
        element = self.inv.element(self.seed.ref)
        if element is not None and _is_text(element):
            self.warn("页面上没有查询按钮, 用回车触发筛选")
            return [self.step("press", self.seed.ref, "Enter")]
        self.warn("没有查询按钮也没有可回车的输入框, 筛选条件不会生效")
        return []

    def submit(self) -> List[Step]:
        if self.submit_eid:
            return [self.step("click", self.submit_eid)]
        return self.apply()

    def rows(self, action: str, value: Any = None) -> List[Step]:
        """列表类断言: 有表格就带表 eid, 没有就当页级断言"""
        return [self.step(action, self.table_eid, value)]

    def no_error(self) -> List[Step]:
        return [self.step("assert_no_error_toast")]

    def no_alert(self) -> List[Step]:
        return [self.step("assert_no_alert")]


# ---------------------------------------------------------------------------
# 步骤模板: 一个策略 key 一份, 卡片 5.5 的「每格怎么测」
# ---------------------------------------------------------------------------

def _b_query_filter(c: _Ctx) -> List[Step]:
    value = c.value()
    steps = c.input(c.seed.ref, value)
    if c.seed.data.get("combine"):
        other = c.sibling_filter()
        if other:
            steps += c.input(other, valid_value(c.inv.element(other)))
        else:
            c.warn("页面上没有第二个筛选项, 组合查询退化为单条件")
    steps += c.apply()
    kind = c.variant
    if kind == "invalid":
        return steps + c.no_error() + c.rows("assert_empty_state")
    if kind == "sqli":
        return steps + c.no_error() + c.rows("assert_row_count_lte", c.result_cap)
    if kind == "xss":
        return steps + c.no_alert() + c.no_error()
    if kind == "boundary":
        return steps + c.no_error() + c.rows("assert_row_count_lte", c.result_cap)
    if kind in ("valid", "combo"):
        return steps + c.no_error() + c.rows("assert_rows_match", value)
    return steps + c.no_error() + c.rows("assert_row_count_gte", 1)


def _b_query_reset(c: _Ctx) -> List[Step]:
    field = c.first_filter
    if not field:
        c.skip("页面上没有能被重置的筛选项")
        return []
    c.warn("重置后的默认值按空串校验; 站点默认非空时请改这条断言")
    steps = c.input(field, valid_value(c.inv.element(field))) + c.apply()
    steps.append(c.step("click", c.seed.ref))
    steps.append(c.step("assert_value", field, ""))
    return steps + c.rows("assert_row_count_gte", 1)


def _b_query_count(c: _Ctx) -> List[Step]:
    steps: List[Step] = []
    field = c.first_filter
    if field:
        steps += c.input(field, valid_value(c.inv.element(field))) + c.apply()
    return steps + c.rows("assert_count_matches_rows")


def _b_pagination_jump(c: _Ctx) -> List[Step]:
    if not c.table_eid:
        c.skip("页面上没有表格, 翻页无从谈起")
        return []
    if c.variant == "page_size":
        return ([c.step("set_page_size", c.table_eid, 50)]
                + c.rows("assert_row_count_lte", 50)
                + c.rows("assert_no_duplicate_rows"))
    page = {"first": 1, "last": "last", "middle": 2, "out_of_range": 9999}.get(c.variant, 2)
    steps = [c.step("paginate", c.table_eid, page)] + c.no_error()
    if c.variant == "out_of_range":
        return steps + c.rows("assert_visible")
    if c.variant == "last":
        return steps + c.rows("assert_row_count_gte", 1)
    return steps + c.rows("assert_no_duplicate_rows")


def _b_pagination_keep_filter(c: _Ctx) -> List[Step]:
    field = c.first_filter
    if not field:
        c.skip("页面上没有筛选项, 只翻页不涉及条件保持")
        return []
    if not c.table_eid:
        c.skip("页面上没有表格, 翻页无从谈起")
        return []
    value = valid_value(c.inv.element(field))
    steps = c.input(field, value) + c.apply()
    steps.append(c.step("paginate", c.table_eid, 2))
    steps.append(c.step("assert_value", field, value))
    return steps + c.rows("assert_rows_match", value)


def _b_sort_column(c: _Ctx) -> List[Step]:
    direction = "desc" if c.variant == "desc" else "asc"
    steps = [c.step("sort", c.seed.ref, direction),
             c.step("assert_sorted", c.seed.ref, direction)]
    return steps + c.rows("assert_no_duplicate_rows")


def _b_sort_null(c: _Ctx) -> List[Step]:
    steps = [c.step("sort", c.seed.ref, "asc"),
             c.step("assert_sorted", c.seed.ref, "asc")]
    return steps + c.rows("assert_no_duplicate_rows")


def _b_sort_across_pages(c: _Ctx) -> List[Step]:
    column = c.first_column()
    if not column or not c.table_eid:
        c.skip("表格没有列名也没有 eid, 无法指定排序对象")
        return []
    return ([c.step("sort", column, "asc"),
             c.step("paginate", c.table_eid, 2),
             c.step("assert_sorted", column, "asc")]
            + c.rows("assert_no_duplicate_rows"))


def _b_row_actions(c: _Ctx) -> List[Step]:
    if c.variant == "no_permission":
        return [c.step("assert_inactive", c.seed.ref)]
    if c.variant == "double_click":
        return ([c.step("double_click", c.seed.ref), c.step("assert_feedback")]
                + c.rows("assert_no_duplicate_rows"))
    # 行内『删除』: 点开确认弹窗就收手 —— 验证入口存在, 不替用户真删
    if c.seed.risk == DESTRUCTIVE:
        return [c.step("click", c.seed.ref), c.step("assert_feedback"),
                c.step("cancel_dialog", value=c.cancel_label)]
    return [c.step("click", c.seed.ref), c.step("assert_feedback")]


def _b_batch_select(c: _Ctx) -> List[Step]:
    if c.variant == "invert":
        # 反选 = 再点一次同一个复选: 第二次必须是 uncheck, 否则『取消选中』根本没被验证
        c.warn("清单里没有独立的『反选』控件, 用全选->取消全选覆盖选中集合")
        return ([c.step("check", c.seed.ref)]
                + c.rows("assert_selection_count_gte", 1)
                + [c.step("uncheck", c.seed.ref)]
                + c.rows("assert_selection_count", 0))
    if c.variant == "cross_page":
        c.warn("规格允许『翻页后清空并提示』, 这里按『保留选中』校验")
        if not c.table_eid:
            c.skip("页面上没有表格, 验不了跨页选择")
            return []
        return ([c.step("check", c.seed.ref),
                 c.step("paginate", c.table_eid, 2)]
                + c.rows("assert_selection_count_gte", 1))
    return [c.step("check", c.seed.ref)] + c.rows("assert_selection_count_gte", 1)


def _b_batch_delete(c: _Ctx) -> List[Step]:
    steps: List[Step] = []
    box = c.select_all
    if box:
        steps.append(c.step("check", box))
    else:
        c.warn("页面上没有复选控件, 批量删除缺少选中对象")
    steps.append(c.step("click", c.seed.ref))
    before = c.row_count or 1
    if c.variant == "confirm":
        steps.append(c.step("confirm_dialog", value=c.confirm_label))
        steps += c.no_error()
        if c.table_eid:
            steps += c.rows("assert_row_count_lte", max(0, before - 1))
        return steps
    steps.append(c.step("cancel_dialog", value=c.cancel_label))
    if c.table_eid:
        steps += c.rows("assert_row_count_gte", before)
    return steps


def _b_state_empty(c: _Ctx) -> List[Step]:
    if c.variant == "no_data":
        return c.rows("assert_empty_state")
    field = c.first_filter
    if not field:
        c.skip("页面上没有筛选项, 造不出『筛到空结果』")
        return []
    return c.input(field, "no_such_value_zzz") + c.apply() + c.rows("assert_empty_state")


def _b_state_loading(c: _Ctx) -> List[Step]:
    return ([c.step("throttle", value="3s"), c.step("reload"), c.step("assert_loading")]
            + c.rows("assert_no_duplicate_rows"))


def _b_state_perf(c: _Ctx) -> List[Step]:
    return [c.step("seed_rows", value=1000), c.step("reload"),
            c.step("assert_page_ready_within", value=3000)]


def _b_consistency_three_way(c: _Ctx) -> List[Step]:
    if c.variant == "count":
        return c.rows("assert_count_matches_rows")
    if c.variant == "detail":
        return [c.step("assert_consistent", value="detail")]
    export = c.export_eid
    if not export:
        c.skip("页面上没有导出按钮, 验证不了『列表 vs 导出』")
        return []
    return [c.step("export", export)] + c.rows("assert_export_matches_list")


def _b_security_text(c: _Ctx) -> List[Step]:
    value = c.value()
    steps = c.input(c.seed.ref, value)
    element = c.inv.element(c.seed.ref)
    in_form = element is not None and element.container in ("form", "dialog")
    steps += c.submit() if in_form else c.apply()
    if c.variant == "script":
        return steps + c.no_alert() + c.no_error()
    if c.variant == "emoji":
        return steps + c.no_error()
    if c.variant == "spaces":
        return steps + c.no_error() + c.rows("assert_rows_match", str(value).strip())
    return steps + c.no_error() + c.rows("assert_row_count_lte", c.result_cap)


def _b_required_each(c: _Ctx) -> List[Step]:
    value = c.seed.data.get("value", "")
    return c.input(c.seed.ref, value) + c.submit() + [
        c.step("assert_field_error", c.seed.ref)]


def _b_required_all(c: _Ctx) -> List[Step]:
    fields = c.required_fields()
    if not fields:
        c.skip("页面上没有必填字段")
        return []
    if len(fields) > c.max_fields:
        c.warn(f"必填项 {len(fields)} 个, 只清前 {c.max_fields} 个 (多出来的拆到别的用例)")
        fields = fields[:c.max_fields]
    steps: List[Step] = []
    for field in fields:
        steps += c.input(field, "") or [c.step("clear", field)]
    steps += c.submit()
    steps += [c.step("assert_field_error", field) for field in fields]
    return steps


def _b_format_each(c: _Ctx) -> List[Step]:
    value = c.value()
    return (c.input(c.seed.ref, value) + c.submit()
            + [c.step("assert_field_error", c.seed.ref)])


def _b_format_unique(c: _Ctx) -> List[Step]:
    value = c.value()
    return (c.input(c.seed.ref, value) + c.submit()
            + [c.step("assert_field_error", c.seed.ref, "已存在")])


def _b_dependency_linkage(c: _Ctx) -> List[Step]:
    element = c.inv.element(c.seed.ref)
    options = list(element.options) if element is not None else []
    if len(options) < 2:
        c.skip("下拉项少于 2 个, 验证不了父子联动")
        return []
    steps = [c.step("select", c.seed.ref, options[0]),
             c.step("select", c.seed.ref, options[1])]
    return steps + c.no_error()


def _b_persist_after_save(c: _Ctx) -> List[Step]:
    field = c.first_field
    if not field:
        c.skip("页面上没有输入字段, 没有东西可以保存")
        return []
    return (c.input(field, c.marker) + c.submit() + c.no_error()
            + [c.step("reload"), c.step("assert_persisted", value=c.marker)])


def _b_submit_double(c: _Ctx) -> List[Step]:
    field = c.first_field
    steps = c.input(field, c.marker) if field else []
    if not field:
        c.warn("页面上没有输入字段, 双击提交用的是页面原有的数据")
    return steps + [c.step("double_click", c.seed.ref)] + c.rows("assert_no_duplicate_rows")


def _b_submit_interrupt(c: _Ctx) -> List[Step]:
    field = c.first_field
    if not field:
        c.skip("页面上没有输入字段, 无法构造『提交中打断』")
        return []
    if c.variant == "offline":
        return ([c.step("set_network", value="offline")] + c.input(field, c.marker)
                + [c.step("click", c.seed.ref), c.step("assert_submit_failed"),
                   c.step("set_network", value="online"), c.step("reload"),
                   c.step("assert_not_persisted", value=c.marker)])
    return (c.input(field, c.marker) + [c.step("click", c.seed.ref), c.step("back")]
            + [c.step("reload"), c.step("assert_not_persisted", value=c.marker)])


def _b_reset_defaults(c: _Ctx) -> List[Step]:
    field = c.first_field
    if not field:
        c.skip("页面上没有输入字段, 重置无从验证")
        return []
    c.warn("重置后的默认值按空串校验; 默认非空的站点请改这条断言")
    element = c.inv.element(field)
    default = "" if _type_of(element) != "select" else (
        str(element.options[0]) if element is not None and element.options else "")
    return (c.input(field, "改过的值") + [c.step("click", c.seed.ref),
                                          c.step("assert_value", field, default)])


def _b_cancel_discard(c: _Ctx) -> List[Step]:
    field = c.first_field
    if not field:
        c.skip("页面上没有输入字段, 取消无从验证")
        return []
    return (c.input(field, c.marker) + [c.step("click", c.seed.ref),
                                        c.step("reload"),
                                        c.step("assert_not_persisted", value=c.marker)])


def _b_credential_negative(c: _Ctx) -> List[Step]:
    user, password = c.user_eid, c.seed.ref
    if not user:
        c.skip("找不到配对的用户名框, 登录用例只能测密码")
        return []
    kind = c.variant
    steps: List[Step] = []
    if kind == "wrong_user":
        steps += c.input(user, "no_such_user_zzz") + c.input(password, c.credentials[1])
    elif kind == "empty_both":
        steps += c.clear(user) + c.clear(password)
    elif kind == "empty_password":
        steps += c.input(user, c.credentials[0]) + c.clear(password)
    else:                                   # wrong_password (以及未知变体)
        steps += c.input(user, c.credentials[0]) + c.input(password, "Wrong@123")
    steps += c.submit()
    return steps + [c.step("assert_login_error")]


def _b_credential_submit(c: _Ctx) -> List[Step]:
    user, password = c.user_eid, c.seed.ref
    if not user:
        c.skip("找不到配对的用户名框, 登录用例只能测密码")
        return []
    if c.variant == "remember_me":
        box = c.select_all
        if not box:
            c.skip("页面上没有『记住我』复选, 验证不了重开浏览器仍登录")
            return []
        steps = [c.step("check", box)]
    elif c.variant == "enter_key":
        return (c.input(user, c.credentials[0]) + c.input(password, c.credentials[1])
                + [c.step("press", password, "Enter"), c.step("assert_logged_in")])
    else:
        steps = []
    steps += c.input(user, c.credentials[0]) + c.input(password, c.credentials[1])
    steps.append(c.step("click", c.submit_eid or c.seed.ref))
    if c.variant == "remember_me":
        steps.append(c.step("reload"))
    return steps + [c.step("assert_logged_in")]


def _b_lockout_repeat(c: _Ctx) -> List[Step]:
    user, password = c.user_eid, c.seed.ref
    if not user:
        c.skip("找不到配对的用户名框, 锁定用例跑不了")
        return []
    attempts = max(1, _as_int(c.seed.data.get("attempts")) or 5)
    c.warn(f"会连续失败 {attempts} 次并可能锁住账号, 请用专用账号")
    steps = c.input(user, c.credentials[0])
    for _ in range(attempts):
        steps += c.input(password, "Wrong@123") + c.submit()
    return steps + [c.step("assert_locked", value=attempts)]


def _b_session_target(c: _Ctx) -> List[Step]:
    if c.variant == "already_logged_in":
        return [c.step("assert_logged_in")]
    if not c.deep_link:
        c.skip("没有提供深链 URL (plan(deep_link=...)), 验证不了登录后回原页")
        return []
    return [c.step("goto", value=c.deep_link), c.step("assert_logged_in")]


def _b_security_password(c: _Ctx) -> List[Step]:
    if c.variant == "autocomplete":
        return [c.step("assert_autocomplete", c.seed.ref, "current-password")]
    return c.input(c.seed.ref, c.credentials[1]) + [c.step("assert_masked", c.seed.ref)]


def _b_masking_sensitive(c: _Ctx) -> List[Step]:
    return [c.step("assert_masked")]


def _b_readonly_fields(c: _Ctx) -> List[Step]:
    return [c.step("assert_inactive", c.seed.ref)]


def _b_actions_permission(c: _Ctx) -> List[Step]:
    if c.variant == "readonly":
        return [c.step("assert_inactive", c.seed.ref)]
    return [c.step("assert_visible", c.seed.ref), c.step("click", c.seed.ref),
            c.step("assert_feedback")]


def _b_actions_delete(c: _Ctx) -> List[Step]:
    steps = [c.step("click", c.seed.ref)]
    if c.variant == "confirm":
        steps.append(c.step("confirm_dialog", value=c.confirm_label))
        return steps + c.no_error()
    steps.append(c.step("cancel_dialog", value=c.cancel_label))
    return steps + [c.step("assert_visible", c.seed.ref)]


def _b_navigation_back(c: _Ctx) -> List[Step]:
    c.warn("返回后是否保留筛选/页码属于站点规格, 断言只能到『不报错』")
    return [c.step("back")] + c.no_error()


def _b_consistency_with_list(c: _Ctx) -> List[Step]:
    return [c.step("assert_consistent", value="list")]


def _b_chart_each(c: _Ctx) -> List[Step]:
    if c.variant == "empty":
        c.warn("需要先构造一个没有数据的时间区间")
        return [c.step("assert_chart_empty", c.seed.ref)]
    return [c.step("assert_chart_matches_source", c.seed.ref)]


def _b_chart_legend(c: _Ctx) -> List[Step]:
    return [c.step("toggle_legend", c.seed.ref)] + c.no_error()


def _b_filter_range(c: _Ctx) -> List[Step]:
    element = c.inv.element(c.seed.ref)
    value = str(c.seed.data.get("value") or "近7天")
    if element is not None and _type_of(element) == "select":
        options = list(element.options)
        if options and value not in options:
            c.warn(f"下拉里没有『{value}』, 改用选项『{options[0]}』")
            value = str(options[0])
    return c.input(c.seed.ref, value) + c.apply() + c.no_error()


def _b_refresh_manual(c: _Ctx) -> List[Step]:
    return [c.step("reload"), c.step("assert_loading")] + c.no_error()


def _b_export_match(c: _Ctx) -> List[Step]:
    export = c.export_eid
    if not export:
        c.skip("页面上没有导出按钮")
        return []
    return [c.step("export", export)] + c.rows("assert_export_matches_list")


def _b_smoke_load(c: _Ctx) -> List[Step]:
    return [c.step("assert_page_ready_within", value=3000)] + c.no_error()


def _b_smoke_interactive(c: _Ctx) -> List[Step]:
    return [c.step("click", c.seed.ref), c.step("assert_feedback")]


def _b_security_inputs(c: _Ctx) -> List[Step]:
    value = c.value()
    steps = c.input(c.seed.ref, value)
    element = c.inv.element(c.seed.ref)
    in_form = element is not None and element.container in ("form", "dialog")
    steps += c.submit() if in_form else c.apply()
    if c.variant == "script":
        return steps + c.no_alert() + c.no_error()
    return steps + c.no_error()


_BUILDERS: Dict[str, Callable[[_Ctx], List[Step]]] = {
    # list_crud
    "query.filter": _b_query_filter,
    "query.reset": _b_query_reset,
    "query.count": _b_query_count,
    "pagination.jump": _b_pagination_jump,
    "pagination.keep_filter": _b_pagination_keep_filter,
    "sort.column": _b_sort_column,
    "sort.null": _b_sort_null,
    "sort.across_pages": _b_sort_across_pages,
    "row_actions.each": _b_row_actions,
    "batch.select": _b_batch_select,
    "batch.delete": _b_batch_delete,
    "state.empty": _b_state_empty,
    "state.loading": _b_state_loading,
    "state.perf": _b_state_perf,
    "consistency.three_way": _b_consistency_three_way,
    "security.text_input": _b_security_text,
    # form
    "required.each": _b_required_each,
    "required.all": _b_required_all,
    "format.each": _b_format_each,
    "format.unique": _b_format_unique,
    "dependency.linkage": _b_dependency_linkage,
    "persist.after_save": _b_persist_after_save,
    "submit.double": _b_submit_double,
    "submit.interrupt": _b_submit_interrupt,
    "reset.defaults": _b_reset_defaults,
    "cancel.discard": _b_cancel_discard,
    # login
    "credential.negative": _b_credential_negative,
    "credential.submit": _b_credential_submit,
    "lockout.repeat": _b_lockout_repeat,
    "session.target": _b_session_target,
    "security.password": _b_security_password,
    # detail
    "masking.sensitive": _b_masking_sensitive,
    "readonly.fields": _b_readonly_fields,
    "actions.permission": _b_actions_permission,
    "actions.delete_confirm": _b_actions_delete,
    "navigation.back": _b_navigation_back,
    "consistency.with_list": _b_consistency_with_list,
    # dashboard
    "chart.each": _b_chart_each,
    "chart.legend": _b_chart_legend,
    "filter.range": _b_filter_range,
    "refresh.manual": _b_refresh_manual,
    "export.match": _b_export_match,
    # unknown
    "smoke.load": _b_smoke_load,
    "smoke.interactive": _b_smoke_interactive,
    "security.inputs": _b_security_inputs,
}


def build_steps(ctx: _Ctx) -> Optional[Tuple[Step, ...]]:
    """种子 -> 步骤; None 表示这条用例出不了可执行步骤 (跳过并记原因)"""
    builder = _BUILDERS.get(ctx.seed.strategy)
    if builder is None:
        # 模板加了新策略但这里没跟: 退化成冒烟, 并在 notes 里报出来
        ctx.warn(f"没有 {ctx.seed.strategy} 的步骤模板, 退化为『点一下 + 别报错』")
        target = ctx.seed.ref if ctx.seed.ref != PAGE_REF else ""
        steps = [ctx.step("assert_visible", target)] if target else [ctx.step("reload")]
        return tuple(steps + ctx.no_error())
    steps = builder(ctx)
    if ctx.skipped:
        return None
    if not steps:
        ctx.warn("步骤模板没有产出步骤, 跳过")
        return None
    return tuple(steps)


# ---------------------------------------------------------------------------
# 校验: 计划 -> 问题清单 (闭环, 不静默)
# ---------------------------------------------------------------------------

_FIELD_ACTIONS = frozenset(("fill", "clear", "select", "check", "uncheck", "press"))


def _element_problem(action: Action, element: ElementSpec) -> str:
    """动作打在这个控件上有没有意义 (fill 打到按钮上是计划写错了, 不是站点坏了)"""
    if action.name in _FIELD_ACTIONS and not element.is_field:
        return (f"{action.name} 只能打在输入控件上, {element.eid} 是 "
                f"{element.kind or element.tag}")
    if action.name == "select" and _type_of(element) != "select":
        return f"select 只能打在下拉框上, {element.eid} 是 {_type_of(element) or '未知类型'}"
    if action.name in ("check", "uncheck") and not _is_check(element):
        return f"{action.name} 只能打在 checkbox/radio 上, {element.eid} 不是"
    return ""


def _value_problem(action: Action, value: Any) -> str:
    if action.value == "req" and value is None:
        return f"{action.name} 缺少 value"
    if action.value == "" and value is not None:
        return f"{action.name} 是页级动作, 不该带 value"
    if value is None or action.value == "":
        return ""
    kind = action.kind
    is_int = isinstance(value, int) and not isinstance(value, bool)
    if kind == "int" and not is_int:
        return f"{action.name} 的 value 要是整数, 拿到 {type(value).__name__}"
    if kind == "page" and not (is_int or value in ("first", "last", "next", "prev")):
        return f"{action.name} 的 value 要是页码或 first/last/next/prev, 拿到 {value!r}"
    if kind == "direction" and value not in ("asc", "desc"):
        return f"{action.name} 的方向只能是 asc/desc, 拿到 {value!r}"
    if kind == "relation" and value not in ("detail", "list"):
        return f"{action.name} 的对照对象只能是 detail/list, 拿到 {value!r}"
    if kind in ("text", "url", "key", "label") and not isinstance(value, str):
        return f"{action.name} 的 value 要是字符串, 拿到 {type(value).__name__}"
    return ""


def validate_step(inventory: Any, step: Step) -> str:
    """单步校验; 返回空串表示没问题。渲染器拿这条当准入门槛"""
    if not isinstance(step, Step):
        return "步骤不是 Step"
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    action = ACTIONS.get(step.action)
    if action is None:
        return f"未知动作 {step.action!r}: 动作词表里没有, 渲染器不会执行"
    if action.eid == "req" and not step.eid:
        return f"{action.name} 缺少 eid"
    if action.eid == "none" and step.eid:
        return f"{action.name} 是页级动作, 不该带 eid"
    if step.eid:
        ref = resolve_ref(inv, step.eid)
        if ref is None:
            return f"{action.name} 的 eid {step.eid!r} 不在元素清单里"
        if action.on and ref.kind not in action.on:
            return f"{action.name} 不能作用在 {ref.kind} ({step.eid}) 上"
        element = inv.element(ref.eid) if ref.eid else None
        problem = _element_problem(action, element) if element is not None else ""
        if problem:
            return problem
    return _value_problem(action, step.value)


def validate_case(inventory: Any, case: PlanCase) -> Tuple[str, ...]:
    """一条用例的全部问题 (含清理步骤)"""
    problems: List[str] = []
    if not case.case_id:
        problems.append("缺少 case_id")
    if case.priority not in PRIORITIES:
        problems.append(f"优先级 {case.priority!r} 不在 P0..P3 里")
    if not case.steps:
        problems.append("没有步骤")
    for index, step in enumerate(case.steps, 1):
        problem = validate_step(inventory, step)
        if problem:
            problems.append(f"步骤 {index} ({step.action}): {problem}")
    for index, step in enumerate(case.cleanup, 1):
        problem = validate_step(inventory, step)
        if problem:
            problems.append(f"清理步骤 {index} ({step.action}): {problem}")
    return tuple(problems)


# ---------------------------------------------------------------------------
# 标签 / 优先级 / 前置条件 / 测试数据
# ---------------------------------------------------------------------------

_BASE_PRECONDITIONS: Dict[str, Tuple[str, ...]] = {
    "list_crud": ("已登录", "拥有该模块的查看权限", "列表中至少有 1 条测试数据"),
    "form": ("已登录", "拥有该模块的新增/编辑权限"),
    "login": ("未登录", "已准备一组可用的测试账号"),
    "detail": ("已登录", "列表中存在至少 1 条记录"),
    "dashboard": ("已登录", "报表数据已生成"),
    UNKNOWN: ("页面可访问",),
}


def _effective_risk(seed: CaseSeed) -> str:
    """这条用例实际会不会动数据。

    no_permission / readonly / not_editable 只断言『入口不可用/不可编辑』, 一下都不点:
    模板把行内『删除』的风险传下来是对的, 但拿它给一条只看不点的用例定 P0 就错了。
    """
    return SAFE if seed.variant in _NO_TOUCH_VARIANTS else seed.risk


def _tags(seed: CaseSeed) -> Tuple[str, ...]:
    risk = _effective_risk(seed)
    flags = set()
    if risk == DESTRUCTIVE:
        flags.add("destructive")
    elif risk == WRITE:
        flags.add("write")
    if seed.priority >= SLOW:
        flags.add("performance")
    if seed.variant in NEGATIVE_VARIANTS:
        flags.add("negative")
    value = seed.data.get("value")
    if (seed.strategy.startswith("security") or seed.group == "security"
            or value in (SQLI_PAYLOAD, XSS_PAYLOAD)):
        flags.add("security")
    if "role" in seed.data:
        flags.add("permission")
    if seed.group in ("consistency", "masking"):
        flags.add("data")
    ordered = [tag for tag in TAG_ORDER if tag in flags and tag != seed.group]
    return (seed.group, *ordered)


def _priority(seed: CaseSeed) -> str:
    """破坏性用例一律 P0: 它能 block 发布, 不因为模板写 MUST 就降级"""
    if _effective_risk(seed) == DESTRUCTIVE:
        return P0
    return _PRIORITY_OF.get(seed.priority, P2)


def _preconditions(seed: CaseSeed, archetype: str) -> Tuple[str, ...]:
    if seed.strategy == "session.target" and seed.variant == "already_logged_in":
        base: Tuple[str, ...] = ("已登录", "会话未过期")
    else:
        base = _BASE_PRECONDITIONS.get(archetype, ("页面可访问",))
    extra: List[str] = []
    risk = _effective_risk(seed)
    if "role" in seed.data:
        extra.append("使用只读账号登录")
    if risk == DESTRUCTIVE:
        extra += ["已备份或可回滚的测试数据", "选中至少 1 条可删除的测试数据"]
    elif risk == WRITE:
        extra.append("使用专用测试数据 (会写库)")
    if "attempts" in seed.data:
        extra.append("该账号未被锁定, 且允许消耗一次锁定")
    if "network" in seed.data or "throttle" in seed.data:
        extra.append("可以模拟弱网/断网")
    if "remember" in seed.data:
        extra.append("允许写 Cookie 的干净浏览器")
    if seed.strategy in ("consistency.three_way", "export.match") and seed.variant in ("export", "rows"):
        extra.append("导出目录可写")
    out: List[str] = []
    for item in (*base, *extra):
        if item not in out:
            out.append(item)
    return tuple(out)


def _test_data(seed: CaseSeed, used: Mapping[str, Any],
               filter_element: Optional[ElementSpec]) -> Dict[str, Any]:
    """test_data: 字段名 -> 具体值 + 辅助参数 (页码/方向/点击次数...)

    字段值从『真正填下去的东西』收集, 而不是从模板数据猜 —— 登录用例的密码、
    带默认值的清空操作都是编译时才确定的。
    """
    out: Dict[str, Any] = {}
    for key, raw in seed.data.items():
        if key in ("value", "type", "token", "length", "filter"):
            continue
        out[DATA_LABELS.get(key, key)] = raw
    for label, value in used.items():
        # 字段名优先用可读的中文 (采集器给的 name 有时就是 password 这种属性值)
        out[DATA_LABELS.get(label, label)] = value
    if "filter" in seed.data:
        # 已经按字段名记过同一个值就别再加一个含混的『筛选值』
        value = resolve_value(seed.data["filter"], filter_element, seed.data)[0]
        if value not in out.values():
            out[DATA_LABELS["filter"]] = value
    return out


def _cleanup(ctx: _Ctx, steps: Sequence[Step]) -> Tuple[Step, ...]:
    """清理: 真删了就没什么可清; 弹窗悬着就关; 写过库就刷新"""
    names = {step.action for step in steps}
    if "confirm_dialog" in names or "cancel_dialog" in names:
        return ()
    if ctx.inv.dialogs and names & {"click", "double_click"}:
        return (Step("cancel_dialog", value=ctx.cancel_label),)
    if _effective_risk(ctx.seed) != SAFE:
        return (Step("reload"),)
    return ()


# ---------------------------------------------------------------------------
# 主入口: 种子 -> 计划
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CaseBundle:
    """build_cases 的返回: 用例 + 四类诊断 (全部要能回溯到具体 cid)"""

    cases: List[PlanCase]
    skipped: List[str]
    unresolved: List[str]
    notes: List[str]
    problems: List[str]


def build_cases(inventory: Any, seeds: Iterable[CaseSeed], *, archetype: str = "",
                page: str = "", credentials: Tuple[str, str] = (DEMO_USERNAME, DEMO_PASSWORD),
                deep_link: str = "", max_fields: int = MAX_FIELDS,
                result_cap: int = RESULT_CAP) -> CaseBundle:
    """种子 -> 用例; 跳过/未解析/校验问题全部带回来 (不静默)"""
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    prefix = CASE_PREFIX.get(archetype, "TC")
    page_name = inv.title or inv.page_name or page or "页面"
    cases: List[PlanCase] = []
    skipped: List[str] = []
    unresolved: List[str] = []
    notes: List[str] = []
    problems: List[str] = []

    for seed in seeds:
        case_id = f"{prefix}-{len(cases) + 1:03d}"
        ctx = _Ctx(inv, seed, case_id=case_id, page=page, credentials=credentials,
                   deep_link=deep_link, max_fields=max_fields, result_cap=result_cap)
        steps = build_steps(ctx)
        notes.extend(ctx.notes)
        unresolved.extend(ctx.unresolved)
        if steps is None:
            skipped.append(f"{seed.cid}: {ctx.skipped or '出不了可执行步骤'}")
            continue
        first_filter = inv.element(ctx.first_filter) if ctx.first_filter else None
        case = PlanCase(
            case_id=case_id,
            title=f"{page_name}-{seed.title.replace(' · ', '-')}",
            priority=_priority(seed),
            tags=_tags(seed),
            preconditions=_preconditions(seed, archetype),
            test_data=_test_data(seed, ctx.used, first_filter),
            steps=steps,
            cleanup=_cleanup(ctx, steps),
            cid=seed.cid,
            expect=tuple(seed.expect),
        )
        bad = validate_case(inv, case)
        if bad:
            problems.extend(f"{case_id} ({seed.cid}): {message}" for message in bad)
        cases.append(case)

    return CaseBundle(cases, skipped, unresolved, notes, problems)


def _profile_name() -> str:
    """当前规则包名 (与 profiles.loader 同一个环境变量); 惰性 import, 感知层不依赖规则包"""
    try:
        from ai_kit.profiles.loader import PROFILE_ENV
    except Exception:                       # 规则包被裁掉时也能出计划
        PROFILE_ENV = "AI_KIT_PROFILE"
    return os.environ.get(PROFILE_ENV, "").strip()


def _model_name() -> str:
    """当前 provider 的 model (只读配置, 不调网络); 没配就是空串"""
    try:
        from ai_kit.config import get_active_config

        return str(get_active_config().get("model") or "")
    except Exception:
        return ""


def plan(inventory: Any, *, page: Optional[str] = None, archetype: str = "",
         profile: str = "", model: str = "", prompt_version: str = PLANNER_VERSION,
         expansion: Optional[Expansion] = None, limit: int = 0, include_slow: bool = True,
         max_per_strategy: int = MAX_PER_STRATEGY,
         credentials: Tuple[str, str] = (DEMO_USERNAME, DEMO_PASSWORD),
         deep_link: str = "", max_fields: int = MAX_FIELDS, result_cap: int = RESULT_CAP,
         generated_at: str = "") -> TestPlan:
    """一页清单 -> 用例计划 (确定性, 不调 LLM)

    inventory 可以是 PageInventory 或 collector 的 dict; 不传 expansion 就现场展开
    (limit / include_slow / max_per_strategy 与 ④ 同一口径)。page 留空取 URL 的 path。
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    exp = expansion or expand(inv, archetype or None, limit=limit,
                              include_slow=include_slow, max_per_strategy=max_per_strategy)
    name = exp.archetype
    target_page = _path_of(inv.url) if page is None else page
    bundle = build_cases(inv, exp.cases, archetype=name, page=target_page,
                         credentials=credentials, deep_link=deep_link,
                         max_fields=max_fields, result_cap=result_cap)
    diagnostics = {
        "missing": list(exp.missing),
        "notes": list(exp.notes) + bundle.notes,
        "skipped": bundle.skipped,
        "unresolved": bundle.unresolved,
        "problems": bundle.problems,
    }
    return TestPlan(page=target_page, archetype=name, profile=profile or _profile_name(),
                    model=model or _model_name(), prompt_version=prompt_version,
                    generated_at=generated_at or _utcnow(), cases=tuple(bundle.cases),
                    diagnostics=diagnostics)


def plan_cache_key(inventory: Any, archetype: str = "") -> str:
    """缓存键: 页面结构 + 三份版本 + 原型。改模板/改词表都必须换键

    原型跟 plan() 同一口径 (空/未知就地规则分类) —— 键里写 unknown 而计划按
    list_crud 生成, 缓存就会把两份不相干的东西当成一份。
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    name = str(archetype or inv.archetype or "").strip().lower()
    if name not in CHECKLISTS:
        name = classify(inv).archetype
    return f"{inv.fingerprint}|strategy=v{STRATEGY_VERSION}|planner={PLANNER_VERSION}|{name}"


# ---------------------------------------------------------------------------
# 可选增强: LLM 改步骤, 但必须过同一套校验
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = """你在为一套 UI 自动化框架写测试步骤, 只输出 JSON。

硬约束:
1. 每一步只能是 {"action": str, "eid": str?, "value": any?}, 不许出现代码/定位串。
2. eid 只能取输入清单里出现过的 ref (元素 eid / c1.column:列名 / c1.row:按钮 / chart:1 / page)。
3. action 只能取下面词表里的名字, 不许自创。
4. 不要编造页面上的控件; 缺人就少写, 不要为了凑步骤硬填。
5. 返回 {"cases": [{"case_id": "LC-001", "steps": [...], "cleanup": [...], "reason": "一句话"}]};
   只返回你确实改过的用例, 没改的不要出现。"""


def _slim_inventory(inv: PageInventory, max_items: int = 40) -> Dict[str, Any]:
    """给 LLM 的清单摘要: 丢掉 html/定位串, 只留能决策的字段"""
    def slim_element(element: ElementSpec) -> Dict[str, Any]:
        row: Dict[str, Any] = {"eid": element.eid, "kind": element.kind,
                               "type": element.type, "name": element.name,
                               "container": element.container, "risk": element.risk}
        if element.options:
            row["options"] = list(element.options)[:8]
        if element.constraints:
            row["constraints"] = dict(element.constraints)
        return row

    return {
        "url": inv.url, "title": inv.title, "archetype": inv.archetype,
        "signals": dict(inv.signals),
        "elements": [slim_element(element) for element in inv.elements[:max_items]],
        "tables": [{"eid": table.get("eid"), "columns": list(table.get("columns") or ())[:10],
                    "row_count": table.get("row_count"),
                    "row_actions": [dict(action) for action in (table.get("row_actions") or ())[:8]]}
                   for table in inv.tables],
        "dialogs": [{"eid": dialog.get("eid"), "buttons": list(dialog.get("buttons") or ())}
                    for dialog in inv.dialogs],
        "notes": list(inv.notes)[:5],
    }


def _action_catalog_text() -> str:
    lines = []
    for action in ACTIONS.values():
        eid = {"req": "必须", "opt": "可选", "none": "不要"}[action.eid]
        value = {"req": "必须", "opt": "可选", "": "不要"}[action.value]
        lines.append(f"- {action.name}: {action.doc} | eid {eid} | value {value}")
    return "\n".join(lines)


def _llm_prompt(inv: PageInventory, current: TestPlan, cases: Sequence[PlanCase]) -> str:
    payload = [{"case_id": case.case_id, "cid": case.cid, "title": case.title,
                "expect": list(case.expect),
                "steps": [step.to_dict() for step in case.steps]} for case in cases]
    return (f"页面: {inv.url}\n原型: {current.archetype}\n\n"
            f"## 动作词表\n{_action_catalog_text()}\n\n"
            f"## 页面清单\n{json.dumps(_slim_inventory(inv), ensure_ascii=False)}\n\n"
            f"## 现有用例 (可以改步骤, 不能改 case_id/title/期望)\n"
            f"{json.dumps(payload, ensure_ascii=False)}")


def _ask_llm(prompt: str) -> Any:
    """默认问法: 惰性 import client —— 默认路径不碰网络也不碰密钥"""
    from ai_kit import client

    return client.chat_json(PLANNER_SYSTEM, prompt, max_retries=0)


def _steps_from(raw: Any) -> Optional[Tuple[Step, ...]]:
    """LLM 给的步骤 -> Step; 结构不对就整条拒掉 (不让半截东西进计划)"""
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    steps: List[Step] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return None
        action = str(item.get("action") or "").strip()
        if not action:
            return None
        steps.append(Step(action, str(item.get("eid") or "").strip(), item.get("value")))
    return tuple(steps)


def plan_with_llm(inventory: Any, *, ask: Optional[Callable[[str], Any]] = None,
                  max_cases: int = 0, model: str = "", **kwargs: Any) -> TestPlan:
    """确定性计划 + LLM 改写 (逐条校验, 不通就保留原步骤)

    ask 可注入 (测试/换模型); 默认走 client.chat_json。任何异常/脏返回都退化成
    确定性计划并在 diagnostics 里说明 —— 规划失败不该让整条流水线挂掉。
    """
    base = plan(inventory, model=model, **kwargs)
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    if not base.cases:
        return base
    pool = list(base.cases[:max_cases] if max_cases > 0 else base.cases)
    diagnostics = dict(base.diagnostics)
    problems = list(base.problems)

    try:
        answer = (ask or _ask_llm)(_llm_prompt(inv, base, pool))
    except Exception as exc:                # noqa: BLE001 — LLM 是增强, 挂了不影响主流程
        diagnostics["llm"] = f"调用失败, 用确定性步骤: {exc}"
        diagnostics["problems"] = problems
        return replace(base, diagnostics=diagnostics)

    items = answer.get("cases") if isinstance(answer, Mapping) else answer
    if not isinstance(items, (list, tuple)) or not items:
        diagnostics["llm"] = "没有可用的返回, 用确定性步骤"
        diagnostics["problems"] = problems
        return replace(base, diagnostics=diagnostics)

    by_id = {case.case_id: case for case in base.cases}
    merged = list(base.cases)
    accepted = rejected = 0
    for item in items:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        case_id = str(item.get("case_id") or "").strip()
        case = by_id.get(case_id)
        if case is None:
            rejected += 1
            problems.append(f"LLM 返回了计划里没有的 {case_id!r}")
            continue
        steps = _steps_from(item.get("steps"))
        cleanup = _steps_from(item.get("cleanup")) or ()
        if steps is None:
            rejected += 1
            problems.append(f"{case_id} (LLM): 步骤结构不对, 保留确定性步骤")
            continue
        candidate = replace(case, steps=steps, cleanup=cleanup)
        bad = validate_case(inv, candidate)
        if bad:
            rejected += 1
            problems.extend(f"{case_id} (LLM): {message}" for message in bad)
            continue
        merged[merged.index(case)] = candidate
        accepted += 1

    diagnostics["llm"] = f"接受 {accepted} 条, 拒绝 {rejected} 条"
    diagnostics["problems"] = problems
    return replace(base, cases=tuple(merged), model=model or base.model,
                   diagnostics=diagnostics)
