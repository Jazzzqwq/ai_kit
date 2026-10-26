# -*- coding: utf-8 -*-
import io
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
out = io.open("_tmp_dbg.txt", "w", encoding="utf-8")


def p(*a):
    out.write(" ".join(str(x) for x in a) + "\n")


import test_planner as t
from ai_kit.perception.inventory import from_inventory
from ai_kit.perception.planner import plan, plan_verified

rounds = []


def dirty(target):
    return {case.case_id: "def test_x():\n    eval('1')\n" for case in target.cases}


def render(target):
    rounds.append(len(target.cases))
    return dirty(target) if len(rounds) == 1 else {}


def fixed(target):
    case = target.cases[0]
    return {"cases": [{"case_id": case.case_id,
                       "steps": [{"action": "click", "eid": "e3"},
                                 {"action": "assert_no_error_toast"}]}]}


prompts = []


def ask(prompt):
    prompts.append(prompt)
    return fixed(plan(from_inventory(t.list_payload())))


result = plan_verified(t.list_payload(), ask=ask, render=render)
p("rounds:", rounds, "prompts:", len(prompts))
d = result.diagnostics
p("verify:", d["verify"])
p("schema:", d["schema"])
p("live:", d["live"][:3])
p("live_skipped:", d["live_skipped"][:3])
p("ast:", d["ast"][:3])
p("ast_skipped:", d.get("ast_skipped"))
p("review count:", len(result.review))
for cid, msgs in list(d["needs_review"].items())[:2]:
    p(" ", cid, msgs[:3])
p("cases:", len(result.cases))
out.close()
print("ok")
