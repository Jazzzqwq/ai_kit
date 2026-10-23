# -*- coding: utf-8 -*-
"""
评测指标库 — 全部 stdlib 纯函数, 可单测, 不依赖 selenium/LLM/项目代码

对外提供:
- top_k_hit / rate                  : top-1 / top-3 命中率
- accuracy + confusion_matrix       : 分类准确率 + 混淆矩阵
- expected_calibration_error (ECE)  : 置信度校准 (分箱可靠性)
- py_compile_pass                   : 生成代码语法编译通过率
- coverage_recall                   : 生成用例对"应覆盖场景"的召回率
- duplication_rate                  : 生成用例重复率 (方法名碰撞 + 步骤 Jaccard 近重复)
"""
from __future__ import annotations

import ast
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


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
