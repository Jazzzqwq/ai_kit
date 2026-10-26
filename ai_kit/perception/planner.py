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

缓存键: plan_cache_key() = 指纹 + profile + model + inventory/strategy/planner 三个
版本 + 原型。改了步骤模板却复用旧计划, 比重新生成一次贵得多; 换了规则包或模型还
复用旧计划, 等于拿上一份答案交这一份卷 (5.9 的缓存键就是为这两件事加的)。
"""
from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from ai_kit.ledger import BudgetExceeded
import ai_kit.perception.prompts as llm_prompts
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

# 步骤模板/动作词表/LLM prompt 改了就 +1: 缓存键要带它, 否则旧计划会盖住新模板
# v4: 5.10 重写 prompt (system 硬约束 + 五块事实 + PO 签名段), 旧计划必须重新生成
PLANNER_VERSION = "planner.v4"

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
    # 5.7: 四层校验仍没过 -> 待人工确认。不进 to_dict: schema 是 ⑥ 的契约,
    # "这条还没验收"是流水线状态, 不是用例内容。
    needs_review: bool = False

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

    @property
    def review(self) -> Tuple[PlanCase, ...]:
        """5.7 校验闭环后仍没过的用例 (其余用例照常渲染执行, 不陪绑)"""
        return tuple(case for case in self.cases if case.needs_review)

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
                           ("schema", "schema 校验 (结构/词表/eid 格式)"),
                           ("live", "活页面解析 (locator 找不到)"),
                           ("ast", "AST 白名单 (渲染产物越界)"),
                           ("duplicates", "去重 (近重复已合并)"),
                           ("live_skipped", "活页面没查的 (不是失败)"),
                           ("notes", "说明"),
                           ("problems", "校验问题")):
            items = list(self.diagnostics.get(key) or ())
            if not items:
                continue
            lines.append(f"  {title}: {len(items)}")
            lines.extend(f"    - {item}" for item in items)
        pending = self.diagnostics.get("needs_review") or {}
        if pending:
            lines.append(f"  需要人工确认: {len(pending)}")
            for case_id, messages in pending.items():
                lines.append(f"    - {case_id}: {'; '.join(messages)}")
        for extra, title in (("verify", "校验闭环"), ("llm", "LLM")):
            value = self.diagnostics.get(extra)
            if value:
                lines.append(f"  {title}: {value}")
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


def plan_cache_key(inventory: Any, archetype: str = "", *, profile: str = "",
                   model: str = "", prompt_version: str = "") -> str:
    """缓存键: 指纹 + profile + model + 三份版本 + 原型 (卡片 5.9)

    原型跟 plan() 同一口径 (空/未知就地规则分类) —— 键里写 unknown 而计划按
    list_crud 生成, 缓存就会把两份不相干的东西当成一份。

    profile / model 空串 = 「和 plan() 同一口径」, 现场从环境解析; 回读缓存时按文件
    里存的成分比对, 不靠「读的时候环境还没被改」这种运气。
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    name = str(archetype or inv.archetype or "").strip().lower()
    if name not in CHECKLISTS:
        name = classify(inv).archetype
    owner = str(profile or _profile_name())
    engine = str(model or _model_name())
    version = str(prompt_version or PLANNER_VERSION)
    return (f"{inv.fingerprint}|profile={owner}|model={engine}"
            f"|strategy=v{STRATEGY_VERSION}|planner={version}|{name}")


# ---------------------------------------------------------------------------
# 可选增强: LLM 改步骤, 但必须过同一套校验 (prompt 约束见 perception/prompts.py)
# ---------------------------------------------------------------------------
#
# 模型只产「用例清单 JSON」, 不产代码 (5.10 的两步法): 代码由 ⑥ 按模板渲染 ——
# 一旦模型开始写 pytest 函数, 定位串/等待时间/隐式睡眠就都回到它手里了。


def _llm_prompt(inv: PageInventory, current: TestPlan, cases: Sequence[PlanCase],
                errors: Optional[Mapping[str, Sequence[str]]] = None, *,
                po_methods: Iterable[str] = (),
                po_signatures: Iterable[str] = ()) -> str:
    """五块事实 (原型/checklist/清单 JSON/PO 签名/schema) + 本轮用例 + 上轮错误

    errors: 上一轮校验的问题 (5.7 的回填重生成); 不传就是一次性改写。
    po_methods / po_signatures: po 模式才有 —— 非 po 模式这两段整段不出现。
    """
    payload = [{"case_id": case.case_id, "cid": case.cid, "title": case.title,
                "expect": list(case.expect),
                "steps": [step.to_dict() for step in case.steps]} for case in cases]
    # checklist 与缓存键同一口径: 计划里的 archetype 是 unknown 时也要给出真正的清单,
    # 否则「策略 checklist」这一段对模型就是空的 —— 那就等于让它自己发明测试点。
    name = str(current.archetype or "").strip().lower()
    if name not in CHECKLISTS:
        name = str(inv.archetype or "").strip().lower()
    if name not in CHECKLISTS:
        name = classify(inv).archetype
    return llm_prompts.build_user_prompt(
        archetype=name,
        checklist=CHECKLISTS.get(name, ()),
        inventory=llm_prompts.slim_inventory(inv),
        actions=ACTIONS.values(),
        po_methods=po_methods,
        po_signatures=po_signatures,
        cases=payload,
        errors=errors,
    )


def _ask_llm(prompt: str) -> Any:
    """默认问法: 惰性 import client —— 默认路径不碰网络也不碰密钥

    temperature / max_tokens 都显式给 (5.10 第 3 条): 不指定上限等于把这次调用的
    花费交给服务端默认值 一一 一份跑飞的 JSON 可能一次烧穿整次 run 的预算。
    """
    from ai_kit import client

    return client.chat_json(llm_prompts.PLANNER_SYSTEM, prompt, max_retries=0,
                            temperature=llm_prompts.TEMPERATURE,
                            max_tokens=llm_prompts.max_tokens())


