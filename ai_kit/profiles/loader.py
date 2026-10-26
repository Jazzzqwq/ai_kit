# -*- coding: utf-8 -*-
"""
规则包加载器 — 把"框架/站点知识"从代码搬进 JSON

    builtin/*.json   框架通用知识 (generic / element_plus / antd), 随包发布
    sites/*.json     站点知识, 覆盖内置包 (含业务类名/账号的不要提交)

支持的键 (缺键 / 类型不对 / 单条写错一律当没有 —— 规则包写错绝不能拖垮自愈):
    schema              int, 目前只认 1
    name / description  元信息, 不参与匹配
    l1_rules            {坏定位器: {by, locator, reason} 或 [by, locator, reason]}
    semantic_hints      [[正则, 语义], ...] 把定位器文本映射到语义类别
    semantic_candidates {语义: [css, ...]}
    intent_keywords     {confirm|cancel|close: [同义词, ...]}
    intent_selectors    {confirm|cancel|close: css}
    l3_containers       [css, ...]  生成相对 XPath 前先确认存在的容器
    l4_containers       [css, ...]  抓给 LLM 的容器, 支持 [role=dialog] 这类通用模式

只依赖 stdlib json —— 不引 PyYAML (任务约束), 规则包一律 JSON。
"""
from __future__ import annotations

import copy
import json
import os
import re
import warnings
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

SCHEMA_VERSION = 1

_HERE = os.path.dirname(os.path.abspath(__file__))
BUILTIN_DIR = os.path.join(_HERE, "builtin")
SITES_DIR = os.path.join(_HERE, "sites")

TABLE_KEYS: Tuple[str, ...] = ("l1_rules", "semantic_candidates",
                               "intent_keywords", "intent_selectors")
LIST_KEYS: Tuple[str, ...] = ("semantic_hints", "l3_containers", "l4_containers")
RULE_KEYS: Tuple[str, ...] = TABLE_KEYS + LIST_KEYS
META_KEYS: Tuple[str, ...] = ("schema", "name", "description")
KNOWN_KEYS: Tuple[str, ...] = META_KEYS + RULE_KEYS

# 默认规则包: generic 打底, element_plus 覆盖 (越靠后越优先)。
# 顺带说明了为什么 element_plus 是"默认"而不是 generic —— 本框架的主战场就是
# Element Plus 站点, 且历史上的硬编码规则全部来自这里, 搬移后候选序列必须不变。
DEFAULT_BUILTIN: Tuple[str, ...] = ("generic", "element_plus")

# 不传 profile 时可用的环境变量 (路径或包名)。项目升级到规则外置版后, 只要写
# AI_KIT_PROFILE=./profiles/my_site.json 就能把站点知识加回来, 不用改代码。
PROFILE_ENV = "AI_KIT_PROFILE"


class ProfileError(ValueError):
    """规则包读不了 / 结构不对 —— 只有显式 load_profile 会抛, 内部一律降级"""


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def _empty_rules() -> Dict[str, Any]:
    """所有键补齐的空规则包 (表给 {}, 数组给 [])"""
    return {key: ({} if key in TABLE_KEYS else []) for key in RULE_KEYS}


def _resolve_path(source: str) -> str:
    """'element_plus' → builtin/element_plus.json, 找不到再看 sites/; 带 .json 或分隔符当路径"""
    text = source.strip()
    if text.endswith(".json") or os.sep in text or "/" in text:
        return text
    path = os.path.join(BUILTIN_DIR, text + ".json")
    if os.path.exists(path):
        return path
    return os.path.join(SITES_DIR, text + ".json")


def load_profile(source: Union[str, os.PathLike, Mapping]) -> Dict[str, Any]:
    """严格加载规则包。

    source 可以是: 已解析的 dict / 规则包路径 / 内置包名 (generic, element_plus, antd)
    或站点包名 (不带 .json)。读不了、JSON 语法错、顶层不是 object、类型根本不是
    路径都抛 ProfileError —— 显式调用方 (命令行、自检、单测) 需要知道哪里写错了。
    """
    if isinstance(source, Mapping):
        return copy.deepcopy(dict(source))
    try:
        path = _resolve_path(os.fspath(source))
    except TypeError as ex:
        raise ProfileError(f"规则包必须是 dict / 路径 / 包名: {source!r}") from ex
    if not os.path.exists(path):
        raise ProfileError(f"规则包不存在: {source}")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as ex:
        raise ProfileError(f"规则包 JSON 语法错: {path} ({ex})") from ex
    except OSError as ex:
        raise ProfileError(f"规则包读不了: {path} ({ex})") from ex
    if not isinstance(data, dict):
        raise ProfileError(f"规则包顶层必须是 JSON object: {path}")
    return data


