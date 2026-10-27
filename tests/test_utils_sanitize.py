# -*- coding: utf-8 -*-
"""
PII 脱敏 (任务 1-1): 出境前把 DOM / 日志里的患者数据抹掉

验收口径不是"函数存在", 而是 **合成患者表单过一遍之后, 一个 PII 都不剩,
同时 DOM 骨架还在** —— 骨架没了 LLM 就推不出选择器, 脱敏等于把自愈能力废掉。

运行: pytest tests/test_utils_sanitize.py
"""
from __future__ import annotations

from selenium.webdriver.common.by import By

from ai_kit import healer
from ai_kit.utils import REDACTED, sanitize

# 合成的患者编辑弹窗: 姓名/身份证/手机号/病历号各一份, 值都塞在最常见的
# 三个位置 (value 属性 / 带 class 提示的元素 / 裸文本)
PATIENT_DIALOG = (
    '<div class="el-dialog" role="dialog"><h2>患者信息编辑</h2>'
    '<div class="el-form-item"><label>姓名</label><div class="el-input">'
    '<input class="el-input__inner" name="patientName" value="张三"/></div></div>'
    '<div class="el-form-item"><label>身份证</label>'
    '<input class="el-input__inner" id="idCard" value="110101199003071234"/></div>'
    '<div class="el-form-item"><label>手机号</label>'
    '<input class="el-input__inner" type="tel" value="13812345678"/></div>'
    '<span class="patient-name">李四</span>'
    '<span class="mrn">病历号：MR20240315008</span>'
    '<textarea name="note">王五 13900001111 家属代述</textarea>'
    '<button class="el-button el-button--danger">删除患者</button>'
    '<div class="el-form-item"><label>性别</label>'
    '<select name="gender"><option value="male">男</option></select></div>'
    "</div>"
)

PII_PROBES = ("张三", "李四", "王五", "110101199003071234",
              "13812345678", "13900001111", "MR20240315008")


# ---------------------------------------------------------------------------
# 逐条规则
# ---------------------------------------------------------------------------

def test_value_attribute_is_redacted():
    out = sanitize('<input class="el-input__inner" value="张三"/>')

    assert f'value="{REDACTED}"' in out
    assert "张三" not in out
    assert 'class="el-input__inner"' in out, "类名必须留着, 否则选择器没了"


def test_bare_value_without_quotes_is_redacted():
    out = sanitize("<input value=13812345678>")

    assert "13812345678" not in out
    assert REDACTED in out


def test_option_value_is_kept_because_it_is_not_user_data():
    """option/li 的 value 是选项标识, 抹掉反而让 LLM 认不出选项语义"""
    for html in ('<option value="male">男</option>', '<li value="2">第二页</li>'):
        assert sanitize(html) == html


def test_phone_in_free_text_with_and_without_separators():
    for text in ("电话 13812345678", "电话 138-1234-5678", "电话 138 1234 5678"):
        out = sanitize(text)
        assert "138" not in out, f"手机号没抹掉: {text} -> {out}"
        assert "电话" in out, "字段名要留着"


def test_id_card_18_and_legacy_15():
    out = sanitize("证件 11010119900307123X / 110101900307123 请核对")

    assert "11010119900307123X" not in out
    assert "110101900307123" not in out
    assert out.count(REDACTED) == 2


def test_medical_record_keeps_field_name():
    out = sanitize("病历号：MR20240315008")

    assert "MR20240315008" not in out
    assert "病历号" in out, "字段名留着, LLM 才知道那一格是病历号"


def test_name_after_keyword_and_after_label():
    assert "张三" not in sanitize("患者姓名：张三")
    assert "张三" not in sanitize("<label>姓名</label><div class='x'>张三</div>")
    assert "姓名" in sanitize("<label>姓名</label><div class='x'>张三</div>")


def test_textarea_content_is_redacted():
    out = sanitize("<textarea name='note'>胸痛三天</textarea>")

    assert "胸痛三天" not in out
    assert "<textarea name='note'>" in out and "</textarea>" in out


def test_element_with_pii_hint_class_is_redacted():
    assert "李四" not in sanitize('<span class="patient-name">李四</span>')
    assert "李四" not in sanitize('<td data-field="realName">李四</td>')


# ---------------------------------------------------------------------------
# 整体验收
# ---------------------------------------------------------------------------

def test_synthetic_patient_form_leaves_no_pii():
    out = sanitize(PATIENT_DIALOG)

    leaked = [p for p in PII_PROBES if p in out]
    assert not leaked, f"患者表单过完 sanitize 还有残留: {leaked}"
    assert out.count(REDACTED) >= 6


