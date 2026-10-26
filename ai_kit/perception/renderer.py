# -*- coding: utf-8 -*-
"""
渲染器 — 计划 (⑤) -> 可跑的 pytest 源码, 双模式

    po 模式          from pages.user_management_page import UserManagementPage
                     每一步映射到 PO 方法 (映射表由人给: 猜方法名就是造方法)

    standalone 模式  from ai_kit.runtime import loc, page
                     page.fill(loc("e12"), "' OR 1=1 --")
                     loc() 从随计划生成的 locators.json 查定位串

为什么要有 standalone: po 模式的前提是「有人写过这个项目的 Page Object」。面对一个
陌生站点, 唯一能跑的就是「计划 + 元素清单 + runtime 原语」—— 换网站只需要换 profile
和计划, 运行时代码一个字都不用改。

两条硬规矩 (都不是实现细节, 是这一层的存在意义):
1. **不猜**: po 模式里没映射的动作、standalone 里没有原语的动作, 一律记问题并拒
   绝生成代码 —— 生成一个编出来的方法名, 比不生成更坏 (它会让 ③ 之外的所有人以为
   这条用例有救)。
2. **AST 准入**: 每条产物在返回前都要过 planner.lint_source 的 ③ 白名单。过不了
   的用例不进 sources, 问题带 case_id 交给上层标 needs_review, 其余用例照常渲染。

locators.json 是「计划 + 元素清单」的运行时快照: eid -> 主定位策略 + 备选定位串。
它必须和计划一起落盘, 产物用 `Path(__file__).with_name("locators.json")` 找它 ——
所以二者必须在同一个目录。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ai_kit.perception.inventory import ElementSpec, PageInventory, from_inventory
from ai_kit.perception.planner import (
    ACTIONS,
    PAGE_REF,
    PlanCase,
    Step,
    TestPlan,
    lint_source,
    resolve_ref,
)

RENDERER_VERSION = 1
MODE_PO = "po"
MODE_STANDALONE = "standalone"
MODES: Tuple[str, ...] = (MODE_PO, MODE_STANDALONE)
LOCATORS_FILENAME = "locators.json"
DEFAULT_DRIVER_FIXTURE = "driver"

# 计划里有、但「不靠页面就能诚实实现」的动作。它们不是 bug, 是需要环境/后端的
# 准备动作或跨页面断言; 硬生成只会得到一条假装通过或必然报错的用例。
UNSUPPORTED_REASONS: Dict[str, str] = {
    "throttle": "需要 CDP 限速, 属环境准备而不是页面动作",
    "set_network": "需要 CDP 断网, 同上",
    "seed_rows": "需要后端的造数接口",
    "export": "需要下载目录与文件校验",
    "assert_consistent": "需要跨页面取同一条记录 (详情页 vs 列表页)",
    "assert_export_matches_list": "需要解析导出文件",
    "assert_chart_matches_source": "图表数值不在 DOM 里, 属于视觉/接口校验",
    "assert_chart_empty": "同上",
    "toggle_legend": "图例不是可独立定位的元素",
}


# ---------------------------------------------------------------------------
# 结果 / 问题 / PO 配置
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RenderProblem:
    """一条「这条用例渲染不了」的说明; case_id 为空的表示整份计划级问题"""

    case_id: str
    message: str

    def __str__(self) -> str:
        return f"{self.case_id}: {self.message}" if self.case_id else self.message


class RenderResult(dict):
    """case_id -> 单条用例的完整源码 (能直接当 ③ 的输入), 外加问题清单

    继承 dict 是为了 plan_verified(render=...) 直接用: 它只认 Mapping。
    """

    def __init__(self, sources: Optional[Mapping[str, str]] = None, *,
                 mode: str = MODE_STANDALONE,
                 problems: Sequence[RenderProblem] = (),
                 locators: Optional[Mapping[str, Any]] = None,
                 notes: Sequence[str] = ()) -> None:
        super().__init__({str(k): str(v) for k, v in (sources or {}).items() if v})
        self.mode = mode
        self.problems = tuple(problems)
        self.locators = dict(locators or {})
        self.notes = tuple(notes)

    @property
    def skipped(self) -> Tuple[str, ...]:
        """没渲染出代码的 case_id (按问题出现顺序去重)"""
        out: List[str] = []
        for problem in self.problems:
            if problem.case_id and problem.case_id not in out:
                out.append(problem.case_id)
        return tuple(out)

    def report(self) -> str:
        lines = [f"渲染 ({self.mode}): {len(self)} 条出代码 / {len(self.skipped)} 条没出"]
        if self.locators:
            total = len(self.locators.get("locators") or {})
            lines.append(f"  {LOCATORS_FILENAME}: {total} 个引用")
        for note in self.notes:
            lines.append(f"  说明: {note}")
        for problem in self.problems:
            lines.append(f"  - {problem}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Written:
    """落盘结果: 产物 + locators.json 是配套的, 必须一起拷走"""

    test_path: str
    locators_path: str
    result: RenderResult

    def __str__(self) -> str:
        return (f"{self.test_path} ({len(self.result)} 条) + "
                f"{self.locators_path or '无 locators'}")


@dataclass(frozen=True)
class PoPage:
    """po 模式的映射表: 一个动作 -> 一条方法调用 (相对 PO 实例)

    mapping 的值是表达式模板, 可用占位符:
        $eid / $eid_q     eid 原文 / 带引号形式
        $label / $label_q 元素可见文案 · 列名 · 行内按钮文案 (原文 / 带引号)
        $value            value 的 Python 字面量 (字符串自带引号)
        $ref / $case_id / $title
    例: {"goto": "open()", "fill": "search_by_name($value)",
         "click": "click_row_action($label_q)"}

    methods 是 pages/ 里真实存在的方法名 (planner.po_methods_from_dir); 传了它,
    AST 才能拦住「映射表里写了一个没人写过的方法」。不传就只查语法与黑名单 ——
    这时 report 里会明说准入变弱了。
    """

    module: str
    cls: str
    mapping: Mapping[str, str] = field(default_factory=dict)
    methods: Tuple[str, ...] = ()
    fixture: str = ""          # 给了就用 conftest 的 fixture, 否则自己 new 一个
    constructor_arg: str = DEFAULT_DRIVER_FIXTURE
    variable: str = "page"


# ---------------------------------------------------------------------------
# 字面量 / 定位串
# ---------------------------------------------------------------------------

def _q(text: Any) -> str:
    """Python 字符串字面量 (中文原样, 引号转义)"""
    return json.dumps(str(text), ensure_ascii=False)


def _literal(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _q(value)
    return repr(value)


def _xpath_text(label: Any) -> str:
    """xpath 里的字符串字面量: 两边引号都有的文本要用 concat() 拼"""
    text = str(label or "")
    if '"' not in text:
        return f'"{text}"'
    if "'" not in text:
        return f"'{text}'"
    parts = text.split('"')
    chunks: List[str] = []
    for index, part in enumerate(parts):
        if part:
            chunks.append(f'"{part}"')
        if index < len(parts) - 1:
            chunks.append("'\"'")
    return "concat(" + ", ".join(chunks) + ")"


def _record_pairs(record: Any) -> List[Tuple[str, str]]:
    """表格/弹窗记录 -> [(by, locator), ...] (兼容 locators 字典与 locator+alternatives)"""
    if isinstance(record, ElementSpec):
        return [(record.selector_by, record.selector)]
    if not isinstance(record, Mapping):
        return []
    pairs: List[Tuple[str, str]] = []
    locators = record.get("locators")
    bys = record.get("locator_by")
    if isinstance(locators, Mapping):
        for name, value in locators.items():
            if not value:
                continue
            by = bys.get(name) if isinstance(bys, Mapping) else ""
            pairs.append((str(by or ("xpath" if "xpath" in str(name) else "css selector")),
                          str(value)))
    if pairs:
        return pairs
    locator = record.get("locator")
    if isinstance(locator, Mapping) and locator.get("value"):
        pairs.append((str(locator.get("by") or "css selector"), str(locator["value"])))
    for alternative in record.get("alternatives") or ():
        if isinstance(alternative, Mapping) and alternative.get("value"):
            pairs.append((str(alternative.get("by") or "css selector"),
                          str(alternative["value"])))
    return pairs


def _element_entry(element: ElementSpec) -> Dict[str, Any]:
    locators = [(element.selector_by, element.selector)]
    locators += [(by or "css selector", value)
                 for by, value in ((element.locator_by.get(name) or name, value)
                                   for name, value in element.locators.items())
                 if value and (by, value) not in locators]
    first = locators[0]
    return {
        "kind": element.kind or "element",
        "label": element.text or element.name or element.eid,
        "by": first[0],
        "value": first[1],
        "alternatives": [[by, value] for by, value in locators[1:]],
    }


def _table_anchor(eid: str, pairs: Sequence[Tuple[str, str]]) -> str:
    """表格子引用的锚点: 有 xpath 就用它, 否则退回采集器打的 data-ai-eid 标记"""
    for by, value in pairs:
        if by == "xpath":
            return f"({value})"
    return f"//*[@data-ai-eid={_xpath_text(eid)}]"


def build_locators(inventory: Any, *, page: str = "") -> Dict[str, Any]:
    """元素清单 -> locators.json 的内容 (eid -> 定位串 + 备选 + 所属表格)

    standalone 产物只认 eid; 这份文件就是 eid 与页面之间的地址簿。列标题 / 行内
    按钮 / 图表在清单里没有独立定位串, 这里按「表格锚点 + 文案」派生出来。
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    entries: Dict[str, Dict[str, Any]] = {}
    for element in inv.elements:
        entries[element.eid] = _element_entry(element)
    for table in inv.tables:
        eid = str(table.get("eid") or "")
        if not eid:
            continue
        pairs = _record_pairs(table)
        anchor = _table_anchor(eid, pairs)
        primary = pairs[0] if pairs else ("css selector", f'[data-ai-eid="{eid}"]')
        entries[eid] = {
            "kind": "table",
            "label": str(table.get("text") or eid)[:60],
            "by": primary[0],
            "value": primary[1],
            "alternatives": [[by, value] for by, value in pairs[1:]],
        }
        for column in table.get("columns") or ():
            name = str(column)
            entries[f"{eid}.column:{name}"] = {
                "kind": "column", "label": name, "table": eid,
                "by": "xpath",
                "value": f"{anchor}//th[normalize-space()={_xpath_text(name)}]",
            }
        for action in table.get("row_actions") or ():
            label = str(action.get("text") or "")
            if not label:
                continue
            text = f"normalize-space()={_xpath_text(label)}"
            entries[f"{eid}.row:{label}"] = {
                "kind": "row-action", "label": label, "table": eid,
                "by": "xpath",
                "value": (f"({anchor}//button[{text}] | {anchor}//a[{text}]"
                          f" | {anchor}//*[@role=\"button\"][{text}])[1]"),
            }
    for dialog in inv.dialogs:
        eid = str(dialog.get("eid") or "")
        selector = str(dialog.get("selector") or "")
        if not eid or not selector:
            continue
        entries[eid] = {"kind": "dialog",
                        "label": str(dialog.get("text") or eid)[:60],
                        "by": "css selector", "value": selector}
    entries[PAGE_REF] = {"kind": "page", "label": inv.title or "整页",
                         "by": "tag name", "value": "html"}
    return {
        "version": 1,
        "renderer": RENDERER_VERSION,
        "url": inv.url,
        "title": inv.title,
        "page": page or inv.page_name or "",
        "fingerprint": inv.fingerprint,
        "locators": {key: entries[key] for key in sorted(entries)},
    }


