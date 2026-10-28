# -*- coding: utf-8 -*-
"""
数据飞轮 — 把人做过的判断, 变成下一轮评测的黄金数据

一份账本只有七个键 (6.4 定的 schema, 不再加): {ts, failure_id, proposal_id, decision,
verify_result, operator, note}。七个键说得出"谁在哪条失败上采纳/拒绝了哪个方案",
却说不出"那次失败的现场长什么样" —— 而黄金样本需要 traceback 与日志。所以导出是一次
join: decisions.jsonl × report.json (report.write 的产物), 按 failure_id 对上,
按 proposal_id 取方案详情。

两类样本, 分两个文件:

    verified (accept + 重跑通过)      -> flywheel_positive.json    正样本
    rejected (人拒 / 采纳但验证不过)  -> flywheel_negative.json    负样本 (含理由)

正样本喂给 evaluation/eval_analyzer: 它读 input.traceback / input.log / expected.category,
本模块写出的 case 就是 7.2 的黄金集 schema, 与手写的 analyzer_cases.json 同形,
所以评测集随使用自然增长 (run_eval --no-flywheel 可退回手写基线做对比)。

负样本不进任何套件的分母 —— 这条区分是有原则的: reject 只说"这个方案不对", 不说明
"归因错了"。把一条定位器补丁被拒的样本塞进分类准确率, 等于把正确的归因算成错。
它是一份供人复盘、以及将来做方案层评测的反面清单。

每周跑一次, 幂等 (重复跑只追加新条目, 不改既有条目):

    python -m ai_kit.proposal.flywheel --decisions decisions.jsonl \\
        --reports artifacts/ --since 2026-10-20 --dry-run

缺 report.json 的决策一律跳过并点名 —— 现场没了就补不出样本, 宁可少一条, 不编一条。

飞轮的偏置 (不藏着): 正样本的 expected_category 取自报告里的 diagnosis, 是**机器给的**,
不是人独立标注的。人采纳补丁并通过验证, 只证明"这条归因链上的动作是对的", 不等于
"分类一定对"。所以每条 case 带 category_source 记着它出自哪版规则; 真要当归因黄金用,
先人工复核 category。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from ai_kit.proposal import applier

FLYWHEEL_VERSION = 1

POSITIVE_NAME = "flywheel_positive.json"
NEGATIVE_NAME = "flywheel_negative.json"

# 默认落在包内的评测黄金集目录 (与 analyzer_cases.json 同处)
DEFAULT_GOLDEN = Path(__file__).resolve().parents[1] / "evaluation" / "golden"

# 文本上限: 黄金集是资产也是仓库里的文件, 一条 200KB 的栈会把 diff 淹掉
MAX_TEXT_CHARS = 4000
MAX_DIFF_CHARS = 12000
MAX_NOTE_CHARS = 1200

# 扫报告时剪掉的目录: --reports 默认是当前目录, 不剪枝会在 .venv 里翻几万个 json
_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".idea", ".vscode", "site-packages",
})

_COMMENTS = {
    "positive": (
        "数据飞轮正样本 (6.6): decisions.jsonl 里 accept 且重跑验证通过的记录, "
        "join report.json 得到失败现场, 写成 7.2 黄金集 schema (source=flywheel, "
        "module=analyzer)。eval_analyzer 默认并入黄金集 (--no-flywheel 退回手写基线)。"
        "expected.category 取自报告里的 diagnosis, 是机器给的, category_source 记着出处 ",
        "—— 当归因黄金用之前应先人工复核。由 python -m ai_kit.proposal.flywheel "
        "生成: 只追加, 不改既有条目。"),
    "negative": (
        "数据飞轮负样本 (6.6): 人拒掉的方案, 以及采纳了但验证没过的补丁, 都带 "
        "rejected_reason。module=proposal —— 不进任何评测套件的分母: reject 只说这个"
        "方案不对, 不说明归因错了。用途是复盘与将来做方案层评测的反面清单。"),
}


class FlywheelError(RuntimeError):
    """用不了: 没有账本 / 报告位置不存在 / 文件写不了"""


# ---------------------------------------------------------------------------
# 样本: 一条决策 -> 一条 case
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Build:
    """一次 join 的结果 (还没落盘)"""

    positive: Tuple[Dict[str, Any], ...] = ()
    negative: Tuple[Dict[str, Any], ...] = ()
    skipped: Tuple[Dict[str, str], ...] = ()
    rows: int = 0


def _clip(text: Any, limit: int) -> Tuple[str, bool]:
    """保头保尾: 栈的开头是用例名, 结尾才是那句异常 (中间截掉最不损失信息)"""
    value = str(text or "")
    if len(value) <= limit:
        return value, False
    head = value[: limit // 2]
    tail = value[-(limit // 2):]
    return f"{head}\n… (中间省略 {len(value) - limit} 字符) …\n{tail}", True


def _case_id(failure_id: str, proposal_id: str) -> str:
    """稳定 id: 同一 failure + 同一方案永远是同一条 (幂等靠它去重)"""
    body = re.sub(r"[^0-9A-Za-z_.-]+", "-", f"{failure_id}-{proposal_id}").strip("-.")
    return f"FLY-{body or 'case'}"[:96]


def _skip(row: Mapping[str, Any], reason: str) -> Dict[str, str]:
    return {"failure_id": str(row.get("failure_id") or ""),
            "proposal_id": str(row.get("proposal_id") or ""),
            "reason": reason}


def _base_case(row: Mapping[str, Any], payload: Mapping[str, Any],
               proposal: Mapping[str, Any], *, module: str) -> Dict[str, Any]:
    """正负样本共有的字段: 现场 + 归因 + 方案 + 谁在什么时候决定的

    写成 7.2 的黄金集 schema ({id, module, rule_version, source, input, expected, note}),
    溯源字段 (failure_id / category_source …) 额外平铺 —— schema 没说不能多, 而少了
    它们就回不到那条决策。
    """
    failure_id = str(row.get("failure_id") or "").strip()
    proposal_id = str(row.get("proposal_id") or "").strip()
    diagnosis = payload.get("diagnosis")
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    trace, trace_cut = _clip(payload.get("traceback"), MAX_TEXT_CHARS)
    log, log_cut = _clip(payload.get("log"), MAX_TEXT_CHARS)
    case: Dict[str, Any] = {
        "id": _case_id(failure_id, proposal_id),
        "module": module,
        "rule_version": str(diagnosis.get("rule_version") or "unknown"),
        "source": "flywheel",
        "input": {"traceback": trace, "log": log},
        "expected": {"category": str(diagnosis.get("category") or ""),
                     "acceptable_categories": []},
        "note": str(row.get("note") or ""),
        # 标签来源: 分类是规则/LLM 给的, 人只确认了动作 (采纳且补丁通过)
        "category_source": f"diagnosis@{diagnosis.get('rule_version') or 'unknown'}",
        "blame": str(diagnosis.get("blame") or ""),
        "verify_cmd": str(proposal.get("verify_cmd") or ""),
        "failure_id": failure_id,
        "proposal_id": proposal_id,
        "decided_by": str(row.get("operator") or ""),
        "decided_at": str(row.get("ts") or ""),
        "verify_result": str(row.get("verify_result") or ""),
    }
    if trace_cut or log_cut:
        case["truncated"] = True
    return case


def _positive(row: Mapping[str, Any], payload: Mapping[str, Any],
              proposal: Mapping[str, Any]) -> Dict[str, Any]:
    """正样本: 人采纳 + 验证通过 -> 期望输出 (分类 / 方案类型 / 目标文件 / 补丁)"""
    case = _base_case(row, payload, proposal, module="analyzer")
    case["label"] = "positive"
    diff, diff_cut = _clip(proposal.get("diff"), MAX_DIFF_CHARS)
    expected = case["expected"]
    expected["type"] = str(proposal.get("type") or "")
    expected["target_file"] = str(proposal.get("target_file") or "")
    expected["diff"] = diff
    expected["risk"] = str(proposal.get("risk") or "")
    if diff_cut:
        case["truncated"] = True
    return case


def _negative(row: Mapping[str, Any], payload: Mapping[str, Any],
              proposal: Mapping[str, Any]) -> Dict[str, Any]:
    """负样本: 只要"为什么不行" —— 人的拒绝理由, 或验证失败的输出尾巴"""
    case = _base_case(row, payload, proposal, module="proposal")
    case["label"] = "negative"
    reason, _ = _clip(row.get("note"), MAX_NOTE_CHARS)
    case["rejected_reason"] = reason
    case["expected"]["type"] = str(proposal.get("type") or "")
    case["expected"]["target_file"] = str(proposal.get("target_file") or "")
    case["expected"]["diff"] = str(proposal.get("diff") or "")
    return case


# ---------------------------------------------------------------------------
# 读: 账本 + 报告目录
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Optional[Any]:
    """读不懂就当没有 (目录里必然混着别的 json); 单个坏文件不该让整次导出失败"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _is_report(payload: Any) -> bool:
    """report.write 的产物: 有 failure_id + proposals 列表 + diagnosis"""
    return (isinstance(payload, dict)
            and bool(str(payload.get("failure_id") or "").strip())
            and isinstance(payload.get("proposals"), list)
            and isinstance(payload.get("diagnosis"), dict))


