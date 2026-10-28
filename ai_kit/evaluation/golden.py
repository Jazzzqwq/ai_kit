# -*- coding: utf-8 -*-
"""
黄金集 (7.2) — 一份 schema, 三套件共用

    {id, module, rule_version, source, input:{...}, expected:{...}, note}

- module       : 这条 case 喂给哪个套件 (analyzer / healer / generator)
- rule_version : 标注那天的规则/管道版本 (analyzer 跟 RULES_VERSION 走)。回归时先看它,
                 才说得清 "这一条是哪版规则判的"
- source       : manual (手写) / flywheel (6.6 账本导出) / collected (真页面采集冻结)。
                 报告里 "手写 25 + 飞轮 3" 就是按它数的
- input        : 喂进去的东西 (analyzer: traceback/log[/screenshot]; healer: 夹具+坏定位器;
                 generator: 页面结构 + PO 签名)
- expected     : 期望输出 (analyzer: category + acceptable_categories; healer: 定位器/目标元素;
                 generator: 应覆盖的分类/关键词)

两种形状都收: 新 schema 直接用; 6.6 之前平铺的老 case (traceback / expected_category …)
在加载时归一化过来 —— 飞轮已经写出过老形状的文件, 存量数据不该因为改 schema 就读不了。

fail 与 skip 的界线: load() 只负责读和归一, 不因为某条 case 脏就整份放弃 (评测要跑得出
数); 脏数据由 check() 一次列清, CLI (python -m ai_kit.evaluation.golden) 在 CI 里据此退出 1。

规模底线 (7.2): analyzer >= 30, healer >= 20。低于底线不是 "也能跑", 是样本太少, 数字
不该被当结论 —— CLI 同样按违规处理。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

MODULES: Sequence[str] = ("analyzer", "healer", "generator", "proposal")

FILES: Mapping[str, str] = {
    "analyzer": "analyzer_cases.json",
    "healer": "healer_cases.json",
    "generator": "generator_cases.json",
    # 方案层的负面清单 (6.6 飞轮导出): 有文件才存在, 当前没有套件消费它
    "proposal": "flywheel_negative.json",
}

# 7.2 的规模底线; generator 的 8 条底线是 8 条采集冻结 case 的数量 (管道自检的 G1/G2 另算)
SCALE_FLOOR: Mapping[str, int] = {"analyzer": 30, "healer": 20, "generator": 8}

# manual 手写 / flywheel 账本导出 / collected 真页面采集脱敏冻结 (collect_golden)
SOURCES: Sequence[str] = ("manual", "flywheel", "collected")

DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden")

# 老形状 -> 新 schema 的字段搬迁表 (input / expected 两半各一张)
_LEGACY_INPUT: Mapping[str, Mapping[str, str]] = {
    "analyzer": {"traceback": "traceback", "log": "log",
                 "screenshot": "screenshot_fixture"},
    "healer": {"fixture": "fixture", "broken_by": "broken_by",
               "broken_locator": "broken_locator", "intent": "intent"},
    "generator": {"page_name": "page_name", "fixture_name": "fixture_name",
                  "po_module": "po_module", "po_class": "po_class",
                  "po_methods": "po_methods", "page_structure": "page_structure"},
}

_LEGACY_EXPECTED: Mapping[str, Mapping[str, str]] = {
    "analyzer": {"category": "expected_category",
                 "acceptable_categories": "acceptable_categories"},
    "healer": {"locator": "expected_locator", "target": "expected_target",
               "layer_hint": "layer_hint"},
    "generator": {"categories": "expected_categories",
                  "keywords": "expected_keywords"},
}

# 每套件的必填期望字段: 缺了这条 case 就判不出对错, 属于脏数据
_REQUIRED_EXPECTED: Mapping[str, Sequence[str]] = {
    "analyzer": ("category",),
    "healer": ("locator", "target"),
    "generator": ("keywords",),
}

_ENVELOPE = ("id", "module", "rule_version", "source", "input", "expected", "note")


class GoldenError(ValueError):
    """用不了: 未知 module / 文件读不出来"""


def module_version(module: str) -> str:
    """标注口径版本: analyzer 跟着规则版本走, 另两套是各自管道/标注口径的版本"""
    if module == "analyzer":
        try:
            from ai_kit.analyzer import RULES_VERSION
            return RULES_VERSION
        except Exception:  # noqa: BLE001 — 版本号拿不到不该让评测跑不起来
            return "rules.unknown"
    return {"healer": "healer.v1", "generator": "generator.v1",
            "proposal": "proposal.v1"}.get(module, "unknown")


def normalize(raw: Mapping[str, Any], module: str,
              source: Optional[str] = None) -> Dict[str, Any]:
    """一条 case 归一成 7.2 schema; 老形状按模块的搬迁表挪字段, 其余字段原样保留

    保留 "其余字段" 是有意的: 飞轮的 failure_id / proposal_id / category_source /
    expected.diff 这些溯源信息不在 schema 里, 但丢掉它们样本就没法回溯。
    """
    if module not in MODULES:
        raise GoldenError(f"未知 module: {module} (只有 {'/'.join(MODULES)})")

    rest = dict(raw)
    if isinstance(rest.get("input"), dict) and isinstance(rest.get("expected"), dict):
        inp = dict(rest.pop("input"))
        exp = dict(rest.pop("expected"))
    else:
        inp = {key: rest.pop(legacy) for key, legacy in _LEGACY_INPUT[module].items()
               if legacy in rest}
        exp = {key: rest.pop(legacy) for key, legacy in _LEGACY_EXPECTED[module].items()
               if legacy in rest}

    case: Dict[str, Any] = {
        "id": str(rest.get("id") or "").strip(),
        "module": str(rest.get("module") or module),
        "rule_version": str(rest.get("rule_version") or module_version(module)),
        "source": str(source or rest.get("source") or "manual"),
        "input": inp,
        "expected": exp,
        "note": str(rest.get("note") or ""),
    }
    if module == "analyzer" and "acceptable_categories" not in exp:
        exp["acceptable_categories"] = []   # 新老形状都保证这个字段在, 免得下游到处 .get
    for key, value in rest.items():   # 溯源/标签字段透传 (label / failure_id / truncated …)
        if key not in case:
            case[key] = value
    return case


def _read(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list):
        raise GoldenError(f"{path}: 顶层要有 \"cases\" 列表")
    return [c for c in cases if isinstance(c, dict)]


def load(module: str, directory: Optional[str] = None,
         extra: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """读某套件的黄金集 (含可选附加文件, 如飞轮正样本), 全部归一成 7.2 schema

    文件不存在 = 空列表 (飞轮文件只在导出过一次后才存在)。
    """
    if module not in MODULES:
        raise GoldenError(f"未知 module: {module} (只有 {'/'.join(MODULES)})")
    base = directory or DEFAULT_DIR
    out: List[Dict[str, Any]] = []
    for name in (FILES[module], *extra):
        path = name if os.path.isabs(name) else os.path.join(base, name)
        if not os.path.isfile(path):
            continue
        out += [normalize(c, module) for c in _read(path)]
    return out


def check(cases: Sequence[Mapping[str, Any]], module: Optional[str] = None) -> List[str]:
    """schema 问题清单 (空 = 干净); 不抛异常 —— 调用方决定是报错还是只打印"""
    problems: List[str] = []
    seen: Dict[str, int] = {}
    for idx, case in enumerate(cases):
        cid = str(case.get("id") or "")
        where = f"[{idx}] {cid or '(无 id)'}"
        if not cid:
            problems.append(f"{where}: 缺 id")
        elif cid in seen:
            problems.append(f"{where}: id 与第 {seen[cid]} 条重复 (去重靠它, 重复就分不清谁是谁)")
        else:
            seen[cid] = idx

        case_module = str(case.get("module") or "")
        if module and case_module != module:
            problems.append(f"{where}: module={case_module!r} 与所在套件 {module!r} 不一致")
        if case_module not in MODULES:
            problems.append(f"{where}: module={case_module!r} 不在 {'/'.join(MODULES)}")
        if str(case.get("source") or "") not in SOURCES:
            problems.append(f"{where}: source={case.get('source')!r} 不在 {'/'.join(SOURCES)}")
        if not str(case.get("rule_version") or "").strip():
            problems.append(f"{where}: 缺 rule_version")
        if not isinstance(case.get("input"), dict):
            problems.append(f"{where}: input 要是对象")
        expected = case.get("expected")
        if not isinstance(expected, dict):
            problems.append(f"{where}: expected 要是对象")
            continue
        for key in _REQUIRED_EXPECTED.get(case_module, ()):
            value = expected.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                problems.append(f"{where}: expected.{key} 为空 (判不出对错的样本不算样本)")
        acceptable = expected.get("acceptable_categories")
        if acceptable is not None:
            if not isinstance(acceptable, list):
                problems.append(f"{where}: expected.acceptable_categories 要是列表")
            elif str(expected.get("category") or "") in [str(a) for a in acceptable]:
                problems.append(f"{where}: acceptable_categories 里不该再写一遍自己")
    return problems


def scale(cases: Sequence[Mapping[str, Any]], module: str) -> Dict[str, Any]:
    """规模对照底线: {n, floor, ok}"""
    floor = SCALE_FLOOR.get(module, 0)
    n = len(cases)
    return {"n": n, "floor": floor, "ok": n >= floor}


# ---------------------------------------------------------------------------
# CLI: CI 里跑一遍, 脏数据/样本不够就退出 1
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="校验黄金集规模与 schema")
    parser.add_argument("--dir", default=DEFAULT_DIR, help="golden 目录")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    problems: List[str] = []
    failed = False
    for module in MODULES:
        cases = load(module, directory=args.dir)
        stat = scale(cases, module)
        if not cases and not stat["floor"]:
            continue      # 没导出过的可选模块 (如 proposal 负面清单) 不占位
        mark = "OK" if stat["ok"] else "样本不足"
        failed = failed or not stat["ok"]
        floor = stat["floor"] or "-"
        print(f"{module:<10} n={stat['n']:<3} 底线 {floor:<3} {mark}")
        problems += check(cases, module)

    if problems:
        print(f"\nschema: {len(problems)} 个问题")
        for line in problems:
            print(f"  - {line}")
    else:
        print("\nschema: 0 个问题")
    return 1 if (problems or failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