def resolve_profile(source: Union[str, os.PathLike, Mapping, None]) -> Dict[str, Any]:
    """宽松加载: 任何错误都退回 {} 并 warn —— healer 内部用这个, 不能因为规则包写错就中断自愈"""
    if source is None:
        return {}
    try:
        return load_profile(source)
    except ProfileError as ex:
        warnings.warn(str(ex), RuntimeWarning, stacklevel=2)
        return {}


def builtin_names() -> List[str]:
    """builtin/ 下可用的规则包名 (按文件名排序)"""
    try:
        files = os.listdir(BUILTIN_DIR)
    except OSError:
        return []
    return sorted(f[:-5] for f in files if f.endswith(".json"))


def site_names() -> List[str]:
    """sites/ 下已存在的站点 profile 名 (只列文件名, 不读内容)"""
    try:
        files = os.listdir(SITES_DIR)
    except OSError:
        return []
    return sorted(f[:-5] for f in files if f.endswith(".json"))


# ---------------------------------------------------------------------------
# 校验: 返回人可读的问题列表, 不抛异常
# ---------------------------------------------------------------------------

def _as_text(value: Any) -> Optional[str]:
    """非空字符串归一; 其他类型 (含纯空白) 返回 None"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def validate_profile(data: Any) -> List[str]:
    """校验规则包结构, 返回问题列表 (空列表 = 干净)。只报告不修改, 供自检/单测使用"""
    if not isinstance(data, dict):
        return ["顶层必须是 JSON object"]

    problems: List[str] = []

    schema = data.get("schema", SCHEMA_VERSION)
    if not isinstance(schema, int) or isinstance(schema, bool):
        problems.append(f"schema 必须是整数, 实际 {schema!r}")
    elif schema > SCHEMA_VERSION:
        problems.append(f"schema={schema} 比本框架支持的 {SCHEMA_VERSION} 新, 新键会被忽略")

    unknown = sorted(k for k in data
                     if k not in KNOWN_KEYS and not str(k).startswith("_"))
    if unknown:
        problems.append(f"未知键 {unknown} 会被忽略")

    for name in ("name", "description"):
        if name in data and not isinstance(data[name], str):
            problems.append(f"{name} 必须是字符串")

    l1 = data.get("l1_rules")
    if l1 is not None:
        if not isinstance(l1, dict):
            problems.append("l1_rules 必须是 object: {坏定位器: 规则}")
        else:
            for broken, rule in l1.items():
                if _as_text(broken) is None:
                    problems.append(f"l1_rules 的键必须是非空字符串: {broken!r}")
                if isinstance(rule, dict):
                    loc = rule.get("locator") or rule.get("selector")
                elif isinstance(rule, (list, tuple)) and len(rule) >= 2:
                    loc = rule[1]
                else:
                    problems.append(
                        f"l1_rules[{broken!r}] 必须是 {{by, locator, reason}} 或 [by, locator, reason]")
                    continue
                if _as_text(loc) is None:
                    problems.append(f"l1_rules[{broken!r}] 缺少非空 locator")

    for key in ("semantic_candidates", "intent_keywords", "intent_selectors"):
        table = data.get(key)
        if table is None:
            continue
        if not isinstance(table, dict):
            problems.append(f"{key} 必须是 object")
            continue
        for k, v in table.items():
            if key == "intent_selectors":
                if _as_text(v) is None:
                    problems.append(f"intent_selectors[{k!r}] 必须是非空字符串")
            elif not isinstance(v, (list, tuple)):
                problems.append(f"{key}[{k!r}] 必须是数组")
            elif not all(isinstance(x, str) for x in v):
                problems.append(f"{key}[{k!r}] 里必须都是字符串")

    hints = data.get("semantic_hints")
    if hints is not None:
        if not isinstance(hints, (list, tuple)):
            problems.append("semantic_hints 必须是数组: [[正则, 语义], ...]")
        else:
            for item in hints:
                if not (isinstance(item, (list, tuple)) and len(item) >= 2
                        and _as_text(item[0]) and _as_text(item[1])):
                    problems.append(f"semantic_hints 条目必须是 [正则字符串, 语义字符串]: {item!r}")
                    continue
                try:
                    re.compile(item[0])
                except re.error as ex:
                    problems.append(f"semantic_hints 正则写错 {item[0]!r}: {ex}")

    for key in ("l3_containers", "l4_containers"):
        items = data.get(key)
        if items is None:
            continue
        if not isinstance(items, (list, tuple)):
            problems.append(f"{key} 必须是数组")
            continue
        for sel in items:
            if _as_text(sel) is None:
                problems.append(f"{key} 里必须都是非空选择器字符串: {sel!r}")

    return problems


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------

def _dedupe(items: Iterable[Any]) -> List[Any]:
    """去重保序; 空值丢掉。list 条目按内容转 tuple 当 key (semantic_hints 是 [[pat, sem]])"""
    seen = set()
    out: List[Any] = []
    for item in items:
        if isinstance(item, (list, tuple)):
            key: Any = tuple(item)
            if not key:
                continue
        else:
            key = item
            if not isinstance(item, str) or not item.strip():
                continue
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def merge_profiles(*profiles: Any) -> Dict[str, Any]:
    """按顺序合并多个规则包, **越靠后越优先** (站点盖框架)。

    - l1_rules 同键后者覆盖, 但后者写畸形时保留前者 (脏规则永不盖好规则)
    - semantic_candidates 同语义拼接, 后者在前 (站点候选先试, 再回落框架候选)
    - intent_keywords 同 bucket 拼接, 后者在前 (站点同义词先试)
    - intent_selectors 同 bucket 后者覆盖 (非空字符串才认)
    - 数组类键 (semantic_hints / l3_containers / l4_containers) 后者在前, 去重保序

    不修改入参, 返回键齐全的新 dict。**返回值与入参完全脱钩** (深拷贝),
    调用方改结果 (哪怕改嵌套的 rule dict) 也不会污染被缓存的内置包。
    """
    merged = _empty_rules()
    for prof in profiles:
        data = copy.deepcopy(dict(prof)) if isinstance(prof, Mapping) else {}
        for key in TABLE_KEYS:
            table = data.get(key)
            if not isinstance(table, Mapping):
                continue
            bucket = merged[key]
            for k, v in table.items():
                if _as_text(k) is None:
                    continue
                if key == "l1_rules":
                    if _usable_l1_rule(v):
                        bucket[k] = v
                elif key == "semantic_candidates":
                    if isinstance(v, (list, tuple)):
                        clean = [s.strip() for s in v if _as_text(s)]
                        bucket[k] = _dedupe([*clean, *(bucket.get(k) or [])])
                elif key == "intent_keywords":
                    if isinstance(v, (list, tuple)):
                        bucket[k] = _dedupe([*v, *(bucket.get(k) or [])])
                elif _as_text(v) is not None:
                    bucket[k] = _as_text(v)
        for key in LIST_KEYS:
            items = data.get(key)
            if isinstance(items, (list, tuple)):
                merged[key] = _dedupe([*items, *merged[key]])
    return merged


def _usable_l1_rule(rule: Any) -> bool:
    """这条 L1 规则能不能落地: {by, locator} / [by, locator] 且有非空 locator"""
    if isinstance(rule, Mapping):
        loc = rule.get("locator") or rule.get("selector")
    elif isinstance(rule, (list, tuple)) and len(rule) >= 2:
        loc = rule[1]
    else:
        return False
    return _as_text(loc) is not None


@lru_cache(maxsize=1)
def default_rules() -> Dict[str, Any]:
    """框架内置规则包 = merge(DEFAULT_BUILTIN)。

    结果缓存复用 (同一个 dict 对象), 调用方只读不写。缺包/包写错只 warn 不抛 ——
    缺一个内置包时 healer 会少一层规则, 但不会崩。
    """
    packs: List[Dict[str, Any]] = []
    for name in DEFAULT_BUILTIN:
        try:
            data = load_profile(name)
        except ProfileError as ex:
            warnings.warn(f"内置规则包 {name} 加载失败, 已跳过: {ex}",
                          RuntimeWarning, stacklevel=2)
            continue
        for issue in validate_profile(data):
            warnings.warn(f"内置规则包 {name}: {issue}", RuntimeWarning, stacklevel=2)
        packs.append(data)
    return merge_profiles(*packs)


def effective_rules(profile: Union[str, os.PathLike, Mapping, None] = None) -> Dict[str, Any]:
    """生效规则 = 内置规则包 + 站点 profile (站点覆盖内置)。

    优先级: 显式 profile > 环境变量 AI_KIT_PROFILE > 仅内置包。
    profile 为空/读不了/类型根本不是规则包 → 直接返回内置包 (零拷贝, 不合并);
    只有 str / PathLike 才会被当成包名去磁盘找, 其余类型静默当没有。
    """
    if profile is None:
        profile = os.environ.get(PROFILE_ENV) or None
    if profile is None:
        return default_rules()
    if isinstance(profile, Mapping):
        site: Dict[str, Any] = profile
    elif isinstance(profile, (str, os.PathLike)):
        site = resolve_profile(profile)
    else:
        site = {}
    if not site:
        return default_rules()
    return merge_profiles(default_rules(), site)
