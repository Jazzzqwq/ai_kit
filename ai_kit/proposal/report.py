# -*- coding: utf-8 -*-
"""
人工确认 v1 — 单文件 HTML 报告 (不做 Web 控制台)

为什么不做控制台: 这是"人看一眼再决定"的环节, 不是常驻服务。单文件 HTML 双击就能看,
能当附件发出去, 能进 CI artifact, 不用起进程、不用账号、不用多一个依赖。代价是没有
按钮 —— 所以三个动作做成 CLI (ai_kit.proposal.applier), 报告里直接印出可复制的命令。

一份报告两个产物 (write 一次写两份, 同目录同主名):
    report.html   人看的: 失败现场 (截图 / traceback / 日志尾部 / DOM 差异) + 诊断 + 方案
    report.json   机器看的: failure_id 与全部方案字段, applier --decision S001 从这里取

三条硬约定:
- 所有外部文本 (traceback / 日志 / diff / 方案标题 / reason) 一律 html.escape。报告里最
  脏的东西恰好是异常栈, 不转义就等于把页面交给被测站点的报错信息。
- 输出里不许出现 <script>: 没有脚本就没有 XSS 面, 也保证"单文件"真的是一张纸。
- 截图内嵌成 data URI 且设上限 (MAX_SHOT_BYTES): 超了就写"太大未内嵌"并留路径, 不把
  一个几十 MB 的 PNG 塞进报告里等人打开。

不写 decisions.jsonl —— 那是 applier 的事 (报告只读不写决策), 否则"谁采纳了"会有两个
写入方, 记录迟早错位。
"""
from __future__ import annotations

import base64
import html
import json
import re
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ai_kit.proposal import diagnoser
from ai_kit.proposal.diagnoser import Decision, FailureContext, FailureDiagnosis
from ai_kit.proposal.solutions import Proposal

REPORT_VERSION = 1

# applier 的默认输入名: write("report.html") 会顺手写出 report.json
PENDING_NAME = "report.json"

# 报告里各块的上限: 报告是给人扫的, 不是日志仓库
MAX_SHOT_BYTES = 3 * 1024 * 1024      # 内嵌截图上限 (base64 之后约 4MB)
MAX_TRACE_CHARS = 8000                # traceback 截断 (保头保尾, 见 _clip)
MAX_DOM_CHARS = 8000
LOG_TAIL_LINES = 40                   # 只给最后 40 行: 前面的铺垫对定位没用
MAX_DIFF_LINES = 400

# 归因 -> 展示用中文 (报告里给人看的, 与代码里的 blame 常量一一对应)
_BLAME_LABELS = {
    diagnoser.BLAME_TEST_CODE: "测试代码",
    diagnoser.BLAME_PRODUCT: "产品缺陷",
    diagnoser.BLAME_ENVIRONMENT: "环境问题",
    diagnoser.BLAME_UNKNOWN: "归因不明",
}


class ReportError(RuntimeError):
    """报告本身的问题 (字段缺失 / pending 文件读不懂) —— applier 也复用这个异常"""


