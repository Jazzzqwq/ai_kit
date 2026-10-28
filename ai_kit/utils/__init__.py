# -*- coding: utf-8 -*-
"""
通用工具

    sanitize           : PII 脱敏 (手机号 / 身份证 / 姓名 / 病历号), 送 LLM 前必经
    sanitize_structure : 对嵌套 dict/list 里的每个字符串跑 sanitize —— 送模型的
                         清单摘要 (slim_inventory / slim_page_structure) 走这个
    dom_diff           : 操作前后 DOM 差异, 用于失败现场重建 (期望变化 vs 实际变化);
                         产物是给 proposal 报告与 LLM 的文本, 所以默认已脱敏

sanitize 的能力边界 (写清楚, 别当它是万能合规方案):
    - 认得出"有形态"的 PII (手机号 / 身份证) 和"有字段名提示"的 PII
      (value 属性, class/name/id 带 patient-name 之类, "姓名:" / "病历号:" 前缀);
      裸文本里孤零零两三个汉字 (表格姓名列没有 class 提示) 正则无法判定,
      宁可漏也不猜 —— 这层由采集侧的字段结构保证;
    - 只处理文本。详情页截图走的是 analyzer 多模态, 图片不在射程内,
      要合规只能裁切或干脆不传;
    - 保留 DOM 骨架, 只换值 (标签名 / 属性名 / class / 结构不变),
      所以 LLM 仍能据此推选择器 —— 这也是敢在出境前默认执行的前提。

纯 stdlib 正则, 无状态, 幂等 (对已脱敏文本再跑一遍结果不变)。
dom_diff 同理: stdlib difflib, 输入两段 HTML, 输出一段可直接塞进提示词的文本。
"""
from __future__ import annotations

import difflib
import html as _html
import re
from typing import Any, List, Match, Tuple

__all__ = ["REDACTED", "sanitize", "sanitize_structure", "dom_diff"]

REDACTED = "[REDACTED]"


# ---------------------------------------------------------------------------
# 规则表 — 顺序即优先级: 先抹属性值, 再抹元素内容, 最后扫自由文本
# ---------------------------------------------------------------------------

# value="..." / value='...' / value=xxx。只认裸 value, 放过 data-value / x-value
# 这类跟 PII 无关的属性; 引号原样保留, 免得 HTML 结构被改坏。
_VALUE_ATTR = re.compile(
    r'(?<![\w-])(value)(\s*=\s*)("[^"]*"|\'[^\']*\'|[^\s>/]+)', re.I)

# 逐标签处理属性: option/li 的 value 是选项标识 (不是用户数据), 留着给 LLM 认选项
_TAG = re.compile(r'<([a-zA-Z][\w-]*)\b([^>]*)>')
_VALUE_FREE_TAGS = frozenset({"option", "li", "param", "progress", "meter", "output", "data"})

_TEXTAREA = re.compile(r'(<textarea\b[^>]*>)([^<]*)(</textarea\s*>)', re.I)

# class / id / name / data-* 里带 PII 提示的元素, 其直接文本一律抹掉:
#   <span class="patient-name">张三</span>  <td data-field="idCard">110101...
_PII_HINTS = (
    "patient[-_ ]?name", "real[-_ ]?name", "user[-_ ]?name", "pname",
    "id[-_ ]?card", "idcard", "id[-_ ]?no", "cert[-_ ]?no", "passport",
    "phone", "mobile", "telephone", "medical[-_ ]?record", "mrn",
)
_PII_ELEMENT = re.compile(
    r'<([a-zA-Z][\w-]*)\b[^>]*?\b(?:class|id|name|data-[\w-]+)\s*=\s*"[^"]*(?:'
    + "|".join(_PII_HINTS)
    + r')[^"]*"[^>]*>([^<]{1,80})</\1\s*>',
    re.I)

# 字段名 + 值: 姓名 / 病历号这类本身没有自证形状的字段, 只能靠 key 兜
# "患者" / "就诊人" 单拎出来是因为只读详情页常写 "就诊人 | 赵六"
_NAME_KEYS = (r"患者姓名|联系人姓名|家属姓名|就诊人|患者|病人|联系人|监护人|"
              r"主治医师|主管医师|医师|医生|护士|操作人|经办人|姓\s*名")
