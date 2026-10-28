# -*- coding: utf-8 -*-
"""
套件4: 方案层人决策评测 (proposal) — 接受率 / 验证成功率 (按方案类型分列)

评的不是黄金集, 是人对着 AI 方案真做过的判断: 数据源 decisions.jsonl
(accept/reject/defer 落账本, 6.4/6.5)。账本是本地文件、不进 git ——
没有账本就 SKIPPED, 绝不拿 0 条编出一个"接受率 0%"。

方案类型不在账本七个键里 (schema 锁死, 见 applier), 唯一诚实的来源是
join report.json (flywheel.decision_type_index); 报告没留档的条目归
unknown 桶并如实计数 —— 宁可一桶 unknown, 不从 note 里猜类型。

负样本清单当初就写了"将来做方案层评测的反面清单" (flywheel docstring),
这一套件是它兑现的第一步: 只消费决策与结果, 不需要浏览器、不花钱。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ai_kit.evaluation import metrics
from ai_kit.proposal import applier, flywheel

# 账本/报告位置: 参数 > 环境变量 > 当前目录 (与 run_eval 的"报告落到 cwd"同一口径)
DECISIONS_ENV = "AI_KIT_DECISIONS"
REPORTS_ENV = "AI_KIT_REPORTS"

TYPE_UNKNOWN = "unknown"


def decisions_path(decisions: Optional[str] = None) -> str:
    return (str(decisions or os.environ.get(DECISIONS_ENV) or "").strip()
            or os.path.join(os.getcwd(), applier.DECISIONS_NAME))


def _annotate_types(rows: List[Dict[str, Any]],
                    reports_target: str) -> List[Dict[str, Any]]:
    """给每行决策补 type; join 不上的留空串 (metrics 里归 unknown)"""
    type_index = flywheel.decision_type_index(reports_target)
    annotated: List[Dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("failure_id") or "").strip(),
               str(row.get("proposal_id") or "").strip())
        item = dict(row)
        item["type"] = type_index.get(key, "")
        annotated.append(item)
    return annotated


def run(decisions: Optional[str] = None,
        reports: Optional[str] = None) -> Dict[str, Any]:
    """读账本 -> 补类型 -> 两项指标 (总体 + 按类型分列)。

    返回里带 join 底数 (n_type_resolved / n_type_unknown): 报告没留档时
    "按类型分列"只剩 unknown 一桶, 这个事实要在数字旁边看得见, 不能装成
    类型本来就是 unknown。
    """
    ledger = decisions_path(decisions)
    if not os.path.isfile(ledger):
        return {"suite": "proposal", "status": "SKIPPED",
                "reason": (f"没有账本 {ledger} —— 先在宿主项目跑几轮 "
                           "python -m ai_kit.proposal.applier 的 accept/reject "
                           "(decisions.jsonl 是本地文件, 不进 git)")}
    rows = applier.read_decisions(ledger)
    if not rows:
        return {"suite": "proposal", "status": "SKIPPED",
                "reason": f"账本 {ledger} 是空的 (一行都没记上)"}

    target = (str(reports or os.environ.get(REPORTS_ENV) or "").strip()
              or os.getcwd())
    annotated = _annotate_types(rows, target)
    computed = metrics.proposal_decision_metrics(annotated)
    n_unknown = (computed["by_type"].get(TYPE_UNKNOWN) or {}).get("n", 0)

    return {
        "suite": "proposal",
        "status": "OK",
        "decisions": ledger,
        "reports_scanned": target,
        "n_rows": len(rows),
        "n_type_resolved": len(rows) - n_unknown,
        "n_type_unknown": n_unknown,
        "n": computed["n"],
        "overall": computed["overall"],
        "by_type": computed["by_type"],
    }


if __name__ == "__main__":
    import pprint
    pprint.pprint(run())
