# -*- coding: utf-8 -*-
"""
评测指标库 — 全部 stdlib 纯函数, 可单测, 不依赖 selenium/LLM/项目代码

对外提供:
- top_k_hit / rate                  : top-1 / top-3 命中率
- layer_breakdown                   : 分层贡献 (L1-L4, 可对照文档估算占比)
- accuracy + confusion_matrix       : 分类准确率 + 混淆矩阵
- confidence_gap                    : 过度/欠自信 (mean(conf) - accuracy, 标题指标)
- expected_calibration_error (ECE)  : 置信度校准 (分箱可靠性)
- py_compile_pass                   : 生成代码语法编译通过率
- coverage_recall                   : 生成用例对"应覆盖场景"的召回率
- duplication_rate                  : 生成用例重复率 (方法名碰撞 + 步骤 Jaccard 近重复)
- proposal_decision_metrics         : 方案层人决策账本 → 接受率 / 验证成功率 (按类型分列)
- perception_chain_metrics          : 感知链两项实跑率 → eid 解析率 / 可执行率
"""
from __future__ import annotations

import ast
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# 通用: 比率
# ---------------------------------------------------------------------------

def safe_div(numerator: float, denominator: float) -> float:
    """除法保护: 分母为 0 返回 0.0"""
    return (numerator / denominator) if denominator else 0.0


def rate(flags: Sequence[bool]) -> float:
    """布尔序列中正例占比"""
    if not flags:
        return 0.0
    return safe_div(sum(1 for f in flags if f), len(flags))


# ---------------------------------------------------------------------------
# top-k 命中
# ---------------------------------------------------------------------------

def top_k_hit(candidate_hits: Sequence[bool], k: int) -> bool:
    """
    前 k 个候选里是否有命中 (True)。candidate_hits 按候选排名顺序排列,
    元素表示"该候选是否解析到期望元素"。
    """
    return any(candidate_hits[:k])


def top_k_rates(case_candidate_hits: Iterable[Sequence[bool]],
                ks: Sequence[int] = (1, 3)) -> Dict[int, float]:
    """
    多条 case 的 top-k 命中率。
    case_candidate_hits: 每条 case 一个 [bool,...] (按候选顺序)。
    返回 {k: 命中率}。
    """
    cases = list(case_candidate_hits)
    out: Dict[int, float] = {}
    for k in ks:
        flags = [top_k_hit(h, k) for h in cases]
        out[k] = rate(flags)
    return out


def layer_breakdown(winners: Sequence[Optional[str]],
                    baseline: Optional[Mapping[str, float]] = None,
                    layers: Sequence[str] = ("L1", "L2", "L3", "L4")) -> Dict[str, Any]:
    """
    分层贡献: 每条 case 的首个命中候选来自哪一层 (None = 所有候选都没命中)。

    baseline 给的是文档里的估算占比 (README 30/50/10/10), 给了就顺带算差值 ——
    实测与估算摆在一起, 才看得出哪一层被高估了。L4 只有注入 LLM 才会出现, 离线
    模式下它恒为 0, 那是口径问题不是策略退化, 由调用方在报告里注明。
    """
    n = len(winners)
    per_layer: Dict[str, Dict[str, Any]] = {}
    for layer in layers:
        count = sum(1 for w in winners if w == layer)
        share = safe_div(count, n)
        row: Dict[str, Any] = {"count": count, "share": round(share, 4)}
        if baseline is not None and layer in baseline:
            row["baseline"] = baseline[layer]
            row["delta"] = round(share - baseline[layer], 4)
        per_layer[layer] = row

    no_hit = sum(1 for w in winners if w is None)
    return {
        "n": n,
        "per_layer": per_layer,
        "no_hit": no_hit,
        "no_hit_share": round(safe_div(no_hit, n), 4),
    }


# ---------------------------------------------------------------------------
# 分类准确率 + 混淆矩阵
# ---------------------------------------------------------------------------

def classification_accuracy(pairs: Sequence[Tuple[str, str]]) -> float:
    """pairs = [(predicted, expected), ...] → 准确率"""
    if not pairs:
        return 0.0
    return rate([p == e for p, e in pairs])


def confusion_matrix(pairs: Sequence[Tuple[str, str]]) -> Dict[str, Dict[str, int]]:
    """
    返回 {expected: {predicted: count}}。
    行=真实类别, 列=预测类别。
    """
    matrix: Dict[str, Dict[str, int]] = {}
    for predicted, expected in pairs:
        row = matrix.setdefault(expected, {})
        row[predicted] = row.get(predicted, 0) + 1
    return matrix


