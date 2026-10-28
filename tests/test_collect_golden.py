# -*- coding: utf-8 -*-
"""
任务卡7 守门测试: 采集冻结 (脱敏承诺) / 零残留扫描 / golden 底线 / 感知链接线

冻结环节的承诺全部在这里固化:
- 域名/标题合成化, html 丢弃, 表格裸文本清零 (结构 row_count/columns 保留);
- sanitize + --scrub 凭据词; 落盘前零残留扫描命中即拒写;
- 手写 case 一个字不动; collected 是合法 source; generator 底线 >= 8。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_kit.evaluation import collect_golden as cg
from ai_kit.evaluation import golden, metrics

REPO_ROOT = Path(__file__).resolve().parents[1]

PHONE = "13800138000"
IDCARD = "110101199001011234"
HOSPITAL = "云南省第二人民医院"


def _fake_page():
    """一页未脱敏的采集结果 (PII 埋进 html/sample_rows/title/url), 冻结必须全抹掉"""
    return {
        "key": "users", "page_name": "用户管理", "route": "#/system/users",
        "notes": [],
        "inventory": {
            "url": "https://admin-test.example-inner.com/#/system/users",
            "title": HOSPITAL,
            "collector_version": 1,
            "counts": {"discovered": 6, "interactive": 4, "kept": 4,
                       "folded_rows": 0, "inputs": 2, "buttons": 1,
                       "selects": 1, "tables": 1},
            "inputs": [
                {"eid": "e1", "kind": "input", "text": "", "name": "patientName",
                 "attrs": {"placeholder": "请输入患者姓名", "value": HOSPITAL},
                 "html": f"<input value='{PHONE}'>", "locator": {"by": "css", "value": "#patientName"},
                 "container": "main", "risk": ""},
                {"eid": "e2", "kind": "input", "text": "", "name": "password",
                 "attrs": {"placeholder": "请输入密码"}, "html": "",
                 "locator": {"by": "css", "value": "#password"},
                 "container": "main", "risk": ""},
            ],
            "buttons": [
                {"eid": "e3", "kind": "button", "text": "查询", "name": "",
                 "attrs": {}, "html": f"<button>{HOSPITAL}</button>",
                 "locator": {"by": "xpath", "value": f"//span[text()='{HOSPITAL}']"},
                 "container": "main", "risk": ""},
            ],
            "selects": [
                {"eid": "e5", "kind": "select", "text": "", "name": "dept",
                 "attrs": {}, "html": "", "options": [HOSPITAL],
                 "locator": {"by": "css", "value": "#dept"},
                 "container": "main", "risk": ""},
            ],
            "tables": [
                {"eid": "c1", "kind": "table", "text": "", "name": "",
                 "attrs": {}, "html": f"<table>{PHONE}{IDCARD}</table>",
                 "locator": {"by": "css", "value": ".el-table"},
                 "container": "main", "risk": "",
                 "columns": ["姓名", "手机号", "操作"],
                 "row_count": 42,
                 "sample_rows": [["张三", PHONE, IDCARD]],
                 "row_actions": [{"text": "删除", "eid": "c1.a1", "count": 42,
                                  "locator": {"by": "xpath", "value": "//a"}}]},
            ],
            "dialogs": [{"eid": "d1", "selector": ".el-dialog",
                         "text": HOSPITAL * 4, "buttons": ["确定", "取消"]}],
            "risks": {"destructive": 1},
        },
    }


# ---------------------------------------------------------------------------
# sanitize_inventory: 脱敏承诺逐条固化
# ---------------------------------------------------------------------------

def test_sanitize_inventory_strips_every_pii_channel():
    inv = cg.sanitize_inventory(_fake_page()["inventory"], "#/system/users",
                                "用户管理", [HOSPITAL])
    text = json.dumps(inv, ensure_ascii=False)
    assert inv["url"] == cg.FAKE_ORIGIN + "/#/system/users"   # 域名换合成, 只留路由
    assert inv["title"] == "用户管理"                          # 标题换成 page_name
    assert inv["freeze"] == {"domain": "fake", "sample_rows": "cleared",
                             "html": "dropped", "sanitize": True}
    for bucket in ("inputs", "buttons", "selects", "tables"):
        for item in inv[bucket]:
            assert item["html"] == ""                          # outerHTML 一律丢
    assert inv["tables"][0]["sample_rows"] == []               # 表格裸文本清零
    assert inv["tables"][0]["row_count"] == 42                 # 结构信息保留
    assert inv["tables"][0]["columns"] == ["姓名", "手机号", "操作"]
    assert len(inv["dialogs"][0]["text"]) <= 40
    assert HOSPITAL not in text and PHONE not in text and IDCARD not in text
    assert "[USER]" in text                                    # 凭据词替换生效
    assert not cg.residual_hits(text, [HOSPITAL])              # 零残留扫描干净


# ---------------------------------------------------------------------------
# freeze_case: 合成出的 case 必须是合法 golden, 期望值从结构推导
# ---------------------------------------------------------------------------

def test_freeze_case_is_valid_golden_and_derives_expected():
    case = cg.freeze_case(_fake_page(), [HOSPITAL])
    assert case["id"] == "G_users"
    assert case["source"] == "collected" and case["source"] in golden.SOURCES
    assert case["module"] == "generator" and case["rule_version"] == cg.RULE_VERSION
    assert set(case["input"]) >= {"page_name", "inventory", "po_class"}
    assert case["input"]["po_class"] == "UsersPage"
    # 期望值是推出来的: 按钮"查询"->query, 行内"删除"->delete + 关键词
    assert "query" in case["expected"]["categories"]
    assert "delete" in case["expected"]["categories"]
    assert "查询" in case["expected"]["keywords"]
    assert "删除" in case["expected"]["keywords"]
    assert golden.check([case], "generator") == []            # schema 体检必须过


# ---------------------------------------------------------------------------
# write_golden: 合并不动手写; 体检不过拒落盘
# ---------------------------------------------------------------------------

def _write_manual(path: Path):
    path.write_text(json.dumps({"cases": [{
        "id": "G1_user_management", "module": "generator",
        "rule_version": "generator.v1", "source": "manual",
        "input": {"page_name": "手编页"},
        "expected": {"keywords": ["查询"]}, "note": "手写",
    }]}, ensure_ascii=False), encoding="utf-8")


def test_write_golden_merges_without_touching_manual(tmp_path):
    _write_manual(tmp_path / golden.FILES["generator"])
    out = cg.write_golden([cg.freeze_case(_fake_page(), [HOSPITAL])], str(tmp_path))
    assert out["n_total"] == 2
    saved = json.loads((tmp_path / golden.FILES["generator"]).read_text(encoding="utf-8"))
    manual = [c for c in saved["cases"] if c["id"] == "G1_user_management"][0]
    assert manual["input"] == {"page_name": "手编页"}          # 手写 case 一个字不动
    collected = [c for c in saved["cases"] if c["id"] == "G_users"][0]
    assert collected["source"] == "collected"


def test_write_golden_refuses_invalid_cases(tmp_path):
    _write_manual(tmp_path / golden.FILES["generator"])
    bad = cg.freeze_case(_fake_page(), [HOSPITAL])
    bad["expected"] = {}                                      # 判不出对错的样本不算样本
    with pytest.raises(SystemExit):
        cg.write_golden([bad], str(tmp_path))
    # 拒落后文件原样
    saved = json.loads((tmp_path / golden.FILES["generator"]).read_text(encoding="utf-8"))
    assert len(saved["cases"]) == 1


# ---------------------------------------------------------------------------
# freeze 主流程: 零残留命中即拒绝写出
# ---------------------------------------------------------------------------

def test_freeze_main_refuses_on_residual(tmp_path):
    # 真实存在的漏网通道: sanitize 只认半角分隔的手机号, 全角空格分隔的能过脱敏 ——
    # 落盘前的零残留扫描认 [-\s], 第二道闸命中必须拒写
    page = _fake_page()
    page["inventory"]["notes"] = ["联系 138　0013　8000"]
    src = tmp_path / "collected.json"
    src.write_text(json.dumps([page], ensure_ascii=False), encoding="utf-8")
    out_dir = tmp_path / "golden"
    out_dir.mkdir()
    (out_dir / golden.FILES["generator"]).write_text(
        json.dumps({"cases": []}), encoding="utf-8")
    with pytest.raises(SystemExit):
        cg.main(["freeze", "--in", str(src), "--golden-dir", str(out_dir)])
    # 拒写: 目标文件还是空的
    assert json.loads((out_dir / golden.FILES["generator"]).read_text(encoding="utf-8"))["cases"] == []


def test_residual_hits_detects_phone_id_and_words():
    assert cg.residual_hits("备注 13800138000", [])
    assert cg.residual_hits("证件 110101199001011234", [])
    assert cg.residual_hits(f"欢迎 {HOSPITAL}", [HOSPITAL])
    assert cg.residual_hits("干净的结构描述: 表格 42 行", [HOSPITAL]) == []


# ---------------------------------------------------------------------------
# golden 底线: generator >= 8, collected 是合法 source
# ---------------------------------------------------------------------------

def test_generator_golden_meets_its_floor():
    cases = golden.load("generator", directory=str(REPO_ROOT / "ai_kit" / "evaluation" / "golden"))
    stat = golden.scale(cases, "generator")
    assert stat["floor"] >= 8
    assert stat["ok"], f"generator 黄金集只剩 {stat['n']} 条, 低于底线 {stat['floor']}"
    assert golden.check(cases, "generator") == []
    assert sum(1 for c in cases if c["source"] == "collected") >= 8


# ---------------------------------------------------------------------------
# eval_generator 感知链接线: 真 golden 跑通 chain, 率与计数自洽
# ---------------------------------------------------------------------------

def test_chain_metrics_on_real_golden():
    from ai_kit.evaluation import eval_generator
    r = eval_generator.run()
    ch = r["chain"]
    assert ch["n_cases"] >= 8 and ch["n_errors"] == 0
    m = ch["metrics"]
    assert m["n_refs"] > 0 and m["refs_resolved"] <= m["n_refs"]
    assert m["n_cases"] > 0 and m["cases_executable"] <= m["n_cases"]
    # 率由计数重算得出 (渲染层拿到的不是拍脑袋数)
    expect = metrics.perception_chain_metrics(
        [x for row in r["cases"] if isinstance(row.get("chain"), dict)
         for x in row["chain"].get("refs") or ()],
        [x for row in r["cases"] if isinstance(row.get("chain"), dict)
         for x in row["chain"].get("cases") or ()])
    assert m == expect


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            import inspect
            if inspect.signature(fn).parameters:
                continue
            fn()
            print(f"  ✓ {name}")
    print("\n[collect_golden 自检] 全部通过")
