# -*- coding: utf-8 -*-
"""
应用方案 — 一个动作一条记录, 不猜人的意思

三个动作 (卡片 6.4), 都是 CLI:

    python -m ai_kit.proposal.applier --decision S001 --accept              # 改文件 -> 重跑验证
    python -m ai_kit.proposal.applier --decision S001 --reject --note "..." # 只记账
    python -m ai_kit.proposal.applier --decision S001 --defer               # 先放着, 只记账

输入是 report.write() 写出的 report.json (--pending), 输出往 decisions.jsonl 追加一行:
    {ts, failure_id, proposal_id, decision, verify_result, operator, note}
七个键就是卡片给的七个, 不再加 —— 回滚状态 / verified 标记都写进 note, 否则 schema
会悄悄分叉。

accept 的六步 (卡片 6.5), 每一步都留痕:
  ① 工作区必须干净。只有被 git 跟踪的改动才算脏: 未跟踪的报告 / 账本不该挡住一次应用,
     而它们恰好都是未跟踪的。脏了默认拒绝, 加 --stash 才先 git stash, 并把 stash 编号
     与恢复命令写进 note —— 人得知道自己的改动去哪了。
  ② 备份。补丁原文落盘到 <repo>/patches/<failure>_<proposal>.patch; --branch-backup
     额外建一条 fix/<failure_id> 分支 (默认不建: 分支列表不该被每次采纳撑满)。
  ③ 打补丁。目标文件必须在白名单里 —— pages/ tests/ config/; ai_kit/ 是产品代码,
     永不改 (硬约束)。先 git apply --check 再真 apply: 打不上时一个字节都不写。
  ④ 重跑 verify_cmd。只认 pytest 的单个 nodeid (<file>::<test>): 跑全量会把别人的
     失败算到这次补丁头上, 而且一次失败本来就只该对一个用例负责。
  ⑤ passed -> 保留补丁, note 标 状态: verified; failed -> git checkout 回滚, note 标
     状态: rejected (回滚也失败时 note 会喊人手动处理)。
  ⑥ 无论结果如何都往 decisions.jsonl 追加一行 —— "试过, 不行" 也要看得见。

硬约束 (卡片 6.5) 每条都有对应的拒绝:
- 绝不自动应用: 只有人显式敲 --accept 才会改文件, 没有任何"看着风险低就自己上手"的路径。
- 单次失败最多应用 1 个方案: 同一 failure_id 已经采纳过别的编号 -> 直接拒绝。
- 永不改产品代码: 白名单之外一律拒绝, 包括 ai_kit/ 自己。
- assertion_update 恒 auto_applicable=false (在 solutions 的 schema 里钉死): 断言变更
  就是预期变更, 必须人审。applier 照样接受它, 但只接受人敲出来的 --accept。

其余几条保守规定 (每条都有代价, 但都比"悄悄改坏"小):
- 永远不用 shell=True: 命令按 argv 跑, 引号 / 管道 / 重定向都只是普通字符。
- 目标文件必须是仓库内的相对路径: 补丁落到仓库外, 回滚命令也就失去了意义。
- 回滚是 git checkout -- <file>: 所以目标文件必须干净, 否则会把人的改动一起丢掉。

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
from typing import (
    Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple,
)

from ai_kit.proposal import report
from ai_kit.proposal.solutions import (
    CODE_TYPES, PROPOSAL_ASSERTION_UPDATE, PROPOSAL_CONFIG_CHANGE, Proposal,
)

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

# ③ 白名单: 只有这些顶层目录下的文件允许被补丁碰 (卡片 6.5)
ALLOWED_DIRS: Tuple[str, ...] = ("pages", "tests", "config")
PRODUCT_DIR = "ai_kit"              # 产品代码, 永不改

# 能应用的类型: 改测试代码的四类 + 改配置 (config_change 的落点是 config/)
APPLICABLE_TYPES: Tuple[str, ...] = (*CODE_TYPES, PROPOSAL_CONFIG_CHANGE)

PATCHES_DIR = "patches"             # ② 备份落点: <repo>/patches/<failure>_<proposal>.patch

# ⑤ 方案的终态标记 (写进 note; decisions.jsonl 仍是七个键)
OUTCOME_VERIFIED = "verified"
OUTCOME_REJECTED = "rejected"
OUTCOME_PENDING = "pending"


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


def _claimed(path: Any, failure_id: str) -> List[str]:
    """这条 failure_id 已经采纳过的方案编号 (按账本顺序去重)

    卡片硬约束是"单次失败最多应用 1 个方案", 所以只要这里出现过别的编号, 就不能再动。
    """
    seen: List[str] = []
    for row in read_decisions(path):
        if row.get("failure_id") != failure_id or row.get("decision") != DECISION_ACCEPT:
            continue
        pid = str(row.get("proposal_id") or "").strip()
        if pid and pid not in seen:
            seen.append(pid)
    return seen


def _check_claimed(path: Any, failure_id: str, proposal_id: str) -> None:
    """一条失败只能采纳一个方案; 同一方案允许在失败的验证后重试"""
    if not str(failure_id or "").strip():
        return                                    # 没有 failure_id 就谈不上"同一次失败"
    others = [pid for pid in _claimed(path, failure_id) if pid != proposal_id]
    if others:
        raise ApplierError(
            f"这次失败 (failure_id={failure_id}) 已经采纳过 {', '.join(others)}: "
            "卡片硬约束是一次失败最多应用一个方案 —— 要换方案, 请在新的失败上做")
    if _already_accepted(path, failure_id, proposal_id):
        raise ApplierError(f"{proposal_id} 已经采纳并验证通过过 (见 {path}), "
                           "不重复应用; 要再试一次请先还原文件并删掉那条记录")


def status_of(row: Mapping[str, Any]) -> str:
    """一行账 -> 方案的终态 (卡片 6.5 的 verified / rejected)

    验证失败的那行决策仍然是 accept (人确实采纳了), 但补丁已经被回滚, 方案本身
    是 rejected; defer 仍在等人。这条换算规则只能有一处, 否则报告与账本会各说各的。
    """
    if row.get("decision") == DECISION_ACCEPT:
        return (OUTCOME_VERIFIED if row.get("verify_result") == VERIFY_PASSED
                else OUTCOME_REJECTED)
    if row.get("decision") == DECISION_REJECT:
        return OUTCOME_REJECTED
    return OUTCOME_PENDING


def outcome(path: Any, failure_id: str, proposal_id: str) -> str:
    """这条方案在这份账本里的终态 (没有记录 = pending)"""
    state = OUTCOME_PENDING
    for row in read_decisions(path):
        if (row.get("failure_id") == failure_id
                and row.get("proposal_id") == proposal_id):
            state = status_of(row)
    return state


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


def _check_whitelist(rel: str) -> None:
    """③ 白名单: 只看最顶层目录

    只比首段而不是 "路径里含 tests": ai_kit/tests/x.py 属于产品包, 改了它就是改产品
    代码, 不能因为它名字里有 tests 就放行。
    """
    head = rel.split("/", 1)[0]
    if head == PRODUCT_DIR:
        raise ApplierError(f"{rel} 是产品代码 ({PRODUCT_DIR}/ 下): "
                           "卡片硬约束是永不改产品代码, 拒绝应用")
    if head not in ALLOWED_DIRS:
        allowed = ", ".join(f"{name}/" for name in ALLOWED_DIRS)
        raise ApplierError(f"{rel} 不在可应用白名单: 只允许 {allowed} 下的文件 "
                           "(补丁只改测试代码 / page object / 配置)")


def _resolve_target(root: str, target_file: str) -> Tuple[str, str]:
    """目标文件: 必须是仓库内的相对路径, 在白名单里, 且真的存在

    绝对路径 / .. / 盘符一律拒绝: 补丁能落到仓库外的话, "git checkout 回滚" 就兜不住了。
    """
    rel = str(target_file or "").strip().replace("\\", "/")
    if not rel:
        raise ApplierError("方案没有 target_file, 不知道要改哪个文件")
    if os.path.isabs(rel) or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise ApplierError(f"target_file 必须是仓库内相对路径: {rel!r}")
    if ".." in rel.split("/"):
        raise ApplierError(f"target_file 不能跳出仓库: {rel!r}")
    _check_whitelist(rel)
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


def _rel_of(root: str, path: Any) -> str:
    """把一个路径变成相对仓库根的 posix 路径 (在仓库外 / 拿不到就返回空串)

    相对路径按仓库根解释: --decisions decisions.jsonl 的意思是"仓库根下的那一份"。
    """
    raw = Path(str(path))
    try:
        full = os.path.realpath(str(raw if raw.is_absolute() else os.path.join(root, raw)))
        if os.path.commonpath([full, root]) != root:
            return ""
    except (OSError, ValueError):
        return ""
    return os.path.relpath(full, root).replace("\\", "/")


def _display(root: str, path: Any) -> str:
    """给人看的路径: 仓库内的写相对路径, 仓外的原样给"""
    return _rel_of(root, path) or str(path)


def _own_artifacts(root: str, decisions: Any, patches: Any) -> Set[str]:
    """自己的产物: 账本 / 报告 / 补丁备份

    它们会被本模块反复重写, 不该拿“工作区脏了”去拦自己人。
    """
    ignored: Set[str] = {report.PENDING_NAME, DECISIONS_NAME}
    for candidate in (decisions, patches):
        rel = _rel_of(root, candidate) if candidate else ""
        if rel:
            ignored.add(rel)
    return ignored


def _porcelain_path(line: str) -> str:
    """porcelain v1 的一行 ("XY <path>") -> 路径

    不能用 _text() 处理整段输出: 它会 strip 掉首行的前导空格, 而 " M tests/x.py" 的
    第一个空格正是状态位的一部分 —— 切掉后路径会变成 "ests/x.py"。
    """
    body = line[3:] if len(line) > 3 else ""
    if " -> " in body:                           # 重命名: 只看新名字
        body = body.split(" -> ")[-1]
    return body.strip().strip('"')


def workspace_changes(root: str, *, ignore: Optional[Iterable[str]] = None
                      ) -> List[str]:
    """① 工作区里会影响回滚的改动

    只看被 git 跟踪的文件 (--untracked-files=no): 未跟踪文件不会被 git apply 拒, 也
    不会被 git checkout -- 碰到; 而报告 (report.json) 与账本 (decisions.jsonl) 本来
    就是未跟踪的生成物 —— 把它们算成脏, 就等于每次应用都先拦自己一道。
    """
    proc = _git(["-c", "core.quotePath=false", "status", "--porcelain",
                 "--untracked-files=no"], root)
    if proc.returncode != 0:
        raise ApplierError(f"git status 失败: {_text(proc.stderr or proc.stdout)[:200]}")
    raw = proc.stdout
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    skip = {_posix_key(name) for name in (ignore or ())}
    found: List[str] = []
    for line in text.splitlines():
        path = _porcelain_path(line)
        if not path:
            continue
        key = _posix_key(path)
        if any(key == name or key.startswith(name + "/") for name in skip):
            continue
        found.append(path)
    return found


def _posix_key(path: Any) -> str:
    """路径的比对形态: posix 分隔符, 去掉开头的 ./ —— 只用于比较, 不用于访问"""
    value = str(path or "").strip().replace("\\", "/")
    return value[2:] if value.startswith("./") else value


def _stash(root: str, message: str) -> str:
    """把工作区的已跟踪改动暂存起来, 返回一个能被人用来恢复的编号

    不用 --include-untracked: 那会把 report.json / decisions.jsonl 一起收走, 账本就断
    了——而且未跟踪文件本来就不会阻碍 apply / checkout。
    """
    proc = _git(["stash", "push", "-m", message], root)
    if proc.returncode != 0:
        raise ApplierError(f"git stash 失败: {_text(proc.stderr or proc.stdout)[:200]} —— "
                           "请先自己提交或还原, 再重试")
    ref = _text(_git(["rev-parse", "--verify", "--quiet", "refs/stash"], root).stdout)
    return "stash@{0}" + (f" ({ref[:12]})" if ref else "")


def _slug(text: Any, limit: int = 60) -> str:
    """文件名安全的编号: failure_id 可能带中文 / 冒号 / 斜杠"""
    value = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(text or "")).strip("-.-")
    return value[:limit].strip("-.-") or "failure"


def save_patch(proposal: Proposal, directory: Any, *, failure_id: str) -> str:
    """② 备份: 把补丁原文落盘 (可重放 / 可审计), 返回路径

    备份先于应用: 补丁打不上也留下了一份“本来要打什么”的证据。
    """
    folder = Path(str(directory))
    name = f"{_slug(failure_id)}_{_slug(proposal.proposal_id)}.patch"
    path = folder / name
    body = proposal.diff if proposal.diff.endswith("\n") else proposal.diff + "\n"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
    except OSError as exc:
        raise ApplierError(f"补丁备份写不了 ({path}): {exc} —— 没备份就不动仓库") from exc
    return str(path)


def create_backup_branch(root: str, failure_id: str) -> str:
    """② (可选) 应用前在 HEAD 上留一条 fix/<failure_id> 分支

    默认不用: 每次采纳都建分支, 分支列表很快就没法看了。已存在时直接报错而不是 -f:
    覆盖一条已有的备份分支, 等于把上次的退路也抹了。
    """
    name = f"fix/{_slug(failure_id)}"
    exists = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], root)
    if exists.returncode == 0:
        raise ApplierError(f"备份分支 {name} 已存在: 换个 failure_id, 或先 git branch -D {name}")
    proc = _git(["branch", name, "HEAD"], root)
    if proc.returncode != 0:
        raise ApplierError(f"建备份分支失败: {_text(proc.stderr or proc.stdout)[:200]}")
    return name


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
    """verify_cmd -> argv; 不是"pytest + 单个 nodeid"就返回 None (调用方据此拒绝)

    ④ 只重跑一个用例: 命令里必须有一个带 "::" 的位置参数 (pytest 的 nodeid)。
    `pytest` / `pytest tests/` 会顺带跑一堆无关用例 —— 别人的失败不该被算到这次补丁
    头上, 而且一次失败本来只该对一个用例负责。
    """
    try:
        argv = shlex.split(str(command or ""), posix=True)
    except ValueError:
        return None
    if not argv:
        return None
    head = os.path.basename(argv[0]).lower()
    if head in _PYTEST_NAMES:
        rest = argv[1:]
    elif head in _PYTHON_NAMES and len(argv) > 2 and argv[1] == "-m" and argv[2] == "pytest":
        rest = argv[3:]
    else:
        return None
    if not any(arg for arg in rest if not arg.startswith("-") and "::" in arg):
        return None
    return argv


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


def _compose(*parts: Any) -> str:
    """note 拼接: 空段丢掉, 用 " | " 连 (一行放得下, 又分得开)"""
    return " | ".join(str(part).strip() for part in parts if str(part or "").strip())


def accept(proposal: Proposal, *, failure_id: str, root: str = ".",
           note: str = "", operator: str = "",
           decisions: Any = DECISIONS_NAME,
           runner: Optional[Callable[[Sequence[str], str], Tuple[str, str]]] = None,
           stash: bool = False, backup_branch: bool = False,
           patches: Any = None) -> DecisionRecord:
    """采纳: 六步 (干净 -> 备份 -> 补丁 -> 验证 -> 保留或回滚 -> 记账)

    每个"用不了"的判定都排在动文件之前 (类型 / diff / 命令 / 是否已采纳过 / 白名单 /
    工作区脏), 所以被拒绝的 accept 不会 stash 掉人的改动, 也不会留半成品。

    stash / backup_branch 只对 accept 有意义。patches 不给时落在 <repo>/patches/,
    跟着仓库走 —— 不会随手写脏当前目录。
    """
    if proposal.type not in APPLICABLE_TYPES:
        raise ApplierError(f"{proposal.proposal_id} 是 {proposal.type} (终态方案), "
                           "没有可应用的补丁: 缺陷单 / 环境清单要人去处理")
    if not proposal.diff.strip():
        raise ApplierError(f"{proposal.proposal_id} 没有 diff, 不能先应用再验证")
    argv = verify_argv(proposal.verify_cmd)
    if argv is None:
        raise ApplierError(
            f"verify_cmd 不是 pytest 的单用例重跑, 拒绝执行: {proposal.verify_cmd!r} —— "
            "这条命令会被真的执行, 必须是 pytest <file>::<test> -q")
    _check_claimed(decisions, failure_id, proposal.proposal_id)

    root = repo_root(root)
    _resolve_target(root, proposal.target_file)      # ③ 白名单 / 存在, 先于任何写操作

    # ① 工作区
    changes = workspace_changes(root, ignore=_own_artifacts(root, decisions, patches))
    stash_ref = ""
    if changes:
        shown = ", ".join(changes[:3]) + (" 等" if len(changes) > 3 else "")
        if not stash:
            raise ApplierError(f"工作区有未提交的改动 ({shown}): 先自己提交或还原, "
                               "或者加 --stash 让 applier 先暂存 (结束后告诉你如何恢复)")
        stash_ref = _stash(root, f"ai-kit: 应用 {proposal.proposal_id} 前自动暂存")

    # ② 备份
    folder = patches if patches else os.path.join(root, PATCHES_DIR)
    backup = save_patch(proposal, folder, failure_id=failure_id)
    branch = create_backup_branch(root, failure_id) if backup_branch else ""

    # ③ 补丁 + ④ 验证; 中途出错时先把"改动去哪了"告诉人
    try:
        _apply(proposal, root)
        status, output = (runner or run_verify)(argv, root)
    except ApplierError as exc:
        if stash_ref:
            raise ApplierError(f"{exc} —— 你的改动还在 {stash_ref}, 恢复: git stash pop") from exc
        raise
    if status not in VERIFY_RESULTS:
        status, output = VERIFY_ERROR, f"验证器返回了未知状态 {status!r}"

    trace = _compose(note, f"补丁备份: {_display(root, backup)}",
                     f"备份分支: {branch}" if branch else "",
                     (f"应用前暂存了 {len(changes)} 处改动 ({stash_ref}), 恢复: git stash pop"
                      if stash_ref else ""),
                     ("断言变更 (预期变更), 由人审后采纳"
                      if proposal.type == PROPOSAL_ASSERTION_UPDATE else ""))

    if status == VERIFY_PASSED:
        note = _compose(trace, f"状态: {OUTCOME_VERIFIED}")
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
    note = _compose(trace, f"状态: {OUTCOME_REJECTED}")
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
    parser.add_argument("--stash", action="store_true",
                        help="工作区有未提交改动时先 git stash (默认拒绝; 会告诉你如何恢复)")
    parser.add_argument("--branch-backup", action="store_true",
                        help="应用前建备份分支 fix/<failure_id> (默认只把补丁落盘)")
    parser.add_argument("--patches", default="", metavar="DIR",
                        help=f"补丁备份目录 (默认 <repo>/{PATCHES_DIR})")
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
        accept_only = {"root": args.repo, "stash": args.stash,
                       "backup_branch": args.branch_backup,
                       "patches": args.patches or None}
        item = decide(action, proposal, failure_id=failure_id, note=args.note,
                      operator=args.operator, decisions=args.decisions,
                      **(accept_only if action == DECISION_ACCEPT else {}))
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
