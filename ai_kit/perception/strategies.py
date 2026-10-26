# -*- coding: utf-8 -*-
"""
策略模板库 — 每个原型"该测什么", 纯数据 + 一个通用展开器

为什么是数据而不是代码: 卡片 §5.5 的 checklist 是"覆盖面的知识", 会经常增删。
写成 if/else 或一堆生成函数, 每加一个覆盖点就要动逻辑、动测试; 写成数据后,
加一行 Variant = 加一个覆盖点, 展开/去重/裁剪/报缺全都不用改。

三件事由这个模块负责, 也是它的重点:
1. **展开**: 模板里的"目标"是角色 (筛选项 / 列名 / 行内按钮 / 复选 ...) 而不是
   具体 eid, 由 TARGETS 里的解析器按元素清单绑定。模板不认识 eid, 才不会被
   两次采集的 eid 重编号搞出不同的用例。
2. **报缺**: 页面上没有的对象 (没表格就没分页) 要进 Expansion.missing, 不能
   静默少测 —— 否则"清单写了 8 组"会悄悄变成"实际只跑了 2 组", 而报告上一切正常。
3. **裁剪**: 30 个筛选项 × 7 个变体 = 210 条, 没人跑得完。MAX_PER_STRATEGY 限
   每个策略绑几个对象, limit 按组轮转裁剪 —— 保覆盖广度, 而不是把后半段策略整组砍掉。

符号 token: 变体的 data 里可写 "@maxlength+1" 这类占位。能从元素约束算出来的
当场算清 (写进 length / number), 算不出来的 (唯一性冲突要查库, 有效值要读真实
数据) 原样留给 planner —— 这里的规矩是**不编数据**, 编出来的用例只能骗自己。

与卡片的两处偏离:
- 一个组里可以有多条策略: query 组的"重置"和"计数一致"绑的对象不同 (按钮 / 表格),
  硬塞进一条会让 target 变成 "filter_field|reset_button" 这种怪物。
- unknown 也给了通用冒烟清单: 卡片说 unknown 交 LLM, 但 LLM 关掉或失败时不能
  一条用例都不出 —— 冒烟是最差的兜底, 也比空计划强。

用法:
    from ai_kit.perception import expand, checklist, describe

    exp = expand(inv)                       # 按 inv.archetype (空则就地规则分类)
    exp.cases, exp.missing, exp.stats
    exp = expand(inv, "list_crud", limit=40)  # 指定原型 + 控制条数
    print(describe("list_crud"))              # 人读清单 (也能直接进提示词)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Iterable, Mapping, Optional, Tuple

from ai_kit.perception.archetype import (
    DASHBOARD,
    DETAIL,
    FORM,
    LIST_CRUD,
    LOGIN,
    UNKNOWN,
    classify,
)
from ai_kit.perception.inventory import ElementSpec, PageInventory, from_inventory

# 模板版本: 改覆盖范围就 +1 —— 展开结果会被 ⑤ 按 fingerprint 缓存, 不带版本号的话
# 新增/删除的覆盖点会永远命中旧缓存 (报告上看着一直在测, 实际早就不测了)
STRATEGY_VERSION = 1

# 优先级: 1 必测 / 2 建议 (要额外账号或造数) / 3 慢 (性能基线, 默认仍展开)
MUST, SHOULD, SLOW = 1, 2, 3

SAFE, WRITE, DESTRUCTIVE = "safe", "write", "destructive"
_RISK_ORDER: Dict[str, int] = {SAFE: 0, WRITE: 1, DESTRUCTIVE: 2}

# 一个策略最多绑几个同类对象 (30 个筛选项不能出 210 条)
MAX_PER_STRATEGY = 8

# 没有 maxlength 时"超长"的默认长度: 测的是服务端健壮性, 不是前端截断
LONG_FALLBACK = 4096

# "有格式可言"的控件类型 (错格式/边界值只对这种字段有意义)
FORMAT_TYPES = frozenset(("email", "tel", "url", "number", "date", "datetime-local",
                          "month", "week", "time"))

# 页级目标的 ref (没有 eid 可指时用它)
PAGE_REF = "page"

# 两条共用载荷: 做成常量而不是每处重写, 免得哪一处手抖写成半截
SQLI_PAYLOAD = "' OR '1'='1"
XSS_PAYLOAD = "\"><svg/onload=alert(1)>"

# 文本型输入 (安全类变体的作用对象); checkbox/radio/select/file/password 不算
_NON_TEXT_TYPES = frozenset((
    "", "checkbox", "radio", "file", "password", "hidden", "submit", "button",
    "reset", "image", "range", "color", "select",
))
_CHECK_TYPES = frozenset(("checkbox", "radio"))

SAVE_WORDS: Tuple[str, ...] = ("保存", "提交", "确定", "确认", "创建", "新增", "更新",
                               "save", "submit", "confirm", "ok")
RESET_WORDS: Tuple[str, ...] = ("重置", "清空", "恢复默认", "reset", "clear")
CANCEL_WORDS: Tuple[str, ...] = ("取消", "关闭", "返回", "cancel", "close", "back")
EXPORT_WORDS: Tuple[str, ...] = ("导出", "下载", "export", "download")
# 真的会删数据的词。不能只看 risk: 采集器的 risk 把"重置/清空"也算 destructive
# (它们确实会丢掉用户输入), 但用例形态和删除完全不同, 混在一起会造出错误的批量删除用例
DELETE_WORDS: Tuple[str, ...] = ("删除", "移除", "清除", "作废", "注销", "解绑",
                                 "delete", "remove", "destroy")
USER_WORDS: Tuple[str, ...] = ("用户", "账号", "帐号", "手机", "邮箱",
                               "user", "account", "login", "email", "phone", "mobile")


# ---------------------------------------------------------------------------
# 小助手
# ---------------------------------------------------------------------------

def _flat(text: Any) -> str:
    """去空白 + 小写: "登 录" / "Sign In" 都要能匹配"""
    return re.sub(r"\s+", "", str(text or "")).lower()


def _type_of(element: ElementSpec) -> str:
    """控件类型: 约束里最全, 其次元素本身 / 属性; 兜底按标签名"""
    for source in (element.constraints.get("type"), element.type, element.attrs.get("type")):
        if isinstance(source, str) and source.strip():
            return source.strip().lower()
    if element.tag in ("textarea", "select"):
        return element.tag
    if str(element.attrs.get("contenteditable", "")).lower() == "true":
        return "contenteditable"
    return ""


def _is_text(element: ElementSpec) -> bool:
    """是不是"能打字的框" —— 安全类/格式类变体只对这种元素有意义"""
    return element.is_field and _type_of(element) not in _NON_TEXT_TYPES


def _is_check(element: ElementSpec) -> bool:
    return element.is_field and (_type_of(element) in _CHECK_TYPES
                                 or element.role in _CHECK_TYPES)


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _slug(value: Any) -> str:
    """用例 id 的那一段: 只换掉分隔符, 中文原样保留 (cid 要能被人读)"""
    text = re.sub(r"[\s/\\|:;,.\[\]()#]+", "_", str(value or "").strip())
    return re.sub(r"_+", "_", text).strip("_")[:24]


def _hit(element: ElementSpec, words: Iterable[str]) -> bool:
    haystack = _flat(f"{element.name} {element.text}")
    return any(_flat(word) in haystack for word in words if word)


def _label_of(element: ElementSpec, prefix: str, index: int) -> str:
    return element.name or element.text or f"{prefix}{index + 1}"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Binding:
    """模板里的"目标"绑到页面上的一个具体对象 (元素 / 列 / 行内按钮 ...)"""

    ref: str                                  # eid, 或 "column:..."; 规划器用它指对象
    label: str = ""                           # 可读名 (进 cid 和提示词)
    kind: str = ""                            # element / table / column / row-action / chart / page
    element: Optional[ElementSpec] = None     # 元素级绑定带原件 (取约束算 token)
    refs: Tuple[str, ...] = ()                # 需要一起动的其他引用 (如 用户名框 + 密码框)


@dataclass(frozen=True)
class Variant:
    """一个覆盖点: 同一策略下的一种输入/操作 + 它的期望"""

    key: str
    title: str
    data: Mapping[str, Any] = field(default_factory=dict)
    expect: Tuple[str, ...] = ()
    priority: int = 0                         # 0 = 继承策略的优先级
    note: str = ""
    # 变体依赖的元素约束: 目标没有这些约束就跳过这条 —— 没 maxlength 的框没有"超长"可测,
    # 硬出一条只会让报告多一条永远绿的无意义用例 (假覆盖比漏测更危险)
    requires: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "title": self.title, "data": dict(self.data),
                "expect": list(self.expect), "priority": self.priority,
                "note": self.note, "requires": list(self.requires)}


@dataclass(frozen=True)
class Strategy:
    """一个策略: 卡片里的一格 (绑什么 + 测哪些变体)"""

    key: str                                  # 全局唯一 (cid 的前缀)
    group: str                                # 卡片里的策略组名 (query/pagination/...)
    title: str
    target: str                               # TARGETS 里的角色名
    variants: Tuple[Variant, ...] = ()        # 空 = 每个目标出 1 条
    goal: str = ""
    priority: int = MUST
    risk: str = SAFE                          # 这一组用例对数据的破坏性
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "group": self.group, "title": self.title,
                "target": self.target, "goal": self.goal, "priority": self.priority,
                "risk": self.risk, "note": self.note,
                "variants": [variant.to_dict() for variant in self.variants]}


@dataclass(frozen=True)
class CaseSeed:
    """展开后的一条用例种子 —— ⑤ planner 的输入, 不是最终代码。

    只带"对哪个 eid 做什么 + 期望什么", 不带定位串/步骤代码:
    那两样分别属于 inventory (selector) 和 renderer (runtime)。
    """

    cid: str
    group: str
    strategy: str
    title: str
    ref: str
    label: str = ""
    kind: str = ""
    refs: Tuple[str, ...] = ()
    data: Dict[str, Any] = field(default_factory=dict)
    expect: Tuple[str, ...] = ()
    priority: int = MUST
    risk: str = SAFE
    note: str = ""

    @property
    def is_destructive(self) -> bool:
        return self.risk == DESTRUCTIVE

    def to_dict(self) -> Dict[str, Any]:
        return {"cid": self.cid, "group": self.group, "strategy": self.strategy,
                "title": self.title, "ref": self.ref, "label": self.label,
                "kind": self.kind, "refs": list(self.refs), "data": dict(self.data),
                "expect": list(self.expect), "priority": self.priority,
                "risk": self.risk, "note": self.note}


@dataclass(frozen=True)
class Expansion:
    """展开结果: 用例种子 + 缺了什么 + 每组几条"""

    archetype: str = UNKNOWN
    cases: Tuple[CaseSeed, ...] = ()
    missing: Tuple[str, ...] = ()             # "pagination.jump: 页面上没有表格"
    notes: Tuple[str, ...] = ()               # 裁剪说明
    stats: Dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.cases)

    def by_group(self) -> Dict[str, List[CaseSeed]]:
        grouped: Dict[str, List[CaseSeed]] = {}
        for case in self.cases:
            grouped.setdefault(case.group, []).append(case)
        return grouped

    def of_priority(self, priority: int = MUST) -> Tuple[CaseSeed, ...]:
        return tuple(case for case in self.cases if case.priority <= priority)

    def to_dict(self) -> Dict[str, Any]:
        return {"archetype": self.archetype, "version": STRATEGY_VERSION,
                "total": self.total, "stats": dict(self.stats),
                "missing": list(self.missing), "notes": list(self.notes),
                "cases": [case.to_dict() for case in self.cases]}

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self):
        return iter(self.cases)


# ---------------------------------------------------------------------------
# 目标解析: 角色 -> 页面上的对象
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Target:
    label: str                                        # "表格" / "筛选项" (报缺用)
    resolve: Callable[[PageInventory], Tuple[Binding, ...]]

    def __call__(self, inventory: PageInventory) -> Tuple[Binding, ...]:
        return self.resolve(inventory)


def _binds(elements: Iterable[ElementSpec], prefix: str,
           kind: str = "element") -> Tuple[Binding, ...]:
    return tuple(Binding(element.eid, _label_of(element, prefix, index), kind, element)
                 for index, element in enumerate(elements))


def _fields(inventory: PageInventory) -> List[ElementSpec]:
    return [element for element in inventory.elements if element.is_field]


def _actions(inventory: PageInventory) -> List[ElementSpec]:
    return [element for element in inventory.elements if element.is_action]


def _in_container(elements: Iterable[ElementSpec], *containers: str) -> List[ElementSpec]:
    return [element for element in elements if element.container in containers]


def _first_layer(*layers: Iterable[ElementSpec]) -> List[ElementSpec]:
    """返回第一个非空层 —— 精确容器优先, 宽泛兜底; 一层都没有才空"""
    for layer in layers:
        found = list(layer)
        if found:
            return found
    return []


def _bind_page(inventory: PageInventory) -> Tuple[Binding, ...]:
    return (Binding(PAGE_REF, "整页", "page"),)


def _bind_table(inventory: PageInventory) -> Tuple[Binding, ...]:
    return tuple(Binding(table["eid"], f"表格{index + 1}", "table")
                 for index, table in enumerate(inventory.tables))


def _bind_column(inventory: PageInventory) -> Tuple[Binding, ...]:
    out = []
    for table in inventory.tables:
        for column in table.get("columns") or ():
            name = str(column or "").strip()
            if name:
                out.append(Binding(f"{table['eid']}.column:{name}", name, "column"))
    return tuple(out)


def _bind_row_action(inventory: PageInventory) -> Tuple[Binding, ...]:
    """行内按钮: 有 eid 就用 eid; 被折叠/超预算的只有文案, 用 text: 前缀指过去"""
    out = []
    for table in inventory.tables:
        for action in table.get("row_actions") or ():
            text = str((action or {}).get("text") or "").strip()
            if not text:
                continue
            eid = action.get("eid")
            ref = eid or f"{table['eid']}.row:{text}"
            # 带上元素原件: 风险等级/约束要从原件读, 否则行内"删除"会被当成 safe
            out.append(Binding(ref, text, "row-action", inventory.element(eid) if eid else None))
    return tuple(out)


def _bind_filter_field(inventory: PageInventory) -> Tuple[Binding, ...]:
    """筛选区的输入; 站点常不给 filter-bar 的类名, 退到工具栏, 再退到非表单区"""
    fields = [element for element in _fields(inventory)
              if _is_text(element) or _type_of(element) == "select"]
    return _binds(_first_layer(_in_container(fields, "filter-bar", "toolbar"),
                               [element for element in fields
                                if element.container not in ("form", "dialog", "table-row")]),
                  "筛选项")


def _bind_text_field(inventory: PageInventory) -> Tuple[Binding, ...]:
    return _binds([element for element in _fields(inventory) if _is_text(element)], "输入框")


def _bind_form_field(inventory: PageInventory) -> Tuple[Binding, ...]:
    fields = [element for element in _fields(inventory) if _is_text(element)]
    return _binds(_first_layer(_in_container(fields, "form", "dialog"), fields), "字段")


def _bind_constrained_field(inventory: PageInventory) -> Tuple[Binding, ...]:
    """有格式/边界可言的字段: 有 pattern/min/max/maxlength, 或本身就是 email/tel/number 这类

    纯文本框 (备注) 配"错格式"用例只会永远绿, 那是假覆盖, 所以不绑它。
    """
    constrained = []
    for element in _fields(inventory):
        if not _is_text(element):
            continue
        has_rule = any(element.constraints.get(key) not in (None, "", False)
                       for key in ("pattern", "min", "max", "maxlength"))
        if has_rule or _type_of(element) in FORMAT_TYPES:
            constrained.append(element)
    return _binds(_first_layer(_in_container(constrained, "form", "dialog"), constrained),
                  "字段")


def _bind_required_field(inventory: PageInventory) -> Tuple[Binding, ...]:
    return _binds([element for element in _fields(inventory)
                   if element.constraints.get("required")
                   and element.container != "table-row"], "必填项")


def _bind_select_field(inventory: PageInventory) -> Tuple[Binding, ...]:
    selects = [element for element in _fields(inventory) if _type_of(element) == "select"]
    return _binds(_first_layer(_in_container(selects, "form", "dialog"), selects), "下拉")


def _bind_checkbox(inventory: PageInventory) -> Tuple[Binding, ...]:
    """每个容器只留一个代表。

    100 行就有 100 个复选, 逐个绑只会得到 300 条一模一样的用例 —— 全选/反选是
    页面级动作, 不是逐行动作。取第一个正好是表头那个全选框 (它在 DOM 里最靠前)。
    """
    seen: Dict[str, ElementSpec] = {}
    for element in _fields(inventory):
        if _is_check(element) and element.container not in seen:
            seen[element.container] = element
    return _binds(seen.values(), "复选")


def _bind_submit(inventory: PageInventory) -> Tuple[Binding, ...]:
    actions = _actions(inventory)
    named = [element for element in actions if _hit(element, SAVE_WORDS)]
    fallback = [element for element in _in_container(actions, "form", "dialog")
                if element.risk != DESTRUCTIVE]
    return _binds(_first_layer(named, fallback), "提交按钮")


def _bind_reset(inventory: PageInventory) -> Tuple[Binding, ...]:
    return _binds([element for element in _actions(inventory) if _hit(element, RESET_WORDS)],
                  "重置按钮")


def _bind_cancel(inventory: PageInventory) -> Tuple[Binding, ...]:
    return _binds([element for element in _actions(inventory) if _hit(element, CANCEL_WORDS)],
                  "取消按钮")


def _bind_delete_button(inventory: PageInventory) -> Tuple[Binding, ...]:
    """页面级删除按钮 (不含行内): 行内删除由 row_action 负责, 这里要的是
    批量删除 / 详情页删除 —— 两者都有"二次确认", 行内删除的确认路径跟着行走。"""
    return _binds([element for element in _actions(inventory)
                   if _hit(element, DELETE_WORDS) and element.container != "table-row"],
                  "删除按钮")


def _bind_action_button(inventory: PageInventory) -> Tuple[Binding, ...]:
    """非破坏性按钮 (行内按钮不算, 那由 row_action 负责)。

    不是只看 risk==safe: 采集器把 新增/保存/编辑 都标成 write, 全排掉的话
    权限类/冒烟类策略在真站点上会一条都绑不上。排除 destructive 就够了。
    """
    return _binds([element for element in _actions(inventory)
                   if element.risk != DESTRUCTIVE and element.container != "table-row"],
                  "按钮")


def _bind_export(inventory: PageInventory) -> Tuple[Binding, ...]:
    return _binds([element for element in _actions(inventory)
                   if _hit(element, EXPORT_WORDS)], "导出按钮")


def _bind_credential(inventory: PageInventory) -> Tuple[Binding, ...]:
    """登录: 主目标是密码框, 把配对的用户名框塞进 refs —— 登录永远是"一对"输入"""
    passwords = [element for element in _fields(inventory) if _type_of(element) == "password"]
    out = []
    for password in passwords:
        user = _sibling_user(inventory, password)
        out.append(Binding(password.eid, password.name or "密码框", "element", password,
                           (user.eid,) if user is not None else ()))
    return tuple(out)


def _sibling_user(inventory: PageInventory, password: ElementSpec) -> Optional[ElementSpec]:
    candidates = [element for element in _fields(inventory)
                  if element is not password and _is_text(element)]
    pool = _in_container(candidates, password.container) or candidates
    for element in pool:
        haystack = _flat(f"{element.name} {element.text} "
                         f"{element.attrs.get('name', '')} {element.attrs.get('placeholder', '')}")
        if any(_flat(word) in haystack for word in USER_WORDS):
            return element
    return pool[0] if pool else None


def _bind_chart(inventory: PageInventory) -> Tuple[Binding, ...]:
    charts = int(inventory.signals.get("charts", 0) or 0)
    return tuple(Binding(f"chart:{index + 1}", f"图表{index + 1}", "chart")
                 for index in range(min(charts, MAX_PER_STRATEGY)))


TARGETS: Dict[str, _Target] = {
    "page": _Target("页面", _bind_page),
    "table": _Target("表格", _bind_table),
    "column": _Target("表格列", _bind_column),
    "row_action": _Target("行内按钮", _bind_row_action),
    "filter_field": _Target("筛选项", _bind_filter_field),
    "text_field": _Target("文本输入框", _bind_text_field),
    "form_field": _Target("表单字段", _bind_form_field),
    "constrained_field": _Target("带格式约束的字段", _bind_constrained_field),
    "required_field": _Target("必填字段", _bind_required_field),
    "select_field": _Target("下拉框", _bind_select_field),
    "checkbox": _Target("复选框", _bind_checkbox),
    "submit": _Target("提交按钮", _bind_submit),
    "reset_button": _Target("重置按钮", _bind_reset),
    "cancel_button": _Target("取消按钮", _bind_cancel),
    "delete_button": _Target("删除按钮 (非行内)", _bind_delete_button),
    "action_button": _Target("按钮", _bind_action_button),
    "export_button": _Target("导出按钮", _bind_export),
    "credential": _Target("密码框", _bind_credential),
    "chart": _Target("图表", _bind_chart),
}


# ---------------------------------------------------------------------------
# list_crud 清单 (卡片 5.5 第一张表, 8 组)
# ---------------------------------------------------------------------------

_LIST_CRUD: Tuple[Strategy, ...] = (
    Strategy(
        key="query.filter", group="query", title="筛选项逐项过滤",
        target="filter_field",
        goal="每个筛选项都能被单独驱动; 空/脏/边界值不能把服务打崩",
        variants=(
            Variant("empty", "空值查询", {"value": ""},
                    ("清空该条件后返回全量结果",)),
            Variant("valid", "有效值查询", {"value": "@valid"},
                    ("结果每一行都匹配该条件",)),
            Variant("invalid", "无效值查询", {"value": "no_such_value_zzz"},
                    ("返回空列表并显示空态, 不报 500",)),
            Variant("boundary", "边界长度", {"value": "@maxlength"},
                    ("边界长度可用; 超出应被截断或给出提示",), priority=SHOULD,
                    requires=("maxlength",)),
            Variant("sqli", "SQL 注入串", {"value": SQLI_PAYLOAD},
                    ("按普通字符串处理, 不得返回全表",)),
            Variant("xss", "XSS 串", {"value": XSS_PAYLOAD},
                    ("原样转义显示, 不执行脚本",)),
            Variant("combo", "组合查询", {"value": "@valid", "combine": True},
                    ("与其他条件同时生效 (AND), 结果满足全部条件",)),
        ),
    ),
    Strategy(
        key="query.reset", group="query", title="重置清空全部条件",
        target="reset_button",
        goal="重置是「清空」而不是「回到上次的值」",
        variants=(
            Variant("after_filter", "筛选后点重置", {},
                    ("所有筛选项回到默认值; 列表回到未过滤状态",)),
        ),
    ),
    Strategy(
        key="query.count", group="query", title="结果计数与总数一致",
        target="table",
        goal="'共 N 条' 不能和内实际行数打架",
        variants=(
            Variant("total_vs_rows", "条件过滤后的计数", {"filter": "@valid"},
                    ("显示的 '共 N 条' 与结果行数一致 (单页内)",)),
        ),
    ),
    Strategy(
        key="pagination.jump", group="pagination", title="页码跳转",
        target="table",
        goal="边界页码不能白屏, 也不能给出重复/漏掉的数据",
        variants=(
            Variant("first", "首页", {"page": 1}, ("首页按钮置灰, 第一页数据正确",)),
            Variant("last", "末页", {"page": "last"}, ("末页按钮置灰, 最后一页数据正确",)),
            Variant("middle", "中间页", {"page": "middle"}, ("数据连续, 不与前后页重复",)),
            Variant("out_of_range", "越界页码", {"page": 9999},
                    ("被夹到合法范围或明确提示, 不能 500/白屏",)),
            Variant("page_size", "每页条数切换", {"size": 50},
                    ("每页条数生效, 总数不变; 切换后回到第一页",)),
        ),
    ),
    Strategy(
        key="pagination.keep_filter", group="pagination", title="翻页保留筛选条件",
        target="table",
        goal="翻页不该把用户的筛选条件悄悄清掉",
        variants=(
            Variant("keep", "带条件翻页", {"filter": "@valid", "page": 2},
                    ("第二页仍然带着该条件; 筛选项显示的值不丢",)),
        ),
    ),
    Strategy(
        key="sort.column", group="sort", title="列排序",
        target="column",
        goal="每个可排序列的升降序都要真的生效, 且互为逆序",
        variants=(
            Variant("asc", "升序", {"direction": "asc"}, ("数据按该列升序排列",)),
            Variant("desc", "降序", {"direction": "desc"},
                    ("降序, 且与升序结果互为逆序 (不多不少)",)),
        ),
    ),
    Strategy(
        key="sort.null", group="sort", title="空值行位置",
        target="column",
        goal="空值不该在排序后到处乱窜",
        variants=(
            Variant("nulls", "含空值的列", {"direction": "asc"},
                    ("空值行固定在一端, 翻页时不重复不丢失",), priority=SHOULD),
        ),
    ),
    Strategy(
        key="sort.across_pages", group="sort", title="排序跨页保持",
        target="table",
        goal="排序状态要跟页码一起带过去",
        variants=(
            Variant("page2", "排序后翻页", {"direction": "asc", "page": 2},
                    ("第二页延续第一页的排序",)),
        ),
    ),
    Strategy(
        key="row_actions.each", group="row_actions", title="行内按钮",
        target="row_action",
        goal="行内按钮必须作用于'该行', 而不是第一行或上一次点的那行",
        variants=(
            Variant("normal", "正常路径", {}, ("作用于该行数据 (改完/删完只有这一行变)",)),
            Variant("no_permission", "无权限账号", {"role": "readonly"},
                    ("按钮不显示或置灰; 直接构造请求也应被拦",),
                    priority=SHOULD, note="需要只读账号"),
            Variant("double_click", "重复双击", {"click_times": 2},
                    ("只触发一次 (或幂等), 不产生重复数据/重复弹窗",)),
        ),
    ),
    Strategy(
        key="batch.select", group="batch", title="批量选择",
        target="checkbox",
        goal="全选/反选/跨页选择的选中集合必须和用户理解的一致",
        variants=(
            Variant("select_all", "全选", {}, ("本页全部行被选中, 计数同步",)),
            Variant("invert", "反选", {}, ("选中集合恰好是补集",)),
            Variant("cross_page", "跨页选择", {"page": 2},
                    ("翻页后之前的选中不丢 (或明确清空并提示)",), priority=SHOULD),
        ),
    ),
    Strategy(
        key="batch.delete", group="batch", title="批量删除双路径",
        target="delete_button",
        goal="确认和取消两条路径都必须验: 只验确认等于放走最危险的一类 bug",
        risk=DESTRUCTIVE,
        variants=(
            Variant("confirm", "确认路径", {"confirm": True},
                    ("选中行被删除, 列表与计数同步更新",)),
            Variant("cancel", "取消路径", {"confirm": False},
                    ("点取消后一条数据都不能少",)),
        ),
    ),
    Strategy(
        key="state.empty", group="state", title="空数据态",
        target="table",
        variants=(
            Variant("no_result", "筛到空结果", {"filter": "no_such_value_zzz"},
                    ("显示空态文案, 不是白屏也不是一直转圈",)),
            Variant("no_data", "本来就没有数据", {},
                    ("空态与引导动作 (如新建) 都在",), priority=SHOULD),
        ),
    ),
    Strategy(
        key="state.loading", group="state", title="加载态",
        target="table",
        variants=(
            Variant("slow", "慢网络", {"throttle": "3s"},
                    ("加载中有明确 loading, 结束后消失, 不出现双份数据",),
                    priority=SHOULD),
        ),
    ),
    Strategy(
        key="state.perf", group="state", title="大数据量基线",
        target="table",
        priority=SLOW,
        variants=(
            Variant("rows", "1000+ 行", {"rows": 1000},
                    ("首屏渲染 < 3s, 翻页仍可用",), priority=SLOW),
        ),
    ),
    Strategy(
        key="consistency.three_way", group="consistency", title="三处数据一致",
        target="table",
        goal="列表 / 详情 / 导出 是同一份数据的三张脸",
        variants=(
            Variant("count", "计数 vs 行数", {},
                    ("'共 N 条' 与翻完所有页的行数之和一致",)),
            Variant("detail", "列表 vs 详情", {},
                    ("同一条记录的关键字段一致",)),
            Variant("export", "列表 vs 导出", {"export": True},
                    ("导出行数与列表总数一致, 字段顺序一致",), priority=SHOULD),
        ),
    ),
    Strategy(
        key="security.text_input", group="security", title="特殊字符输入",
        target="text_field",
        goal="输入侧的脏数据不能变成存储型漏洞或 500",
        variants=(
            Variant("too_long", "超长", {"value": "@maxlength+1"},
                    ("被截断或明确报错, 不报 500",)),
            Variant("emoji", "emoji", {"value": "测试🙂🚀<>"},
                    ("保存后原样显示, 不乱码",)),
            Variant("spaces", "前后空格", {"value": "  关键字  "},
                    ("trim 后再查询/保存 (或明确说明按原样匹配)",)),
            Variant("script", "脚本串", {"value": XSS_PAYLOAD},
                    ("不执行, 不破坏页面结构 (没有 alert 弹窗)",)),
        ),
    ),
)


# ---------------------------------------------------------------------------
# form 清单
# ---------------------------------------------------------------------------

_FORM: Tuple[Strategy, ...] = (
    Strategy(
        key="required.each", group="required", title="必填逐项为空",
        target="required_field",
        goal="每个必填项单独漏填都要被拦住 —— 只验「全部空提交」会漏掉单字段校验",
        variants=(
            Variant("blank", "留空提交", {"value": ""},
                    ("提交被拦, 该字段下提示'必填'",)),
            Variant("spaces", "只填空格", {"value": "   "},
                    ("空格不算填写: 仍应提示必填",)),
        ),
    ),
    Strategy(
        key="required.all", group="required", title="全部必填留空",
        target="submit",
        variants=(
            Variant("all_blank", "全空提交", {},
                    ("一次提交把所有必填项都标出来, 不是只报第一个",)),
        ),
    ),
    Strategy(
        key="format.each", group="format", title="字段格式与边界",
        target="constrained_field",
        goal="错格式必须挡住; 上下界各越一位",
        variants=(
            Variant("wrong", "错格式", {"value": "@invalid_format"},
                    ("提示格式错误且不提交",)),
            Variant("min_minus_1", "下界 -1", {"value": "@min-1"},
                    ("低于下界的值被拦",), priority=SHOULD, requires=("min",)),
            Variant("max_plus_1", "上界 +1", {"value": "@max+1"},
                    ("高于上界的值被拦或截断",), priority=SHOULD, requires=("max",)),
        ),
    ),
    Strategy(
        key="format.unique", group="format", title="唯一性冲突",
        target="form_field",
        variants=(
            Variant("duplicate", "提交库里已存在的值", {"value": "@existing"},
                    ("提示唯一键冲突, 不产生重复记录",),
                    priority=SHOULD, note="需要先查库拿到已存在的值"),
        ),
    ),
    Strategy(
        key="dependency.linkage", group="dependency", title="依赖字段联动",
        target="select_field",
        variants=(
            Variant("parent_change", "改父字段", {},
                    ("子字段选项刷新 / 被重置, 不残留非法组合",)),
        ),
    ),
    Strategy(
        key="persist.after_save", group="persist", title="保存后持久化",
        target="submit",
        goal="保存成功不等于落库; 刷新/重登再看一眼",
        variants=(
            Variant("reload", "保存后刷新", {},
                    ("刷新页面 (甚至重登) 后数据仍在",)),
        ),
    ),
    Strategy(
        key="submit.double", group="submit", title="重复提交",
        target="submit",
        variants=(
            Variant("double_click", "双击提交", {"click_times": 2},
                    ("只生成一条记录",)),
        ),
    ),
    Strategy(
        key="submit.interrupt", group="submit", title="提交中被打断",
        target="submit",
        variants=(
            Variant("leave", "提交中返回", {},
                    ("不留在半提交状态: 要么全写要么没写",), priority=SHOULD),
            Variant("offline", "提交中断网", {"network": "offline"},
                    ("给出明确失败提示, 且可重试",), priority=SHOULD),
        ),
    ),
    Strategy(
        key="reset.defaults", group="reset", title="重置恢复默认",
        target="reset_button",
        variants=(
            Variant("after_edit", "改完再重置", {},
                    ("字段回到初始默认值 (不是空白)",)),
        ),
    ),
    Strategy(
        key="cancel.discard", group="cancel", title="取消放弃修改",
        target="cancel_button",
        variants=(
            Variant("with_changes", "改完点取消", {},
                    ("不落库; 再次进入看不到这次修改",)),
        ),
    ),
)


# ---------------------------------------------------------------------------
# login 清单
# ---------------------------------------------------------------------------

_LOGIN: Tuple[Strategy, ...] = (
    Strategy(
        key="credential.negative", group="credentials", title="错误凭证组合",
        target="credential",
        goal="错误提示不能泄露「用户是否存在」",
        variants=(
            Variant("wrong_password", "密码错误", {"password": "Wrong@123"},
                    ("提示'用户名或密码错误', 不指出是哪一项错",)),
            Variant("wrong_user", "用户不存在", {"username": "no_such_user_zzz"},
                    ("提示与密码错误完全一致",)),
            Variant("empty_both", "两项都空", {},
                    ("前端直接拦, 不发出登录请求",)),
            Variant("empty_password", "只填用户名", {"password": ""},
                    ("密码必填提示",)),
        ),
    ),
    Strategy(
        key="credential.submit", group="credentials", title="正常登录",
        target="credential",
        variants=(
            Variant("success", "正确凭证", {},
                    ("跳转首页/目标页, 会话可用 (后续接口不带 401)",)),
            Variant("enter_key", "回车提交", {"key": "Enter"},
                    ("与点按钮等价",), priority=SHOULD),
            Variant("remember_me", "记住我", {"remember": True},
                    ("重开浏览器仍保持登录",), priority=SHOULD),
        ),
    ),
    Strategy(
        key="lockout.repeat", group="lockout", title="连续失败锁定",
        target="credential",
        risk=WRITE,
        variants=(
            Variant("n_times", "连续 5 次错误口令", {"attempts": 5},
                    ("触发锁定或验证码, 并提示剩余次数",), priority=SHOULD),
        ),
    ),
    Strategy(
        key="session.target", group="session", title="登录后去向",
        target="page",
        variants=(
            Variant("deep_link", "从深链进来", {},
                    ("登录后回到原目标页, 而不是首页",)),
            Variant("already_logged_in", "已登录访问登录页", {},
                    ("直接跳走, 不再显示登录表单",), priority=SHOULD),
        ),
    ),
    Strategy(
        key="security.password", group="security", title="密码安全",
        target="credential",
        variants=(
            Variant("masked", "观察密码框", {},
                    ("输入为掩码; 页面源码/日志/URL 里没有明文",)),
            Variant("autocomplete", "浏览器自动填充", {},
                    ("autocomplete=current-password, 允许密码管理器",),
                    priority=SHOULD),
        ),
    ),
)


# ---------------------------------------------------------------------------
# detail 清单
# ---------------------------------------------------------------------------

_DETAIL: Tuple[Strategy, ...] = (
    Strategy(
        key="masking.sensitive", group="masking", title="敏感字段脱敏",
        target="page",
        goal="详情页最容易把不该给的字段整包吐出来",
        variants=(
            Variant("masked", "查看敏感字段", {},
                    ("手机号/证件号/密码等按需脱敏; 前端接口里不返回明文",)),
        ),
    ),
    Strategy(
        key="readonly.fields", group="readonly", title="只读展示",
        target="form_field",
        variants=(
            Variant("not_editable", "尝试编辑字段", {},
                    ("详情页字段不可编辑 (没有输入框或已 disabled)",),
                    priority=SHOULD),
        ),
    ),
    Strategy(
        key="actions.permission", group="actions", title="操作按钮权限",
        target="action_button",
        variants=(
            Variant("normal", "有权限", {}, ("编辑/删除入口可见且可用",)),
            Variant("readonly", "无权限账号", {"role": "readonly"},
                    ("入口不显示或置灰; 直接改 URL 也应被拦",), priority=SHOULD,
                    note="需要只读账号"),
        ),
    ),
    Strategy(
        key="actions.delete_confirm", group="actions", title="删除二次确认",
        target="delete_button",
        risk=DESTRUCTIVE,
        variants=(
            Variant("confirm", "确认删除", {"confirm": True},
                    ("记录被删, 返回列表后查不到",)),
            Variant("cancel", "取消删除", {"confirm": False},
                    ("记录仍在, 数据一条都没少",)),
        ),
    ),
    Strategy(
        key="navigation.back", group="navigation", title="返回列表",
        target="page",
        variants=(
            Variant("keep_filter", "从列表点进来再返回", {},
                    ("回到列表时筛选/页码保持 (或被明确重置并提示)",)),
        ),
    ),
    Strategy(
        key="consistency.with_list", group="consistency", title="与列表一致",
        target="page",
        variants=(
            Variant("same_record", "同一条记录", {},
                    ("列表行与详情页的关键字段一致",)),
        ),
    ),
)


# ---------------------------------------------------------------------------
# dashboard 清单
# ---------------------------------------------------------------------------

_DASHBOARD: Tuple[Strategy, ...] = (
    Strategy(
        key="chart.each", group="chart", title="图表数据",
        target="chart",
        goal="看板的价值全在「图上的数对不对」",
        variants=(
            Variant("data_match", "与数据源一致", {},
                    ("图上的数值/合计与表格或接口返回一致",)),
            Variant("empty", "无数据", {"filter": "empty-range"},
                    ("显示'暂无数据'而不是空白/报错",), priority=SHOULD),
        ),
    ),
    Strategy(
        key="chart.legend", group="chart", title="图例与坐标轴",
        target="chart",
        variants=(
            Variant("toggle", "点图例", {},
                    ("对应系列隐藏/显示, 坐标轴不失真",), priority=SHOULD),
        ),
    ),
    Strategy(
        key="filter.range", group="filter", title="筛选联动",
        target="filter_field",
        variants=(
            Variant("range", "切换时间范围", {"value": "近7天"},
                    ("所有卡片/图表同步刷新且口径一致",)),
        ),
    ),
    Strategy(
        key="refresh.manual", group="refresh", title="刷新更新",
        target="page",
        variants=(
            Variant("manual", "手动刷新", {},
                    ("数据随之更新, 期间有 loading",)),
        ),
    ),
    Strategy(
        key="export.match", group="export", title="导出与展示一致",
        target="export_button",
        variants=(
            Variant("rows", "导出", {},
                    ("导出数据与当前筛选下的图表/表格一致",), priority=SHOULD),
        ),
    ),
)


# ---------------------------------------------------------------------------
# unknown: 通用冒烟 (卡片说交 LLM, 但关掉 LLM 时不能一条用例都不出)
# ---------------------------------------------------------------------------

_GENERIC: Tuple[Strategy, ...] = (
    Strategy(
        key="smoke.load", group="smoke", title="页面能打开",
        target="page",
        variants=(
            Variant("load", "打开页面", {},
                    ("返回 200, 无 JS 报错, 标题非空",)),
        ),
    ),
    Strategy(
        key="smoke.interactive", group="smoke", title="关键按钮可点",
        target="action_button",
        variants=(
            Variant("click", "逐个点击安全按钮", {},
                    ("点击有反馈 (跳转/弹窗/提示), 不白屏不 500",), priority=SHOULD),
        ),
    ),
    Strategy(
        key="security.inputs", group="security", title="输入框不崩",
        target="text_field",
        variants=(
            Variant("too_long", "超长", {"value": "@maxlength+1"},
                    ("被截断或明确报错, 不报 500",)),
            Variant("script", "脚本串", {"value": XSS_PAYLOAD},
                    ("不执行, 不破坏页面结构",), priority=SHOULD),
        ),
    ),
)


CHECKLISTS: Dict[str, Tuple[Strategy, ...]] = {
    LOGIN: _LOGIN,
    LIST_CRUD: _LIST_CRUD,
    FORM: _FORM,
    DETAIL: _DETAIL,
    DASHBOARD: _DASHBOARD,
    UNKNOWN: _GENERIC,
}


# ---------------------------------------------------------------------------
# 取值: 模板 -> 具体种子
# ---------------------------------------------------------------------------

_DEFAULT_VARIANT = Variant("cover", "覆盖一次")


def _resolve_token(token: str, element: ElementSpec) -> Dict[str, Any]:
    """能算的当场算清; 算不了就返回空 (token 原样留给 planner) —— 不编数据"""
    constraints = element.constraints
    if token in ("@maxlength", "@maxlength+1"):
        limit = _as_int(constraints.get("maxlength"))
        if limit <= 0:
            # 没有 maxlength 时"超长"测的是服务端健壮性, 给个固定长度
            return {"length": LONG_FALLBACK} if token.endswith("+1") else {}
        length = limit + 1 if token.endswith("+1") else limit
        return {"length": length, "limit": limit}
    if token in ("@min-1", "@min", "@max+1", "@max"):
        key = token[1:].split("-")[0].split("+")[0]
        bound = _as_number(constraints.get(key))
        if bound is None:
            return {}
        shift = -1 if token.endswith("-1") else (1 if token.endswith("+1") else 0)
        return {"number": bound + shift, "bound": bound}
    if token == "@invalid_format":
        pattern = constraints.get("pattern")
        if isinstance(pattern, str) and pattern:
            return {"pattern": pattern}
    return {}


def _bind_data(raw: Mapping[str, Any], binding: Binding) -> Dict[str, Any]:
    """变体数据 + 绑定来的上下文 (控件类型 / 算得出的边界)"""
    data = dict(raw)
    element = binding.element
    if element is None:
        return data
    if element.is_field:
        data.setdefault("type", _type_of(element))
    value = data.get("value")
    if isinstance(value, str) and value.startswith("@"):
        data["token"] = value
        data.update(_resolve_token(value, element))
    return data


def _satisfied(variant: Variant, binding: Binding) -> bool:
    """变体要求的约束在目标元素上是否齐。没有元素 (列/表格/页面) 时不筛"""
    if not variant.requires or binding.element is None:
        return True
    constraints = binding.element.constraints
    return all(constraints.get(key) not in (None, "", False) for key in variant.requires)


def _risk_of(strategy: Strategy, binding: Binding) -> str:
    """用例风险取策略与元素的较高者: 行内"删除"按钮不能因为模板写着 safe 就标 safe"""
    risk = strategy.risk
    if binding.element is not None:
        element_risk = binding.element.risk
        if _RISK_ORDER.get(element_risk, 0) > _RISK_ORDER.get(risk, 0):
            return element_risk
    return risk


def _seed(archetype: str, strategy: Strategy, binding: Binding, variant: Variant,
          index: int, seen: Dict[str, int]) -> CaseSeed:
    key = _slug(binding.label) or f"t{index + 1}"
    cid = f"{archetype}.{strategy.key}.{key}.{variant.key}"
    seen[cid] = seen.get(cid, 0) + 1
    if seen[cid] > 1:                              # 两张表都有"编辑" -> 加序号, 不能撞
        cid = f"{cid}#{seen[cid]}"
    return CaseSeed(
        cid=cid,
        group=strategy.group,
        strategy=strategy.key,
        title=f"{strategy.title} · {binding.label} · {variant.title}",
        ref=binding.ref,
        label=binding.label,
        kind=binding.kind,
        refs=binding.refs,
        data=_bind_data(variant.data, binding),
        expect=variant.expect,
        priority=variant.priority or strategy.priority,
        risk=_risk_of(strategy, binding),
        note=variant.note or strategy.note,
    )


def _trim(cases: List[CaseSeed], limit: int) -> List[CaseSeed]:
    """按组轮转裁剪: 先每组一条, 再第二圈… 保覆盖广度

    直接 `cases[:limit]` 会把靠后的策略组 (security/consistency) 整组砍掉,
    报告上却看不出来 —— 那正是最需要测的部分。
    """
    if limit <= 0 or len(cases) <= limit:
        return cases
    buckets: Dict[str, List[int]] = {}
    for index, case in enumerate(cases):
        buckets.setdefault(case.group, []).append(index)
    picked, groups = set(), list(buckets)
    while len(picked) < limit:
        moved = False
        for group in groups:
            bucket = buckets[group]
            if not bucket:
                continue
            picked.add(bucket.pop(0))
            moved = True
            if len(picked) >= limit:
                break
        if not moved:
            break
    return [case for index, case in enumerate(cases) if index in picked]


def _archetype_of(inventory: PageInventory, archetype: Optional[str]) -> str:
    name = str(archetype or inventory.archetype or "").strip().lower()
    if name in CHECKLISTS:
        return name
    return classify(inventory).archetype           # 空/未知 -> 就地规则分类


def expand(inventory: Any, archetype: Optional[str] = None, *,
           groups: Optional[Iterable[str]] = None,
           limit: int = 0,
           include_slow: bool = True,
           max_per_strategy: int = MAX_PER_STRATEGY) -> Expansion:
    """按原型把 checklist 展开成用例种子。

    inventory 可以是 PageInventory 或 collector 的 dict; archetype 留空则用
    inventory.archetype, 再空就地规则分类。limit > 0 时按组轮转裁剪。
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    name = _archetype_of(inv, archetype)
    wanted = set(groups) if groups is not None else None

    cases: List[CaseSeed] = []
    missing: List[str] = []
    notes: List[str] = []
    seen: Dict[str, int] = {}

    for strategy in CHECKLISTS.get(name, ()):
        if wanted is not None and strategy.group not in wanted:
            continue
        if not include_slow and strategy.priority >= SLOW:
            continue
        target = TARGETS.get(strategy.target)
        if target is None:                        # 模板写错 target: 报出来, 别静默丢
            missing.append(f"{strategy.key}: 未知绑定目标 {strategy.target}")
            continue
        bindings = target(inv)
        if not bindings:
            missing.append(f"{strategy.key}: 页面上没有{target.label}")
            continue
        if len(bindings) > max_per_strategy:
            notes.append(f"{strategy.key}: {target.label} {len(bindings)} 个, "
                         f"只展开前 {max_per_strategy} 个")
            bindings = bindings[:max_per_strategy]
        for index, binding in enumerate(bindings):
            resolved = [variant for variant in (strategy.variants or (_DEFAULT_VARIANT,))
                        if _satisfied(variant, binding)]
            if not resolved:                      # 约束一条都不满足: 不造假用例
                continue
            for variant in resolved:
                cases.append(_seed(name, strategy, binding, variant, index, seen))

    trimmed = _trim(cases, limit)
    if limit > 0 and len(trimmed) < len(cases):
        notes.append(f"按组轮转裁剪: {len(cases)} -> {len(trimmed)} 条 (limit={limit})")

    stats: Dict[str, int] = {}
    for case in trimmed:
        stats[case.group] = stats.get(case.group, 0) + 1
    return Expansion(archetype=name, cases=tuple(trimmed), missing=tuple(missing),
                     notes=tuple(notes), stats=stats)