def _steps_from(raw: Any, field_name: str = "steps"
                ) -> Tuple[Optional[Tuple[Step, ...]], Tuple[str, ...]]:
    """LLM 给的步骤 -> Step; 有一个键不对就整条拒掉 (不让半截东西进计划)

    解析走 ① 的 step_from_dict: 生成侧和校验侧必须是同一套口径 —— 两边各写一份,
    就会出现「改写能进计划、存成 JSON 再读回来却过不了校验」这种自相矛盾。
    """
    if raw is None:
        return (), ()
    if not isinstance(raw, (list, tuple)) or not raw:
        return None, (f"{field_name} 结构不对, 要非空数组",)
    steps, problems = _steps_of(raw, field_name)
    if problems:
        return None, tuple(problems)
    return steps, ()


def plan_with_llm(inventory: Any, *, ask: Optional[Callable[[str], Any]] = None,
                  max_cases: int = 0, model: str = "", po_methods: Iterable[str] = (),
                  po_signatures: Iterable[str] = (), **kwargs: Any) -> TestPlan:
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
        answer = (ask or _ask_llm)(_llm_prompt(inv, base, pool, po_methods=po_methods,
                                               po_signatures=po_signatures))
    except BudgetExceeded:
        # 预算用完不是「LLM 抽风」: 吞成「用确定性步骤」就等于钱花完了却没人知道
        raise
    except Exception as exc:                # noqa: BLE001 — LLM 是增强, 挂了不影响主流程
        diagnostics["llm"] = f"调用失败, 用确定性步骤: {exc}"
        diagnostics["problems"] = problems
        return replace(base, diagnostics=diagnostics)

    items = answer.get("cases") if isinstance(answer, Mapping) else answer
    if not isinstance(items, (list, tuple)) or not items:
        diagnostics["llm"] = "没有可用的返回, 用确定性步骤"
        diagnostics["problems"] = problems
        return replace(base, diagnostics=diagnostics)

    merged, accepted, rejected = _merge_rewrites(inv, base.cases, items, problems)
    diagnostics["llm"] = f"接受 {accepted} 条, 拒绝 {rejected} 条"
    diagnostics["problems"] = problems
    return replace(base, cases=tuple(merged), model=model or base.model,
                   diagnostics=diagnostics)


# ---------------------------------------------------------------------------
# 5.7 校验闭环: ① schema -> ② 活页面 -> ③ AST 白名单 -> ④ 去重
# ---------------------------------------------------------------------------
#
# 四层各看一个东西, 少一层就会有一类错误漏到执行阶段:
#   ① 只看计划自己 (必填键 / 优先级取值 / 动作词表 / eid 语法), 不碰页面;
#   ② 拿清单里的定位串去活页面 find_elements —— 采集时看见的元素, 现在还在吗;
#   ③ 看 ⑥ 的渲染产物: 白名单外的调用 (eval / subprocess / 编出来的 PO 方法) 不放过;
#   ④ 同一件事写两遍的用例, 跑起来只会让失败数翻倍。
# 每层只产出「问题」, 处置在上层: 有问题就回填 prompt 重生成 (最多 2 次),
# 还不过就标 needs_review —— 卡住一条, 不能卡住整页。

PLAN_KEYS: Tuple[str, ...] = ("page", "archetype", "profile", "model",
                              "prompt_version", "generated_at", "cases")
CASE_KEYS: Tuple[str, ...] = ("case_id", "title", "priority", "tags", "preconditions",
                              "test_data", "steps", "cleanup")
STEP_KEYS: Tuple[str, ...] = ("action", "eid", "value")
CASE_ID_RE = re.compile(r"^[A-Z]{2}-\d{3,}$")
# 元素/表格/弹窗的 id: 采集器发的是 e0/c0/d1, 但计划也可能吃手写清单 (f1/u1), 只管形状
_ELEMENT_ID_RE = re.compile(r"^[A-Za-z]\w*$")


def eid_format_problem(ref: Any) -> str:
    """① eid 是不是清单引用的语法 (它在不在清单里由 ② 管, 不在这里报)

    只形状校验: e12 / c0 / d1 是采集器的叫法, 手写清单的 f1/u1 同样能用 ——
    在这里卡死字母前缀, 会把非采集器来源的全部计划判成错。
    """
    text = str(ref or "").strip()
    if not text:
        return "eid 是空串"
    if text == PAGE_REF:
        return ""
    if text.startswith("chart:"):
        index = _as_int(text.split(":", 1)[1])
        return "" if index >= 1 else f"{text!r} 的图表序号要从 1 开始"
    for sep in (".column:", ".row:"):
        if sep in text:
            head, _, tail = text.partition(sep)
            if not _ELEMENT_ID_RE.match(head):
                return f"{text!r} 的表格 eid {head!r} 不合法 (字母开头, 不含分隔符)"
            if not tail.strip():
                return f"{text!r} 的表格引用要写成 c1{sep}<名字>"
            if any(other in tail for other in (".column:", ".row:")):
                return f"{text!r} 的列名/按钮名里不能再嵌表格引用"
            return ""
    if ":" in text or "." in text:
        return (f"{text!r} 不是合法引用: 只认 e1 / c1 / d1 / c1.column:列名 / "
                f"c1.row:按钮 / chart:1 / page")
    if not _ELEMENT_ID_RE.match(text):
        return f"{text!r} 的 eid 要以字母开头, 只含字母/数字/_"
    return ""


def step_from_dict(raw: Any) -> Tuple[Optional[Step], str]:
    """JSON 里的一步 -> Step; 返回 (步骤, 问题)

    多一个键就要拒不收: 出现 locator / timeout 这类键, 说明有人把 ⑥ 的活干到计划里了。
    """
    if not isinstance(raw, Mapping):
        return None, f"步骤不是对象: {type(raw).__name__}"
    extra = [str(key) for key in raw if key not in STEP_KEYS]
    if extra:
        return None, f"步骤出现卡片外的键 {extra}: 定位串/等待时间不该写进计划"
    action = str(raw.get("action") or "").strip()
    if not action:
        return None, "步骤缺少 action"
    return Step(action, str(raw.get("eid") or "").strip(), raw.get("value")), ""


def _steps_of(raw: Any, field_name: str) -> Tuple[Tuple[Step, ...], List[str]]:
    if raw is None:
        return (), []
    if not isinstance(raw, (list, tuple)):
        return (), [f"{field_name} 要是数组, 拿到 {type(raw).__name__}"]
    steps: List[Step] = []
    problems: List[str] = []
    for index, item in enumerate(raw, 1):
        step, problem = step_from_dict(item)
        if problem:
            problems.append(f"{field_name} {index}: {problem}")
            continue
        steps.append(step)                     # type: ignore[arg-type]
    return tuple(steps), problems


