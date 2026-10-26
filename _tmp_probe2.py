# -*- coding: utf-8 -*-
"""5.7 校验闭环探针: 三张合成页 + AST 三种模式"""
import io
import sys

sys.path.insert(0, ".")
out = io.open("_tmp_probe2.txt", "w", encoding="utf-8")


def p(*args):
    out.write(" ".join(str(a) for a in args) + "\n")


from ai_kit.perception.planner import (  # noqa: E402
    lint_source, plan, verify, dedupe_cases, step_tokens, normalize_title,
    jaccard, plan_from_dict, validate_schema, eid_format_problem, plan_verified,
)
from ai_kit.perception.inventory import from_inventory  # noqa: E402
sys.path.insert(0, "tests")
import test_planner as t  # noqa: E402


for name, payload in (("LIST", t.list_payload()), ("FORM", t.form_payload()),
                      ("LOGIN", t.login_payload())):
    inv = from_inventory(payload)
    base = plan(inv)
    p(f"\n===== {name}: plan {base.total} 条 =====")
    kept, dups = dedupe_cases(base.cases)
    p(f"去重后 {len(kept)} 条, 合并 {len(dups)} 条")
    for dup in dups:
        p("  DUP", dup)
    # 最像的几对
    pairs = []
    cases = list(base.cases)
    for i, a in enumerate(cases):
        for b in cases[i + 1:]:
            score = jaccard(step_tokens(a), step_tokens(b))
            same = normalize_title(a.title) == normalize_title(b.title)
            if score > 0.6 or same:
                pairs.append((round(score, 3), same, a.case_id, b.case_id, a.title))
    pairs.sort(reverse=True)
    for row in pairs[:6]:
        p("  PAIR", row)

    v = verify(base, inventory=inv)
    p("verify:", v.diagnostics["verify"])
    p("review:", [c.case_id for c in v.review])
    p("problems:", v.problems[:5])
    p("schema:", v.diagnostics["schema"])
    p("live_skipped:", v.diagnostics["live_skipped"])
    p("report tail:", v.report().splitlines()[-3:])

# ---- ① schema
p("\n===== ① schema =====")
good = plan(from_inventory(t.list_payload())).to_dict()
restored, problems = plan_from_dict(good)
p("roundtrip cases", len(restored.cases), "problems", problems)
p("roundtrip equal", restored.to_dict() == good)
bad = plan_from_dict({"cases": [{"case_id": "X", "title": "", "priority": "P9",
                                 "steps": [{"action": "explode", "eid": "zz9"},
                                           {"action": "fill", "eid": "e1", "value": 1,
                                            "locator": "#x"}]}]})
p("bad problems:", bad[1])
for eid in ("e1", "e12", "c0", "d1", "page", "chart:1", "chart:0",
            "c1.column:姓名", "c1.row:删除", "c1.column:", "x9", "e-1", ""):
    p(f"  eid {eid!r:18s} -> {eid_format_problem(eid)!r}")

# ---- ③ AST
p("\n===== ③ AST =====")
samples = {
    "standalone_ok": """
import pytest
from ai_kit import runtime

def test_x(driver):
    runtime.goto(driver, "https://x/list")
    el = runtime.wait_visible(driver, "css selector", "#kw")
    runtime.fill(el, "abc")
    runtime.click(el)
    assert runtime.get_text(el) == "abc"
""",
    "standalone_bad": """
from ai_kit import runtime
import requests
import os

def test_x(driver):
    el = driver.find_element("css selector", "#kw")
    el.send_keys("abc")
    runtime.fill(el, "abc")
    os.system("rm -rf /")
    eval("1+1")
""",
    "po_ok": """
import pytest
from pages.patient_list import PatientListPage

def test_x(driver):
    page = PatientListPage(driver)
    page.open()
    page.search_by_name("张三")
    assert page.row_count() == 3
""",
    "po_bad": """
from pages.patient_list import PatientListPage

def test_x(driver):
    page = PatientListPage(driver)
    page.open()
    page.search_by_telepathy("张三")
    page.driver_side_hack()
""",
    "broken": "def test_x(:\n",
}
for name, src in samples.items():
    mode = "po" if name.startswith("po") else "standalone" if name.startswith("standalone") else ""
    po = ("open", "search_by_name", "row_count") if name.startswith("po") else ()
    found = lint_source(src, mode=mode, po_methods=po)
    p(f"  {name} ({mode or 'no-mode'}): {len(found)} 条")
    for item in found:
        p("    -", item)