_NAME_VALUE = re.compile(rf'({_NAME_KEYS})(\s*[:：=]\s*|\s+)([\u4e00-\u9fa5·]{{2,6}})')

# <label>姓名</label><div class="x">张三</div> — 值在别的标签里
_NAME_LABEL = re.compile(
    rf'({_NAME_KEYS})\s*[:：]?\s*</(?:label|span|div|td|th|p)>\s*'
    r'(?:<[a-zA-Z][^>]*>\s*){0,3}([\u4e00-\u9fa5·]{2,6})')

_RECORD_KEYS = (r"病历号|病案号|住院号|门诊号|就诊号|就诊卡号|医保号|医保卡号|社保号|"
                r"检查号|检验号|报告单号")
_RECORD_VALUE = re.compile(
    rf'({_RECORD_KEYS})(\s*[:：=]?\s*)([A-Za-z0-9][A-Za-z0-9\-]{{3,31}})')

# <span>住院号：</span><span>ZY2024090007</span> — 只读详情页里值在下一个标签
_RECORD_LABEL = re.compile(
    rf'({_RECORD_KEYS})\s*[:：]?\s*</(?:label|span|div|td|th|p)>\s*'
    r'(?:<[a-zA-Z][^>]*>\s*){0,3}([A-Za-z0-9][A-Za-z0-9\-]{3,31})')

# 身份证 18 位 (末位可为 X) 与老式 15 位; 先 18 后 15, 免得长号被短号切碎
_ID_CARD = re.compile(r'(?<![\dA-Za-z])\d{17}[\dXx](?![\dA-Za-z])')
_ID_CARD_15 = re.compile(r'(?<![\dA-Za-z])\d{15}(?![\dA-Za-z])')

# 手机号: 11 位, 允许 138-1234-5678 / 138 1234 5678 这种分隔写法
_PHONE = re.compile(r'(?<!\d)1[3-9]\d(?:[- ]?\d{4}){2}(?!\d)')


# ---------------------------------------------------------------------------
# 替换辅助
# ---------------------------------------------------------------------------

def _swap(match: Match[str], group: int, replacement: str) -> str:
    """把匹配文本里第 group 组的内容换成 replacement, 其余字节原样保留"""
    whole = match.group(0)
    start = match.start(group) - match.start(0)
    return whole[:start] + replacement + whole[start + len(match.group(group)):]


def _already(match: Match[str], group: int) -> bool:
    """该组已经是脱敏占位符 (幂等): 再抹一次会把 [REDACTED] 套娃"""
    return REDACTED in (match.group(group) or "")


def _redact_value(m: Match[str]) -> str:
    if _already(m, 3):
        return m.group(0)
    raw = m.group(3)
    quote = raw[0] if raw[:1] in ('"', "'") else ""
    return f"{m.group(1)}{m.group(2)}{quote}{REDACTED}{quote}"


def _redact_tag(m: Match[str]) -> str:
    """抹单个开标签订单里的 value 属性 (选项类标签不动)"""
    if m.group(1).lower() in _VALUE_FREE_TAGS:
        return m.group(0)
    attrs = _VALUE_ATTR.sub(_redact_value, m.group(2))
    return m.group(0) if attrs == m.group(2) else f"<{m.group(1)}{attrs}>"


def _redact_text(m: Match[str], group: int = 2) -> str:
    """抹掉标签内容 (textarea / 带 PII 提示的元素), 空白内容不动"""
    body = m.group(group)
    if not body.strip() or _already(m, group):
        return m.group(0)
    return _swap(m, group, REDACTED)


def _redact_keyed(m: Match[str]) -> str:
    """关键词 + 分隔符 + 值: 只换值, 字段名留着 (LLM 靠它认字段用途)"""
    if _already(m, 3):
        return m.group(0)
    return _swap(m, 3, REDACTED)