def _iter_json(root: Path) -> Iterator[Path]:
    """递归找 json, 剪掉依赖/缓存目录 (报告可能散在各次运行自己的 artifacts 下)"""
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in _SKIP_DIRS)
        for name in sorted(files):
            if name.endswith(".json"):
                yield Path(base) / name


def read_reports(target: Any) -> Dict[str, Dict[str, Any]]:
    """failure_id -> report.json 内容

    target 可以是单个文件, 也可以是目录 (递归)。同一次失败存在多份报告时取 created_at
    最新的那份 —— 报告是重新生成的, 旧的那份现场可能已经被修掉了。
    """
    path = Path(str(target))
    if not path.exists():
        raise FlywheelError(f"报告位置不存在: {path} (--reports 指向 report.json 或它所在目录)")
    files = [path] if path.is_file() else list(_iter_json(path))
    found: Dict[str, Dict[str, Any]] = {}
    for item in files:
        payload = _read_json(item)
        if not _is_report(payload):
            continue
        failure_id = str(payload.get("failure_id") or "").strip()
        old = found.get(failure_id)
        if old is None or (str(payload.get("created_at") or "")
                           >= str(old.get("created_at") or "")):
            found[failure_id] = payload
    return found


def _proposal_of(payload: Mapping[str, Any],
                 proposal_id: str) -> Optional[Dict[str, Any]]:
    """按编号取方案 (与 applier._proposal_of 同口径: 精确匹配)"""
    for raw in payload.get("proposals") or ():
        if isinstance(raw, dict) and str(raw.get("proposal_id") or "").strip() == proposal_id:
            return raw
    return None