# ---------------------------------------------------------------------------
# 只读查询
# ---------------------------------------------------------------------------

def checklist(archetype: str = LIST_CRUD) -> Tuple[Strategy, ...]:
    """该原型的策略组 (数据本身, 不绑定页面)"""
    return CHECKLISTS.get(str(archetype or "").strip().lower(), ())


def groups(archetype: str = LIST_CRUD) -> Tuple[str, ...]:
    """该原型的策略组名 (按清单顺序, 去重)"""
    names: List[str] = []
    for strategy in checklist(archetype):
        if strategy.group not in names:
            names.append(strategy.group)
    return tuple(names)


def find(key: str, archetype: str = "") -> Optional[Strategy]:
    """按策略 key 找 (archetype 留空则在全部清单里找)"""
    pool = checklist(archetype) if archetype else tuple(
        strategy for group in CHECKLISTS.values() for strategy in group)
    for strategy in pool:
        if strategy.key == key:
            return strategy
    return None


def describe(archetype: str = LIST_CRUD) -> str:
    """人读清单: 组 / 策略 / 绑定目标 / 变体。可直接塞进 planner 提示词。"""
    strategies = checklist(archetype)
    if not strategies:
        return f"{archetype}: 没有策略清单"
    lines = [f"{archetype} 策略清单 · {len(groups(archetype))} 组 {len(strategies)} 条"]
    current = ""
    for strategy in strategies:
        if strategy.group != current:
            current = strategy.group
            lines.append(f"[{current}]")
        target = TARGETS[strategy.target].label if strategy.target in TARGETS else strategy.target
        variants = " / ".join(variant.title for variant in strategy.variants) or "每个目标 1 条"
        mark = "" if strategy.priority == MUST else f" (P{strategy.priority})"
        lines.append(f"  - {strategy.title}{mark} | 对象: {target} | {variants}")
    return "\n".join(lines)