def _redact_labeled(m: Match[str]) -> str:
    """<label>姓名</label>...张三: 只换值 (第 2 组)"""
    if _already(m, 2):
        return m.group(0)
    return _swap(m, 2, REDACTED)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def sanitize(text: str) -> str:
    """送 LLM 前把 PII 抹掉, 返回新字符串 (不改入参)。

    覆盖: HTML value 属性 / textarea 内容 / 带 PII 提示的元素内文 /
    姓名 (字段名或 label 后面) / 病历号一类业务编号 / 手机号 / 身份证。

    非 str (None / bytes) 容错: None -> "", bytes 按 utf-8 解; 其余 str() 兜底,
    因为自愈链路宁可少一个字符, 也不该在拼提示词时抛异常。
    """
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    elif not isinstance(text, str):
        text = "" if text is None else str(text)
    if not text:
        return text

    out = _TAG.sub(_redact_tag, text)
    out = _TEXTAREA.sub(_redact_text, out)
    # 字段名 + 值先跑: 这样 <span class="mrn">病历号：MR001</span> 保留字段名,
    # 只抹值; 否则会被下面的元素内文规则整段抹掉, LLM 就不知道那格是啥了。
    out = _NAME_VALUE.sub(_redact_keyed, out)
    out = _NAME_LABEL.sub(_redact_labeled, out)
    out = _RECORD_VALUE.sub(_redact_keyed, out)
    out = _RECORD_LABEL.sub(_redact_labeled, out)
    out = _PII_ELEMENT.sub(_redact_text, out)
    out = _ID_CARD.sub(REDACTED, out)
    out = _ID_CARD_15.sub(REDACTED, out)
    out = _PHONE.sub(REDACTED, out)
    return out