def test_structure_survives_so_llm_can_still_pick_selectors():
    out = sanitize(PATIENT_DIALOG)

    for keep in ('class="el-dialog"', 'role="dialog"', 'class="el-button el-button--danger"',
                 'name="patientName"', 'id="idCard"', "<select name=\"gender\">",
                 'value="male"', "删除患者"):
        assert keep in out, f"脱敏改坏了 DOM 骨架: {keep}"


def test_sanitize_is_idempotent():
    once = sanitize(PATIENT_DIALOG)

    assert sanitize(once) == once, "已脱敏文本再跑一遍不能把 [REDACTED] 套娃"


def test_clean_html_is_untouched():
    html = ('<div class="el-dialog" role="dialog">'
            '<button class="el-button el-button--primary">确定</button></div>')

    assert sanitize(html) == html, "没有 PII 的 HTML 必须原样返回"


def test_non_string_input_is_tolerated():
    """自愈链路宁可少一个字符, 也不该在拼提示词时抛异常"""
    assert sanitize(None) == ""
    assert sanitize("") == ""
    assert "13812345678" not in sanitize(b"\xe7\x94\xb5\xe8\xaf\x9d 13812345678")


# ---------------------------------------------------------------------------
# 接线: L4 拼提示词前必须过 sanitize
# ---------------------------------------------------------------------------

class _Element:
    size = {"width": 100, "height": 20}

    def __init__(self, html: str):
        self._html = html

    def is_displayed(self) -> bool:
        return True

    def get_attribute(self, name: str):
        return self._html if name == "outerHTML" else None


class _Driver:
    def __init__(self, html_by_selector):
        self._html = html_by_selector

    def find_elements(self, by, value):
        return [_Element(self._html[value])] if value in self._html else []


def test_l4_prompt_carries_no_pii():
    driver = _Driver({".el-dialog": PATIENT_DIALOG})
    captured = {}

    def fake_llm(system_prompt, user_prompt):
        captured["prompt"] = user_prompt
        return {"candidates": [{"selector": ".el-button--danger", "confidence": 0.8}]}

    out = healer._l4_llm_candidates(driver, ".old-delete-btn", intent="删除患者",
                                    llm_client=fake_llm, containers=[".el-dialog"])

    assert out == [(By.CSS_SELECTOR, ".el-button--danger", 0.8)]
    prompt = captured["prompt"]
    leaked = [p for p in PII_PROBES if p in prompt]
    assert not leaked, f"L4 提示词里还在泄 PII: {leaked}"
    assert REDACTED in prompt
    assert "el-button--danger" in prompt, "脱敏后仍要留下可推选择器的结构"


def test_l4_truncation_still_applies_after_sanitize():
    big = '<div class="el-dialog">' + '<i class="x"></i>' * 500 + "</div>"
    captured = {}

    def fake_llm(system_prompt, user_prompt):
        captured["prompt"] = user_prompt
        return {"candidates": []}

    healer._l4_llm_candidates(_Driver({".el-dialog": big}), ".x",
                              llm_client=fake_llm, containers=[".el-dialog"])

    assert "...[TRUNCATED]" in captured["prompt"]


# ---------------------------------------------------------------------------
# 接线: analyzer 的 traceback / log 文本同样要过 sanitize
# ---------------------------------------------------------------------------

def test_analyzer_prompt_carries_no_pii(monkeypatch):
    """失败断言里常带着被测页面填的值, 这些原样出境到 LLM 归因"""
    from ai_kit import analyzer, client

    captured = {}

    def fake_chat_json(system, user, **kwargs):
        captured["user"] = user
        return {"category": analyzer.CATEGORY_UNKNOWN, "confidence": 0.4}

    monkeypatch.setattr(client, "chat_json", fake_chat_json)
    monkeypatch.setattr(client, "llm_available", lambda: True)

    traceback = ('Traceback (most recent call last):\n  File "t.py", line 9\n'
                 'AssertionError: 病历号：MR20240315008\n'
                 'assert "13812345678" == "13812345670"')
    analyzer.diagnose_with_llm(traceback)

    user = captured["user"]
    for probe in ("MR20240315008", "13812345678", "13812345670"):
        assert probe not in user, f"analyzer 提示词里还在泄 PII: {probe}"
    assert "AssertionError" in user and "病历号" in user, "归因需要的结构不能抹没"
