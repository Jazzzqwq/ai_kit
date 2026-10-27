# -*- coding: utf-8 -*-
"""任务 6.4: 人工确认 v1 — 报告是一张能发出去的纸, 不是一个服务

锁死这些事 (任务卡):
    单文件 HTML: 失败现场卡 (截图 / traceback / 日志尾部 / DOM 差异) + 诊断 + 方案列表含 diff
    报告里印出三个动作的命令 (accept / reject / defer), 决策由 ai_kit.proposal.applier 落账
    write() 同时写 report.json —— applier 的输入, 与 html 是同一份数据

为什么"单文件"要写成测试: 引一张外部 CDN 的 CSS 是最容易顺手做的事, 而报告最需要
被打开的两个场合 (离线环境、发给别人) 恰好都加载不了它。
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_kit.proposal import diagnoser as D
from ai_kit.proposal import report as R
from ai_kit.proposal.solutions import Proposal

TARGET = "tests/test_user.py"
CASE = "test_open_dialog"
BROKEN = "body > div.el-dialog"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40

DIFF = ("--- a/tests/test_user.py\n"
        "+++ b/tests/test_user.py\n"
        "@@ -4,3 +4,3 @@\n"
        " def test_open_dialog():\n"
        '-    locator = "body > div.el-dialog"\n'
        '+    locator = ".el-dialog"\n'
        "     assert locator\n")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

def diag_locator() -> D.FailureDiagnosis:
    return D.blame_by_rules(D.FailureContext(
        traceback="NoSuchElementException: Unable to locate element: " + BROKEN,
        log="find_element failed", case_id="LC-002", test_name=CASE))


def diag_product() -> D.FailureDiagnosis:
    return D.blame_by_rules(D.FailureContext(
        traceback="AssertionError: 期望 3 行, 实际 0 行",
        observation=D.PageObservation(text="保存失败"), case_id="LC-002", test_name=CASE))


def patch_proposal() -> Proposal:
    return Proposal(proposal_id="S001", type="locator_patch",
                    title=f"{BROKEN} → .el-dialog", target_file=TARGET, diff=DIFF,
                    risk="low", auto_applicable=True, confidence=0.9,
                    verify_cmd=f"pytest {TARGET}::{CASE} -q",
                    rollback_cmd=f"git checkout -- {TARGET}",
                    reason="L1 规则给出 .el-dialog (权重 1.10): Element Plus dialog DOM")


def wait_proposal() -> Proposal:
    return Proposal(proposal_id="S002", type="wait_adjust", title="显式等待 10s → 20s",
                    target_file=TARGET, diff="", risk="medium", auto_applicable=False,
                    confidence=0.6, verify_cmd=f"pytest {TARGET}::{CASE} -q",
                    rollback_cmd="", reason="日志里 retried 10s, 等待时间可能不够")


def build_report(*, traceback: str = "TimeoutException: timed out", log: str = "line",
                 case_id: str = "LC-002", test_name: str = CASE,
                 diagnosis=None, proposals=None, **overrides) -> R.FailureReport:
    """现场材料属于 FailureContext, 报告字段 (截图 / DOM 差异 / failure_id) 单独给"""
    context = D.FailureContext(traceback=traceback, log=log, case_id=case_id,
                               test_name=test_name)
    kwargs = dict(screenshot="", dom_diff="- <div class=x>\n+ <div>", failure_id="LC-002")
    kwargs.update(overrides)
    return R.build(context, diagnosis or diag_locator(),
                   [patch_proposal()] if proposals is None else proposals, **kwargs)


def render(**overrides) -> str:
    return R.render(build_report(**overrides))


# ---------------------------------------------------------------------------
# 单文件 / 转义: 报告的两条底线
# ---------------------------------------------------------------------------

def test_the_report_is_one_self_contained_file():
    """引一个外部 CDN 最省事, 但离线打不开 —— 报告要能当附件发出去"""
    body = render()

    assert body.startswith("<!DOCTYPE html>")
    assert "<style>" in body
    assert "<script" not in body.lower()
    assert 'src="http' not in body and 'href="http' not in body


def test_every_external_text_is_escaped():
    """报告里最脏的东西恰好是异常栈 (来自被测站点), 不转义就是把页面交出去"""
    diagnosis = diag_locator()
    body = R.render(R.build(
        D.FailureContext(traceback='NoSuchElementException: <script>alert(1)</script>',
                         log="<img src=x onerror=alert(1)>", case_id="LC-002",
                         test_name=CASE),
        diagnosis, [patch_proposal()], failure_id="LC-002"))

    assert "<script>alert" not in body
    assert "&lt;script&gt;" in body
    assert "<img src=x" not in body


def test_the_diff_is_escaped_too():
    """diff 里的内容同样来自源码, 一条删除行就能塞进标签"""
    nasty = DIFF.replace("assert locator", 'assert "<script>x</script>"')
    proposal = replace(patch_proposal(), diff=nasty)
    body = R.render(R.build(D.FailureContext(case_id="LC-002", test_name=CASE),
                            diag_locator(), [proposal], failure_id="LC-002"))

    assert "<script>x</script>" not in body
    assert "&lt;script&gt;x&lt;/script&gt;" in body


# ---------------------------------------------------------------------------
# 失败现场: 四样材料
# ---------------------------------------------------------------------------

def test_the_scene_has_the_four_pieces_the_card_asks_for(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)
    body = render(screenshot=str(shot), traceback="TimeoutException: boom",
                  dom_diff="- <div class=old>\n+ <div class=new>")

    assert "<img" in body
    assert "TimeoutException: boom" in body
    assert "日志尾部" in body
    assert "- &lt;div class=old&gt;" in body or "&lt;div class=old&gt;" in body


def test_the_screenshot_is_inlined_as_a_data_uri(tmp_path):
    """单文件的意思是: 把 html 拷到别处, 截图还在"""
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)
    body = render(screenshot=str(shot))

    assert "data:image/png;base64," in body
    assert str(shot) not in body, "内嵌之后不该再依赖这张图在磁盘上的位置"


def test_a_missing_screenshot_is_explained_not_ignored():
    body = render(screenshot="")

    assert "没有采集到截图" in body


def test_an_unreadable_screenshot_keeps_the_path_for_the_human(tmp_path):
    body = render(screenshot=str(tmp_path / "gone.png"))

    assert "截图读不到" in body
    assert "gone.png" in body


def test_an_oversized_screenshot_is_not_inlined(tmp_path, monkeypatch):
    """把一个几十 MB 的 PNG 塞进 html, 打开的人只会以为浏览器卡死了"""
    monkeypatch.setattr(R, "MAX_SHOT_BYTES", 8)
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)
    body = render(screenshot=str(shot))

    assert "data:image/png;base64," not in body
    assert "超过内嵌上限" in body


def test_the_log_shows_only_the_tail_and_says_how_many_lines():
    log = "\n".join(f"line-{index}" for index in range(100))
    body = render(log=log)

    assert "只显示最后 40 行, 共 100 行" in body
    assert "line-99" in body
    assert "line-0" not in body


def test_a_long_traceback_keeps_both_ends():
    """异常栈开头是哪个用例, 结尾才是真正那句异常, 中间才该省"""
    traceback = "start-" + "x" * 20000 + "-tail"
    body = render(traceback=traceback)

    assert "start-" in body
    assert "-tail" in body
    assert "中间省略" in body


def test_a_failure_without_materials_says_so():
    body = R.render(R.build(D.FailureContext(case_id="LC-002", test_name=CASE),
                            diag_locator(), [], failure_id="LC-002"))

    assert "没有 traceback" in body
    assert "没有日志" in body
    assert "没有采集到 DOM 差异" in body


# ---------------------------------------------------------------------------
# 诊断
# ---------------------------------------------------------------------------

def test_the_diagnosis_block_carries_the_evidence_and_the_gate():
    body = render()

    assert "根因" in body
    assert diag_locator().root_cause in body or "NoSuchElementException" in body
    assert "证据" in body
    assert "可改测试代码" in body


def test_an_ambiguous_diagnosis_is_flagged_in_the_report():
    """6.2 的 acceptable_categories 在报告里必须看得见, 否则人只看到第一条结论"""
    ambiguous = replace(diag_locator(),
                        acceptable_categories=("script_wait",))
    body = R.render(R.build(D.FailureContext(case_id="LC-002", test_name=CASE),
                            ambiguous, [patch_proposal()], failure_id="LC-002"))

    assert "另一条评审也会点头的分类" in body
    assert "script_wait" in body


def test_a_product_bug_report_shows_the_terminal_decision():
    body = R.render(R.build(D.FailureContext(case_id="LC-002", test_name=CASE),
                            diag_product(), [], failure_id="LC-002"))

    assert "product_bug" in body
    assert "可改测试代码" in body and "否" in body


def test_a_weak_diagnosis_is_marked_below_the_gate():
    weak = replace(diag_locator(), confidence=0.3)
    body = R.render(R.build(D.FailureContext(case_id="LC-002", test_name=CASE),
                            weak, [patch_proposal()], failure_id="LC-002"))

    assert "结论偏弱" in body


# ---------------------------------------------------------------------------
# 方案列表
# ---------------------------------------------------------------------------

def test_the_proposal_card_shows_type_risk_reason_and_diff():
    body = render()

    assert "S001" in body
    assert "locator_patch" in body
    assert "为什么" in body
    assert "L1 规则给出 .el-dialog" in body
    assert 'class="add"' in body and 'class="del"' in body


def test_the_diff_colouring_separates_the_three_kinds_of_lines():
    body = render()

    assert '<span class="hunk">@@' in body
    # 引号统一转成 &quot; (同 _esc 的 quote=True); 浏览器里看到的还是原文
    assert '<span class="add">+    locator = &quot;.el-dialog&quot;' in body
    assert '<span class="del">-    locator = &quot;body &gt; div.el-dialog&quot;' in body


def test_a_proposal_without_a_diff_says_so_instead_of_showing_an_empty_box():
    body = R.render(R.build(D.FailureContext(case_id="LC-002", test_name=CASE),
                            diag_locator(), [wait_proposal()], failure_id="LC-002"))

    assert "这条方案不带补丁" in body


def test_the_three_commands_are_printed_for_a_proposal_with_a_patch():
    body = render()

    assert "--decision S001 --accept" in body
    assert "--decision S001 --reject" in body
    assert "--decision S001 --defer" in body
    # 引号在 HTML 里是 &quot; (浏览器里看到的才是 ")
    assert "--pending &quot;report.json&quot;" in body


def test_a_proposal_without_a_patch_does_not_offer_accept():
    """没有 diff 的方案印出 --accept, 人只会白跑一趟 (applier 一定会拒)"""
    body = R.render(R.build(D.FailureContext(case_id="LC-002", test_name=CASE),
                            diag_locator(), [wait_proposal()], failure_id="LC-002"))

    assert "--decision S002 --accept" not in body
    assert "--decision S002 --reject" in body and "--decision S002 --defer" in body
    assert "没有可应用的动作" in body


def test_the_commands_point_at_the_real_pending_file():
    body = R.render(build_report(), pending_name="run-42.json")

    assert "--pending &quot;run-42.json&quot;" in body


def test_no_proposals_is_explained_and_not_left_blank():
    body = R.render(R.build(D.FailureContext(case_id="LC-002", test_name=CASE),
                            diag_locator(), [], failure_id="LC-002"))

    assert "没有可执行方案" in body


def test_the_footer_says_where_decisions_go():
    body = render()

    assert "decisions.jsonl" in body


def test_the_footer_states_the_6_5_limits_where_a_human_reads_them():
    """白名单与"一次失败一个方案"写在人要照做的地方, 不是只写在代码里"""
    body = render()

    assert "pages/ tests/ config/" in body
    assert "一次失败只应用一个方案" in body
    assert "--stash" in body
    assert "patches/" in body


# ---------------------------------------------------------------------------
# 落盘: html 给人, json 给 applier
# ---------------------------------------------------------------------------

def test_write_creates_the_html_and_the_pending_json(tmp_path):
    html_path, json_path = R.write(build_report(), tmp_path / "report.html")

    assert Path(html_path).is_file() and Path(html_path).name == "report.html"
    assert Path(json_path).name == "report.json", "默认与 html 同主名, 报告里的命令才连得上"


def test_the_pending_json_carries_the_failure_id_and_every_proposal_field(tmp_path):
    _, json_path = R.write(build_report(), tmp_path / "report.html")
    payload = json.loads(Path(json_path).read_text(encoding="utf-8"))

    assert payload["failure_id"] == "LC-002"
    assert payload["version"] == R.REPORT_VERSION
    item = payload["proposals"][0]
    assert set(item) == {"proposal_id", "type", "title", "target_file", "diff", "risk",
                         "auto_applicable", "confidence", "verify_cmd", "rollback_cmd",
                         "reason"}


def test_the_pending_json_keeps_chinese_readable(tmp_path):
    """账本与报告都要能直接 cat 出来看, 满屏 u4e2d 只会让人放弃排查"""
    _, json_path = R.write(build_report(), tmp_path / "report.html")
    text = Path(json_path).read_text(encoding="utf-8")

    assert "L1 规则给出" in text
    assert "\\u" not in text, "json 里不该是 \\uXXXX 转义"


def test_both_files_are_written_with_lf(tmp_path):
    html_path, json_path = R.write(build_report(), tmp_path / "report.html")

    assert b"\r\n" not in Path(html_path).read_bytes()
    assert b"\r\n" not in Path(json_path).read_bytes()


def test_a_custom_pending_path_is_honoured(tmp_path):
    html_path, json_path = R.write(build_report(), tmp_path / "report.html",
                                   pending=tmp_path / "deep" / "pending.json")

    assert Path(json_path) == tmp_path / "deep" / "pending.json"
    assert Path(json_path).is_file()
    assert '--pending &quot;pending.json&quot;' in Path(html_path).read_text(encoding="utf-8")


def test_load_pending_round_trips_into_proposals(tmp_path):
    _, json_path = R.write(build_report(), tmp_path / "report.html")
    payload = R.load_pending(json_path)
    items = R.pending_proposals(payload)

    assert [item.proposal_id for item in items] == ["S001"]
    assert items[0].diff == DIFF
    assert items[0].auto_applicable is True


def test_load_pending_explains_a_missing_file(tmp_path):
    with pytest.raises(R.ReportError, match="没有这份报告数据"):
        R.load_pending(tmp_path / "nope.json")


def test_load_pending_explains_a_broken_file(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(R.ReportError, match="读不了"):
        R.load_pending(broken)


def test_load_pending_rejects_a_file_without_proposals(tmp_path):
    odd = tmp_path / "odd.json"
    odd.write_text('{"failure_id": "x"}', encoding="utf-8")

    with pytest.raises(R.ReportError, match="proposals"):
        R.load_pending(odd)


def test_a_tampered_pending_file_is_caught_by_the_schema(tmp_path):
    """手改 report.json 比手改 python 容易得多 —— 重新构造时再过一遍校验"""
    _, json_path = R.write(build_report(), tmp_path / "report.html")
    payload = R.load_pending(json_path)
    payload["proposals"][0]["diff"] = ""

    with pytest.raises(R.ReportError, match="locator_patch 必须带 diff"):
        R.pending_proposals(payload)


def test_a_duplicate_proposal_id_is_rejected():
    """编号重复, applier --decision 就会应用错那条"""
    with pytest.raises(R.ReportError, match="方案编号重复"):
        R.FailureReport(failure_id="LC-002", created_at="2026-01-01T00:00:00Z",
                        diagnosis=diag_locator(), decision=D.decide(diag_locator()),
                        proposals=(patch_proposal(), patch_proposal()))


def test_an_empty_failure_id_is_rejected():
    with pytest.raises(R.ReportError, match="failure_id"):
        R.FailureReport(failure_id="  ", created_at="2026-01-01T00:00:00Z",
                        diagnosis=diag_locator(), decision=D.decide(diag_locator()))


# ---------------------------------------------------------------------------
# failure_id: 报告与 decisions.jsonl 之间的那根线
# ---------------------------------------------------------------------------

def test_the_failure_id_comes_from_the_case_when_not_given():
    payload = build_report(failure_id="")

    assert payload.failure_id.startswith("LC-002-")
    assert " " not in payload.failure_id


def test_an_explicit_failure_id_is_sanitised():
    payload = build_report(failure_id="LC 002/第三次")

    assert payload.failure_id == "LC-002"


def test_the_same_explicit_id_is_reproducible():
    moment = datetime(2026, 10, 27, 10, 0, 0, tzinfo=timezone.utc)

    first = build_report(moment=moment)
    second = build_report(moment=moment)

    assert first.failure_id == second.failure_id
    assert first.created_at == "2026-10-27T10:00:00Z"


def test_the_title_falls_back_to_the_failure_id():
    report = R.FailureReport(failure_id="LC-002", created_at="2026-01-01T00:00:00Z",
                             diagnosis=diag_locator(), decision=D.decide(diag_locator()))

    assert report.title == "LC-002 失败"


def test_proposals_are_looked_up_by_id():
    report = build_report()

    assert report.proposal("S001") is not None
    assert report.proposal("S999") is None