def case_schema_problems(case: PlanCase) -> Tuple[str, ...]:
    """① 的单条版本 (validate_schema / 重生成合并 / ⑥ 读缓存都走这里)"""
    problems: List[str] = []
    if not CASE_ID_RE.match(case.case_id or ""):
        problems.append(f"case_id {case.case_id!r} 不符合 XY-001 的编号规则")
    if not str(case.title or "").strip():
        problems.append("缺少 title")
    if case.priority not in PRIORITIES:
        problems.append(f"优先级 {case.priority!r} 不在 P0..P3 里")
    if not case.steps:
        problems.append("没有步骤")
    if not case.tags:
        problems.append("缺少 tags")
    for index, step in enumerate((*case.steps, *case.cleanup), 1):
        action = ACTIONS.get(step.action)
        if action is None:
            problems.append(f"步骤 {index}: 未知动作 {step.action!r}, 动作词表里没有")
        if step.eid:
            problem = eid_format_problem(step.eid)
            if problem:
                problems.append(f"步骤 {index}: {problem}")
        elif action is not None and action.eid == "req":
            problems.append(f"步骤 {index}: {step.action} 缺少 eid")
    return tuple(problems)


def validate_schema(target: Any) -> Tuple[str, ...]:
    """① JSON schema: 必填键 / 取值类型 / action 白名单 / eid 格式

    跑在 ② 之前, 因为它不需要页面: 计划是从 LLM 或 JSON 回来的, 先看结构对不对,
    再拿页面验证 —— 结构错的计划去碰页面只会得到一堆看不懂的 NoSuchElement。
    """
    if isinstance(target, Mapping):
        return plan_from_dict(target)[1]
    if not isinstance(target, TestPlan):
        return (f"不是 TestPlan: {type(target).__name__}",)
    problems: List[str] = []
    for case in target.cases:
        problems.extend(f"{case.case_id or '(无编号)'}: {message}"
                        for message in case_schema_problems(case))
    return tuple(problems)


def case_from_dict(raw: Any) -> Tuple[Optional[PlanCase], Tuple[str, ...]]:
    """JSON 用例 -> PlanCase; 结构不可救才返回 None (能构造的都留, 问题带出去)

    救回来的理由: 一条缺 title 的用例依然有步骤, 人补个标题就能跑; 直接丢掉等于
    把「表里有 8 条, 计划里只剩 6 条」这种账留给下一个人去对。
    """
    if not isinstance(raw, Mapping):
        return None, (f"用例不是对象: {type(raw).__name__}",)
    problems: List[str] = []
    missing = [key for key in CASE_KEYS if key not in raw]
    if missing:
        problems.append(f"缺少必填键 {missing}")
    extra = [str(key) for key in raw if key not in CASE_KEYS and key not in ("cid", "expect")]
    if extra:
        problems.append(f"出现卡片外的键 {extra}")
    steps, step_problems = _steps_of(raw.get("steps"), "steps")
    cleanup, cleanup_problems = _steps_of(raw.get("cleanup"), "cleanup")
    problems.extend(step_problems + cleanup_problems)
    data = raw.get("test_data")
    tags = raw.get("tags")
    preconditions = raw.get("preconditions")
    case = PlanCase(
        case_id=str(raw.get("case_id") or "").strip(),
        title=str(raw.get("title") or "").strip(),
        priority=str(raw.get("priority") or P1).strip(),
        tags=tuple(str(tag) for tag in tags) if isinstance(tags, (list, tuple)) else (),
        preconditions=(tuple(str(item) for item in preconditions)
                       if isinstance(preconditions, (list, tuple)) else ()),
        test_data=dict(data) if isinstance(data, Mapping) else {},
        steps=steps,
        cleanup=cleanup,
        cid=str(raw.get("cid") or "").strip(),
        expect=tuple(str(item) for item in raw.get("expect") or ()),
    )
    if data is not None and not isinstance(data, Mapping):
        problems.append(f"test_data 要是对象, 拿到 {type(data).__name__}")
    for field_name in ("tags", "preconditions"):
        value = raw.get(field_name)
        if value is not None and not isinstance(value, (list, tuple)):
            problems.append(f"{field_name} 要是数组, 拿到 {type(value).__name__}")
    return case, tuple(problems)


def plan_from_dict(payload: Any) -> Tuple[Optional[TestPlan], Tuple[str, ...]]:
    """卡片 5.6 的 JSON -> TestPlan (⑥ 读缓存 / 人改计划后回灌都走这里)"""
    if not isinstance(payload, Mapping):
        return None, (f"计划不是 JSON object: {type(payload).__name__}",)
    problems: List[str] = [f"计划出现卡片外的键 [{key}]"
                           for key in payload if key not in PLAN_KEYS]
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, (list, tuple)):
        return None, tuple(problems + ["cases 必须是数组"])
    cases: List[PlanCase] = []
    for index, raw in enumerate(raw_cases, 1):
        case, case_problems = case_from_dict(raw)
        problems.extend(f"用例 {index}: {message}" for message in case_problems)
        if case is not None:
            cases.append(case)
    plan_ = TestPlan(
        page=str(payload.get("page") or ""),
        archetype=str(payload.get("archetype") or UNKNOWN),
        profile=str(payload.get("profile") or ""),
        model=str(payload.get("model") or ""),
        prompt_version=str(payload.get("prompt_version") or PLANNER_VERSION),
        generated_at=str(payload.get("generated_at") or ""),
        cases=tuple(cases),
    )
    return plan_, tuple(problems)


# ---------------------------------------------------------------------------
# ② 活页面解析: 清单里的定位串现在还找得到元素吗
# ---------------------------------------------------------------------------

def _by_of(name: str) -> str:
    """定位策略名 -> selenium 的 By 字符串 (采集器写的 by 缺失时才用)"""
    key = _flat(name)
    return "xpath" if ("xpath" in key or key == "text") else "css selector"


