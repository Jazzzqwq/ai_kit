# -*- coding: utf-8 -*-
"""
metrics.py 纯离线自检 — 用已知期望值断言各指标算法正确。

运行:
    pytest tests/test_metrics.py      # 或直接 python tests/test_metrics.py
"""
from __future__ import annotations

from ai_kit.evaluation import metrics as m


def _close(a: float, b: float, tol: float = 1e-4) -> bool:
    return abs(a - b) < tol


def test_top_k():
    assert m.top_k_hit([False, True], 1) is False
    assert m.top_k_hit([False, True], 2) is True
    rates = m.top_k_rates([[False, True], [True, False]], ks=(1, 3))
    assert rates[1] == 0.5, rates
    assert rates[3] == 1.0, rates


def test_accuracy_confusion():
    pairs = [("A", "A"), ("B", "A"), ("A", "A")]
    assert _close(m.classification_accuracy(pairs), 2 / 3)
    cm = m.confusion_matrix(pairs)
    assert cm["A"]["A"] == 2 and cm["A"]["B"] == 1


def test_ece():
    # 手算: bin0.6(1条,conf.6,准0)=gap.6 权1/3=.2 ; bin0.9(2条,conf.9,准1)=gap.1 权2/3=.0667
    r = m.expected_calibration_error([(0.9, True), (0.9, True), (0.6, False)], n_bins=10)
    assert _close(r["ece"], 0.2667, 1e-3), r["ece"]
    assert r["n"] == 3


def test_py_compile():
    ok, _ = m.py_compile_pass("def f():\n    return 1\n")
    assert ok is True
    bad, err = m.py_compile_pass("def f(:")
    assert bad is False and "SyntaxError" in err


def test_coverage():
    r = m.coverage_recall("add delete query reset", ["add", "query", "export"])
    assert r["matched"] == ["add", "query"]
    assert r["missed"] == ["export"]
    assert _close(r["recall"], 2 / 3)


def test_jaccard_and_dup():
    assert _close(m.jaccard({"a", "b"}, {"a", "c"}), 1 / 3)
    nc = m.method_name_collisions(["test_a", "test_a", "test_b"])
    assert nc["total"] == 3 and nc["unique"] == 2 and nc["duplicates"] == 1
    dup = m.duplication_rate("def test_a():\n    '''x'''\n    pass\ndef test_a():\n    '''x'''\n    pass\n")
    assert dup["n_methods"] == 2
    assert dup["name_collision"]["collision_rate"] == 0.5


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[metrics 自检] 全部通过 ({len(fns)} 项)")


if __name__ == "__main__":
    _run_all()
