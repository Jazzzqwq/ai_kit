# -*- coding: utf-8 -*-
"""
元素清单 — 采集结果(dict) -> 冻结数据模型 (ElementSpec / PageInventory)

采集器的产物是给机读的 JSON dict: 键可缺、值可脏、顺序不定。规划器/渲染器不该
在 dict 里摸键, 所以这一层做三件事:

1. **定型**: dict -> ElementSpec / PageInventory (frozen dataclass)。
   eid 是唯一引用 —— 计划里只准出现 eid, 具体定位串由这里派生。
2. **算指纹**: fingerprint = sha1(结构骨架), 当"这页还是不是上次那页"的缓存键。
   只认结构不认数据, 理由见 canonical_dom 的注释。
3. **派生视图**: links / forms 采集器没有单独出口 (a 被并进 button, form 只有容器
   类别), 但下游要, 所以从元素清单派生 —— 派生, 而不是为了它再采一次页面。

卡片 §5.3 的字段一个不少; 多出来的是 kind / primary / locator_by / actions /
options / attrs / aria / text / html / depth, 全部直接来自采集结果, 少留一个就得
为了拿个 placeholder 再采一次页面。

两个偏离卡片的地方 (都是有意为之):
- elements / tables / links ... 用 tuple 而不是 List: 字段冻住了、内容还能 append
  的"冻结数据类"是自欺欺人。要 list 请 to_dict()。
- stats 里的 truncated 是 0/1 而不是 bool: 卡片要求 Dict[str, int], 布尔混进 int
  字典会让下游的求和/统计踩坑。要布尔看 is_truncated。

降级不抛: 传 None / 非 mapping / 脏元素 -> 空清单或跳过该条, 从不 raise。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import cached_property
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from ai_kit.perception.collector import COLLECTOR_VERSION, collect

INVENTORY_VERSION = 1
FINGERPRINT_PREFIX = f"inventory/v{INVENTORY_VERSION}"

# 风险等级: 采集器只会给这三档 (None 归一成 safe); 未知字符串原样保留 ——
# 下游一律按 `risk != "safe"` 判危险, 所以"新等级"最坏是被当成危险, 不会被漏掉。
RISK_LEVELS: Tuple[str, ...] = ("safe", "write", "destructive")
SAFE = "safe"

# 容器归类的固定顺序 (派生 forms 时按它排, 免得 dict 顺序变成接口的一部分)
CONTAINER_ORDER: Tuple[str, ...] = ("form", "dialog", "filter-bar", "toolbar", "table-row", "page")

_FIELD_KINDS = ("input", "select")

# 采集器的 strategy -> 卡片里的定位串名字。id 的三个分档 (`id` / `id(疑似动态)` /
# `id(需转义)`) 在这里合成一个键: 稳定性差异留在 stability 里, 键名不该跟着打分变。
_STRATEGY_ALIASES = {
    "id(疑似动态)": "id",
    "id(需转义)": "id",
    "semantic-css": "css",
    "relative-xpath": "xpath",
}
_XPATH_KEYS = ("xpath", "text")


# ---------------------------------------------------------------------------
# 宽容取值 (跨层口径一致; 与 collector 的同名助手语义相同, 但这里不 import 私有名)
# ---------------------------------------------------------------------------

def _items(value: Any) -> List[Any]:
    return value if isinstance(value, (list, tuple)) else []


def _dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any, limit: int = 0) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    return text[:limit] if limit > 0 else text


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path_of(url: str) -> str:
    """指纹里只留 path (丢掉 query / fragment): 翻页、加筛选参数不该换缓存键"""
    if not url:
        return ""
    try:
        return urlsplit(url).path or ""
    except ValueError:
        return ""


def _locator_name(by: str, strategy: str) -> str:
    name = _text(strategy).lower()
    name = _STRATEGY_ALIASES.get(name, name)
    if name:
        return name
    return "xpath" if by == "xpath" else "css"


def _default_by(name: str) -> str:
    return "xpath" if name in _XPATH_KEYS else "css selector"


# ---------------------------------------------------------------------------
# ElementSpec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ElementSpec:
    """一个可操作元素 (表格根也走这个形状, kind="table")。

    前 10 个字段就是卡片 §5.3 的字段, 后面是同一个采集结果里顺手带的补充字段。
    locators / locator_by / state / constraints / attrs / aria 都是 dict, 语义上只读;
    真冻结它们要包 MappingProxyType, 那会让 to_dict / json 到处需要解包, 不划算。
    """

    eid: str
    tag: str = ""
    role: str = ""
    name: str = ""
    locators: Dict[str, str] = field(default_factory=dict)
    stability: float = 0.0
    state: Dict[str, Any] = field(default_factory=dict)
    constraints: Dict[str, Any] = field(default_factory=dict)
    container: str = "page"
    risk: str = SAFE
    # --- 卡片之外的补充 (全部来自采集结果) ---
    kind: str = ""
    type: str = ""
    primary: str = ""
    locator_by: Dict[str, str] = field(default_factory=dict)
    actions: Tuple[str, ...] = ()
    options: Tuple[str, ...] = ()
    attrs: Dict[str, Any] = field(default_factory=dict)
    aria: Dict[str, Any] = field(default_factory=dict)
    text: str = ""
    html: str = ""
    depth: int = 0

    @property
    def selector(self) -> str:
        """当前最稳的定位串; 一个都没采到就退回 eid 标记 (采集时写过 data-ai-eid)"""
        return self.locators.get(self.primary) or f'[data-ai-eid="{self.eid}"]'

    @property
    def selector_by(self) -> str:
        return self.locator_by.get(self.primary) or _default_by(self.primary)

    @property
    def is_field(self) -> bool:
        return self.kind in _FIELD_KINDS

    @property
    def is_action(self) -> bool:
        return self.kind == "button"

    @property
    def is_risky(self) -> bool:
        return self.risk != SAFE

    def locator(self, name: str = "") -> Optional[str]:
        """取指定策略的定位串, 不传则取主定位串"""
        return self.locators.get(name or self.primary)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "eid": self.eid,
            "kind": self.kind,
            "type": self.type,
            "tag": self.tag,
            "role": self.role,
            "name": self.name,
            "text": self.text,
            "locators": dict(self.locators),
            "locator_by": dict(self.locator_by),
            "primary": self.primary,
            "stability": self.stability,
            "state": dict(self.state),
            "constraints": dict(self.constraints),
            "container": self.container,
            "risk": self.risk,
            "actions": list(self.actions),
            "options": list(self.options),
            "attrs": dict(self.attrs),
            "aria": dict(self.aria),
            "html": self.html,
            "depth": self.depth,
        }


# ---------------------------------------------------------------------------
# PageInventory
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PageInventory:
    """一页的元素清单。archetype 由 ③ archetype.py 回填 (with_archetype)。"""

    url: str = ""
    title: str = ""
    fingerprint: str = ""
    archetype: str = ""
    captured_at: str = ""
    elements: Tuple[ElementSpec, ...] = ()
    forms: Tuple[Dict[str, Any], ...] = ()
    tables: Tuple[Dict[str, Any], ...] = ()
    links: Tuple[Dict[str, Any], ...] = ()
    dialogs: Tuple[Dict[str, Any], ...] = ()
    stats: Dict[str, int] = field(default_factory=dict)
    signals: Dict[str, int] = field(default_factory=dict)
    page_name: str = ""
    notes: Tuple[str, ...] = ()

    # --- 查询 ---

    @cached_property
    def by_eid(self) -> Dict[str, ElementSpec]:
        """eid -> ElementSpec (只含可交互元素; 表格/弹窗走 lookup)"""
        return {element.eid: element for element in self.elements}

    def element(self, eid: str) -> Optional[ElementSpec]:
        return self.by_eid.get(_text(eid))

    def lookup(self, eid: str) -> Optional[Any]:
        """按 eid 拿任何东西: 元素 / 表格 dict / 弹窗 dict"""
        key = _text(eid)
        found = self.by_eid.get(key)
        if found is not None:
            return found
        for record in (*self.tables, *self.dialogs):
            if _text(record.get("eid")) == key:
                return record
        return None

    @property
    def risky_elements(self) -> Tuple[ElementSpec, ...]:
        return tuple(element for element in self.elements if element.is_risky)

    @property
    def cache_key(self) -> str:
        """缓存键就是指纹 (已经带 inventory 版本前缀)"""
        return self.fingerprint

    @property
    def is_truncated(self) -> bool:
        return bool(self.stats.get("truncated"))

    def __len__(self) -> int:
        return len(self.elements)

    def __iter__(self) -> Iterator[ElementSpec]:
        return iter(self.elements)

    # --- 变换 / 序列化 ---

    def with_archetype(self, archetype: str) -> "PageInventory":
        """③ 分类完回填原型; 指纹不动 —— 原型是解读, 不是页面结构"""
        return replace(self, archetype=_text(archetype))

    def to_dict(self) -> Dict[str, Any]:
        """JSON 友好版 (elements/forms/... 都摊成 list), 可直接塞缓存 / 进提示词"""
        return {
            "url": self.url,
            "title": self.title,
            "fingerprint": self.fingerprint,
            "archetype": self.archetype,
            "captured_at": self.captured_at,
            "page_name": self.page_name,
            "truncated": self.is_truncated,
            "stats": dict(self.stats),
            "signals": dict(self.signals),
            "elements": [element.to_dict() for element in self.elements],
            "forms": [dict(record) for record in self.forms],
            "tables": [dict(record) for record in self.tables],
            "links": [dict(record) for record in self.links],
            "dialogs": [dict(record) for record in self.dialogs],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# dict -> ElementSpec
# ---------------------------------------------------------------------------

def _locator_view(element: Mapping) -> Tuple[Dict[str, str], Dict[str, str], str, float]:
    """折叠 locator + alternatives -> (定位串, by 映射, 主定位名, 稳定度)。

    键名口径: data-testid/data-test/... 原样, 三个 id 分档合成 id, aria-label /
    name 原样, semantic-css -> css, relative-xpath -> xpath, text -> text。
    主定位取 stability 最高的那个 (同分先到先得) —— 采集器按"优先级"排序, 而
    `el-id-1234-5` 这种疑似动态 id 排在 aria-label 前面但分数更低, 不能盲取第一个。
    """
    values: Dict[str, str] = {}
    bys: Dict[str, str] = {}

    explicit = element.get("locators")
    if isinstance(explicit, Mapping):          # 已经是 to_dict 的形状 (往返/缓存读回)
        raw_bys = _dict(element.get("locator_by"))
        for key, value in explicit.items():
            name, locator = _text(key), _text(value)
            if not name or not locator:
                continue
            values[name] = locator
            bys[name] = _text(raw_bys.get(key)) or _default_by(name)
        if values:
            primary = _text(element.get("primary"))
            if primary not in values:
                primary = next(iter(values))
            return values, bys, primary, round(max(0.0, min(1.0, _num(element.get("stability")))), 2)

    best, best_stability = "", -1.0
    for candidate in [element.get("locator"), *_items(element.get("alternatives"))]:
        candidate = _dict(candidate)
        by, locator = _text(candidate.get("by")), _text(candidate.get("value"))
        if not by or not locator:
            continue
        name = _locator_name(by, _text(candidate.get("strategy")))
        if name not in values:                 # 同策略先到先得 (采集器已按优先级排序)
            values[name] = locator
            bys[name] = by
        stability = _num(candidate.get("stability"))
        if stability > best_stability:
            best, best_stability = name, stability

    if not values:
        return {}, {}, "", 0.0
    if best not in values:
        best = next(iter(values))
    return values, bys, best, round(max(0.0, min(1.0, best_stability)), 2)


def _name_of(raw: Mapping, attrs: Mapping, aria: Mapping) -> str:
    """可读名: text > aria-label > placeholder > title > alt > name 属性。

    取不到就返回空串, **不要拿 eid 兜底**: 纯图标按钮(× 关闭)没有可读名, 若用 eid
    顶上, 指纹里的 name 就会随 eid 重编号而变 —— 同一页面两次采集算出不同缓存键。
    """
    for value in (_text(raw.get("text")), _text(aria.get("label")),
                  _text(attrs.get("placeholder")), _text(attrs.get("title")),
                  _text(attrs.get("alt"))):
        if value:
            return value[:60]
    return _text(raw.get("name"))[:60]


def _constraints_of(raw: Any) -> Dict[str, Any]:
    """约束只留标量: 嵌套结构对下游没意义, 还会把脏数据带进约束校验"""
    out: Dict[str, Any] = {}
    for key, value in _dict(raw).items():
        name = _text(key)
        if not name or value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        if isinstance(value, (str, int, float, bool)):
            out[name] = value
    return out


def _state_of(raw_state: Any, constraints: Mapping[str, Any]) -> Dict[str, Any]:
    """visible / disabled / readonly。

    采集器只采得到得到"被渲染出来"的元素 (不可见的已被过滤), 所以没传 state 时
    visible 视为 True; disabled / readonly 从约束里读 —— 同一份事实不留两份。
    """
    given = _dict(raw_state)
    if given:
        return {"visible": bool(given.get("visible", True)),
                "disabled": bool(given.get("disabled", False)),
                "readonly": bool(given.get("readonly", False))}
    return {"visible": True,
            "disabled": bool(constraints.get("disabled")),
            "readonly": bool(constraints.get("readonly"))}


def _build_element(raw: Any, kind_hint: str = "") -> Optional[ElementSpec]:
    """单条 -> ElementSpec; 没有 eid 的直接丢 (下游引用不到它)"""
    raw = _dict(raw)
    eid = _text(raw.get("eid"))
    if not eid:
        return None

    attrs, aria = _dict(raw.get("attrs")), _dict(raw.get("aria"))
    attrs.pop("value", None)                   # 双保险: 采集器已拦过, 这里再拦一次 (PII)
    constraints = _constraints_of(raw.get("constraints"))
    locators, locator_by, primary, stability = _locator_view(raw)
    actions = tuple(a for a in (_text(x) for x in _items(raw.get("actions"))) if a)
    options = tuple(o for o in (_text(x, 40) for x in _items(raw.get("options"))) if o)

    return ElementSpec(
        eid=eid,
        tag=_text(raw.get("tag")),
        role=_text(raw.get("role")),
        name=_name_of(raw, attrs, aria),
        locators=locators,
        stability=stability,
        state=_state_of(raw.get("state"), constraints),
        constraints=constraints,
        container=_text(raw.get("container")) or "page",
        risk=_text(raw.get("risk")) or SAFE,
        kind=_text(raw.get("kind")) or kind_hint,
        type=_text(raw.get("type")),
        primary=primary,
        locator_by=locator_by,
        actions=actions,
        options=options,
        attrs=attrs,
        aria=aria,
        text=_text(raw.get("text"), 80),
        html=_text(raw.get("html")),
        depth=max(0, _int(raw.get("depth"))),
    )


def _raw_elements(payload: Mapping) -> List[Tuple[str, Any]]:
    """两种形状都吃: 采集器按类别分桶 (inputs/buttons/selects), to_dict 用 elements"""
    elements = payload.get("elements")
    if isinstance(elements, (list, tuple)):
        return [("", item) for item in elements]
    pairs: List[Tuple[str, Any]] = []
    for key, kind in (("inputs", "input"), ("buttons", "button"), ("selects", "select")):
        pairs.extend((kind, item) for item in _items(payload.get(key)))
    return pairs


# ---------------------------------------------------------------------------
# 派生视图: tables / dialogs / links / forms / stats
# ---------------------------------------------------------------------------

def _table_record(raw: Any) -> Optional[Dict[str, Any]]:
    record = _dict(raw)
    eid = _text(record.get("eid"))
    if not eid:
        return None
    record["eid"] = eid
    record["kind"] = "table"
    record["columns"] = [c for c in (_text(x, 40) for x in _items(record.get("columns"))) if c]
    record["row_count"] = max(0, _int(record.get("row_count")))
    record["sample_rows"] = [
        [_text(cell, 60) for cell in row][:20]
        for row in _items(record.get("sample_rows"))[:2]
        if isinstance(row, (list, tuple))
    ]
    actions = []
    for item in _items(record.get("row_actions"))[:20]:
        item = _dict(item)
        text = _text(item.get("text"), 40)
        if not text:
            continue
        actions.append({"text": text,
                        "eid": _text(item.get("eid")) or None,
                        "count": max(0, _int(item.get("count")))})
    record["row_actions"] = actions
    return record


def _dialog_record(raw: Any) -> Optional[Dict[str, Any]]:
    record = _dict(raw)
    eid = _text(record.get("eid"))
    if not eid:
        return None
    return {"eid": eid,
            "selector": _text(record.get("selector")),
            "text": _text(record.get("text"), 160),
            "buttons": [b for b in (_text(x, 40) for x in _items(record.get("buttons"))) if b][:8]}


def _links_of(elements: Sequence[ElementSpec]) -> Tuple[Dict[str, Any], ...]:
    """采集器把 a 并进 button 类 (href 仍在 attrs 里), links 从这里派生。

    javascript: 伪链接不算链接 —— 它点了不跳转, 归 button 的行为更接近实际。
    """
    out = []
    for element in elements:
        if element.tag != "a":
            continue
        href = _text(element.attrs.get("href"))
        if not href or href.lower().startswith("javascript:"):
            continue
        out.append({"eid": element.eid, "name": element.name, "href": href,
                    "container": element.container, "risk": element.risk})
    return tuple(out)


def _forms_of(elements: Sequence[ElementSpec]) -> Tuple[Dict[str, Any], ...]:
    """按容器聚出"表单"记录: 字段 / 动作 / 必填 / 危险动作。

    采集器只给容器**类别**不给 form 的身份, 所以同一容器下的两个 <form> 会被合成
    一条 —— 这是当前的已知限制 (要拆先给采集器加 form 指纹)。没有字段的容器
    (纯 toolbar) 不产记录: 那不是表单。
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for element in elements:
        if not (element.is_field or element.is_action):
            continue
        bucket = grouped.setdefault(element.container, {
            "container": element.container, "fields": [], "actions": [],
            "required": [], "risky": [],
        })
        if element.is_field:
            bucket["fields"].append(element.eid)
            if element.constraints.get("required"):
                bucket["required"].append(element.eid)
        else:
            bucket["actions"].append(element.eid)
        if element.is_risky:
            bucket["risky"].append(element.eid)

    def order(container: str) -> Tuple[int, str]:
        return (CONTAINER_ORDER.index(container) if container in CONTAINER_ORDER
                else len(CONTAINER_ORDER), container)

    forms = []
    for container in sorted(grouped, key=order):
        bucket = grouped[container]
        if not bucket["fields"]:
            continue
        bucket["field_count"] = len(bucket["fields"])
        bucket["has_required"] = bool(bucket["required"])
        forms.append(bucket)
    return tuple(forms)