def _locator_pairs(record: Any) -> List[Tuple[str, str]]:
    """一条记录 (ElementSpec / 表格 dict / 弹窗 dict) -> [(by, locator), ...]"""
    if isinstance(record, ElementSpec):
        pairs = [(_by_of(record.locator_by.get(name) or name), locator)
                 for name, locator in record.locators.items() if locator]
        return pairs or [(record.selector_by, record.selector)]
    locators = record.get("locators")
    raw_bys = record.get("locator_by")
    pairs: List[Tuple[str, str]] = []
    if isinstance(locators, Mapping):
        for name, locator in locators.items():
            by = raw_bys.get(name) if isinstance(raw_bys, Mapping) else ""
            text = str(locator or "")
            if text:
                pairs.append((str(by or _by_of(str(name))), text))
    if pairs:
        return pairs
    selector = str(record.get("selector") or "")
    return [("css selector", selector)] if selector else []


def _row_action_xpath(label: str) -> str:
    """行内按钮没 eid 时 (被折叠的行) 只能按文案找 —— 这也是它唯一的身份"""
    text = str(label or "").replace('"', "'")
    return (f'//button[normalize-space()="{text}"] | //a[normalize-space()="{text}"]'
            f' | //*[@role="button"][normalize-space()="{text}"]')


def _live_find(driver: Any, pairs: Sequence[Tuple[str, str]]) -> bool:
    """逐条定位串试一次; 任意一条命中就算元素还在 (定位漂移交给 healer)"""
    for by, locator in pairs:
        try:
            if driver.find_elements(by, locator):
                return True
        except Exception:                # noqa: BLE001 — 单条定位串失效不算事故
            continue
    return False


def _record_by_eid(inv: PageInventory, eid: str) -> Optional[Mapping]:
    for record in (*inv.tables, *inv.dialogs):
        if str(record.get("eid") or "") == eid:
            return record
    return None


