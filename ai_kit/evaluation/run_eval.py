# -*- coding: utf-8 -*-
"""
AI 评测统一入口 — 汇总三套件, 输出 Markdown + JSON 报告

用法:
    python -m ai_kit.evaluation.run_eval                       # 默认: 0成本/离线/确定性
    python -m ai_kit.evaluation.run_eval --llm                 # 含真实 LLM (花钱/慢/非确定)
    python -m ai_kit.evaluation.run_eval --suite healer,analyzer
    python -m ai_kit.evaluation.run_eval --out ai_evaluation_report.md

可移植: 全程不 import 项目的 pages/config/common; 只依赖 ai_kit.* + selenium。
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, List

from ai_kit.evaluation import driver_provider
from ai_kit.evaluation import eval_healer, eval_analyzer, eval_generator

# 报告默认落到当前工作目录 (独立包不能写自身目录); 可用 AI_KIT_REPORT_DIR 覆盖
_OUT_DIR = os.environ.get("AI_KIT_REPORT_DIR") or os.getcwd()
_SUITES = {"healer": eval_healer, "analyzer": eval_analyzer, "generator": eval_generator}


def run(use_llm: bool = False, suites: List[str] = None,
        include_flywheel: bool = True) -> Dict[str, Any]:
    suites = suites or ["healer", "analyzer", "generator"]

    # 自愈需要浏览器; 归因的截图多模态也需要 (仅 --llm)。能起就共享一个。
    need_driver = ("healer" in suites) or (use_llm and "analyzer" in suites)
    driver = driver_provider.get_driver() if need_driver else None

    reports: Dict[str, Any] = {}
    try:
        for name in suites:
            mod = _SUITES.get(name)
            if not mod:
                reports[name] = {"suite": name, "status": "UNKNOWN"}
                continue
            if name == "generator":
                reports[name] = mod.run(use_llm=use_llm)
            elif name == "analyzer":
                reports[name] = mod.run(use_llm=use_llm, driver=driver,
                                        include_flywheel=include_flywheel)
            else:  # healer
                reports[name] = mod.run(use_llm=use_llm, driver=driver)
    finally:
        driver_provider.quit_driver(driver)

    return {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "use_llm": use_llm,
            "driver_available": driver is not None,
            "suites": suites,
            "include_flywheel": include_flywheel,
        },
        "reports": reports,
    }


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------

def _pct(x) -> str:
    return f"{x*100:.1f}%" if isinstance(x, (int, float)) else str(x)


def _md(report: Dict[str, Any]) -> str:
    meta = report["meta"]
    R = report["reports"]
    lines: List[str] = []
    lines.append("# AI 评测报告")
    lines.append("")
    lines.append(f"- 生成时间: {meta['generated_at']}")
    lines.append(f"- 模式: {'含真实 LLM (--llm)' if meta['use_llm'] else '离线规则/骨架 (0 成本, 默认)'}")
    lines.append(f"- 浏览器可用: {meta['driver_available']}")
    lines.append("")
    lines.append("> 说明: 以下均为**黄金集实测值**, 用于替代 README 中的估算数字。")
    lines.append("")

    # ---- 自愈 ----
    h = R.get("healer")
    if h:
        lines.append("## 1. 自愈评测 (top-1/top-3 命中率)")
        if h.get("status") == "SKIPPED":
            lines.append(f"- ⚠ SKIPPED: {h.get('reason')}")
        else:
            lines.append(f"- 样本: {h['n']} 条 | top-1 命中: **{_pct(h['top1'])}** | top-3 命中: **{_pct(h['top3'])}**")
            lines.append("- 分层归因 (首个命中候选来自哪层, 对应 README 30/50/10/10):")
            lines.append("")
            lines.append("| 层 | 命中数 | 占比 |")
            lines.append("|----|-------|------|")
            n = h["n"] or 1
            for layer, cnt in h["per_layer_winner"].items():
                lines.append(f"| {layer} | {cnt} | {_pct(cnt/n)} |")
            lines.append(f"| 未命中 | {h['no_hit_cases']} | {_pct(h['no_hit_cases']/n)} |")
            lines.append("")
            lines.append("<details><summary>每条 case 明细</summary>\n")
            for c in h["cases"]:
                lines.append(f"- `{c['id']}` top1={c['top1_hit']} top3={c['top3_hit']} "
                             f"winner_rank={c['winner_rank']} winner_layer={c['winner_layer']} "
                             f"(hint={c['layer_hint']}, n_cand={c['n_candidates']})")
            lines.append("\n</details>\n")
        lines.append("")

    # ---- 归因 ----
    a = R.get("analyzer")
    if a:
        lines.append("## 2. 归因评测 (分类准确率 + 置信度校准)")
        rule = a["rule"]
        origin = (f" (手写 {a['n_manual']} + 飞轮 {a['n_flywheel']})"
                  if a.get("n_flywheel") else "")
        lines.append(f"- 样本: {a['n']} 条{origin}")
        lines.append(f"- **Phase-1 规则**: 准确率 {_pct(rule['accuracy'])} | 平均置信 {rule['avg_confidence']} | ECE {rule['calibration']['ece']}")
        b = a.get("blame")
        if b:
            lines.append(f"- **blame 层 (6.2)**: 规则版本 {b['rule_version']} | 分类一致率 {_pct(b['accuracy'])} | "
                         f"容错后 {_pct(b['tolerant_accuracy'])} (宽容 {b['n_tolerated']} 条) | 分布 {b['blame_counts']}")
        if a.get("llm"):
            llm = a["llm"]
            cmp = a["compare"]
            lines.append(f"- **Phase-2 LLM**: 准确率 {_pct(llm['accuracy'])} | 平均置信 {llm['avg_confidence']} | ECE {llm['calibration']['ece']}")
            lines.append(f"- 每次成本 (LLM): {cmp['llm_cost_per_case']} 元/次")
            lines.append("")
            lines.append("> README 对比 (0.80 规则 vs 0.97 LLM) 实测:")
            lines.append(f"  - 规则平均置信 {cmp['rule_avg_confidence']} / 准确 {_pct(cmp['rule_accuracy'])} vs "
                         f"LLM 平均置信 {cmp['llm_avg_confidence']} / 准确 {_pct(cmp['llm_accuracy'])}")
        lines.append("")
        lines.append("### 置信度可靠性表 (规则模式, ECE 分箱)")
        lines.append("")
        lines.append("| 置信区间 | 样本数 | 平均置信 | 实际准确 | gap |")
        lines.append("|---------|-------|---------|---------|-----|")
        for b in rule["calibration"]["reliability"]:
            if b["count"] == 0:
                continue
            lines.append(f"| {b['bin']} | {b['count']} | {b['avg_confidence']} | {b['accuracy']} | {b['gap']} |")
        lines.append("")
        lines.append("### 混淆矩阵 (行=期望, 列=预测, 规则模式)")
        lines.append("")
        for exp, row in rule["confusion_matrix"].items():
            lines.append(f"- {exp}: {row}")
        lines.append("")

    # ---- 生成 ----
    g = R.get("generator")
    if g:
        lines.append("## 3. 生成评测 (py_compile率 / 覆盖率 / 重复率)")
        if g.get("skeleton_mode"):
            lines.append("> ⚠ 当前为 **骨架模式**(未调 LLM), 仅验证编译/去重管道可运行, 不代表真实生成质量。加 `--llm` 测真实产出。")
        lines.append(f"- 模式: {g['mode']} | 样本: {g['n']}")
        lines.append(f"- **py_compile 通过率**: {_pct(g['py_compile_pass_rate'])}")
        lines.append(f"- **覆盖率(召回)**: {_pct(g['avg_coverage_recall'])}")
        lines.append(f"- **重复率**: 方法名碰撞 {_pct(g['avg_name_collision_rate'])} | 步骤近重复 {_pct(g['avg_step_near_dup_rate'])}")
        lines.append(f"- 每次成本: {g['cost'].get('per_call', 'n/a')} 元/次")
        lines.append("")

    # ---- 成本汇总 ----
    if meta["use_llm"]:
        lines.append("## 4. 成本汇总")
        lines.append("")
        lines.append("| 套件 | LLM 调用 | 输入 tokens | 输出 tokens | 总成本(元) | 单次(元) |")
        lines.append("|------|---------|------------|------------|-----------|---------|")
        for name in ("analyzer", "generator"):
            rep = R.get(name)
            if not rep:
                continue
            if name == "analyzer":
                cs = rep.get("llm", {}).get("cost", {})
            else:
                cs = rep.get("cost", {})
            if cs:
                lines.append(f"| {name} | {cs.get('llm_calls',0)} | {cs.get('prompt_tokens',0)} | "
                             f"{cs.get('completion_tokens',0)} | {cs.get('total_cost_yuan','n/a')} | {cs.get('per_call','n/a')} |")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="AI 评测统一入口")
    parser.add_argument("--llm", action="store_true", help="调用真实 LLM (有成本/慢/非确定)")
    parser.add_argument("--suite", default="healer,analyzer,generator",
                        help="逗号分隔: healer,analyzer,generator")
    parser.add_argument("--no-flywheel", action="store_true",
                        help="不并入数据飞轮正样本 (退回手写黄金集基线, 便于对比)")
    parser.add_argument("--out", default=os.path.join(_OUT_DIR, "ai_evaluation_report.md"))
    parser.add_argument("--json", default=os.path.join(_OUT_DIR, "ai_evaluation_report.json"))
    args = parser.parse_args()

    # Windows 控制台默认 GBK, 报告/日志含中文或符号会 UnicodeEncodeError; 尽早切 UTF-8
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    suites = [s.strip() for s in args.suite.split(",") if s.strip()]
    report = run(use_llm=args.llm, suites=suites, include_flywheel=not args.no_flywheel)

    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    md = _md(report)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md + "\n")

    print(md)
    print(f"\n[评测] Markdown 报告: {args.out}")
    print(f"[评测] JSON  报告: {args.json}")


if __name__ == "__main__":
    main()