# ---------------------------------------------------------------------------
# 置信度校准 (ECE)
# ---------------------------------------------------------------------------

def expected_calibration_error(samples: Sequence[Tuple[float, bool]],
                               n_bins: int = 10) -> Dict[str, Any]:
    """
    计算 Expected Calibration Error。
    samples = [(confidence, is_correct), ...]
    把置信度分成 n_bins 个等宽箱 [0,1/n),[1/n,2/n)...[1-1/n,1]
    每箱 gap = |平均置信 - 箱内准确率|, ECE = Σ (箱样本数/总样本) * gap
    返回 {ece, n, reliability: [bin...]}
    """
    n = len(samples)
    if n == 0:
        return {"ece": 0.0, "n": 0, "reliability": []}

    bins: List[List[Tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for conf, correct in samples:
        conf = max(0.0, min(1.0, float(conf)))
        idx = min(int(conf * n_bins), n_bins - 1)  # conf=1.0 落最后一箱
        bins[idx].append((conf, correct))

    reliability: List[Dict[str, Any]] = []
    ece = 0.0
    width = 1.0 / n_bins
    for i, b in enumerate(bins):
        cnt = len(b)
        if cnt == 0:
            reliability.append({
                "bin": f"{i*width:.1f}-{(i+1)*width:.1f}",
                "count": 0, "avg_confidence": None,
                "accuracy": None, "gap": None,
            })
            continue
        avg_conf = sum(c for c, _ in b) / cnt
        acc = sum(1 for _, ok in b if ok) / cnt
        gap = abs(avg_conf - acc)
        ece += (cnt / n) * gap
        reliability.append({
            "bin": f"{i*width:.1f}-{(i+1)*width:.1f}",
            "count": cnt, "avg_confidence": round(avg_conf, 3),
            "accuracy": round(acc, 3), "gap": round(gap, 3),
        })

    return {"ece": round(ece, 4), "n": n, "reliability": reliability}


def confidence_gap(samples: Sequence[Tuple[float, bool]],
                   band: float = 0.02) -> Dict[str, Any]:
    """
    过度自信 / 欠自信 —— 标题指标: mean(confidence) − accuracy

    正数 = 过度自信 (说得比做得好); 负数 = 欠自信 (比实际表现保守);
    |gap| <= band 算校准良好。

    与 ECE 的分工: ECE 逐箱看 "每个置信区间贴着对角线吗", 一个数讲不清原因;
    gap 只看整体偏差, 一个数就能读。所以标题用 gap, 分箱表留给 ECE 做细节。
    """
    n = len(samples)
    if n == 0:
        return {"gap": 0.0, "mean_confidence": 0.0, "accuracy": 0.0,
                "n": 0, "verdict": "无样本", "band": band}
    mean_conf = sum(float(c) for c, _ in samples) / n
    accuracy = rate([bool(ok) for _, ok in samples])
    gap = mean_conf - accuracy
    if gap > band:
        verdict = "过度自信"
    elif gap < -band:
        verdict = "欠自信"
    else:
        verdict = "校准良好"
    return {
        "gap": round(gap, 4),
        "mean_confidence": round(mean_conf, 4),
        "accuracy": round(accuracy, 4),
        "n": n,
        "verdict": verdict,
        "band": band,
    }


# ---------------------------------------------------------------------------
# 方案层人决策: 接受率 / 验证成功率 (按方案类型分列)
# ---------------------------------------------------------------------------

# 账本三值与验证结果口径 (与 applier 的 schema 对齐; 纯函数不 import 它, 只认字符串)
_ACCEPT = "accept"
_REJECT = "reject"
_DEFER = "defer"
_PASSED = "passed"


def _decision_bucket(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """一组同类型决策 -> 计数 + 两项率 (接受率 / 验证成功率)"""
    decided = accepted = rejected = deferred = verified = 0
    for row in rows:
        decision = str(row.get("decision") or "").strip().lower()
        verify = str(row.get("verify_result") or "").strip().lower()
        if decision == _DEFER:
            deferred += 1
            continue
        if decision in (_ACCEPT, _REJECT):
            decided += 1
        if decision == _ACCEPT:
            accepted += 1
            if verify == _PASSED:
                verified += 1
        elif decision == _REJECT:
            rejected += 1
    return {
        "n": len(rows),
        "decided": decided,          # accept + reject (defer 还没决定, 不进分母)
        "accepted": accepted,
        "rejected": rejected,
        "deferred": deferred,
        "verified": verified,        # accept 且重跑验证通过
        "acceptance_rate": round(safe_div(accepted, decided), 4),
        # 只有 accept 才会跑验证: 一条都没采纳时这个率没有意义, 交给调用方按 accepted==0 显示 n/a
        "verify_success_rate": round(safe_div(verified, accepted), 4),
    }


def proposal_decision_metrics(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """方案层账本 -> 接受率 / 验证成功率, 总体一份 + 按方案类型各一份。

    rows 每条形如 {type, decision, verify_result} —— type 由调用方 join report.json 补上
    (账本七个键里没有 type, 见 applier); 补不到时传空串, 这里归到 "unknown" 桶, 不编类型。

    两项率的口径 (写死在这里, 报告照搬):
    - acceptance_rate   = accept / (accept + reject)   —— defer 未决定, 不进分母;
    - verify_success_rate = (accept 且 verify=passed) / accept —— 只有采纳了才会重跑验证。
    分母为 0 的率一律 0.0, 由调用方结合 accepted/decided 计数决定是显 0% 还是 n/a。
    """
    buckets: Dict[str, List[Mapping[str, Any]]] = {}
    for row in rows:
        kind = str(row.get("type") or "").strip() or "unknown"
        buckets.setdefault(kind, []).append(row)
    return {
        "n": len(rows),
        "overall": _decision_bucket(list(rows)),
        "by_type": {kind: _decision_bucket(items) for kind, items in sorted(buckets.items())},
    }


# ---------------------------------------------------------------------------
# 感知链实跑: eid 解析率 / 可执行率
# ---------------------------------------------------------------------------

def perception_chain_metrics(ref_rows: Sequence[Mapping[str, Any]],
                             case_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """plan -> render -> verify 链路的两个可算率; 计数由调用方采集, 这里只算率。

    ref_rows  每条形如 {resolved: bool} —— plan 里一个非空 step.eid 能否在采集清单里
              resolve_ref 到元素 (含 cleanup; 复合 ref 如 c1.column:X 也走同一扇门)。
    case_rows 每条形如 {rendered: bool, needs_review: bool} —— 一条用例能否出码且
              verify 后未被标 needs_review。

    口径 (写死在这里, 报告照搬):
    - eid_parse_rate   = resolved / 全部非空 eid;
    - executable_rate  = (出码且未标 review) / 全部用例。
    分母为 0 返回 0.0, 由渲染层结合 n_refs/n_cases 显 n/a (没采到 inventory 时不是 0%)。
    确定性规划器"派生不出来就 skip", 离线模式 eid 解析率预期接近 100%; 真正能打破
    它的是 --llm 幻觉引用 —— 本期不接 LLM 感知链, 读数时要注明。
    """
    n_refs = len(ref_rows)
    resolved = sum(1 for r in ref_rows if r.get("resolved"))
    n_cases = len(case_rows)
    executable = sum(1 for c in case_rows
                     if c.get("rendered") and not c.get("needs_review"))
    return {
        "n_refs": n_refs,
        "refs_resolved": resolved,
        "eid_parse_rate": round(safe_div(resolved, n_refs), 4),
        "n_cases": n_cases,
        "cases_executable": executable,
        "executable_rate": round(safe_div(executable, n_cases), 4),
    }


# ---------------------------------------------------------------------------
# 元素覆盖率: 交互元素被用例引用到的比例
# ---------------------------------------------------------------------------

def element_coverage(inventory_eids: Sequence[str],
                     referenced_eids: Sequence[str]) -> Dict[str, Any]:
    """交互元素覆盖率 = 至少被一条用例引用到的交互元素 / inventory 里的全部交互元素

    分母只认 inventory.by_eid (可交互元素)；表格/弹窗/图表走 lookup, 不在里面 ——
    复合引用 (c1.column:X / chart:N) 指向的是这些 lookup 记录, 对交互元素没有意义,
    所以只按精确 eid 命中算覆盖, 不把复合引用凑进分子。

    与 coverage_recall 的区别: 那个按"计划文本里出现多少预期关键词"算, 计划用 eid
    引用时天然偏低; 这个只看元素有没被点到, 与计划用什么措辞无关。
    """
    total_eids = sorted({str(e).strip() for e in inventory_eids or () if str(e).strip()})
    refs = {str(e).strip() for e in referenced_eids or () if str(e).strip()}
    covered = [e for e in total_eids if e in refs]
    missing = [e for e in total_eids if e not in refs]
    return {
        "total": len(total_eids),
        "covered": len(covered),
        "rate": round(safe_div(len(covered), len(total_eids)), 4),
        "missing": missing[:10],   # 只留样例, 不在报告里塞整页清单
    }


# ---------------------------------------------------------------------------
# py_compile 通过率
# ---------------------------------------------------------------------------

def py_compile_pass(code: str) -> Tuple[bool, str]:
    """
    编译字符串代码, 返回 (是否通过, 错误信息)。
    优先用 compile() 做纯内存语法编译 (不落盘), 兼顾 ast 兜底。
    """
    if code is None or not code.strip():
        return False, "empty code"
    try:
        compile(code, "<generated>", "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno})"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def py_compile_rate(codes: Sequence[str]) -> Dict[str, Any]:
    """一组生成代码的编译通过率 + 失败明细"""
    results = []
    for i, c in enumerate(codes):
        ok, err = py_compile_pass(c)
        results.append({"index": i, "passed": ok, "error": err})
    passed = sum(1 for r in results if r["passed"])
    return {
        "total": len(codes),
        "passed": passed,
        "pass_rate": rate([r["passed"] for r in results]),
        "failures": [r for r in results if not r["passed"]],
    }


# ---------------------------------------------------------------------------
# 覆盖率 (召回率)
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def coverage_recall(generated_text: str,
                    expected_keywords: Sequence[str],
                    expected_categories: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """
    生成内容对"应覆盖场景"的召回率。
    generated_text: 生成的 plan/代码拼成的全文。
    expected_keywords: 命中即视为覆盖到该场景的词 (子串匹配, 忽略大小写/空白)。
    expected_categories: 额外检查的类别词 (同样子串匹配)。
    返回 {recall, matched, missed, total}
    """
    hay = _normalize(generated_text)
    checks: List[str] = list(expected_keywords or [])
    if expected_categories:
        checks += list(expected_categories)
    checks = [c for c in checks if c]

    matched, missed = [], []
    for kw in checks:
        (matched if _normalize(kw) in hay else missed).append(kw)

    return {
        "total": len(checks),
        "matched": matched,
        "missed": missed,
        "recall": rate([True] * len(matched) + [False] * len(missed)) if checks else 0.0,
    }


# ---------------------------------------------------------------------------
# 重复率
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set:
    """中英文混合分词的轻量版: 英文按词, 中文按字符 bigram"""
    tokens: set = set()
    # 英文/数字词
    for w in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
        tokens.add(w)
    # 中文字符 bigram
    han = re.findall(r"[\u4e00-\u9fff]", text)
    for i in range(len(han) - 1):
        tokens.add(han[i] + han[i + 1])
    return tokens


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    return (inter / union) if union else 0.0


def method_name_collisions(names: Sequence[str]) -> Dict[str, Any]:
    """方法名归一化后的碰撞 (重名) 比例"""
    norm = [_normalize(n) for n in names if n]
    total = len(norm)
    uniq = len(set(norm))
    dup = total - uniq
    return {
        "total": total, "unique": uniq, "duplicates": dup,
        "collision_rate": safe_div(dup, total) if total else 0.0,
    }


def step_near_duplication(step_texts: Sequence[str],
                          threshold: float = 0.8) -> Dict[str, Any]:
    """
    步骤文本两两 Jaccard 相似度 > threshold 视为近重复。
    近重复率 = 近重复对数 / 总对数。
    """
    texts = [s for s in step_texts if s and s.strip()]
    token_sets = [_tokenize(t) for t in texts]
    n = len(token_sets)
    total_pairs = 0
    dup_pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            total_pairs += 1
            if jaccard(token_sets[i], token_sets[j]) >= threshold:
                dup_pairs += 1
    return {
        "n_steps": n,
        "total_pairs": total_pairs,
        "near_dup_pairs": dup_pairs,
        "near_dup_rate": safe_div(dup_pairs, total_pairs) if total_pairs else 0.0,
        "threshold": threshold,
    }


def extract_test_methods(code: str) -> List[Tuple[str, str]]:
    """
    从生成的 Python 代码里用 AST 抽取 test_ 方法, 返回 [(方法名, docstring+body文本)]。
    解析失败返回空列表。用于喂给重复率指标。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    out: List[Tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            body_src = ast.get_source_segment(code, node) or node.name
            out.append((node.name, body_src))
    return out


def duplication_rate(code: str,
                     threshold: float = 0.8) -> Dict[str, Any]:
    """综合重复率: 方法名碰撞 + 步骤近重复 (从 AST 抽 test 方法体)"""
    methods = extract_test_methods(code)
    names = [m for m, _ in methods]
    bodies = [b for _, b in methods]
    return {
        "n_methods": len(methods),
        "name_collision": method_name_collisions(names),
        "step_duplication": step_near_duplication(bodies, threshold=threshold),
    }
