# -*- coding: utf-8 -*-
"""
用例生成 prompt — 约束写死在 system, 事实全部放 user, 错误原文回填 (5.10)

五条约束, 每条都对应一个具体的失败模式:

1. **system 固定声明「只能用给定的 eid, 禁止发明元素/方法」**。模型在自由发挥时
   最喜欢补一个看起来合理的 e29 或 page.search_by_magic(); 这两个错误在 ③ 会被
   拦下, 但那时已经花了一次调用。约束前置到 system 才是省钱的写法。

2. **user 只含五块事实**: 页面原型 / 策略 checklist / 元素清单 JSON / PO 方法签名
   (仅 po 模式) / 输出 schema。多一块就多一个漂移源 —— 早先的 prompt 里塞了
   自然语言描述的页面结构和"参考代码", 模型会照着参考代码的法子编方法名。
   另外两段 (本轮要改的用例 / 上次校验错误) 是「任务指令」不是事实: 重试时必须
   带上, 否则模型不知道改哪一条、改成什么。

3. **temperature=0.1, max_tokens 显式给**。不显式给 max_tokens 就等于把这次调用
   的上限交给服务端默认值, 一份跑飞的 JSON 可能把预算一次烧穿。

4. **两步法不变**: 模型只产「用例清单 JSON」, 代码由 renderer 模板渲染。prompt 里
   绝不能出现"写一个 pytest 函数"—— 一旦模型开始产代码, 定位串、等待时间、隐式睡眠
   就都回到它手里, 5.8 的 locators.json 与 5.9 的缓存全部失效。清单可缓存、可评测、
   可人工审, 这正是两步法的价值。

5. **重试回填原文**。把 `步骤 2 (click): 清单里没有 e99` 原样贴回去, 比转述成
   "有些 eid 不对" 有效得多; 附带一行归纳 (`以下 eid 不存在: e99`) 是为了让模型
   一眼看到类别, 原文是为了让它看到位置。

改这里的任何一句 prompt 都要 +planner.PLANNER_VERSION (就在 planner.py 定义) ——
缓存键带着它, 否则旧计划会继续被复用, 你会以为 prompt 没生效。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ai_kit.utils import sanitize_structure

# 生成代码用例必须保守: 0.1 足够稳定, 再高就会出现"灵机一动"的步骤
TEMPERATURE = 0.1
# 显式上限: 只改没过的用例, 2000 token 够写几十条; 可用环境变量覆盖
MAX_TOKENS_ENV = "AI_KIT_LLM_MAX_TOKENS"
DEFAULT_MAX_TOKENS = 2000
# 清单 JSON 里每条元素最多带几个选项/约束 —— 全带上会把 prompt 撑成字典
MAX_OPTIONS = 8
# 策略档位 -> 计划里的优先级 (strategies 里 MUST/SHOULD/SLOW 就是 1/2/3)
_PRIORITY = {1: "P1", 2: "P2", 3: "P3"}


# ---------------------------------------------------------------------------
# system: 只放约束, 不放本页事实
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = """你在为一套 UI 自动化框架生成测试用例清单, 只输出 JSON, 不写代码。

硬约束:
1. 只能使用 user 消息「元素清单」里出现过的 eid; 清单里没有的元素一律不许使用。
2. 只能使用「PO 方法签名」里列出的方法 (给了这一段时); 没列出的方法名一律不许出现。
3. 不许发明元素、方法、定位串、超时或等待; 也不许输出任何 Python 代码 (import / def /
   find_element / execute_script 都不行) —— 代码由渲染器生成, 你只负责清单。
4. 每个 step 只能是 {"action", "eid", "value"} 三个键, action 必须取自「输出 schema」
   给出的动作词表; 缺字段就少写这一步, 不要猜。
5. 不清楚的用例可以不写。少一条用例, 好过编一条永远红的用例。

输出 schema:
{"cases": [{"case_id": "LC-001",
            "steps": [{"action": "fill", "eid": "<清单里的 eid>", "value": "x"},
                      {"action": "click", "eid": "<清单里的 eid>"}],
            "cleanup": [],
            "reason": "一句话说明为什么这么改"}]}