def decision_type_index(reports_target: Any) -> Dict[Tuple[str, str], str]:
    """(failure_id, proposal_id) -> 方案类型。

    账本七个键里没有 type, 而方案层评测要"按类型分列" —— 唯一诚实的来源是 join
    report.json (proposal.type 是方案生成时定死的), 而不是从 note 里猜。报告不存在时
    返回空表, 调用方自己把该条归 unknown。与建正/负样本走同一张 read_reports。
    """
    index: Dict[Tuple[str, str], str] = {}
    try:
        reports = read_reports(reports_target)
    except FlywheelError:
        return index
    for failure_id, payload in reports.items():
        for raw in payload.get("proposals") or ():
            if not isinstance(raw, dict):
                continue
            pid = str(raw.get("proposal_id") or "").strip()
            kind = str(raw.get("type") or "").strip()
            if pid and kind:
                index[(failure_id, pid)] = kind
    return index


# ---------------------------------------------------------------------------
# 拼: 一条决策该变成什么
# ---------------------------------------------------------------------------

def build(decisions_path: Any = applier.DECISIONS_NAME,
          reports_target: Any = ".", *, since: str = "") -> Build:
    """decisions.jsonl × 报告 -> 正样本 / 负样本 / 跳过清单 (不落盘, 便于 --dry-run)

    since 非空时只认 ts >= since 的决策 (ts 是 "2026-10-20T09:00:00Z", 前缀比较即可)。
    过滤发生在 join 之前: 周导出不该为了筛掉旧记录先读一遍现场。
    """
    ledger = Path(str(decisions_path))
    if not ledger.is_file():
        raise FlywheelError(f"没有账本: {ledger} (先做几次 accept / reject, 账本在 6.4/6.5 里)")
    rows = applier.read_decisions(ledger)
    reports = read_reports(reports_target)
    cutoff = str(since or "").strip()

    positive: List[Dict[str, Any]] = []
    negative: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    for row in rows:
        if cutoff and str(row.get("ts") or "") < cutoff:
            skipped.append(_skip(row, f"决策时间 {row.get('ts')} 早于 {cutoff}"))
            continue
        state = applier.status_of(row)
        if state == applier.OUTCOME_PENDING:
            # defer: 人还没决定 —— 没有标签, 也就没有样本
            skipped.append(_skip(row, "还在 defer, 没有标签"))
            continue
        failure_id = str(row.get("failure_id") or "").strip()
        payload = reports.get(failure_id)
        if payload is None:
            skipped.append(_skip(row, "缺 report.json (没有失败现场, 补不出样本)"))
            continue
        proposal = _proposal_of(payload, str(row.get("proposal_id") or "").strip())
        if proposal is None:
            skipped.append(_skip(row, "报告里没有这个方案编号 (报告与账本不是同一批?)"))
            continue
        if state == applier.OUTCOME_VERIFIED:
            if not str(((payload.get("diagnosis") or {}).get("category") or "")).strip():
                # 没有期望值就不是样本; 写个空字符串进黄金集, 等于给自己的评分放水
                skipped.append(_skip(row, "报告没有 diagnosis.category, 当不了期望值"))
                continue
            positive.append(_positive(row, payload, proposal))
        else:
            negative.append(_negative(row, payload, proposal))
    return Build(positive=tuple(positive), negative=tuple(negative),
                 skipped=tuple(skipped), rows=len(rows))


