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


def test_ece_bin_count_is_configurable():
    """7.1 口径是 5 箱; 分箱数变了可靠性表长度跟着变"""
    r = m.expected_calibration_error([(0.9, True), (0.1, False)], n_bins=5)
    assert len(r["reliability"]) == 5
    assert r["reliability"][4]["count"] == 1 and r["reliability"][0]["count"] == 1


def test_confidence_gap():
    # 平均置信 .9, 准确率 .5 -> 说得好听做得不行
    over = m.confidence_gap([(0.9, True), (0.9, False)])
    assert over["verdict"] == "过度自信" and _close(over["gap"], 0.4)
    # 平均置信 .5, 准确率 1.0 -> 比实际表现保守
    under = m.confidence_gap([(0.5, True), (0.5, True)])
    assert under["verdict"] == "欠自信" and _close(under["gap"], -0.5)
    # 平均置信 .8, 准确率 4/5=.8 -> 贴着对角线
    good = m.confidence_gap([(0.8, True)] * 4 + [(0.8, False)])
    assert good["verdict"] == "校准良好" and _close(good["gap"], 0.0)
    assert m.confidence_gap([])["verdict"] == "无样本"


def test_layer_breakdown():
    baseline = {"L1": 0.3, "L2": 0.5, "L3": 0.1, "L4": 0.1}
    r = m.layer_breakdown(["L1", "L2", "L2", None], baseline=baseline)
    assert r["per_layer"]["L2"]["count"] == 2
    assert _close(r["per_layer"]["L2"]["share"], 0.5)
    assert _close(r["per_layer"]["L2"]["delta"], 0.0), "正好撞上 README 估算"
    assert _close(r["per_layer"]["L1"]["delta"], -0.05)
    assert r["no_hit"] == 1 and _close(r["no_hit_share"], 0.25)
    assert "baseline" not in m.layer_breakdown(["L1"])["per_layer"]["L1"], \
        "不给基线就不编差值"


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


def test_proposal_decision_metrics_overall():
    """接受率 = accept/(accept+reject), defer 不进分母; 验证成功率 = 采纳且 passed / 采纳数"""
    rows = [
        {"type": "locator_patch", "decision": "accept", "verify_result": "passed"},
        {"type": "locator_patch", "decision": "accept", "verify_result": "failed"},
        {"type": "locator_patch", "decision": "reject", "verify_result": "skipped"},
        {"type": "wait_adjust", "decision": "defer", "verify_result": "skipped"},
    ]
    r = m.proposal_decision_metrics(rows)
    assert r["n"] == 4
    o = r["overall"]
    assert o["decided"] == 3 and o["accepted"] == 2 and o["rejected"] == 1
    assert o["deferred"] == 1 and o["verified"] == 1
    assert _close(o["acceptance_rate"], 2 / 3), o
    assert _close(o["verify_success_rate"], 1 / 2), o


def test_proposal_decision_metrics_by_type_and_unknown():
    """按类型分列; type 缺失归 unknown 桶, 不编类型"""
    rows = [
        {"type": "data_fix", "decision": "accept", "verify_result": "passed"},
        {"type": "", "decision": "reject", "verify_result": "skipped"},
    ]
    r = m.proposal_decision_metrics(rows)
    assert set(r["by_type"]) == {"data_fix", "unknown"}, r["by_type"]
    assert r["by_type"]["data_fix"]["acceptance_rate"] == 1.0
    assert r["by_type"]["unknown"]["rejected"] == 1


def test_proposal_decision_metrics_empty_denominators():
    """一条都没采纳 (accepted=0) 或全 defer (decided=0): 率为 0.0, 由渲染层显 n/a"""
    r = m.proposal_decision_metrics(
        [{"type": "env_issue", "decision": "defer", "verify_result": "skipped"}])
    assert r["overall"]["decided"] == 0
    assert r["overall"]["acceptance_rate"] == 0.0
    assert r["overall"]["verify_success_rate"] == 0.0
    assert m.proposal_decision_metrics([])["n"] == 0


def test_perception_chain_metrics_rates():
    """eid 解析率 = resolved/全部引用; 可执行率 = 出码且未标 review/全部用例"""
    r = m.perception_chain_metrics(
        [{"resolved": True}, {"resolved": True}, {"resolved": False}],
        [{"rendered": True, "needs_review": False},
         {"rendered": True, "needs_review": True},
         {"rendered": False, "needs_review": False}])
    assert r["n_refs"] == 3 and r["refs_resolved"] == 2
    assert r["eid_parse_rate"] == round(2 / 3, 4)
    assert r["n_cases"] == 3 and r["cases_executable"] == 1
    assert r["executable_rate"] == round(1 / 3, 4)
    # 出码但标了 review 不算可执行; 没出码也不算
    assert m.perception_chain_metrics([], [{"rendered": True, "needs_review": True}])["executable_rate"] == 0.0


def test_perception_chain_metrics_empty_denominators():
    """没采到 inventory (分母 0): 率是 0.0, 由渲染层结合计数显 n/a, 不是假 0%"""
    r = m.perception_chain_metrics([], [])
    assert r["n_refs"] == 0 and r["n_cases"] == 0
    assert r["eid_parse_rate"] == 0.0 and r["executable_rate"] == 0.0


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"\n[metrics 自检] 全部通过 ({len(fns)} 项)")


if __name__ == "__main__":
    _run_all()