只返回你确实改过的用例, 没改的不要出现在结果里。"""


def max_tokens() -> int:
    """本次调用显式带上的上限; 配错就退回默认值 (prompt 不该因配置崩掉)"""
    raw = (os.environ.get(MAX_TOKENS_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_TOKENS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_TOKENS
    return value if value > 0 else DEFAULT_MAX_TOKENS


# ---------------------------------------------------------------------------
# user: 五块事实
# ---------------------------------------------------------------------------

def _bullet(text: str) -> str:
    return f"- {text}"


def archetype_block(archetype: str) -> str:
    return f"## 页面原型\n{archetype or '(未分类)'}"


def checklist_block(strategies: Iterable[Any]) -> str:
    """策略 checklist: 该原型要测什么 (模型不该自己发明测试点)

    strategies 是 strategies.Strategy 的可鸭子类型序列; 这里只读属性, 不 import,
    免得模板层反向依赖策略库。
    """
    lines: List[str] = ["## 策略 checklist"]
    for strategy in strategies or ():
        variants = ", ".join(str(getattr(item, "key", "") or getattr(item, "title", ""))
                             for item in getattr(strategy, "variants", ()) or ())
        detail = [f"目标 {getattr(strategy, 'target', '')}"]
        if variants:
            detail.append(f"变体 {variants}")
        # 策略的 priority 是 strategies 里的整数档位 (1/2/3), 模型只认 P1..P3
        priority = _PRIORITY.get(getattr(strategy, "priority", 0), "")
        if priority:
            detail.append(priority)
        goal = str(getattr(strategy, "goal", "") or "")
        if goal:
            detail.append(goal)
        note = str(getattr(strategy, "note", "") or "")
        if note:
            detail.append(note)
        group = str(getattr(strategy, "group", "") or "")
        title = str(getattr(strategy, "title", "") or getattr(strategy, "key", ""))
        label = f"[{group}] " if group else ""
        lines.append(_bullet(f"{label}{title} ({'; '.join(detail)})"))
    if len(lines) == 1:
        lines.append("- (该原型没有策略清单, 按元素清单里的可交互控件保守生成)")
    return "\n".join(lines)


def inventory_block(inventory: Any) -> str:
    """元素清单 JSON: 已经是序列化好的就原样用, 是对象就 dump"""
    if isinstance(inventory, str) and inventory.strip():
        text = inventory
    else:
        text = json.dumps(inventory if inventory is not None else {},
                          ensure_ascii=False, indent=2)
    return f"## 元素清单 (JSON)\n```json\n{text}\n```"


def po_block(methods: Iterable[str] = (), signatures: Iterable[str] = ()) -> str:
    """PO 方法签名 (po 模式才有); 非 po 模式整段省略 —— 写「不适用」也是噪音"""
    rows = [str(item) for item in signatures or ()]
    if not rows:
        # 只给了方法名时写 name(...), 不写 name(): 后者是在替模型断言「这个方法零参数」,
        # 而 ③ 只拦得住不存在的方法名, 拦不住参数个数传错。
        rows = [f"{str(name)}(...)" for name in methods or ()]
    if not rows:
        return ""
    body = "\n".join(_bullet(row) for row in rows)
    return ("## PO 方法签名 (只能用这些)\n"
            f"{body}\n"
            "调用时按这里给出的参数顺序传值; 没列出的方法一律不许使用。")


def actions_block(actions: Iterable[Any]) -> str:
    """输出 schema 的取值域: action 词表 (name / eid / value / 一句话说明)"""
    lines: List[str] = ["## 输出 schema", "动作词表 (action 只能取这些):"]
    for action in actions or ():
        eid = {"req": "必须", "opt": "可选", "none": "不要"}
        value = {"req": "必须", "opt": "可选", "": "不要"}
        lines.append(_bullet(
            f"{getattr(action, 'name', '')}: {getattr(action, 'doc', '')}"
            f" | eid {eid.get(getattr(action, 'eid', 'none'), '不要')}"
            f" | value {value.get(getattr(action, 'value', ''), '不要')}"))
    lines.append("")
    lines.append('返回 {"cases": [{"case_id", "steps", "cleanup", "reason"}]}, '
                 "steps 里每一步只含 action / eid / value。")
    return "\n".join(lines)


def cases_block(cases: Iterable[Mapping[str, Any]]) -> str:
    """本轮要改的用例 (改写模式才给): 只改步骤, case_id/title/期望是骨架"""
    rows = list(cases or ())
    if not rows:
        return ""
    text = json.dumps(rows, ensure_ascii=False, indent=2)
    return ("## 本轮要改的用例 (只能改 steps/cleanup, 不能改 case_id/title/期望)\n"
            f"```json\n{text}\n```")


# ---------------------------------------------------------------------------
# 第 5 条: 校验错误原文回填
# ---------------------------------------------------------------------------

# 从 planner 的问题文案里认出「这个 eid 不在清单里」「这个方法不存在」。
# 文案有两种来源, 形状不同, 都要认 (改 planner 的报错文案时必须回来对一遍):
#   validate_step:  click 的 eid 'e99' 不在元素清单里
#   check_live:     e99: 清单里没有这个引用
#   lint_source:    search_by_name() 不在白名单: po 模式只准用 runtime 原语 ...
#   lint_source:    .foo_bar() 不是 pages/ 里的 PO 方法 (...)
_EID_QUOTED = re.compile(r"eid '([^']+)' 不在元素清单里")
_EID_REF = re.compile(r"^([A-Za-z][\w.:]*): 清单里没有")
_METHOD_BARE = re.compile(r"([A-Za-z_]\w*)\(\) 不在白名单")
_METHOD_ATTR = re.compile(r"\.([A-Za-z_]\w*)\(\) (?:不是 pages/ 里的 PO 方法|越界)")


def _unique(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(item for item in values if item))


def summarize_errors(errors: Mapping[str, Sequence[str]]
                    ) -> Tuple[List[str], List[str]]:
    """错误清单 -> (不存在的 eid, 不存在的方法)

    只做「归类提示」, 不做改写: 认不出来的问题不进归纳句, 仍然以原文形式给模型。
    硬凑归纳句会让模型以为「只有这一类问题」, 反而漏改。
    """
    eids: List[str] = []
    methods: List[str] = []
    for messages in errors.values():
        for message in messages or ():
            text = str(message)
            eids.extend(match.group(1) for match in _EID_QUOTED.finditer(text))
            eids.extend(match.group(1) for match in _EID_REF.finditer(text))
            methods.extend(match.group(1) for match in _METHOD_BARE.finditer(text)
                           if match.group(1))
            methods.extend(match.group(1) for match in _METHOD_ATTR.finditer(text)
                           if match.group(1))
    return _unique(eids), _unique(methods)


def errors_block(errors: Optional[Mapping[str, Sequence[str]]]) -> str:
    """上一轮没通过的地方: 归纳句 + 原文行

    原文行形如 `- LC-002: 步骤 2 (click): click 的 eid 'e99' 不在元素清单里` ——
    位置和原因都要在, 只给一句「e99 不存在」模型不知道该改哪条用例的哪一步。
    """
    if not errors:
        return ""
    lines = ["## 上次校验没通过的地方 (只改这些, 没列出的步骤别动)"]
    eids, methods = summarize_errors(errors)
    if eids:
        lines.append(f"以下 eid 不存在: {', '.join(eids)}")
    if methods:
        lines.append(f"以下 PO 方法不存在: {', '.join(methods)}")
    originals = [f"- {case_id}: {message}"
                 for case_id, messages in errors.items() for message in messages or ()]
    if originals:
        lines.append("原文:")
        lines.extend(originals)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 拼装
# ---------------------------------------------------------------------------

def build_user_prompt(*, archetype: str, checklist: Iterable[Any] = (),
                      inventory: Any = None, actions: Iterable[Any] = (),
                      po_methods: Iterable[str] = (), po_signatures: Iterable[str] = (),
                      cases: Iterable[Mapping[str, Any]] = (),
                      errors: Optional[Mapping[str, Sequence[str]]] = None) -> str:
    """五块事实 (顺序固定) + 可选的「本轮用例」与「上轮错误」

    顺序固定是为了让缓存与人工审阅都能按段取: 换了顺序, diff 会显示整段都变了。
    """
    blocks = [
        archetype_block(archetype),
        checklist_block(checklist),
        inventory_block(inventory),
        po_block(po_methods, po_signatures),
        actions_block(actions),
        cases_block(cases),
        errors_block(errors),
    ]
    return "\n\n".join(block for block in blocks if block)


def slim_inventory(inventory: Any, *, max_items: int = 40,
                   max_options: int = MAX_OPTIONS) -> Dict[str, Any]:
    """PageInventory -> 给模型看的清单摘要 (丢 html/定位串, 只留能决策的字段)

    定位串绝不给模型: 它会忍不住把 xpath 抄进 value 里, 而计划里只准有 eid。

    出门前统一过 sanitize_structure: 元素可见文本 (name / options) 里可能就有
    姓名手机号, 而这一层是唯一把所有字段汇到一起的地方 —— 在这里脱敏一次,
    比让每个调用方自己记得脱敏可靠。
    """
    def element(spec: Any) -> Dict[str, Any]:
        row: Dict[str, Any] = {
            "eid": getattr(spec, "eid", ""),
            "kind": getattr(spec, "kind", ""),
            "type": getattr(spec, "type", ""),
            "name": getattr(spec, "name", ""),
            "container": getattr(spec, "container", ""),
            "risk": getattr(spec, "risk", ""),
        }
        options = list(getattr(spec, "options", ()) or ())[:max_options]
        if options:
            row["options"] = options
        constraints = dict(getattr(spec, "constraints", {}) or {})
        if constraints:
            row["constraints"] = constraints
        return row

    def table(item: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "eid": item.get("eid"),
            "columns": list(item.get("columns") or ())[:10],
            "row_count": item.get("row_count"),
            "row_actions": [dict(action) for action in (item.get("row_actions") or ())[:8]],
        }

    return sanitize_structure({
        "url": getattr(inventory, "url", ""),
        "title": getattr(inventory, "title", ""),
        "archetype": getattr(inventory, "archetype", ""),
        "signals": dict(getattr(inventory, "signals", {}) or {}),
        "elements": [element(spec) for spec in getattr(inventory, "elements", ())[:max_items]],
        "tables": [table(item) for item in getattr(inventory, "tables", ())],
        "dialogs": [{"eid": dialog.get("eid"),
                     "buttons": list(dialog.get("buttons") or ())}
                    for dialog in getattr(inventory, "dialogs", ())],
        "notes": list(getattr(inventory, "notes", ()) or ())[:5],
    })


def output_schema() -> Dict[str, Any]:
    """机器可读的 schema 摘要 (测试/文档用; prompt 里用的是同一份字段名)"""
    return {"cases": [{"case_id": "str", "steps": [{"action": "str", "eid": "str",
                                                    "value": "any?"}],
                       "cleanup": "list", "reason": "str"}]}