# ---------------------------------------------------------------------------
# 写: 只追加 + 原子替换
# ---------------------------------------------------------------------------

def load_cases(path: Any) -> List[Dict[str, Any]]:
    """读已有的 golden 文件 (不存在 = 空; 坏文件不静默覆盖, 由 write 里的人来看)"""
    payload = _read_json(Path(str(path)))
    if not isinstance(payload, dict):
        return []
    cases = payload.get("cases")
    return [item for item in cases if isinstance(item, dict)] if isinstance(cases, list) else []


def write_cases(path: Any, cases: Sequence[Mapping[str, Any]], *, kind: str) -> int:
    """整份重写: 黄金集不能半截 (先写 .tmp 再 os.replace)"""
    target = Path(str(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": FLYWHEEL_VERSION,
        "kind": kind,
        "_comment": _COMMENTS.get(kind, ""),
        "cases": list(cases),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, target)
    return len(payload["cases"])


def export(decisions_path: Any = applier.DECISIONS_NAME,
           reports_target: Any = ".", *, golden: Any = DEFAULT_GOLDEN,
           since: str = "", dry_run: bool = False) -> Dict[str, Any]:
    """导出一次: 返回摘要 (加了什么 / 跳过什么), 文件只追加不改旧条目"""
    built = build(decisions_path, reports_target, since=since)
    folder = Path(str(golden))
    positive_path = folder / POSITIVE_NAME
    negative_path = folder / NEGATIVE_NAME
    old_positive = load_cases(positive_path)
    old_negative = load_cases(negative_path)
    known = {str(case.get("id")) for case in old_positive + old_negative}

    fresh_positive = [case for case in built.positive if str(case.get("id")) not in known]
    fresh_negative = [case for case in built.negative if str(case.get("id")) not in known]
    duplicates = (len(built.positive) + len(built.negative)
                  - len(fresh_positive) - len(fresh_negative))

    if not dry_run:
        if fresh_positive:
            write_cases(positive_path, old_positive + fresh_positive, kind="positive")
        if fresh_negative:
            write_cases(negative_path, old_negative + fresh_negative, kind="negative")

    return {
        "decisions": str(decisions_path),
        "rows": built.rows,
        "positive_added": len(fresh_positive),
        "negative_added": len(fresh_negative),
        "positive_total": len(old_positive) + len(fresh_positive),
        "negative_total": len(old_negative) + len(fresh_negative),
        "positive_path": str(positive_path),
        "negative_path": str(negative_path),
        "duplicates": duplicates,
        "skipped": list(built.skipped),
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _summary(result: Mapping[str, Any], *, since: str = "") -> str:
    lines = [f"[飞轮] 账本 {result['decisions']}: 读了 {result['rows']} 条决策"
             + (f" (只看 {since} 之后)" if since else "")]
    lines.append(f"  正样本 +{result['positive_added']} -> "
                 f"{Path(str(result['positive_path'])).name} "
                 f"(共 {result['positive_total']})")
    lines.append(f"  负样本 +{result['negative_added']} -> "
                 f"{Path(str(result['negative_path'])).name} "
                 f"(共 {result['negative_total']})")
    if result["duplicates"]:
        lines.append(f"  已存在 {result['duplicates']} 条: 幂等跳过, 既有条目一个字都不改")
    if result["skipped"]:
        lines.append(f"  跳过 {len(result['skipped'])} 条:")
        for item in result["skipped"][:10]:
            lines.append(f"    {item['failure_id']}/{item['proposal_id']} —— {item['reason']}")
        if len(result["skipped"]) > 10:
            lines.append(f"    … 还有 {len(result['skipped']) - 10} 条")
    if result["dry_run"]:
        lines.append("  (dry-run: 没有写文件)")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_kit.proposal.flywheel",
        description="把 decisions.jsonl (带标签的决策) 导成 evaluation/golden 里的样本")
    parser.add_argument("--decisions", default=applier.DECISIONS_NAME,
                        help=f"账本路径 (默认 {applier.DECISIONS_NAME})")
    parser.add_argument("--reports", default=".",
                        help="report.json 或它所在目录 (递归找; 默认当前目录)")
    parser.add_argument("--golden", default=str(DEFAULT_GOLDEN),
                        help="黄金集目录 (默认 ai_kit/evaluation/golden)")
    parser.add_argument("--since", default="",
                        help='只导出这个时间之后的决策, 如 2026-10-20 (ts 是 UTC ISO)')
    parser.add_argument("--dry-run", action="store_true",
                        help="只算不写: 先看这一周会加什么")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                   # noqa: BLE001 — 老控制台不该让导出挂掉
        pass
    args = _parser().parse_args(argv)
    try:
        result = export(args.decisions, args.reports, golden=args.golden,
                        since=args.since, dry_run=args.dry_run)
    except FlywheelError as exc:
        print(f"[飞轮] 用不了: {exc}", file=sys.stderr)
        return 2
    print(_summary(result, since=args.since))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
