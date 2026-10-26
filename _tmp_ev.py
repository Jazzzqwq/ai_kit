# -*- coding: utf-8 -*-
import io
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
out = io.open("_tmp_ev.txt", "w", encoding="utf-8")


def p(*a):
    out.write(" ".join(str(x) for x in a) + "\n")


import test_planner as t
from ai_kit.perception.inventory import from_inventory
from ai_kit.perception.planner import plan, verify, plan_from_dict

for name, payload in (("LIST", t.list_payload()), ("FORM", t.form_payload()),
                      ("LOGIN", t.login_payload())):
    inv = from_inventory(payload)
    base = plan(inv)
    v = verify(base, inventory=inv, dedupe=False)
    p(f"{name}: {base.total} 条 | verify -> {v.diagnostics['verify']} | review {len(v.review)}")
    p("   live_skipped:", v.diagnostics["live_skipped"][:2])

target = plan(from_inventory(t.list_payload()))
v = verify(target, inventory=t.list_payload())
p(f"LIST 去重: {target.total} -> {v.total}")
for line in v.diagnostics["duplicates"]:
    p("   ", line)
restored, problems = plan_from_dict(target.to_dict())
p("roundtrip:", len(restored.cases), "problems:", problems, "equal:",
  restored.to_dict() == target.to_dict())
out.close()
print("ok")