def _entries(doc: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    inner = doc.get("locators") if isinstance(doc.get("locators"), Mapping) else doc
    return {str(key): dict(value) for key, value in inner.items()}


# ---------------------------------------------------------------------------
# 代码模板
# ---------------------------------------------------------------------------

STANDALONE_HEADER = Template('''# -*- coding: utf-8 -*-
"""$title — standalone 渲染产物 (renderer 生成, eid 对应 $locators)

重新渲染会覆盖本文件。要改行为, 改计划或 ai_kit/runtime 的原语。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_kit.runtime import loc, page, use_locators

use_locators(Path(__file__).with_name("$locators"))


@pytest.fixture(autouse=True)
def _ai_kit_page($driver_fixture):
    page.bind($driver_fixture)
    try:
        yield
    finally:
        page.unbind()
''')

PO_HEADER = Template('''# -*- coding: utf-8 -*-
"""$title — po 渲染产物 (renderer 生成): 每步只调 $cls 的公开方法

重新渲染会覆盖本文件。动作 -> PO 方法的映射在 PoPage.mapping 里, 不在这里猜。
"""
from __future__ import annotations

import pytest

from $module import $cls
''')


def _slug(text: Any) -> str:
    raw = str(text or "")
    out = "".join(char if char.isascii() and (char.isalnum() or char == "_") else "_"
                  for char in raw.lower())
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or "case"


def _test_name(case: PlanCase) -> str:
    return f"test_{_slug(case.case_id)}"


def _docstring(case: PlanCase, indent: str = "    ") -> List[str]:
    lines = [f'{indent}"""{case.case_id} 《{case.title}》']
    lines.append("")
    lines.append(f"{indent}{case.cid or case.case_id} · {case.priority}"
                 + (" · 待人工确认" if case.needs_review else ""))
    if case.expect:
        lines.append(f"{indent}期望: {'; '.join(case.expect)}")
    if case.preconditions:
        lines.append(f"{indent}前置: {'; '.join(case.preconditions)}")
    lines.append(f'{indent}"""')
    return lines


def _label_of(inventory: Optional[PageInventory], eid: str) -> str:
    if not inventory or not eid:
        return ""
    ref = resolve_ref(inventory, eid)
    if ref is None:
        return ""
    if ref.kind == "row-action":
        return ref.text
    if ref.kind == "column":
        return ref.column
    element = inventory.element(ref.eid) if ref.eid else None
    return (element.text or element.name) if element is not None else (ref.text or "")


# ---------------------------------------------------------------------------
# standalone: 动作 -> page 原语
# ---------------------------------------------------------------------------

def _target(step: Step) -> str:
    return f"loc({_q(step.eid)})" if step.eid else "None"


def _table_target(step: Step) -> str:
    """表格类断言允许不带 eid (整页第一张表)"""
    return f"loc({_q(step.eid)})" if step.eid else "None"


def _standalone_line(step: Step) -> Tuple[str, str]:
    """一步 -> (代码, 问题)。问题非空 = 这一步渲染不了, 不能假装能"""
    action = step.action
    if action not in ACTIONS:
        return "", f"未知动作 {action!r}, 动作词表里没有"
    if action in UNSUPPORTED_REASONS:
        return "", f"{action} 渲染不了: {UNSUPPORTED_REASONS[action]}"
    value = _literal(step.value)
    target = _target(step)
    table = _table_target(step)
    if action == "goto":
        return f"page.goto({value})", ""
    if action == "reload":
        return "page.reload()", ""
    if action == "back":
        return "page.back()", ""
    if action == "fill":
        return f"page.fill({target}, {value})", ""
    if action == "clear":
        return f"page.clear({target})", ""
    if action == "select":
        return f"page.select_option({target}, {value})", ""
    if action == "check":
        return f"page.check({target})", ""
    if action == "uncheck":
        return f"page.uncheck({target})", ""
    if action == "click":
        return f"page.click({target})", ""
    if action == "double_click":
        return f"page.double_click({target})", ""
    if action == "press":
        return f"page.press_key({target}, {value})", ""
    if action == "paginate":
        return f"page.paginate({table}, {value})", ""
    if action == "set_page_size":
        return f"page.set_page_size({table}, {value})", ""
    if action == "sort":
        return f"page.sort({target}, {value})", ""
    if action == "confirm_dialog":
        return (f"page.confirm_dialog({value})" if step.value is not None
                else "page.confirm_dialog()", "")
    if action == "cancel_dialog":
        return (f"page.cancel_dialog({value})" if step.value is not None
                else "page.cancel_dialog()", "")
    if action in ("assert_visible", "assert_inactive", "wait_visible", "wait_clickable"):
        return f"page.{action}({target})", ""
    if action in ("assert_text", "assert_value", "assert_autocomplete"):
        return f"page.{action}({target}, {value})", ""
    if action == "assert_field_error":
        return (f"page.assert_field_error({target}, {value})" if step.value is not None
                else f"page.assert_field_error({target})", "")
    if action == "assert_masked":
        return (f"page.assert_masked({target})" if step.eid
                else "page.assert_masked()", "")
    if action in ("assert_no_error_toast", "assert_no_alert", "assert_loading",
                  "assert_feedback", "assert_login_error", "assert_logged_in",
                  "assert_submit_failed"):
        return f"page.{action}()", ""
    if action == "assert_page_ready_within":
        return f"page.assert_page_ready_within({value})", ""
    if action in ("assert_persisted", "assert_not_persisted"):
        return f"page.{action}({value})", ""
    if action == "assert_locked":
        return (f"page.assert_locked({value})" if step.value is not None
                else "page.assert_locked()", "")
    if action in ("assert_count_matches_rows", "assert_no_duplicate_rows",
                  "assert_empty_state"):
        return f"page.{action}({table})", ""
    if action in ("assert_row_count", "assert_row_count_lte", "assert_row_count_gte",
                  "assert_selection_count", "assert_selection_count_gte"):
        return f"page.{action}({table}, {value})", ""
    if action == "assert_rows_match":
        return f"page.assert_rows_match({table}, {value})", ""
    if action == "assert_sorted":
        return f"page.assert_sorted({target}, {value})", ""
    if action == "assert_count":
        return f"page.assert_count({target}, {value})", ""
    if action == "assert_attr":
        return "", "assert_attr 需要属性名, 计划里只有 value 一个槽位"
    return "", f"{action} 还没有对应的 runtime 原语"


def _standalone_block(case: PlanCase, *, driver_fixture: str) -> Tuple[str, List[str]]:
    problems: List[str] = []
    body: List[str] = []
    for step in case.steps:
        line, problem = _standalone_line(step)
        if problem:
            problems.append(f"步骤 {step.action}: {problem}")
            continue
        body.append(f"    {line}")
    cleanup: List[str] = []
    for step in case.cleanup:
        line, problem = _standalone_line(step)
        if problem:
            problems.append(f"清理步骤 {step.action}: {problem}")
            continue
        cleanup.append(f"        {line}")
    if problems:
        return "", problems

    lines = [f"def {_test_name(case)}({driver_fixture}):"]
    lines.extend(_docstring(case))
    if cleanup:
        lines.append("    try:")
        lines.extend(f"    {line}" for line in body or ["        pass"])
        lines.append("    finally:          # 清理步骤一定要跑, 否则脏数据留在页面上")
        lines.extend(cleanup)
    else:
        lines.extend(body or ["    pass"])
    return "\n".join(lines) + "\n", []


# ---------------------------------------------------------------------------
# po: 动作 -> PO 方法 (映射表由人给)
# ---------------------------------------------------------------------------

def _po_placeholders(inventory: Optional[PageInventory], case: PlanCase,
                     step: Step) -> Dict[str, str]:
    label = _label_of(inventory, step.eid)
    return {
        "eid": step.eid,
        "eid_q": _q(step.eid),
        "ref": step.eid,
        "label": label,
        "label_q": _q(label),
        "value": _literal(step.value),
        "case_id": case.case_id,
        "title": case.title,
    }


def _po_block(case: PlanCase, po: PoPage,
              inventory: Optional[PageInventory]) -> Tuple[str, List[str]]:
    problems: List[str] = []
    body: List[str] = []
    for step in list(case.steps) + list(case.cleanup):
        template = po.mapping.get(step.action)
        if template is None:
            problems.append(f"步骤 {step.action}: 没有映射到 {po.cls} 的方法 "
                            f"(po 模式不猜方法名)")
            continue
        try:
            expression = Template(str(template)).substitute(
                _po_placeholders(inventory, case, step))
        except (KeyError, ValueError) as exc:
            problems.append(f"步骤 {step.action}: 映射模板 {template!r} 用错占位符 ({exc})")
            continue
        body.append(f"    {po.variable}.{expression}")
    if problems:
        return "", problems

    signature = po.fixture or po.constructor_arg
    lines = [f"def {_test_name(case)}({signature}):"]
    lines.extend(_docstring(case))
    if po.fixture:
        lines.append(f"    {po.variable} = {po.fixture}")
    else:
        lines.append(f"    {po.variable} = {po.cls}({po.constructor_arg})")
    lines.extend(body or ["    pass"])
    return "\n".join(lines) + "\n", []


# ---------------------------------------------------------------------------
# 组装 / 准入
# ---------------------------------------------------------------------------

def _missing_refs(case: PlanCase, entries: Mapping[str, Any]) -> List[str]:
    """计划引用了、locators 里却没有的 eid (生成出来必然找不到元素)

    这一层本该由 ② 拦住; 但渲染器可以被单独调用 (不经 plan_verified), 所以要自己
    查一遍 —— 生成一条注定跑挂的用例, 比不生成更费人的时间。
    """
    missing: List[str] = []
    for step in (*case.steps, *case.cleanup):
        eid = step.eid
        if not eid or eid == PAGE_REF or eid in entries:
            continue
        if eid not in missing:
            missing.append(eid)
    return missing


@dataclass
class _Run:
    blocks: Dict[str, str]
    problems: List[RenderProblem]
    locators: Dict[str, Any]
    notes: List[str]


def _run(target: TestPlan, *, inventory: Optional[PageInventory], mode: str,
         po: Optional[PoPage], locators: Optional[Mapping[str, Any]],
         driver_fixture: str) -> _Run:
    """逐条拼出用例体 (不含模块头); ③ 的检定在 render 里对完整模块做

    不在这里 lint 的原因: po 模式的类名、standalone 的 fixture 与 use_locators 都在
    模块头里 —— 拿半截源码去查白名单, 会把每一个合法用例都判成「调了没登记的东西」。
    """
    problems: List[RenderProblem] = []
    notes: List[str] = []
    blocks: Dict[str, str] = {}

    if mode == MODE_STANDALONE:
        if inventory is None:
            message = ("没有元素清单: 生成不了 locators.json, standalone 的 loc() "
                       "无从查起")
            problems.extend(RenderProblem(case.case_id, message) for case in target.cases)
            return _Run({}, problems, {}, notes)
        doc = dict(locators) if locators is not None else build_locators(
            inventory, page=target.page)
        if not doc.get("locators"):
            notes.append("locators 是空的: 这份清单没有任何可定位的引用")
    else:
        doc = dict(locators or {})
        if po is None:
            problems.extend(RenderProblem(case.case_id,
                                          "po 模式需要一个 PoPage (module/cls/mapping)")
                            for case in target.cases)
            return _Run({}, problems, doc, notes)
        if not po.mapping:
            problems.extend(RenderProblem(case.case_id, "PoPage.mapping 是空的, 无法映射任何动作")
                            for case in target.cases)
            return _Run({}, problems, doc, notes)

    methods = tuple(po.methods if po is not None else ())
    if mode == MODE_PO and not methods:
        notes.append("没有传 pages/ 的方法清单 (po_methods), ③ 只查了语法与黑名单; "
                     "准入变弱了")

    entries = doc.get("locators") if isinstance(doc.get("locators"), Mapping) else {}
    for case in target.cases:
        if mode == MODE_STANDALONE:
            missing = _missing_refs(case, entries)
            if missing:
                problems.extend(RenderProblem(
                    case.case_id,
                    f"locators 里没有 {eid}: 清单与计划不配套, 生成的代码找不到元素")
                    for eid in missing)
                continue
            block, case_problems = _standalone_block(case, driver_fixture=driver_fixture)
        else:
            block, case_problems = _po_block(case, po, inventory)   # type: ignore[arg-type]
        if case_problems:
            problems.extend(RenderProblem(case.case_id, message)
                            for message in case_problems)
            continue
        blocks[case.case_id] = block
    return _Run(blocks, problems, doc, notes)


def _header(target: TestPlan, mode: str, po: Optional[PoPage],
            driver_fixture: str) -> str:
    title = target.page or (po.cls if po is not None else "页面")
    if mode == MODE_STANDALONE:
        return STANDALONE_HEADER.substitute(
            title=title, locators=LOCATORS_FILENAME, driver_fixture=driver_fixture)
    return PO_HEADER.substitute(title=title, module=po.module, cls=po.cls)  # type: ignore[union-attr]


def render(target: Any, *, inventory: Any = None, mode: str = MODE_STANDALONE,
           po: Optional[PoPage] = None, locators: Optional[Mapping[str, Any]] = None,
           driver_fixture: str = DEFAULT_DRIVER_FIXTURE,
           po_methods: Iterable[str] = ()) -> RenderResult:
    """计划 -> {case_id: 单条用例的完整源码}; 渲染不了的用例进 problems

    直接喂给 plan_verified: `plan_verified(inv, render=make_renderer(inv))`。
    """
    if not isinstance(target, TestPlan):
        raise TypeError(f"render 需要 TestPlan, 拿到 {type(target).__name__}")
    if mode not in MODES:
        raise ValueError(f"未知渲染模式 {mode!r}: 只认 {' / '.join(MODES)}")
    inv = (inventory if isinstance(inventory, PageInventory)
           else from_inventory(inventory) if inventory is not None else None)
    run = _run(target, inventory=inv, mode=mode, po=po, locators=locators,
               driver_fixture=driver_fixture)
    head = _header(target, mode, po, driver_fixture) if run.blocks else ""
    # ③ 准入: 对「完整模块」(头 + 本轮用例) 做 AST 白名单检查 —— 导包/类名/fixture
    # 都在头里, 只查用例体会把合法产物全判成越界。过不了的不交给上层 (免得写脏文件)。
    methods = tuple(po.methods if po is not None else ()) or tuple(po_methods)
    problems = list(run.problems)
    sources: Dict[str, str] = {}
    for case_id, block in run.blocks.items():
        source = head + "\n" + block if head else block
        lint = lint_source(source, mode=mode, po_methods=methods)
        if lint:
            problems.extend(RenderProblem(case_id, str(problem)) for problem in lint)
            continue
        sources[case_id] = source
    return RenderResult(sources, mode=mode, problems=problems,
                        locators=run.locators, notes=run.notes)


def make_renderer(inventory: Any, **options: Any):
    """把 inventory 绑进闭包: plan_verified 的 render 只接收一个 plan 参数"""
    def _render(target: TestPlan) -> RenderResult:
        return render(target, inventory=inventory, **options)

    _render.__name__ = "render"
    return _render


def _module_name(target: TestPlan, mode: str) -> str:
    raw = target.page or "page"
    tail = raw.rstrip("/").split("/")[-1] or "page"
    return f"test_{_slug(tail)}_{mode}.py"


def write(target: Any, *, inventory: Any = None, out_dir: Any,
          mode: str = MODE_STANDALONE, po: Optional[PoPage] = None,
          driver_fixture: str = DEFAULT_DRIVER_FIXTURE,
          po_methods: Iterable[str] = (), locators: Optional[Mapping[str, Any]] = None
          ) -> Written:
    """落盘: 一个测试模块 + 同目录的 locators.json (standalone 必需, po 不需要)

    只写渲染成功的用例; 没渲染成功的在 result.problems 里, 不落半截文件。
    """
    if not isinstance(target, TestPlan):
        raise TypeError(f"write 需要 TestPlan, 拿到 {type(target).__name__}")
    folder = Path(str(out_dir))
    folder.mkdir(parents=True, exist_ok=True)
    result = render(target, inventory=inventory, mode=mode, po=po, locators=locators,
                    driver_fixture=driver_fixture, po_methods=po_methods)
    test_path = folder / _module_name(target, mode)
    if result:
        head = _header(target, mode, po, driver_fixture)
        # render 里每条源码都带了头; 单个模块只保留一份头
        blocks = [source[len(head) + 1:] if source.startswith(head) else source
                  for source in result.values()]
        test_path.write_text(head + "\n" + "\n".join(blocks), encoding="utf-8")
    locators_path = ""
    if mode == MODE_STANDALONE and result.locators:
        path = folder / LOCATORS_FILENAME
        path.write_text(json.dumps(result.locators, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        locators_path = str(path)
    return Written(str(test_path) if result else "", locators_path, result)


__all__ = [
    "DEFAULT_DRIVER_FIXTURE",
    "LOCATORS_FILENAME",
    "MODES",
    "MODE_PO",
    "MODE_STANDALONE",
    "PoPage",
    "RENDERER_VERSION",
    "RenderProblem",
    "RenderResult",
    "UNSUPPORTED_REASONS",
    "Written",
    "build_locators",
    "make_renderer",
    "render",
    "write",
]