def _signals_of(raw: Any) -> Dict[str, int]:
    """页面级信号 (charts 等): 给原型分类用, 只留非负整数"""
    signals: Dict[str, int] = {}
    for key, value in _dict(raw).items():
        name = _text(key)
        if name:
            signals[name] = max(0, _int(value))
    return signals


def _stats_of(elements: Sequence[ElementSpec], tables: Sequence[Mapping],
              links: Sequence[Mapping], dialogs: Sequence[Mapping],
              forms: Sequence[Mapping], payload: Mapping) -> Dict[str, int]:
    depths = [element.depth for element in elements]
    depths.extend(_int(table.get("depth")) for table in tables)
    return {
        "interactive_count": len(elements),
        "inputs": sum(1 for element in elements if element.kind == "input"),
        "buttons": sum(1 for element in elements if element.kind == "button"),
        "selects": sum(1 for element in elements if element.kind == "select"),
        "tables": len(tables),
        "links": len(links),
        "dialogs": len(dialogs),
        "forms": len(forms),
        "max_depth": max(depths) if depths else 0,
        "truncated": 1 if payload.get("truncated") else 0,
        "risky_write": sum(1 for element in elements if element.risk == "write"),
        "risky_destructive": sum(1 for element in elements if element.risk == "destructive"),
    }


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------