# ---- ⑤ 闭环
p("\n===== ⑤ plan_verified =====")
payload = t.login_payload()
inv = from_inventory(payload)
calls = []


def ask(prompt):
    calls.append(prompt)
    return {"cases": [{"case_id": "LG-001", "steps": [
        {"action": "fill", "eid": "u1", "value": "qa_user"},
        {"action": "fill", "eid": "p1", "value": "Pa55word!"},
        {"action": "click", "eid": "b1"},
        {"action": "assert_logged_in"}]}]}


def bad_ask(prompt):
    calls.append(prompt)
    return {"cases": [{"case_id": "LG-001", "steps": [{"action": "click", "eid": "e999"}]}]}


v1 = plan_verified(payload, ask=ask)
p("clean plan review:", len(v1.review), "| verify:", v1.diagnostics["verify"])
v2 = plan_verified(payload, ask=bad_ask)
p("bad rewrites review:", [(c.case_id, c.needs_review) for c in v2.cases][:3])
p("bad verify:", v2.diagnostics["verify"])
p("prompt carries errors:", "上次校验没通过" in calls[-1])

# ---- 单边: 强制把一条用例的 eid 改错, 看四层能否抓到
p("\n===== 破坏性检查 =====")
from dataclasses import replace  # noqa: E402

broken_case = replace(base.cases[0], steps=(replace(base.cases[0].steps[0], eid="e999"),))
broken_plan = replace(base, cases=(broken_case,) + base.cases[1:])
v3 = verify(broken_plan, inventory=inv)
p("broken case review:", broken_case.case_id, broken_case.cid)
p("needs_review:", list(v3.diagnostics["needs_review"])[:3])
p("problems:", v3.problems[:3])

# ---- ② 真浏览器
p("\n===== ② 真浏览器 =====")
from ai_kit.evaluation import driver_provider  # noqa: E402

LIST_HTML = """
<div class="filter-bar"><input name="kw" placeholder="关键字"><button>查询</button></div>
<table><thead><tr><th>名称</th></tr></thead>
<tbody><tr><td>记录A</td><td><button>删除</button></td></tr></tbody></table>
"""
drv = driver_provider.get_driver()
if drv is None:
    p("没有浏览器, 跳过")
else:
    import tempfile, os  # noqa: E402
    from ai_kit.perception import from_collect  # noqa: E402

    path = os.path.join(tempfile.mkdtemp(), "p.html")
    io.open(path, "w", encoding="utf-8").write(
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>t</title></head><body>"
        + LIST_HTML + "</body></html>")
    drv.get(driver_provider.to_file_url(path))
    inv2 = from_collect(drv)
    base2 = plan(inv2)
    p("真页 plan", base2.total, "条, 元素", len(inv2))
    v4 = verify(base2, inventory=inv2, driver=drv)
    p("真页 verify:", v4.diagnostics["verify"])
    p("live:", v4.diagnostics["live"][:4])
    p("live_skipped:", v4.diagnostics["live_skipped"][:6])
    # 把 eid 指向一个不存在的 ref, 看 live 层是否抓到
    if base2.cases:
        bad = replace(base2.cases[0], steps=(
            replace(base2.cases[0].steps[0], eid="e999"),))
        v5 = verify(replace(base2, cases=(bad,) + base2.cases[1:]),
                    inventory=inv2, driver=drv)
        p("e999 live:", [line for line in v5.diagnostics["live"] if "e999" in line][:2])
    driver_provider.quit_driver(drv)

out.write("\n")
out.close()
print("ok")