# ---------------------------------------------------------------------------
# 数据: 一份报告需要的全部材料
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FailureReport:
    """一次失败的报告内容 (纯数据; 渲染不依赖 driver / 网络)

    failure_id 是报告与 decisions.jsonl 之间的唯一连接: 同一次失败的所有决策都挂在它
    下面, 事后复盘能从 decisions.jsonl 反查到这份 html 与那一版方案。
    """

    failure_id: str
    created_at: str
    diagnosis: FailureDiagnosis
    decision: Decision
    proposals: Tuple[Proposal, ...] = ()
    case_id: str = ""
    test_name: str = ""
    url: str = ""
    screenshot: str = ""
    traceback: str = ""
    log: str = ""
    dom_diff: str = ""
    steps: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.failure_id).strip():
            raise ReportError("failure_id 不能为空: decisions.jsonl 靠它把决策挂回这次失败")
        seen = set()
        for item in self.proposals:
            key = str(item.proposal_id)
            if key in seen:
                raise ReportError(f"方案编号重复: {key} (applier --decision 会取错)")
            seen.add(key)

    @property
    def title(self) -> str:
        subject = self.test_name or self.case_id or self.failure_id
        return f"{subject} 失败"

    def proposal(self, proposal_id: str) -> Optional[Proposal]:
        """按编号取方案 (与 applier 的查找口径一致: 精确匹配)"""
        wanted = str(proposal_id or "").strip()
        for item in self.proposals:
            if item.proposal_id == wanted:
                return item
        return None

    def as_dict(self) -> Dict[str, Any]:
        """applier 的输入 (report.json): 只有这份坏掉时才需要人来看 html"""
        return {
            "version": REPORT_VERSION,
            "failure_id": self.failure_id,
            "created_at": self.created_at,
            "case_id": self.case_id,
            "test_name": self.test_name,
            "url": self.url,
            "screenshot": self.screenshot,
            "traceback": self.traceback,
            "log": self.log,
            "dom_diff": self.dom_diff,
            "steps": list(self.steps),
            "diagnosis": self.diagnosis.as_dict(),
            "decision": self.decision.as_dict(),
            "proposals": [item.as_dict() for item in self.proposals],
        }


