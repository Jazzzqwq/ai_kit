# -*- coding: utf-8 -*-
"""
通用工具

    sanitize  : PII 脱敏 (手机号 / 身份证 / 姓名 / 病历号), 送 LLM 前必经
    dom_diff  : 操作前后 DOM 差异, 用于失败现场重建 (期望变化 vs 实际变化)
                —— 目前只有声明, 还没实现; 下面能用的只有 sanitize

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
"""
from __future__ import annotations

import re
from typing import Match

__all__ = ["REDACTED", "sanitize"]

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
_NAME_KEYS = (r"患者姓名|联系人姓名|家属姓名|监护人|主治医师|主管医师|医师|医生|护士|"
              r"操作人|经办人|姓\s*名")
_NAME_VALUE = re.compile(rf'({_NAME_KEYS})(\s*[:：=]\s*|\s+)([\u4e00-\u9fa5·]{{2,6}})')

# <label>姓名</label><div class="x">张三</div> — 值在别的标签里
_NAME_LABEL = re.compile(
    rf'({_NAME_KEYS})\s*[:：]?\s*</(?:label|span|div|td|th|p)>\s*'
    r'(?:<[a-zA-Z][^>]*>\s*){0,3}([\u4e00-\u9fa5·]{2,6})')

_RECORD_KEYS = (r"病历号|病案号|住院号|门诊号|就诊号|就诊卡号|医保号|医保卡号|社保号|"
                r"检查号|检验号|报告单号")
_RECORD_VALUE = re.compile(
    rf'({_RECORD_KEYS})(\s*[:：=]?\s*)([A-Za-z0-9][A-Za-z0-9\-]{{3,31}})')

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
    out = _PII_ELEMENT.sub(_redact_text, out)
    out = _ID_CARD.sub(REDACTED, out)
    out = _ID_CARD_15.sub(REDACTED, out)
    out = _PHONE.sub(REDACTED, out)
    return out