def check_live(inventory: Any, refs: Iterable[str], driver: Any) -> Tuple[List[str], List[str]]:
    """② eid 活页面解析检查; 返回 (问题, 查不了的项)

    查不了的 (列标题 / 图表) 单独记, 不冒充失败: 用假的「找不到」去凑数, 只会训练
    人忽略这份报告。driver 传 None 就整层跳过, 并说明。
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    if driver is None:
        return [], ["没有 driver, 活页面这一层没跑"]
    problems: List[str] = []
    skipped: List[str] = []
    for ref in dict.fromkeys(str(item) for item in refs if item):
        target = resolve_ref(inv, ref)
        if target is None:
            problems.append(f"{ref}: 清单里没有这个引用")
            continue
        if target.kind == "page":
            continue
        if target.kind == "column":
            skipped.append(f"{ref}: 列标题不是可交互元素 (由 sort/assert_sorted 覆盖)")
            continue
        if target.kind == "chart":
            skipped.append(f"{ref}: 图表没有独立定位串 (渲染器按 canvas/svg 序号找)")
            continue
        if target.kind == "row-action" and not target.eid:
            if not _live_find(driver, [("xpath", _row_action_xpath(target.text))]):
                problems.append(f"{ref}: 活页面上找不到文案为『{target.text}』的行内按钮")
            continue
        record: Any = inv.element(target.eid) if target.eid else None
        if record is None and target.eid:
            record = _record_by_eid(inv, target.eid)
        if record is None:
            problems.append(f"{ref}: 清单里没有 {target.eid}")
            continue
        pairs = _locator_pairs(record)
        if not pairs:
            skipped.append(f"{ref}: 记录里没有定位串")
            continue
        if not _live_find(driver, pairs):
            problems.append(f"{ref}: 清单里的定位串在活页面上一个都找不到")
    return problems, skipped


# ---------------------------------------------------------------------------
# ③ AST 白名单: 渲染产物 (⑥) 只准碰这些东西
# ---------------------------------------------------------------------------
#
# 这份清单必须和 runtime/__init__.py 一起改: 加了一个原语却没登记, 渲染产物会在
# ③ 被拦下 —— 这是故意的, 白名单的意义就是「两边都认了才能用」。

RUNTIME_PRIMITIVES: Tuple[str, ...] = (
    # actions (带失败上下文, 替代裸 selenium 调用)
    "click", "double_click", "fill", "clear", "select_option", "check", "uncheck",
    "press_key", "get_text", "get_value", "goto", "reload", "back",
    # 表格 / 分页 / 弹窗 (standalone 只靠 loc() 也能做)
    "paginate", "set_page_size", "sort", "confirm_dialog", "cancel_dialog",
    # asserts
    "assert_visible", "assert_hidden", "assert_inactive", "assert_text", "assert_value",
    "assert_count", "assert_attr", "assert_field_error", "assert_masked",
    "assert_autocomplete", "assert_no_error_toast", "assert_no_alert", "assert_loading",
    "assert_page_ready_within", "assert_row_count", "assert_row_count_lte",
    "assert_row_count_gte", "assert_count_matches_rows", "assert_rows_match",
    "assert_sorted", "assert_no_duplicate_rows", "assert_empty_state",
    "assert_selection_count", "assert_selection_count_gte", "assert_feedback",
    "assert_persisted", "assert_not_persisted", "assert_login_error", "assert_logged_in",
    "assert_locked", "assert_submit_failed",
    # waits (替代 time.sleep)
    "wait_visible", "wait_clickable", "wait_text", "wait_gone",
)

# runtime 的入口函数 (不是计划里的动作, 但渲染产物要靠它们接上线):
#   loc("e12") / use_locators(path) / page.bind(driver) / page.unbind() / screenshot()
RUNTIME_HELPERS: Tuple[str, ...] = (
    "loc", "use_locators", "bind", "unbind", "screenshot",
)
# runtime 门面的别名: 产物里写 page.fill(...) 和 runtime.fill(...) 都算走了原语
RUNTIME_RECEIVERS: Tuple[str, ...] = ("page", "runtime", "rt")

# 自由函数调用: 内置函数够渲染器拼字符串/算数就行
_SAFE_BUILTINS = frozenset((
    "print", "len", "str", "int", "float", "bool", "range", "enumerate", "zip",
    "list", "dict", "tuple", "set", "sorted", "any", "all", "min", "max", "sum",
    "round", "abs", "repr", "isinstance", "getattr",
))

# 普通 Python 方法 (容器/字符串/pathlib): 两种模式都放行
_PY_METHODS = frozenset((
    "append", "extend", "insert", "pop", "get", "items", "keys", "values", "update",
    "setdefault", "copy", "join", "split", "splitlines", "strip", "lstrip", "rstrip",
    "replace", "lower", "upper", "startswith", "endswith", "format", "encode", "decode",
    "read_text", "write_text", "exists", "mkdir", "with_name", "with_suffix", "resolve",
    "isoformat", "strftime", "sleep", "dumps", "loads", "group", "search", "match",
    "findall", "sub", "fullmatch", "partition", "fromkeys", "index", "count", "sort",
    "reverse", "close",
))

# 白名单里的构造器: `Path(__file__)` 是产物找 locators.json 的唯一办法
_SAFE_CTORS = frozenset(("Path",))

# pytest 的调用面: 渲染产物是测试文件, 它当然要能断言/参数化
_PYTEST_METHODS = frozenset((
    "raises", "fail", "skip", "xfail", "warns", "approx", "mark", "fixture",
    "parametrize", "importorskip",
))

# selenium 的方法: 只在 po 模式放行 (PO 内部本来就是这些;
# standalone 必须走 runtime 原语, 否则 ③ 就失去意义)
_SELENIUM_METHODS = frozenset((
    "find_element", "find_elements", "get", "send_keys", "text", "get_attribute",
    "get_property", "get_dom_attribute", "is_displayed", "is_enabled", "is_selected",
    "submit", "current_url", "title", "page_source", "refresh", "back", "forward",
    "quit", "close", "switch_to", "execute_script", "execute_async_script",
    "implicitly_wait", "until", "add_argument", "maximize_window", "set_window_size",
    "save_screenshot", "value_of_css_property", "tag_name", "location",
    "visibility_of_element_located", "presence_of_element_located", "element_to_be_clickable",
    "text_to_be_present_in_element", "invisibility_of_element_located", "staleness_of",
))

# 打死不许出现的东西 (白名单之外再补一层黑名单: 白名单漏一个就是洞)
BANNED_CALLS = frozenset(("eval", "exec", "compile", "open", "__import__", "input",
                          "globals", "locals", "vars", "breakpoint"))
BANNED_METHODS = frozenset(("system", "popen", "remove", "unlink", "rmdir", "rmtree",
                            "kill", "fork", "spawn", "setattr", "delattr"))
# 属性调用单独一份: `page.open()` 是 PO 的标准入口, 不能因为裸 open() 被禁就跟着禁
BANNED_ATTRS = frozenset(("eval", "exec", "compile", "__import__")) | BANNED_METHODS

# import 前缀白名单: 渲染产物是测试代码, 不是脚本
# "ai_kit" 是为了 `from ai_kit import runtime`; 真正该导的只有 runtime 子模块
ALLOWED_IMPORT_PREFIXES: Tuple[str, ...] = (
    "selenium", "pytest", "ai_kit", "pages", "json", "os", "re", "time",
    "datetime", "pathlib", "typing", "dataclasses", "csv", "math", "string",
    "collections", "contextlib", "functools", "itertools", "io", "tempfile", "uuid",
)


@dataclass(frozen=True)
class LintProblem:
    """③ 的一条问题; 带行号, 不然改起来靠猜"""

    line: int
    message: str

    def __str__(self) -> str:
        return f"第 {self.line} 行: {self.message}"


def _imported_modules(node: ast.AST) -> List[str]:
    """import 语句里出现的模块名; 相对 import (level > 0) 交给调用方放行"""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:
            return []                  # from .pages import X: 同一个包, 不算越界
        return [node.module or ""]
    return []


def _own_functions(tree: ast.AST) -> set:
    """本文件定义的函数/方法: 渲染器自己的辅助函数不该被当成越界调用"""
    return {node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _call_problem(node: ast.Call, mode: str, primitives: set, py_names: set,
                  po_names: set, own_names: set) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
        if name in BANNED_CALLS:
            return f"不许调用 {name}()"
        if name in own_names or name in primitives or name in _SAFE_BUILTINS \
                or name in _SAFE_CTORS:
            return ""
        if not mode:
            return ""
        return (f"{name}() 不在白名单: {mode} 模式只准用 runtime 原语 / "
                f"内置函数 / 本文件定义的辅助函数")
    if not isinstance(func, ast.Attribute):
        return ""
    name = func.attr
    if name in BANNED_ATTRS:
        return f"不许调用 .{name}()"
    if not mode:
        return ""
    if name in py_names or name in own_names:
        return ""
    receiver = func.value.id if isinstance(func.value, ast.Name) else ""
    if mode == "standalone":
        if name in primitives:
            if receiver in RUNTIME_RECEIVERS:
                return ""
            return (f".{name}() 必须走 runtime.{name}(...) / page.{name}(...): "
                    f"standalone 模式只准用 runtime 原语, 不直接碰 selenium 对象")
        return (f".{name}() 越界: standalone 模式只准用 runtime 原语 "
                f"(runtime.{name}(...) 或先在 runtime 里登记这个原语)")
    if name in primitives or name in _SELENIUM_METHODS or name in _PYTEST_METHODS:
        return ""
    if name in po_names or receiver == "self":
        return ""
    return f".{name}() 不是 pages/ 里的 PO 方法 (po 模式只收 PO 方法 + runtime 原语)"


def lint_source(source: str, *, mode: str = "", po_methods: Iterable[str] = (),
                allow_calls: Iterable[str] = ()) -> Tuple[LintProblem, ...]:
    """③ AST 白名单检查: 渲染出来的测试代码能不能进仓库

    mode="po"          调用的方法必须在 pages/ 里存在 (po_methods / po_methods_from_dir)
    mode="standalone"  动作只能走 runtime 原语, 不直接碰 selenium 对象
    mode=""            只查危险调用与 import (还没定模式时的最弱检查)
    """
    text = str(source or "")
    if not text.strip():
        return (LintProblem(1, "源码是空的"),)
    if mode not in ("", "po", "standalone"):
        return (LintProblem(1, f"未知渲染模式 {mode!r}: 只认 po / standalone"),)
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return (LintProblem(exc.lineno or 1, f"源码解析失败: {exc.msg}"),)
    primitives = set(RUNTIME_PRIMITIVES) | set(RUNTIME_HELPERS) \
        | {str(name) for name in allow_calls if name}
    po_names = {str(name) for name in po_methods if name}
    # pytest 的 fixture/mark 两边都要能写: 产物本身就是测试文件
    py_names = set(_PY_METHODS) | _PYTEST_METHODS
    own_names = _own_functions(tree)
    # 从 pages 导入的类本身要能实例化 (PO 模式的第一个动作就是 `XxxPage(driver)`)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pages"):
            primitives.update(alias.asname or alias.name for alias in node.names)
    problems: List[LintProblem] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for module in _imported_modules(node):
                if not module or module == "__future__":
                    continue
                if any(module == prefix or module.startswith(prefix + ".")
                       for prefix in ALLOWED_IMPORT_PREFIXES):
                    continue
                problems.append(LintProblem(
                    node.lineno,
                    f"不许 import {module}: 渲染产物只碰 selenium / runtime / pages / 标准库"))
        elif isinstance(node, ast.Call):
            problem = _call_problem(node, mode, primitives, py_names, po_names, own_names)
            if problem:
                problems.append(LintProblem(node.lineno, problem))
    return tuple(problems)


def po_methods_from_dir(directory: Any) -> Tuple[str, ...]:
    """扫 pages/ 下的 .py 收集方法名 (只读不执行: 被测项目的代码不该在这一步跑起来)

    po 模式的 ③ 就拿它当准入清单: 用例里出现 pages/ 没有的方法, 就是渲染器编了方法名。
    """
    root = Path(str(directory or ""))
    if not root.is_dir():
        return ()
    names = set()
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        names.update(_own_functions(tree))
    return tuple(sorted(names))


def _signature_of(node: ast.AST) -> str:
    """函数定义 -> name(a, b=...) (去掉 self/cls; 默认值只写 =..., 不求值)"""
    args = list(getattr(node.args, "posonlyargs", ())) + list(node.args.args)
    defaults = list(node.args.defaults)
    pad = len(args) - len(defaults)
    rows: List[str] = []
    for index, arg in enumerate(args):
        if index == 0 and arg.arg in ("self", "cls"):
            continue
        rows.append(arg.arg if index < pad else f"{arg.arg}=...")
    if node.args.vararg:
        rows.append(f"*{node.args.vararg.arg}")
    if node.args.kwarg:
        rows.append(f"**{node.args.kwarg.arg}")
    return f"{node.name}({', '.join(rows)})"


def po_signatures_from_dir(directory: Any) -> Tuple[str, ...]:
    """扫 pages/ 下的 .py 收集「方法名(参数)」(只读 AST, 不 import 被测项目)

    卡片 5.10 要的是签名而不是光秃秃的方法名: 模型看到 search_by_name(name) 才能
    排对参数; 只给名字, 它就会去猜参数个数 —— 而 ③ 只能拦住不存在的方法名,
    拦不住参数传错。
    """
    root = Path(str(directory or ""))
    if not root.is_dir():
        return ()
    rows: List[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        rows.extend(_signature_of(node) for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not node.name.startswith("_"))
    return tuple(sorted(dict.fromkeys(rows)))


# ---------------------------------------------------------------------------
# ④ 去重: 标题归一化 + 步骤序列 Jaccard
# ---------------------------------------------------------------------------

DUP_THRESHOLD = 0.8

_TITLE_NOISE = re.compile(
    r"[\s\-—_`~!@#$%^&*()+=|\\:;\"'<>,.?/\[\]{}、，。；：！？（）【】《》“”‘’·]+")


def normalize_title(title: Any) -> str:
    """标题归一化: 去标点/空白 + 全小写。「患者列表 - 姓名查询。」和
    「患者列表姓名查询」是同一条用例的两个写法。"""
    return _TITLE_NOISE.sub("", str(title or "")).lower()


def step_tokens(case: PlanCase) -> Tuple[str, ...]:
    """一条用例的步骤序列特征 (动作 + 引用 + 取值), 清洗步骤也算进去"""
    tokens = []
    for step in (*case.steps, *case.cleanup):
        value = "" if step.value is None else str(step.value)
        tokens.append(f"{step.action}@{step.eid}={value}")
    return tuple(tokens)


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    first, second = set(left), set(right)
    if not first and not second:
        return 1.0
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


@dataclass(frozen=True)
class Duplicate:
    """④ 的判定结果: 留哪条、丢哪条、为什么

    带两条用例的标题: 只报 case_id 的话, 看报告的人无从判断「这两个东西真的是
    同一件事吗, 还是模板退化了」—— 而后者才是必须回修的。
    """

    keep: str
    drop: str
    similarity: float
    reason: str
    keep_title: str = ""
    drop_title: str = ""

    def __str__(self) -> str:
        keep = f"{self.keep}《{_clip(self.keep_title)}》"
        drop = f"{self.drop}《{_clip(self.drop_title)}》"
        return (f"{drop} 与 {keep} 近重复 ({self.reason}, "
                f"相似度 {self.similarity:.2f}), 已合并")


def _clip(text: Any, limit: int = 26) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[:limit] + "…"


def _priority_rank(priority: str) -> int:
    return PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)


def dedupe_cases(cases: Sequence[PlanCase], threshold: float = DUP_THRESHOLD
                 ) -> Tuple[Tuple[PlanCase, ...], Tuple[Duplicate, ...]]:
    """④ 标题归一化相同, 或步骤序列 Jaccard > threshold -> 判近重复

    保留优先级高的 (同优先级留先出现的): 两条几乎一样时, 那条 P0 才是要看的。
    返回 (保留的用例, 删掉的判定), 删了哪条必须能说出来 —— 静默去重比不去重更坏。
    """
    kept: List[PlanCase] = []
    found: List[Duplicate] = []
    for case in cases:
        title = normalize_title(case.title)
        best: Optional[PlanCase] = None
        score = 0.0
        reason = ""
        for other in kept:
            if title and title == normalize_title(other.title):
                best, score, reason = other, 1.0, "标题归一化后相同"
                break
            value = jaccard(step_tokens(case), step_tokens(other))
            if value > threshold and value > score:
                best, score, reason = other, value, f"步骤序列 Jaccard {value:.2f}"
        if best is None:
            kept.append(case)
            continue
        if _priority_rank(case.priority) < _priority_rank(best.priority):
            kept[kept.index(best)] = case
            found.append(Duplicate(case.case_id, best.case_id, score,
                                   reason + ", 留优先级更高的",
                                   keep_title=case.title, drop_title=best.title))
        else:
            found.append(Duplicate(best.case_id, case.case_id, score, reason,
                                   keep_title=best.title, drop_title=case.title))
    return tuple(kept), tuple(found)


def find_duplicates(cases: Sequence[PlanCase],
                    threshold: float = DUP_THRESHOLD) -> Tuple[Duplicate, ...]:
    """只报告不合并 (想先看一眼再决定删哪条时用)"""
    return dedupe_cases(cases, threshold)[1]


# ---------------------------------------------------------------------------
# 汇总: 四层跑完 -> 带 needs_review 标记的计划
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VerifySummary:
    """四层各报了几条 (进 report, 也验证「哪一层真的跑了」)"""

    schema: int = 0
    live: int = 0
    ast: int = 0
    duplicates: int = 0
    review: int = 0

    def __str__(self) -> str:
        return (f"① schema {self.schema} / ② 活页面 {self.live} / "
                f"③ AST {self.ast} / ④ 合并 {self.duplicates} / "
                f"待确认 {self.review}")


def _as_sources(raw: Any, cases: Sequence[PlanCase]) -> Dict[str, str]:
    """渲染产物 -> case_id -> 源码。按序给 list 也收 (⑥ 的渲染器两种都可能返回)"""
    if isinstance(raw, Mapping):
        return {str(key): str(value) for key, value in raw.items() if value}
    if isinstance(raw, (list, tuple)):
        return {case.case_id: str(source) for case, source in zip(cases, raw) if source}
    return {}


def _render_sources(render: Callable[[TestPlan], Any], target: TestPlan
                    ) -> Tuple[Optional[Dict[str, str]], str, Dict[str, List[str]]]:
    """调渲染器 -> (sources, 失败说明, 渲染器拒收的理由)

    渲染爆了不该让校验也爆; 渲染器「拒收某条用例」(没有原语 / locators 里缺 eid /
    po 没映射) 也不能当没发生 —— 不把理由带上去, 这些用例会在报告里被当成「一切
    正常」, 而它们根本没有代码可跑。
    """
    def _reasons(raw: Any) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for item in getattr(raw, "problems", ()) or ():
            case_id = str(getattr(item, "case_id", "") or "")
            message = str(getattr(item, "message", "") or item)
            if case_id and message:
                out.setdefault(case_id, []).append(message)
        return out

    try:
        raw = render(target)
    except Exception as exc:            # noqa: BLE001 — 渲染器有 bug, 但校验要继续
        return None, f"渲染失败, ③ AST 没跑: {exc}", {}
    return _as_sources(raw, target.cases), "", _reasons(raw)


def verify(plan_: TestPlan, *, inventory: Any = None, driver: Any = None,
           mode: str = "", po_methods: Iterable[str] = (),
           sources: Any = None, render_problems: Any = None,
           dedupe: bool = True, threshold: float = DUP_THRESHOLD) -> TestPlan:
    """四层校验闭环的纯函数版: 原计划 -> 带问题/标记的新计划 (不改入参)

    sources: case_id -> 渲染出来的源码 (⑥ 给的); 不传就跳过 ③ 并说明。
    render_problems: case_id -> [渲染器拒收的理由]; 它们算 ③ 的问题 —— 没有产物的
    用例不能因为「没东西可查」就默认通过。
    inventory 不传只能跑 ①: 没有清单就无从解析引用, 拿空清单硬跑只会满屏假问题。
    """
    if not isinstance(plan_, TestPlan):
        raise TypeError(f"verify 需要 TestPlan, 拿到 {type(plan_).__name__}")
    inv = (inventory if isinstance(inventory, PageInventory)
           else from_inventory(inventory) if inventory is not None else None)
    diagnostics = dict(plan_.diagnostics)
    by_case: Dict[str, List[str]] = {}

    def note(case_id: str, message: str) -> None:
        bucket = by_case.setdefault(case_id, [])
        if message not in bucket:
            bucket.append(message)

    # ① schema: 不需要页面, 先看结构
    schema_lines = list(validate_schema(plan_))
    diagnostics["schema"] = schema_lines
    for case in plan_.cases:
        for message in case_schema_problems(case):
            note(case.case_id, message)

    # ② 引用校验 (静态) + 活页面 find_element
    live_lines: List[str] = []
    live_skipped: List[str] = []
    if inv is None:
        live_skipped = ["没有元素清单, ② 引用/活页面校验没跑"]
    else:
        for case in plan_.cases:
            for message in validate_case(inv, case):
                live_lines.append(f"{case.case_id}: {message}")
                note(case.case_id, message)
        refs = [step.eid for case in plan_.cases
                for step in (*case.steps, *case.cleanup) if step.eid]
        found, live_skipped = check_live(inv, refs, driver)
        for line in found:
            ref = line.split(":", 1)[0].strip()
            for case in plan_.cases:
                if any(step.eid == ref for step in (*case.steps, *case.cleanup)):
                    note(case.case_id, line.split(": ", 1)[-1])
                    break
        live_lines.extend(found)
    diagnostics["live"] = live_lines
    diagnostics["live_skipped"] = live_skipped

    # ③ AST 白名单: 只看渲染产物 (+ 渲染器自己说「这条生成不了」的理由)
    ast_lines: List[str] = []
    for case_id, messages in (render_problems or {}).items():
        for message in messages or ():
            ast_lines.append(f"{case_id}: {message}")
            note(str(case_id), str(message))
    rendered = _as_sources(sources, plan_.cases) if sources else {}
    if rendered:
        for case in plan_.cases:
            source = rendered.get(case.case_id)
            if not source:
                continue
            for problem in lint_source(source, mode=mode, po_methods=po_methods):
                ast_lines.append(f"{case.case_id}: {problem}")
                note(case.case_id, str(problem))
    else:
        diagnostics["ast_skipped"] = "没有渲染产物 (sources), ③ AST 这一层没跑"
    diagnostics["ast"] = ast_lines

    # ④ 去重: 合并是修复, 不算失败
    cases, duplicates = dedupe_cases(plan_.cases) if dedupe else (plan_.cases, ())
    diagnostics["duplicates"] = [str(dup) for dup in duplicates]

    kept_ids = {case.case_id for case in cases}
    final: List[PlanCase] = []
    for case in cases:
        messages = by_case.get(case.case_id) or []
        # 两个方向都要写: 上一轮标记过的用例这一轮过了, 就得摘掉标记;
        # 只置位不清位, 重生成修好的用例会永远留在待确认队列里。
        final.append(replace(case, needs_review=bool(messages)))
    diagnostics["needs_review"] = {case_id: messages
                                   for case_id, messages in by_case.items()
                                   if case_id in kept_ids}

    problems = list(diagnostics.get("problems") or ())
    problems.extend(schema_lines)
    problems.extend(live_lines)
    problems.extend(ast_lines)
    diagnostics["problems"] = list(dict.fromkeys(problems))
    diagnostics["verify"] = str(VerifySummary(
        schema=len(schema_lines), live=len(live_lines), ast=len(ast_lines),
        duplicates=len(duplicates), review=len(diagnostics["needs_review"])))
    return replace(plan_, cases=tuple(final), diagnostics=diagnostics)


# ---------------------------------------------------------------------------
# ⑤ 闭环: 校验不过 -> 把问题回填 prompt 重生成 (最多 max_retries 次)
# ---------------------------------------------------------------------------

def _merge_rewrites(inv: Optional[PageInventory], cases: Sequence[PlanCase],
                    items: Sequence[Any], problems: List[str]
                    ) -> Tuple[List[PlanCase], int, int]:
    """LLM/人改过的步骤 -> 回填。逐条过 ① schema + ② 引用校验

    只改 steps/cleanup: case_id/title/期望/优先级是骨架, 改了等于换了条用例,
    后面按 case_id 做的缓存和报告全部对不上。
    """
    position_of = {case.case_id: index for index, case in enumerate(cases)}
    merged = list(cases)
    accepted = rejected = 0
    for item in items:
        if not isinstance(item, Mapping):
            rejected += 1
            continue
        case_id = str(item.get("case_id") or "").strip()
        position = position_of.get(case_id)
        if position is None:
            rejected += 1
            problems.append(f"LLM 返回了计划里没有的 {case_id!r}")
            continue
        steps, step_problems = _steps_from(item.get("steps"))
        cleanup, cleanup_problems = _steps_from(item.get("cleanup"), "cleanup")
        if "steps" not in item or steps is None or cleanup_problems:
            rejected += 1
            detail = list(step_problems) + list(cleanup_problems)
            problems.append(f"{case_id} (LLM): 步骤结构不对, 保留确定性步骤"
                            + (f" ({'; '.join(detail)})" if detail else ""))
            continue
        candidate = replace(merged[position], steps=steps, cleanup=cleanup,
                            needs_review=False)
        bad = list(case_schema_problems(candidate))
        if inv is not None:
            bad.extend(validate_case(inv, candidate))
        if bad:
            rejected += 1
            problems.extend(f"{case_id} (LLM): {message}"
                            for message in dict.fromkeys(bad))
            continue
        merged[position] = candidate
        accepted += 1
    return merged, accepted, rejected


def plan_verified(inventory: Any, *, ask: Optional[Callable[[str], Any]] = None,
                  max_retries: int = 2, mode: str = "", po_methods: Iterable[str] = (),
                  po_signatures: Iterable[str] = (),
                  render: Optional[Callable[[TestPlan], Mapping[str, str]]] = None,
                  driver: Any = None, dedupe: bool = True,
                  threshold: float = DUP_THRESHOLD, **kwargs: Any) -> TestPlan:
    """确定性计划 -> 四层校验 -> 问题回填重生成 (最多 max_retries 次) -> needs_review

    render 传了才有 ③: 计划还不是代码, 没渲染就没 AST 可查。
    ask 传了才重生成: 纯规则路径的失败只能靠人, 拿同一套模板重试只会得到同一个错。
    重生成只动没过的用例, 已经过的不会因为隔壁一条失败而被重写 (回归 diff 会炸)。
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    options: Dict[str, Any] = dict(inventory=inv, driver=driver, mode=mode,
                                   po_methods=po_methods, dedupe=dedupe,
                                   threshold=threshold)
    base = plan(inv, **kwargs)
    sources, render_error, render_problems = (
        _render_sources(render, base) if render else (None, "", {}))
    current = verify(base, sources=sources, render_problems=render_problems, **options)
    attempts: List[str] = [f"渲染失败: {render_error}"] if render_error else []
    for attempt in range(1, max(0, int(max_retries)) + 1):
        pending = dict(current.diagnostics.get("needs_review") or {})
        if not pending:
            break
        if ask is None:
            attempts.append(f"第 {attempt} 轮: 没有 ask, 不重生成")
            break
        targets = [case for case in current.cases if case.case_id in pending]
        try:
            answer = ask(_llm_prompt(inv, current, targets, errors=pending,
                                     po_methods=po_methods,
                                     po_signatures=po_signatures))
        except BudgetExceeded:
            # 预算用完: 直接停, 不把它降级成「这一轮 LLM 抽风, 换下一轮」
            raise
        except Exception as exc:                # noqa: BLE001 — LLM 挂了不该让闭环崩
            attempts.append(f"第 {attempt} 轮: 调用失败 ({exc})")
            break
        items = answer.get("cases") if isinstance(answer, Mapping) else answer
        if not isinstance(items, (list, tuple)) or not items:
            attempts.append(f"第 {attempt} 轮: 没有可用的返回")
            break
        problems: List[str] = []
        merged, accepted, rejected = _merge_rewrites(inv, current.cases, items, problems)
        attempts.append(f"第 {attempt} 轮: 接受 {accepted} 条, 拒绝 {rejected} 条")
        if accepted == 0:
            break
        diagnostics = dict(current.diagnostics)
        diagnostics["problems"] = list(diagnostics.get("problems") or ()) + problems
        updated = replace(current, cases=tuple(merged), diagnostics=diagnostics)
        sources, render_error, render_problems = (
            _render_sources(render, updated) if render else (None, "", {}))
        if render_error:
            attempts.append(render_error)
        current = verify(updated, sources=sources, render_problems=render_problems,
                         **options)
    diagnostics = dict(current.diagnostics)
    tail = f"重生成 {len(attempts)} 轮, 待人工确认 {len(current.review)} 条"
    diagnostics["verify"] = ("; ".join(attempts) + " -> " + tail) if attempts else tail
    return replace(current, diagnostics=diagnostics)