def _stamp(moment: Optional[datetime] = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def _iso(moment: Optional[datetime] = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug_id(text: Any, limit: int = 60) -> str:
    """failure_id 里的脏字符换掉: 它会出现在命令行与 JSON 里, 不能带空格/引号/斜杠"""
    value = re.sub(r"[^0-9A-Za-z_.:-]+", "-", str(text or "")).strip("-")
    return value[:limit] or "failure"


def build(context: FailureContext, diagnosis: FailureDiagnosis,
          proposals: Sequence[Proposal] = (), *,
          screenshot: str = "", dom_diff: str = "",
          failure_id: str = "", url: str = "",
          steps: Optional[Sequence[str]] = None,
          moment: Optional[datetime] = None) -> FailureReport:
    """攒一份报告: 归因与方案都给全, 现场材料缺哪块就缺哪块 (报告里明说没有)

    failure_id 显式给就用它 (可复现的测试/重跑同一份), 不给则 "<case 或 test>-<时间戳>"。
    """
    ident = _slug_id(failure_id) if str(failure_id or "").strip() else \
        f"{_slug_id(context.case_id or context.test_name or 'failure', 40)}-{_stamp(moment)}"
    return FailureReport(
        failure_id=ident,
        created_at=_iso(moment),
        diagnosis=diagnosis,
        decision=diagnoser.decide(diagnosis),
        proposals=tuple(proposals),
        case_id=str(context.case_id or ""),
        test_name=str(context.test_name or ""),
        url=str(url or ""),
        screenshot=str(screenshot or ""),
        traceback=str(context.traceback or ""),
        log=str(context.log or ""),
        dom_diff=str(dom_diff or ""),
        steps=tuple(str(item) for item in (steps if steps is not None else context.steps)),
    )


# ---------------------------------------------------------------------------
# 小工具: 转义 / 截断 / 截图内嵌
# ---------------------------------------------------------------------------

def _esc(text: Any) -> str:
    """唯一的转义入口: 属性值也走它 (quote=True)"""
    return html.escape(str(text if text is not None else ""), quote=True)


def _clip(text: Any, limit: int) -> Tuple[str, int]:
    """超长文本保头保尾: 异常栈开头是哪个用例, 结尾才是真正那句异常"""
    value = str(text or "")
    if len(value) <= limit:
        return value, len(value)
    head = value[: limit // 2]
    tail = value[-(limit // 2):]
    return f"{head}\n… (中间省略 {len(value) - limit} 字符) …\n{tail}", len(value)


def _tail_lines(text: Any, limit: int = LOG_TAIL_LINES) -> Tuple[str, int]:
    lines = str(text or "").splitlines()
    return "\n".join(lines[-limit:]), len(lines)


def _shot_tag(path: str) -> str:
    """截图: 能读就内嵌, 读不到/太大就写明原因 (别让人以为页面本来就没截图)"""
    if not path:
        return '<p class="empty">没有采集到截图 (runtime 的 screenshot() 返回空)</p>'
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        return f'<p class="empty">截图读不到: {_esc(path)} ({_esc(exc)})</p>'
    if not data:
        return f'<p class="empty">截图是空文件: {_esc(path)}</p>'
    if len(data) > MAX_SHOT_BYTES:
        return (f'<p class="empty">截图 {len(data) // 1024} KB, 超过内嵌上限 '
                f'{MAX_SHOT_BYTES // 1024 // 1024} MB, 未内嵌: {_esc(path)}</p>')
    mime = "image/jpeg" if str(path).lower().endswith((".jpg", ".jpeg")) else "image/png"
    payload = base64.b64encode(data).decode("ascii")
    return (f'<img alt="失败截图" src="data:{mime};base64,{payload}">')


def _diff_block(diff: str) -> str:
    """diff 按行上色: + 绿 / - 红 / @@ 紫; 只是着色, 内容原样保留"""
    if not str(diff or "").strip():
        return '<p class="empty">这条方案不带补丁 (参数类建议按 verify_cmd 手工确认)</p>'
    rows: List[str] = []
    lines = str(diff).splitlines()
    for line in lines[:MAX_DIFF_LINES]:
        if line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+++") or line.startswith("---"):
            kind = "head"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "del"
        else:
            kind = "ctx"
        # 换行写在 span 里: 行背景才贴着文字, 不靠块布局 (pre 里块元素会多出一行空白)
        rows.append(f'<span class="{kind}">{_esc(line)}\n</span>')
    more = ""
    if len(lines) > MAX_DIFF_LINES:
        more = (f'<span class="ctx">… 还有 {len(lines) - MAX_DIFF_LINES} 行, '
                f'完整补丁见 report.json\n</span>')
    return '<pre class="diff">' + "".join(rows) + more + "</pre>"


def _tag(text: str, tone: str = "") -> str:
    """小标签; tone 为空时不拼一个多余的空 class"""
    css = f"tag {tone}" if tone else "tag"
    return f'<span class="{css}">{_esc(text)}</span>'


def _risk_tone(risk: str) -> str:
    return {"low": "low", "medium": "medium", "high": "high"}.get(risk, "")


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1f2328;--muted:#656d76;--line:#d8dee4;
--add:#e6ffec;--del:#ffebe9;--low:#1a7f37;--mid:#9a6700;--high:#cf222e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{background:#24292f;color:#fff;padding:20px 28px}
header h1{margin:6px 0 10px;font-size:20px;font-weight:600}
.cid{font:12px/1.4 ui-monospace,Consolas,monospace;opacity:.75;word-break:break-all}
.tags{display:flex;flex-wrap:wrap;gap:6px}
header .tag{background:#3d444d;color:#e6edf3}
main{max-width:1080px;margin:0 auto;padding:20px 16px 48px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:16px 20px;margin-bottom:18px}
h2{font-size:16px;margin:0 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:12px;margin:16px 0 6px;color:var(--muted);text-transform:uppercase;
letter-spacing:.06em}
p{margin:6px 0}
pre{background:#f6f8fa;border:1px solid var(--line);border-radius:6px;padding:10px 12px;
overflow:auto;max-height:520px;margin:0;
font:12px/1.55 ui-monospace,Consolas,"Cascadia Mono",monospace;white-space:pre}
.grid{display:grid;grid-template-columns:max-content 1fr;gap:4px 18px;margin:0}
.grid dt{color:var(--muted)}
.grid dd{margin:0;word-break:break-word}
.tag{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;
background:#eaeef2;color:#57606a;white-space:nowrap}
.tag.low{background:#dafbe1;color:var(--low)}
.tag.medium{background:#fff8c5;color:var(--mid)}
.tag.high{background:#ffebe9;color:var(--high)}
.shot img{max-width:100%;border:1px solid var(--line);border-radius:6px;display:block}
.empty{color:var(--muted);font-style:italic}
.proposal{border:1px solid var(--line);border-left:4px solid var(--line);
border-radius:6px;padding:12px 16px;margin:16px 0}
.proposal.auto{border-left-color:var(--low)}
.proposal.review{border-left-color:var(--mid)}
.p-head{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:8px}
.pid{font:600 13px/1 ui-monospace,Consolas,monospace;color:var(--muted)}
.p-title{font-weight:600}
.why{margin:10px 0}
.cmd{margin:8px 0 0}
.cmd span{color:var(--muted);font-size:12px}
.cmd code{display:block;margin-top:2px;padding:6px 8px;background:#f6f8fa;
border:1px solid var(--line);border-radius:4px;font:12px/1.5 ui-monospace,Consolas,monospace;
white-space:pre-wrap;word-break:break-all}
.diff span.add{background:var(--add)}
.diff span.del{background:var(--del)}
.diff span.hunk{color:#8250df}
.diff span.head{color:var(--muted)}
.warn{border-left:3px solid #d4a72c;background:#fff8c5;padding:8px 12px;border-radius:4px}
footer{max-width:1080px;margin:0 auto;padding:0 16px 48px;color:var(--muted);font-size:12px}
"""


def _header(report: FailureReport) -> str:
    blame = report.diagnosis.blame
    label = _BLAME_LABELS.get(blame, blame)
    confidence = f"{report.diagnosis.confidence:.2f}"
    tags = [_tag(f"归因: {label}"),
            _tag(f"置信 {confidence}"),
            _tag(f"方案 {len(report.proposals)} 条"),
            _tag(report.created_at)]
    subject = report.test_name or report.case_id or "(未命名的用例)"
    return (f'<header><div class="cid">{_esc(report.failure_id)}</div>'
            f"<h1>{_esc(subject)} 失败</h1>"
            f'<div class="tags">{"".join(tags)}</div></header>')


def _scene(report: FailureReport) -> str:
    trace, trace_len = _clip(report.traceback, MAX_TRACE_CHARS)
    log_tail, log_total = _tail_lines(report.log)
    dom, dom_len = _clip(report.dom_diff, MAX_DOM_CHARS)
    parts = ['<section class="card"><h2>失败现场</h2>',
             f'<div class="shot">{_shot_tag(report.screenshot)}</div>']
    if report.url:
        parts.append(f'<h3>页面</h3><p>{_esc(report.url)}</p>')
    if trace.strip():
        note = f" (共 {trace_len} 字符, 已截断)" if trace_len > MAX_TRACE_CHARS else ""
        parts.append(f"<h3>traceback{note}</h3><pre>{_esc(trace)}</pre>")
    else:
        parts.append('<h3>traceback</h3><p class="empty">没有 traceback</p>')
    if log_tail.strip():
        more = (f" (只显示最后 {LOG_TAIL_LINES} 行, 共 {log_total} 行)"
                if log_total > LOG_TAIL_LINES else "")
        parts.append(f"<h3>日志尾部{more}</h3><pre>{_esc(log_tail)}</pre>")
    else:
        parts.append('<h3>日志尾部</h3><p class="empty">没有日志</p>')
    if dom.strip():
        note = f" (共 {dom_len} 字符, 已截断)" if dom_len > MAX_DOM_CHARS else ""
        parts.append(f"<h3>DOM 差异{note}</h3><pre>{_esc(dom)}</pre>")
    else:
        parts.append('<h3>DOM 差异</h3><p class="empty">没有采集到 DOM 差异</p>')
    if report.steps:
        items = "".join(f"<li>{_esc(item)}</li>" for item in report.steps)
        parts.append(f"<h3>执行步骤</h3><ol>{items}</ol>")
    parts.append("</section>")
    return "".join(parts)


def _diagnosis(report: FailureReport) -> str:
    diagnosis = report.diagnosis
    decision = report.decision
    rows = [
        ("分类", diagnosis.category),
        ("归因", _BLAME_LABELS.get(diagnosis.blame, diagnosis.blame)),
        ("置信度", f"{diagnosis.confidence:.2f}"
                   + ("" if diagnosis.is_certain else f" (低于 {diagnoser.THRESHOLD} 闸门, 结论偏弱)")),
        ("来源", diagnosis.source),
        ("规则版本", diagnosis.rule_version),
        ("下一步", decision.proposal_type or "人工处理"),
        ("可改测试代码", "是" if decision.may_edit_test_code else "否"),
    ]
    meta = "".join(f"<dt>{_esc(key)}</dt><dd>{_esc(value)}</dd>" for key, value in rows)
    parts = ['<section class="card"><h2>诊断</h2>',
             f'<dl class="grid">{meta}</dl>',
             f'<h3>根因</h3><p>{_esc(diagnosis.root_cause or "(没有具体根因)")}</p>']
    if decision.reason:
        parts.append(f'<h3>决策依据</h3><p>{_esc(decision.reason)}</p>')
    if diagnosis.evidence:
        items = "".join(f"<li>{_esc(item)}</li>" for item in diagnosis.evidence)
        parts.append(f"<h3>证据</h3><ol>{items}</ol>")
    if diagnosis.suggestion:
        parts.append(f'<h3>建议</h3><p>{_esc(diagnosis.suggestion)}</p>')
    if diagnosis.acceptable_categories:
        others = "、".join(diagnosis.acceptable_categories)
        parts.append(f'<p class="warn">另一条评审也会点头的分类: <b>{_esc(others)}</b>'
                     " —— 两条方案并列在下面, 别只看第一条。</p>")
    parts.append("</section>")
    return "".join(parts)


def _commands(proposal: Proposal, pending_name: str) -> str:
    """三条 (或两条) 命令 —— 只有真有补丁的方案才给 --accept

    wait_adjust / product_bug / env_issue 没有 diff: 把 --accept 印出来只会让人白跑一趟
    (applier 会拒), 不如当场说清楚这条要怎么处理。
    """
    base = f"python -m ai_kit.proposal.applier --decision {proposal.proposal_id}"
    tail = f' --pending "{pending_name}"' if pending_name else ""
    parts = []
    if proposal.is_code_change and proposal.diff.strip():
        parts.append(f'<div class="cmd"><span>采纳 · 应用并重跑验证 (不过自动回滚)</span>'
                     f"<code>{_esc(base)} --accept{_esc(tail)}</code></div>")
    else:
        parts.append('<div class="cmd"><span>这条方案没有补丁 (终态结论 / 参数类建议), '
                     "没有可应用的动作 —— 改完之后用下面的命令记一笔</span></div>")
    parts.append(f'<div class="cmd"><span>拒绝 · 记下为什么 (note 会进 decisions.jsonl)</span>'
                 f"<code>{_esc(base)} --reject"
                 f" --note &quot;文案变更应同步产品&quot;{_esc(tail)}</code></div>")
    parts.append(f'<div class="cmd"><span>搁置 · 只记账, 之后再决定</span>'
                 f"<code>{_esc(base)} --defer{_esc(tail)}</code></div>")
    return "".join(parts)


def _proposal_card(proposal: Proposal, pending_name: str) -> str:
    tone = "auto" if proposal.auto_applicable else "review"
    flags = [_tag(proposal.type),
             _tag("可自动应用" if proposal.auto_applicable else "需人工确认"),
             _tag(f"置信 {proposal.confidence:.2f}")]
    meta = [("target_file", proposal.target_file or "(无)"),
            ("verify_cmd", proposal.verify_cmd or "(无)"),
            ("rollback_cmd", proposal.rollback_cmd or "(无)")]
    rows = "".join(f"<dt>{_esc(key)}</dt><dd>{_esc(value)}</dd>" for key, value in meta)
    return (
        f'<article class="proposal {tone}">'
        f'<div class="p-head"><span class="pid">{_esc(proposal.proposal_id)}</span>'
        f'{_tag(proposal.risk, _risk_tone(proposal.risk))}{"".join(flags)}'
        f'<span class="p-title">{_esc(proposal.title)}</span></div>'
        f'<dl class="grid">{rows}</dl>'
        f'<p class="why"><b>为什么</b> {_esc(proposal.reason)}</p>'
        f"{_diff_block(proposal.diff)}"
        f"{_commands(proposal, pending_name)}"
        "</article>")


def _proposals(report: FailureReport, pending_name: str) -> str:
    parts = [f'<section class="card"><h2>方案 ({len(report.proposals)})</h2>']
    if not report.proposals:
        parts.append('<p class="empty">这次失败没有可执行方案 (归因不明 / 没有比原定位器更好的'
                     "候选 / LLM 没给草稿)。先看上面的现场与证据, 由人决定怎么处理。</p>")
    for proposal in report.proposals:
        parts.append(_proposal_card(proposal, pending_name))
    parts.append("</section>")
    return "".join(parts)


def _footer(report: FailureReport, pending_name: str) -> str:
    return (f"<footer>ai_kit report v{REPORT_VERSION} · {_esc(report.failure_id)}<br>"
            f"采纳会在仓库里直接改文件并跑 verify_cmd, 验证不通过自动回滚; 三个动作都会往 "
            f"decisions.jsonl 追加一行。方案数据见 {_esc(pending_name or PENDING_NAME)}。</footer>")


def render(report: FailureReport, *, pending_name: str = PENDING_NAME) -> str:
    """FailureReport -> 完整 HTML 字符串 (不读网络、不读 driver; 只可能读截图文件)"""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="generator" content="ai_kit report v{REPORT_VERSION}">'
        f"<title>{_esc(report.title)} · {_esc(report.failure_id)}</title>"
        f"<style>{_CSS}</style></head><body>"
        f"{_header(report)}<main>{_scene(report)}{_diagnosis(report)}"
        f"{_proposals(report, pending_name)}</main>"
        f"{_footer(report, pending_name)}</body></html>\n"
    )


# ---------------------------------------------------------------------------
# 落盘: html 给人, json 给 applier
# ---------------------------------------------------------------------------

def _write_text(path: Path, text: str) -> None:
    """newline="\\n": 报告是生成物, 换行不该跟着平台变 (否则 diff 永远显示整文件改动)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write(report: FailureReport, path: Any, *,
          pending: Any = None) -> Tuple[str, str]:
    """写 html + json 两份; 返回 (html 路径, json 路径)

    pending 不给时与 html 同主名 (report.html -> report.json), 报告里印的命令也指向它。
    """
    html_path = Path(str(path))
    pending_path = Path(str(pending)) if pending else html_path.with_suffix(".json")
    _write_text(html_path, render(report, pending_name=pending_path.name))
    _write_text(pending_path, json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n")
    return str(html_path), str(pending_path)


def load_pending(path: Any) -> Dict[str, Any]:
    """读回 report.json (applier 用): 坏文件要说清坏在哪, 不是抛 KeyError"""
    target = Path(str(path))
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReportError(f"没有这份报告数据: {target} (先生成报告, 或确认 --pending 指向它)") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportError(f"{target} 读不了: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReportError(f"{target} 的内容不是 JSON 对象")
    if not isinstance(payload.get("proposals"), list):
        raise ReportError(f"{target} 里没有 proposals 列表 (不是 report.write 的产物?)")
    return payload


def pending_proposals(payload: Dict[str, Any]) -> Tuple[Proposal, ...]:
    """report.json -> Proposal; 重新过一遍构造校验 (文件被手改过会在这里被拦住)

    只取 Proposal 的字段, 多余键忽略; 缺字段用空值补齐后交给 __post_init__ 判 —— 让它
    报"locator_patch 必须带 diff"这种具体错, 而不是 TypeError 说要几个参数。
    """
    known = {item.name for item in fields(Proposal)}
    items: List[Proposal] = []
    for raw in payload.get("proposals") or ():
        if not isinstance(raw, dict):
            raise ReportError(f"proposals 里有非对象条目: {raw!r}")
        data = {name: raw.get(name, "") for name in known}
        data["auto_applicable"] = bool(raw.get("auto_applicable"))
        try:
            data["confidence"] = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ReportError(f"方案 {raw.get('proposal_id')!r} 的 confidence 不是数字") from exc
        for name in ("proposal_id", "type", "title", "target_file", "diff", "risk",
                     "verify_cmd", "rollback_cmd", "reason"):
            data[name] = str(raw.get(name) or "")
        try:
            items.append(Proposal(**data))
        except (TypeError, ValueError) as exc:
            raise ReportError(f"方案 {raw.get('proposal_id')!r} 不合法: {exc}") from exc
    return tuple(items)