def sanitize_structure(value: Any) -> Any:
    """递归把结构里每个字符串过一遍 sanitize —— 给「送 LLM 的清单摘要」用

    slim_inventory / slim_page_structure 的产物是嵌套 dict: 逐字符串过筛比
    "json.dumps 完再整体跑一次"稳 —— 键名与结构原样保留, 也不会因为两段文本
    拼接后恰好凑出手机号形状而误抹。

    只递归 dict / list / tuple / str; 数字、布尔、None 原样返回 —— 行数、eid
    编号这些不是 PII, 转成字符串反而破坏下游的类型判断。返回新对象不改入参;
    sanitize 幂等, 所以本函数也幂等。
    """
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, dict):
        return {key: sanitize_structure(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(sanitize_structure(item) for item in value)
    if isinstance(value, list):
        return [sanitize_structure(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# DOM 差异 (失败现场重建)
# ---------------------------------------------------------------------------
#
# 只比两样东西, 都是"用户/模型能直接看懂"的粒度:
#   1. 可见文本行 —— 去掉标签后按块级元素断行, 反映"页面上多了/少了什么字";
#   2. 标签签名   —— tag + role/type/name, 反映"结构多了/少了一个按钮"。
# class 刻意不进签名: 重渲染时 class 顺序、哈希类名最吵, 一加进来 diff 全是噪声;
# 文本行已经能反映内容变化, 不需要靠 class 再猜一遍。

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
_BLOCK_END = re.compile(
    r"</(?:div|p|li|tr|td|th|h[1-6]|section|article|header|footer|button|label|"
    r"option|a)\s*>|<br\s*/?>", re.I)
_ANY_TAG = re.compile(r"<[^>]*>", re.S)
_TAG_OPEN = re.compile(r"<([a-zA-Z][\w-]*)\b([^>]*)>", re.S)
_ATTR = re.compile(r"([\w:-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
_WHITESPACE = re.compile(r"\s+")
# 签名里留哪些属性: role/type 决定控件性质, name 是表单字段名 (与 PII 无关)
_SIG_ATTRS = ("role", "type", "name")


def _as_html(value: Any) -> str:
    """宽容取 HTML 文本: None/bytes/脏数据一律降级成空串, 这里不抛"""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _visible_lines(markup: str) -> List[str]:
    """HTML -> 可见文本行 (块级结束标签断行, 折叠空白, 丢掉空行)"""
    text = _SCRIPT_STYLE.sub(" ", markup)      # 脚本样式里的字符串不是"看得见的"
    text = _BLOCK_END.sub("\n", text)
    text = _html.unescape(_ANY_TAG.sub("", text))
    lines = (_WHITESPACE.sub(" ", row).strip() for row in text.splitlines())
    return [row for row in lines if row]


def _tag_signatures(markup: str) -> List[str]:
    """HTML -> 标签签名序列: <tag role=.. type=.. name=..>"""
    text = _SCRIPT_STYLE.sub(" ", markup)
    out: List[str] = []
    for match in _TAG_OPEN.finditer(text):
        attrs = {key.lower(): raw.strip("\"'")[:20]
                 for key, raw in _ATTR.findall(match.group(2))}
        parts = [f"<{match.group(1).lower()}"]
        parts += [f" {key}={attrs[key]}" for key in _SIG_ATTRS if attrs.get(key)]
        out.append("".join(parts) + ">")
    return out


def _diff_sequences(before: List[str], after: List[str]
                    ) -> Tuple[List[str], List[str], List[Tuple[str, str]]]:
    """(新增, 消失, 变化) —— 用 difflib 的 opcodes, 变化按位配对"""
    added: List[str] = []
    removed: List[str] = []
    changed: List[Tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            added.extend(after[j1:j2])
        elif tag == "delete":
            removed.extend(before[i1:i2])
        elif tag == "replace":
            left, right = before[i1:i2], after[j1:j2]
            for index in range(min(len(left), len(right))):
                changed.append((left[index], right[index]))
            removed.extend(left[len(right):])
            added.extend(right[len(left):])
    return added, removed, changed


def _diff_section(title: str, added: List[str], removed: List[str],
                  changed: List[Tuple[str, str]], limit: int) -> List[str]:
    """一段差异 -> markdown 行; 每类最多列 limit 条, 剩下的只报条数"""
    if not (added or removed or changed):
        return []
    lines = [f"## {title} (+{len(added)} -{len(removed)} ~{len(changed)})"]
    lines += [f"+ {item}" for item in added[:limit]]
    if len(added) > limit:
        lines.append(f"+ ... 还有 {len(added) - limit} 条新增")
    lines += [f"- {item}" for item in removed[:limit]]
    if len(removed) > limit:
        lines.append(f"- ... 还有 {len(removed) - limit} 条消失")
    lines += [f"~ {old} -> {new}" for old, new in changed[:limit]]
    if len(changed) > limit:
        lines.append(f"~ ... 还有 {len(changed) - limit} 条变化")
    return lines


def dom_diff(before: Any, after: Any, *, limit: int = 20,
             redact: bool = True) -> str:
    """操作前后 DOM 的可见差异 —— 失败现场重建 (期望变化 vs 实际变化)

    输入两段 HTML (通常是同一个容器操作前后的 outerHTML, 也接受纯文本),
    输出一段可直接塞进提示词的 markdown, 分「可见文本」与「结构」两段。
    没有变化时返回一句话说明, 不是空串 —— 空串在下游会被当成"没采集"。

    redact: 默认 True。这段文本的用途就是给 proposal 报告和 LLM 重建现场,
    属于出境点, 与 healer L4 同源, 所以在这里统一脱敏, 不指望每个调用点记得。

    边界: 不比样式、不比像素、不比属性顺序; 只回答"字变了没、结构变了没"。
    """
    old_html, new_html = _as_html(before), _as_html(after)
    sections: List[str] = []

    old_lines, new_lines = _visible_lines(old_html), _visible_lines(new_html)
    sections += _diff_section("可见文本", *_diff_sequences(old_lines, new_lines), limit)

    old_tags, new_tags = _tag_signatures(old_html), _tag_signatures(new_html)
    sections += _diff_section("结构", *_diff_sequences(old_tags, new_tags), limit)

    if not sections:
        return "未检测到可见变化 (文本与结构都一样)"
    text = "\n".join(sections)
    return sanitize(text) if redact else text
