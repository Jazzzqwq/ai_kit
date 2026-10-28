# -*- coding: utf-8 -*-
"""
AI 评测统一入口 — 汇总三套件 + 方案层人决策账本, 输出 Markdown + JSON 报告

用法:
    python -m ai_kit.evaluation.run_eval                       # 默认: 0成本/离线/确定性
    python -m ai_kit.evaluation.run_eval --llm                 # 含真实 LLM (花钱/慢/非确定)
    python -m ai_kit.evaluation.run_eval --smoke               # --llm 冒烟: 各 1 条, 验 usage/成本
    python -m ai_kit.evaluation.run_eval --suite healer,analyzer
    python -m ai_kit.evaluation.run_eval --out ai_evaluation_report.md
    python -m ai_kit.evaluation.run_eval --decisions decisions.jsonl   # 账本位置 (默认 cwd)

可移植: 全程不 import 项目的 pages/config/common; 只依赖 ai_kit.* + selenium。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

from ai_kit.evaluation import driver_provider
from ai_kit.evaluation import eval_healer, eval_analyzer, eval_generator, eval_proposal

# 报告默认落到当前工作目录 (独立包不能写自身目录); 可用 AI_KIT_REPORT_DIR 覆盖
_OUT_DIR = os.environ.get("AI_KIT_REPORT_DIR") or os.getcwd()
_SUITES = {"healer": eval_healer, "analyzer": eval_analyzer, "generator": eval_generator}


def run(use_llm: bool = False, suites: List[str] = None,
        include_flywheel: bool = True, limit: int = 0,
        decisions: str = "", reports: str = "") -> Dict[str, Any]:
    suites = suites or ["healer", "analyzer", "generator"]
    limit = int(limit or 0)

    # 自愈需要浏览器; 归因的截图多模态也需要 (仅 --llm)。能起就共享一个。
    need_driver = ("healer" in suites) or (use_llm and "analyzer" in suites)
    driver = driver_provider.get_driver() if need_driver else None

    reports_rows: Dict[str, Any] = {}
    try:
        for name in suites:
            mod = _SUITES.get(name)
            if not mod:
                reports_rows[name] = {"suite": name, "status": "UNKNOWN"}
                continue
            if name == "generator":
                reports_rows[name] = mod.run(use_llm=use_llm, limit=limit or None)
            elif name == "analyzer":
                reports_rows[name] = mod.run(use_llm=use_llm, driver=driver,
                                            include_flywheel=include_flywheel,
                                            limit=limit or None)
            else:  # healer
                reports_rows[name] = mod.run(use_llm=use_llm, driver=driver)
    finally:
        driver_provider.quit_driver(driver)

    result = {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "use_llm": use_llm,
            "driver_available": driver is not None,
            "suites": suites,
            "include_flywheel": include_flywheel,
            "limit": limit,
        },
        "reports": reports_rows,
    }
    # 方案层人决策指标 (套件4): 读本地账本 decisions.jsonl, 不碰浏览器不花钱;
    # 没账本就在报告里 SKIPPED, 不编数字。
    result["proposal"] = eval_proposal.run(decisions=decisions, reports=reports or None)
    if limit and use_llm and set(suites) <= {"analyzer", "generator"}:
        # 只有 "真的调到了 + usage 采到了 + 价目表认识这个型号" 三条都过才算冒烟成功
        result["smoke"] = smoke_check(result)
    return result


def smoke_check(report: Dict[str, Any]) -> Dict[str, Any]:
    """--llm 冒烟验收: 1 条 analyzer + 1 条 generator, 验 usage 采集与成本换算"""
    checks: List[Dict[str, Any]] = []
    for name in ("analyzer", "generator"):
        rep = report["reports"].get(name) or {}
        cost = ((rep.get("llm") or {}).get("cost") if name == "analyzer"
                else rep.get("cost")) or {}
        price = cost.get("price_per_1k")
        checks.append({
            "suite": name,
            "llm_calls": cost.get("llm_calls", 0),
            "prompt_tokens": cost.get("prompt_tokens", 0),
            "completion_tokens": cost.get("completion_tokens", 0),
            "total_cost_yuan": cost.get("total_cost_yuan"),
            "per_call": cost.get("per_call"),
            "price_known": isinstance(price, dict),
            "ok": bool(cost.get("llm_calls", 0) > 0
                       and cost.get("prompt_tokens", 0) > 0
                       and isinstance(price, dict)),
        })
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------

def _pct(x) -> str:
    return f"{x*100:.1f}%" if isinstance(x, (int, float)) else str(x)


def _pp(delta) -> str:
    """占比差值 (百分点): +3.3 pp / -8.3 pp; 没有基线就一根横线"""
    return f"{delta * 100:+.1f} pp" if isinstance(delta, (int, float)) else "-"


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
            lines.append("")
            lines.append("### 分层贡献 (首个命中候选来自哪层, 对照 README 估算)")
            lines.append("")
            lines.append("| 层 | 首个命中 | 实测占比 | 候选命中 case 数 | README 估算 | 差值 |")
            lines.append("|----|---------|---------|----------------|------------|------|")
            layer_rows = (h.get("layers") or {}).get("per_layer") or {}
            hit_counts = h.get("per_layer_hit") or {}
            for layer, row in layer_rows.items():
                lines.append(
                    f"| {layer} | {row['count']} | {_pct(row['share'])} | "
                    f"{hit_counts.get(layer, '-')} | {_pct(row.get('baseline'))} | "
                    f"{_pp(row.get('delta'))} |")
            n = h["n"] or 1
            lines.append(f"| 未命中 | {h['no_hit_cases']} | {_pct(h['no_hit_cases']/n)} | - | 0% | - |")
            l4 = layer_rows.get("L4") or {}
            if not h.get("use_llm") and not l4.get("count"):
                lines.append("")
                lines.append("> L4 只有 `--llm` 注入 LLM 才会产生候选, 离线模式恒为 0 —— 这一行是口径限制, 不是策略退化。")
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
        lines.append("## 2. 归因评测 (分类准确率 + 过度/欠自信 + ECE)")
        rule = a["rule"]
        origin = (f" (手写 {a['n_manual']} + 飞轮 {a['n_flywheel']})"
                  if a.get("n_flywheel") else "")
        lines.append(f"- 样本: {a['n']} 条{origin} | 规则版本 {rule['rule_version']}")
        lines.append(f"- **准确率**: 严格 {_pct(rule['accuracy'])} | "
                     f"含容错 {_pct(rule.get('accuracy_tolerant', rule['accuracy']))} "
                     f"(宽容 {rule.get('n_tolerated', 0)} 条)")
        gap = rule.get("confidence") or {}
        bins = len(rule["calibration"]["reliability"])
        lines.append(f"- **过度/欠自信**: 平均置信 {gap.get('mean_confidence', rule['avg_confidence'])} "
                     f"− 准确率 {_pct(gap.get('accuracy', rule['accuracy']))} = "
                     f"**{gap.get('gap', 0):+.3f}** ({gap.get('verdict', 'n/a')}) | "
                     f"ECE ({bins} 箱) {rule['calibration']['ece']}")
        b = a.get("blame")
        if b:
            lines.append(f"- **blame 层 (6.2)**: 规则版本 {b['rule_version']} | 分类一致率 {_pct(b['accuracy'])} | "
                         f"容错后 {_pct(b['tolerant_accuracy'])} (宽容 {b['n_tolerated']} 条) | "
                         f"分布 {b['blame_counts']}")
        if a.get("llm"):
            llm = a["llm"]
            cmp = a["compare"]
            llm_gap = llm.get("confidence") or {}
            lines.append(f"- **Phase-2 LLM**: 准确率 {_pct(llm['accuracy'])} | "
                         f"含容错 {_pct(llm.get('accuracy_tolerant', llm['accuracy']))} | "
                         f"平均置信 {llm['avg_confidence']} | ECE {llm['calibration']['ece']} | "
                         f"校准 {llm_gap.get('gap', 0):+.3f} ({llm_gap.get('verdict', 'n/a')})")
            lines.append(f"- 每次成本 (LLM): {cmp['llm_cost_per_case']} 元/次")
            lines.append("")
            lines.append("> README 对比 (0.80 规则 vs 0.97 LLM) 实测:")
            lines.append(f"  - 规则平均置信 {cmp['rule_avg_confidence']} / 准确 {_pct(cmp['rule_accuracy'])} vs "
                         f"LLM 平均置信 {cmp['llm_avg_confidence']} / 准确 {_pct(cmp['llm_accuracy'])}")
        lines.append("")
        lines.append(f"### 置信度可靠性表 (规则模式, ECE 分 {bins} 箱)")
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
        lines.append(f"- **重复率**: 方法名碰撞 {_pct(g['avg_name_collision_rate'])}"
                     f" | 步骤近重复 {_pct(g['avg_step_near_dup_rate'])}")
        lines.append(f"- 每次成本: {g['cost'].get('per_call', 'n/a')} 元/次 | "
                     f"每页成本: {g['cost'].get('per_page', 'n/a')} 元/页 (共 {g.get('n_pages', g['n'])} 页)")
        lines.append("")

    # ---- 方案层人决策 (套件4): 本地账本, 有就报没有就 SKIPPED ----
    p = report.get("proposal")
    if p:
        lines.append("## 4. 方案层人决策 (接受率 / 验证成功率, 按方案类型分列)")
        if p.get("status") == "SKIPPED":
            lines.append(f"- ⚠ SKIPPED: {p.get('reason')}")
        else:
            lines.append(f"- 账本: {p['decisions']} ({p['n_rows']} 条, 本地文件不进 git) | "
                         f"类型 join: 认出 {p['n_type_resolved']} / unknown {p['n_type_unknown']}")
            lines.append("")
            lines.append("| 方案类型 | 已决定 | 采纳 | 拒绝 | 挂起 | 接受率 | 验证成功率 |")
            lines.append("|---------|-------|------|------|------|--------|-----------|")
            for kind, b in p["by_type"].items():
                acc = _pct(b["acceptance_rate"]) if b["decided"] else "n/a"
                vsr = _pct(b["verify_success_rate"]) if b["accepted"] else "n/a"
                lines.append(f"| {kind} | {b['decided']} | {b['accepted']} | "
                             f"{b['rejected']} | {b['deferred']} | {acc} | {vsr} |")
            o = p["overall"]
            acc = _pct(o["acceptance_rate"]) if o["decided"] else "n/a"
            vsr = _pct(o["verify_success_rate"]) if o["accepted"] else "n/a"
            lines.append(f"| **总体** | {o['decided']} | {o['accepted']} | "
                         f"{o['rejected']} | {o['deferred']} | **{acc}** | **{vsr}** |")
            lines.append("")
            lines.append("> 口径: 接受率 = accept/(accept+reject), defer 未决定不进分母; "
                         "验证成功率 = 采纳且重跑验证通过/采纳数, 一条都没采纳时是 n/a 不是 0%。")
            if p["n_type_unknown"]:
                lines.append("> 账本七个键里没有类型, 类型靠 join report.json 补; "
                             "没留档报告的条目全部落在 unknown 桶 —— 不是编的类型。")
        lines.append("")

    # ---- 成本汇总 ----
    lines.append("## 5. 成本汇总 (token × 价目表)")
    lines.append("")
    if meta["use_llm"]:
        lines.append("| 套件 | 样本 n | LLM 调用 | 输入 tokens | 输出 tokens | 总成本(元) | 单次(元) | 每页(元) |")
        lines.append("|------|--------|---------|------------|------------|-----------|---------|---------|")
        for name in ("analyzer", "generator"):
            rep = R.get(name)
            if not rep:
                continue
            if name == "analyzer":
                cs = rep.get("llm", {}).get("cost", {})
            else:
                cs = rep.get("cost", {})
            if cs:
                lines.append(f"| {name} | {rep.get('n', 0)} | {cs.get('llm_calls', 0)} | "
                             f"{cs.get('prompt_tokens', 0)} | {cs.get('completion_tokens', 0)} | "
                             f"{cs.get('total_cost_yuan', 'n/a')} | {cs.get('per_call', 'n/a')} | "
                             f"{cs.get('per_page', 'n/a')} |")
    else:
        lines.append("- 本次未调用 LLM (离线规则/骨架模式), token 成本为 0; `--llm` 才会产生成本。")
        lines.append("- 价目表快照: 见 JSON 的 `price_per_1k` (缺配置时为 n/a, 不编数字)。")
    lines.append("")

    # ---- 冒烟 (--smoke) ----
    smoke = report.get("smoke")
    if smoke:
        lines.append("## 6. `--llm` 冒烟 (usage 采集 + 成本换算)")
        lines.append("")
        lines.append("| 套件 | LLM 调用 | 输入 tokens | 输出 tokens | 单次(元) | 价目表 | 结论 |")
        lines.append("|------|---------|------------|------------|---------|--------|------|")
        for c in smoke["checks"]:
            lines.append(f"| {c['suite']} | {c['llm_calls']} | {c['prompt_tokens']} | "
                         f"{c['completion_tokens']} | {c['per_call']} | "
                         f"{'命中' if c['price_known'] else '缺失'} | "
                         f"{'✓' if c['ok'] else '✗'} |")
        lines.append("")
        lines.append(f"- 结论: **{'通过' if smoke['ok'] else '不通过'}** "
                     f"(三条都要过: 真的调到了 / usage 采到了 / 价目表认识这个型号)")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="AI 评测统一入口")
    parser.add_argument("--llm", action="store_true", help="调用真实 LLM (有成本/慢/非确定)")
    parser.add_argument("--suite", default="healer,analyzer,generator",
                        help="逗号分隔: healer,analyzer,generator")
    parser.add_argument("--no-flywheel", action="store_true",
                        help="不并入数据飞轮正样本 (退回手写黄金集基线, 便于对比)")
    parser.add_argument("--smoke", action="store_true",
                        help="--llm 冒烟: analyzer/generator 各跑 1 条, 验 usage 采集与成本换算")
    parser.add_argument("--decisions", default="",
                        help="方案层账本位置 (默认当前目录 decisions.jsonl)")
    parser.add_argument("--reports", default="",
                        help="类型 join 用的 report.json 位置 (默认当前目录)")
    parser.add_argument("--out", default=os.path.join(_OUT_DIR, "ai_evaluation_report.md"))
    parser.add_argument("--json", default=os.path.join(_OUT_DIR, "ai_evaluation_report.json"))
    args = parser.parse_args()

    # Windows 控制台默认 GBK, 报告/日志含中文或符号会 UnicodeEncodeError; 尽早切 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    if args.smoke:
        suites, use_llm, limit = ["analyzer", "generator"], True, 1
    else:
        suites = [s.strip() for s in args.suite.split(",") if s.strip()]
        use_llm, limit = args.llm, 0
    report = run(use_llm=use_llm, suites=suites, limit=limit,
                 include_flywheel=not args.no_flywheel,
                 decisions=args.decisions, reports=args.reports)

    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    md = _md(report)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md + "\n")

    print(md)
    print(f"\n[评测] Markdown 报告: {args.out}")
    print(f"[评测] JSON  报告: {args.json}")

    smoke = report.get("smoke")
    if smoke and not smoke["ok"]:
        print("[评测] 冒烟失败: 没调到 LLM / usage 没采到 / 价目表不认识当前型号", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