def canonical_dom(inventory: Any) -> str:
    """结构骨架的规范字符串 (指纹的原料)。

    只收"结构", 刻意丢掉: 行数、样例行内容、弹窗正文、单元素文本以外的数据、html、
    深度、eid 顺序。理由是缓存键的用途 —— 判"同一个页面", 而不是"同一次渲染":
    - 列表页多 20 条数据 / 换了批记录 -> 结构没变, 应该命中旧计划;
    - 按钮从"编辑/删除"变成"编辑/作废" -> name 变 -> 必须换计划;
    - 表格多一列、弹窗按钮变了 -> 必须换计划。
    多算 (miss) 只是浪费一次生成, 少算 (错命中) 会把旧计划套到新页面上, 后者更贵。
    排序后拼接: 元素顺序 / eid 重编号不影响指纹。
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    body = []
    for element in inv.elements:
        located = f"{element.locator_by.get(element.primary, '')}={element.locators.get(element.primary, '')}"
        body.append("|".join(("el", element.tag, element.role, element.container,
                              element.risk, element.name, located)))
    for table in inv.tables:
        body.append("|".join((
            "tb",
            ",".join(_text(column) for column in _items(table.get("columns"))),
            ",".join(sorted(_text(action.get("text")) for action in _items(table.get("row_actions")))),
        )))
    for dialog in inv.dialogs:
        body.append("|".join((
            "dlg", _text(dialog.get("selector")),
            ",".join(sorted(_text(button) for button in _items(dialog.get("buttons")))),
        )))
    body.sort()
    head = [FINGERPRINT_PREFIX, f"collector={COLLECTOR_VERSION}", "path=" + _path_of(inv.url),
            "signals=" + ",".join(f"{k}={v}" for k, v in sorted(inv.signals.items()))]
    return "\n".join(head + body)


def fingerprint_of(inventory: Any) -> str:
    """sha1(canonical_dom) —— 缓存键"""
    return hashlib.sha1(canonical_dom(inventory).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def from_inventory(raw: Any, *, page_name: str = "", captured_at: str = "",
                   archetype: str = "") -> PageInventory:
    """采集结果 -> PageInventory。

    吃三种输入: collector.collect 的 dict / to_dict 的往返结果 / 已经是
    PageInventory。None 或脏值 -> 空清单 (统计全 0), 从不抛。
    """
    if isinstance(raw, PageInventory):
        return replace(raw, archetype=archetype) if archetype and archetype != raw.archetype else raw

    payload = _dict(raw)
    stamp = captured_at or _text(payload.get("captured_at")) or _utcnow()

    elements: List[ElementSpec] = []
    seen: set = set()
    for kind, item in _raw_elements(payload):
        element = _build_element(item, kind_hint=kind)
        if element is None or element.eid in seen:
            continue
        seen.add(element.eid)
        elements.append(element)

    tables: List[Dict[str, Any]] = []
    for item in _items(payload.get("tables"))[:20]:
        record = _table_record(item)
        if record is None or record["eid"] in seen:
            continue
        seen.add(record["eid"])
        tables.append(record)

    dialogs: List[Dict[str, Any]] = []
    for item in _items(payload.get("dialogs"))[:3]:
        record = _dialog_record(item)
        if record is None or record["eid"] in seen:
            continue
        seen.add(record["eid"])
        dialogs.append(record)

    links = _links_of(elements)
    forms = _forms_of(elements)
    inventory = PageInventory(
        url=_text(payload.get("url")),
        title=_text(payload.get("title")),
        fingerprint="",
        archetype=archetype or _text(payload.get("archetype")),
        captured_at=stamp,
        elements=tuple(elements),
        forms=forms,
        tables=tuple(tables),
        links=links,
        dialogs=tuple(dialogs),
        stats=_stats_of(elements, tables, links, dialogs, forms, payload),
        signals=_signals_of(payload.get("signals")),
        page_name=page_name or _text(payload.get("page_name")),
        notes=tuple(n for n in (_text(x, 200) for x in _items(payload.get("notes"))) if n),
    )
    return replace(inventory, fingerprint=fingerprint_of(inventory))


def from_collect(driver, profile=None, *, page_name: str = "", captured_at: str = "",
                 archetype: str = "", **collect_options: Any) -> PageInventory:
    """一步到位: collector.collect + 定型。collect_options 透传给采集器
    (mark_eid / max_elements / max_html)。"""
    raw = collect(driver, profile, page_name=page_name, **collect_options)
    return from_inventory(raw, page_name=page_name, captured_at=captured_at,
                          archetype=archetype)
