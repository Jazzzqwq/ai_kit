# -*- coding: utf-8 -*-
"""
应用方案 — 一个动作一条记录, 不猜人的意思

三个动作 (卡片 6.4), 都是 CLI:

    python -m ai_kit.proposal.applier --decision S001 --accept              # 改文件 -> 重跑验证
    python -m ai_kit.proposal.applier --decision S001 --reject --note "..." # 只记账
    python -m ai_kit.proposal.applier --decision S001 --defer               # 先放着, 只记账

输入是 report.write() 写出的 report.json (--pending), 输出往 decisions.jsonl 追加一行:
    {ts, failure_id, proposal_id, decision, verify_result, operator, note}
七个键就是卡片给的七个, 不再加 —— 回滚状态写在 note 里, 否则 schema 会悄悄分叉。

accept 为什么这么保守 (每条都有代价, 但都比"悄悄改坏"小):
- 文件必须先干净: 回滚命令是 git checkout --, 文件里本来就有没提交的改动时, 回滚会把
  那份改动一起丢掉。脏文件直接拒绝, 让人先自己处理。
- 先 git apply --check 再真 apply: 补丁打不上时一个字节都不写。
- verify_cmd 只跑 pytest (白名单): 这条命令是真要执行的, 6.3 生成的就是
  "pytest <file>::<test> -q"; 换成 rm -rf 之类, 在这里被拦住比"信任上游"靠谱。
- 永远不用 shell=True: 命令按 argv 跑, 引号 / 管道 / 重定向都只是普通字符。
- 目标文件必须是仓库内的相对路径: 补丁落到仓库外, 回滚命令也就失去了意义。
- 验证失败自动回滚, note 写明"已回滚"; 回滚也失败时 note 会喊人手动处理。

退出码: 0 记账完成 (accept 且验证通过 / reject / defer); 1 accept 但验证没过 (已回滚);
2 用不了 (找不到方案 / 补丁打不上 / 命令不像 pytest / 重复采纳 / 文件脏 ...)。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ai_kit.proposal import report
from ai_kit.proposal.solutions import Proposal

DECISIONS_NAME = "decisions.jsonl"
OPERATOR_ENV = "AI_KIT_OPERATOR"

DECISION_ACCEPT = "accept"
DECISION_REJECT = "reject"
DECISION_DEFER = "defer"
DECISIONS: Tuple[str, ...] = (DECISION_ACCEPT, DECISION_REJECT, DECISION_DEFER)

# verify_result 的取值: 卡片只给了字段名, 口径在这里定死
VERIFY_PASSED = "passed"
VERIFY_FAILED = "failed"
VERIFY_TIMEOUT = "timeout"
VERIFY_ERROR = "error"
VERIFY_SKIPPED = "skipped"          # 拒绝 / 搁置: 没有跑验证
VERIFY_RESULTS: Tuple[str, ...] = (VERIFY_PASSED, VERIFY_FAILED, VERIFY_TIMEOUT,
                                   VERIFY_ERROR, VERIFY_SKIPPED)

# 只认 pytest: 这是 6.3 生成 verify_cmd 的唯一形状, 也是"重跑这个用例"的唯一含义
_PYTEST_NAMES = {"pytest", "pytest.exe", "py.test", "py.test.exe"}
_PYTHON_NAMES = {"python", "python.exe", "python3", "python3.exe"}
VERIFY_TIMEOUT = 900.0              # 一个用例的重跑, 15 分钟还没完就没意义了
VERIFY_TAIL_CHARS = 600             # 失败时记进 note 的输出尾巴


class ApplierError(RuntimeError):
    """用不了 (退出码 2): 找不到方案 / 文件脏 / 补丁打不上 / 命令不像 pytest"""


@dataclass(frozen=True)
class DecisionRecord:
    """decisions.jsonl 的一行 (字段顺序与卡片一致)"""

    ts: str
    failure_id: str
    proposal_id: str
    decision: str
    verify_result: str
    operator: str
    note: str = ""

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            raise ValueError(f"决策只能是 {DECISIONS}: 拿到 {self.decision!r}")
        if self.verify_result not in VERIFY_RESULTS:
            raise ValueError(f"verify_result 只能是 {VERIFY_RESULTS}: "
                             f"拿到 {self.verify_result!r}")
        if not str(self.proposal_id).strip():
            raise ValueError("决策必须挂在一个 proposal_id 上")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "failure_id": self.failure_id,
            "proposal_id": self.proposal_id,
            "decision": self.decision,
            "verify_result": self.verify_result,
            "operator": self.operator,
            "note": self.note,
        }

    def to_line(self) -> str:
        """一行 JSON: 追加写, 每行独立可读 (坏一行不影响其它行)"""
        return json.dumps(self.as_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 小工具: 时间 / 人 / 记录文件
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _operator(explicit: str = "") -> str:
    """谁做的决定: 显式参数 > AI_KIT_OPERATOR > 系统用户名 (审计里这一栏不能空)"""
    for candidate in (explicit, os.environ.get(OPERATOR_ENV, "")):
        if str(candidate or "").strip():
            return str(candidate).strip()
    try:
        return getpass.getuser() or "unknown"
    except Exception:                              # noqa: BLE001 — 取不到用户名不该让流程挂
        return "unknown"


def record(item: DecisionRecord, path: Any = DECISIONS_NAME) -> str:
    """追加一行; 返回写到的路径 (便于在输出里回显给人)"""
    target = Path(str(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(item.to_line() + "\n")
    return str(target)


def read_decisions(path: Any = DECISIONS_NAME) -> List[Dict[str, Any]]:
    """读已有决策 (坏行跳过: 一行坏数据不该让整份账本不可用)"""
    target = Path(str(path))
    if not target.is_file():
        return []
    rows: List[Dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _already_accepted(path: Any, failure_id: str, proposal_id: str) -> bool:
    """同一条方案已经采纳并验证通过过 —— 再 apply 一次只会打不上补丁"""
    for row in read_decisions(path):
        if (row.get("failure_id") == failure_id
                and row.get("proposal_id") == proposal_id
                and row.get("decision") == DECISION_ACCEPT
                and row.get("verify_result") == VERIFY_PASSED):
            return True
    return False


# ---------------------------------------------------------------------------
# 仓库 / 文件 / 补丁
# ---------------------------------------------------------------------------

def _text(blob: Any) -> str:
    """git 的输出按 UTF-8 解 (git 一律用字节模式调, 见 _git)"""
    if isinstance(blob, bytes):
        return blob.decode("utf-8", "replace").strip()
    return str(blob or "").strip()


def _git(args: Sequence[str], root: str, *, patch: str = "") -> subprocess.CompletedProcess:
    """git 按 argv 跑, 补丁按 UTF-8 **字节** 喂 stdin

    不能用 text=True 喂补丁: 文本模式下 Python 写 stdin 会把 \\n 翻成 \\r\\n
    (Windows), 于是 git 拿带 CR 的上下文行去搜一个 LF 文件, 永远搜不到 —— 而补丁明明
    是对的, 人只会看到一句没头没脑的 "patch does not apply"。其余命令不需要输出解码,
    统一用 _text() 转。
    """
    try:
        return subprocess.run(["git", *args], cwd=root,
                              input=(patch or "").encode("utf-8"), capture_output=True)
    except OSError as exc:                         # git 不在 PATH 上 / 目录不可用
        raise ApplierError(f"git 用不了 ({exc}): accept 要靠 git 打补丁与回滚, "
                           "没装 git 的环境不能用自动应用") from exc


def repo_root(start: Any = ".") -> str:
    """仓库根: accept 要靠 git 打补丁和回滚, 不在仓库里就没有安全的回滚"""
    proc = _git(["rev-parse", "--show-toplevel"], str(start))
    if proc.returncode != 0 or not _text(proc.stdout):
        raise ApplierError(f"{start} 不在 git 仓库里 (git rev-parse 失败: "
                           f"{_text(proc.stderr or proc.stdout)[:200]})")
    return os.path.realpath(_text(proc.stdout))


def _resolve_target(root: str, target_file: str) -> Tuple[str, str]:
    """目标文件: 必须是仓库内的相对路径, 且真的存在

    绝对路径 / .. / 盘符一律拒绝: 补丁能落到仓库外的话, "git checkout 回滚" 就兜不住了。
    """
    rel = str(target_file or "").strip().replace("\\", "/")
    if not rel:
        raise ApplierError("方案没有 target_file, 不知道要改哪个文件")
    if os.path.isabs(rel) or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ApplierError(f"target_file 必须是仓库内相对路径: {rel!r}")
    if ".." in rel.split("/"):
        raise ApplierError(f"target_file 不能跳出仓库: {rel!r}")
    full = os.path.realpath(os.path.join(root, rel))
    try:
        inside = os.path.commonpath([full, root]) == root
    except ValueError:                             # Windows 上跨盘会抛
        inside = False
    if not inside:
        raise ApplierError(f"target_file 落在仓库外: {rel!r} -> {full}")
    if not os.path.isfile(full):
        raise ApplierError(f"target_file 不存在: {rel} (补丁没有落点)")
    return full, rel


def _require_clean(root: str, rel: str) -> None:
    """文件必须干净: 回滚是 git checkout --, 脏文件的改动会被一起丢掉"""
    proc = _git(["status", "--porcelain", "--", rel], root)
    if proc.returncode != 0:
        raise ApplierError(f"git status 失败: {_text(proc.stderr or proc.stdout)[:200]}")
    if _text(proc.stdout):
        raise ApplierError(
            f"{rel} 有未提交的改动 ({_text(proc.stdout).splitlines()[0]}): "
            "先自己提交或还原 —— 回滚用 git checkout, 会把你没提交的改动一起丢掉")


def _apply(proposal: Proposal, root: str) -> str:
    """打补丁: --check 先过一遍, 再真写; 返回落地文件的相对路径"""
    _resolve_target(root, proposal.target_file)
    if not proposal.diff.strip():
        raise ApplierError(f"{proposal.proposal_id} 没有 diff, 没有什么可应用")
    _require_clean(root, proposal.target_file)
    check = _git(["apply", "--check", "-p1", "-"], root, patch=proposal.diff)
    if check.returncode != 0:
        raise ApplierError(f"补丁打不上 (文件可能已经变了): "
                           f"{_text(check.stderr or check.stdout)[:300]}")
    applied = _git(["apply", "-p1", "-"], root, patch=proposal.diff)
    if applied.returncode != 0:
        raise ApplierError(f"git apply 失败: {_text(applied.stderr or applied.stdout)[:300]}")
    return proposal.target_file


def _rollback(proposal: Proposal, root: str) -> bool:
    """回到 HEAD 的版本; 返回是否成功 (失败时由调用方在 note 里喊人)"""
    try:
        rel = _resolve_target(root, proposal.target_file)[1]
    except ApplierError:
        return False
    proc = _git(["checkout", "--", rel], root)
    if proc.returncode != 0:
        return False
    return not _text(_git(["status", "--porcelain", "--", rel], root).stdout)


# ---------------------------------------------------------------------------
# 验证: 只跑 pytest, 按 argv
# ---------------------------------------------------------------------------

def verify_argv(command: str) -> Optional[List[str]]:
    """verify_cmd -> argv; 不是 pytest 就返回 None (调用方据此拒绝)

    认两种写法: `pytest <target> -q` 与 `python -m pytest <target> -q`, 其余一律不认。
    """
    try:
        argv = shlex.split(str(command or ""), posix=True)
    except ValueError:
        return None
    if not argv:
        return None
    head = os.path.basename(argv[0]).lower()
    if head in _PYTEST_NAMES:
        return argv
    if head in _PYTHON_NAMES and len(argv) > 2 and argv[1] == "-m" and argv[2] == "pytest":
        return argv
    return None


def _tail(text: str, limit: int = VERIFY_TAIL_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else "…" + value[-(limit - 1):]


def run_verify(argv: Sequence[str], root: str) -> Tuple[str, str]:
    """跑一次验证, 返回 (verify_result, 输出尾巴)

    PATH 上没有 pytest 时用同一个解释器 `-m pytest` 重试: Windows 上 .venv\\Scripts
    经常没进 PATH, 直接报 error 会让人以为补丁有问题。
    """
    try:
        proc = subprocess.run(list(argv), cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=VERIFY_TIMEOUT)
    except subprocess.TimeoutExpired:
        return VERIFY_TIMEOUT, f"超过 {VERIFY_TIMEOUT:g}s 还没跑完"
    except OSError as exc:
        if os.path.basename(str(argv[0])).lower() in _PYTEST_NAMES:
            fallback = [sys.executable, "-m", "pytest", *argv[1:]]
            return run_verify(fallback, root)
        return VERIFY_ERROR, str(exc)
    status = VERIFY_PASSED if proc.returncode == 0 else VERIFY_FAILED
    return status, _tail((proc.stdout or "") + (proc.stderr or ""))


# ---------------------------------------------------------------------------
# 三个动作
# ---------------------------------------------------------------------------

def _record(*, failure_id: str, proposal: Proposal, decision: str,
            verify_result: str, operator: str, note: str) -> DecisionRecord:
    return DecisionRecord(ts=_now(), failure_id=str(failure_id or ""),
                          proposal_id=proposal.proposal_id, decision=decision,
                          verify_result=verify_result, operator=_operator(operator),
                          note=str(note or "").strip())


def reject(proposal: Proposal, *, failure_id: str, note: str = "", operator: str = "",
           decisions: Any = DECISIONS_NAME) -> DecisionRecord:
    """拒绝: 不动一个字节, 只记账。note 是这条记录里唯一能解释"为什么"的地方"""
    item = _record(failure_id=failure_id, proposal=proposal, decision=DECISION_REJECT,
                   verify_result=VERIFY_SKIPPED, operator=operator, note=note)
    record(item, decisions)
    return item


def defer(proposal: Proposal, *, failure_id: str, note: str = "", operator: str = "",
          decisions: Any = DECISIONS_NAME) -> DecisionRecord:
    """搁置: 之后还能再 --accept / --reject; 同一条方案允许先搁置后采纳"""
    item = _record(failure_id=failure_id, proposal=proposal, decision=DECISION_DEFER,
                   verify_result=VERIFY_SKIPPED, operator=operator, note=note)
    record(item, decisions)
    return item


def accept(proposal: Proposal, *, failure_id: str, root: str = ".",
           note: str = "", operator: str = "",
           decisions: Any = DECISIONS_NAME,
           runner: Optional[Callable[[Sequence[str], str], Tuple[str, str]]] = None
           ) -> DecisionRecord:
    """采纳: 打补丁 -> 跑验证 -> 不通过就回滚; 无论哪种结果都留一条记录

    所有"用不了"的情况都在动文件之前判定 (类型 / diff / 命令 / 重复采纳 / 文件脏),
    所以被拒绝的 accept 不会在仓库里留下半成品。
    """
    if not proposal.is_code_change:
        raise ApplierError(f"{proposal.proposal_id} 是 {proposal.type} (终态方案), "
                           "没有可应用的补丁: 缺陷单 / 环境清单要人去处理")
    if not proposal.diff.strip():
        raise ApplierError(f"{proposal.proposal_id} 没有 diff, 不能先应用再验证")
    argv = verify_argv(proposal.verify_cmd)
    if argv is None:
        raise ApplierError(
            f"verify_cmd 不是 pytest 调用, 拒绝执行: {proposal.verify_cmd!r} —— "
            "这条命令会被真的执行, 只认 pytest 的两种写法")
    if _already_accepted(decisions, failure_id, proposal.proposal_id):
        raise ApplierError(f"{proposal.proposal_id} 已经采纳并验证通过过 (见 {decisions}), "
                           "不重复应用; 要再试一次请先还原文件并删掉那条记录")

    root = repo_root(root)
    _apply(proposal, root)
    status, output = (runner or run_verify)(argv, root)
    if status not in VERIFY_RESULTS:
        status, output = VERIFY_ERROR, f"验证器返回了未知状态 {status!r}"

    if status == VERIFY_PASSED:
        item = _record(failure_id=failure_id, proposal=proposal, decision=DECISION_ACCEPT,
                       verify_result=status, operator=operator,
                       note=" | ".join(part for part in (note.strip(),
                                                         f"验证输出: {output}") if part))
        record(item, decisions)
        return item

    rolled = _rollback(proposal, root)
    rollback_note = ("验证失败, 已回滚" if rolled
                     else "验证失败, 回滚也失败了 —— 请手动 git checkout -- "
                          f"{proposal.target_file}")
    item = _record(failure_id=failure_id, proposal=proposal, decision=DECISION_ACCEPT,
                   verify_result=status, operator=operator,
                   note=" | ".join(part for part in (note.strip(), rollback_note,
                                                     f"验证输出: {output}") if part))
    record(item, decisions)
    return item


def decide(action: str, proposal: Proposal, **kwargs: Any) -> DecisionRecord:
    """按动作名分流 (main 与调用方都用这一个口子, 免得三处各写一遍分支)"""
    if action == DECISION_ACCEPT:
        return accept(proposal, **kwargs)
    if action == DECISION_REJECT:
        return reject(proposal, **kwargs)
    if action == DECISION_DEFER:
        return defer(proposal, **kwargs)
    raise ApplierError(f"未知动作 {action!r}; 只能是 {DECISIONS}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_kit.proposal.applier",
        description="人工确认一个方案: --accept 应用并重跑验证, --reject / --defer 只记账")
    parser.add_argument("--decision", required=True, metavar="S001",
                        help="方案编号 (报告里的 S001 / S002 ...)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--accept", action="store_true",
                       help="应用补丁并跑 verify_cmd, 不过自动回滚")
    group.add_argument("--reject", action="store_true", help="不应用, 只记账")
    group.add_argument("--defer", action="store_true", help="先放着, 只记账")
    parser.add_argument("--note", default="", help="决策理由 (拒绝时建议写)")
    parser.add_argument("--operator", default="",
                        help=f"谁做的决定 (默认取 {OPERATOR_ENV} 或系统用户名)")
    parser.add_argument("--pending", default=report.PENDING_NAME,
                        help=f"report.write 写出的报告数据 (默认 {report.PENDING_NAME})")
    parser.add_argument("--decisions", default=DECISIONS_NAME,
                        help=f"决策账本 (默认 {DECISIONS_NAME}, 追加写)")
    parser.add_argument("--repo", default=".", help="仓库根 (默认当前目录)")
    return parser


def _action_of(args: argparse.Namespace) -> str:
    for name in DECISIONS:
        if getattr(args, name, False):
            return name
    raise ApplierError("必须给一个动作: --accept / --reject / --defer")


def _proposal_of(payload: Mapping[str, Any], proposal_id: str) -> Proposal:
    items = report.pending_proposals(dict(payload))
    wanted = str(proposal_id or "").strip()
    for item in items:
        if item.proposal_id == wanted:
            return item
    available = ", ".join(item.proposal_id for item in items) or "(没有方案)"
    raise ApplierError(f"报告里没有 {wanted} 这条方案; 可用: {available}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        action = _action_of(args)
        payload = report.load_pending(args.pending)
        proposal = _proposal_of(payload, args.decision)
        failure_id = str(payload.get("failure_id") or "")
        item = decide(action, proposal, failure_id=failure_id, note=args.note,
                      operator=args.operator, decisions=args.decisions,
                      **({"root": args.repo} if action == DECISION_ACCEPT else {}))
    except (ApplierError, report.ReportError) as exc:
        print(f"[applier] 用不了: {exc}", file=sys.stderr)
        return 2

    print(f"[applier] {proposal.proposal_id} {proposal.type}/{proposal.risk} "
          f"{action} -> {item.verify_result}")
    if item.note:
        print(f"           {item.note}")
    print(f"[applier] 已写入 {args.decisions} (failure_id={item.failure_id})")
    return 0 if item.verify_result in (VERIFY_PASSED, VERIFY_SKIPPED) else 1


if __name__ == "__main__":
    raise SystemExit(main())
