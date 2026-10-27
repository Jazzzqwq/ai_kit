# -*- coding: utf-8 -*-
"""
智能失败分析 — Failure Analyzer (Phase 1 + Phase 2 LLM 版)
Phase 1: 规则匹配 (快, 不依赖 LLM)
Phase 2: 规则不确定时自动调 LLM 精准归因 (慢, 但更准)

调用优先级: Phase 1 规则 → confidence<0.8 或 UNKNOWN → Phase 2 LLM
如果 LLM 不可用 (没填 key / 挂了), 自动降级回 Phase 1 规则结果
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ai_kit.utils import sanitize


@dataclass
class FailureDiagnosis:
    category: str                  # 分类标签
    confidence: float              # 置信度 0~1
    summary: str                   # 一句话根因
    evidence: List[str] = field(default_factory=list)  # 命中的规则
    suggestion: str = ""           # 处理建议
    source: str = "rule"           # "rule" (Phase1) / "llm" (Phase2)

    def print_report(self) -> None:
        src_label = "📋规则" if self.source == "rule" else "🧠LLM"
        print(f"\n  [分析 {src_label}] === 失败归因 ===")
        print(f"  分类: {self.category}")
        print(f"  置信度: {self.confidence:.2f}")
        print(f"  根因: {self.summary}")
        if self.evidence:
            print(f"  命中规则: {', '.join(self.evidence)}")
        if self.suggestion:
            print(f"  建议: {self.suggestion}")


# ---------------------------------------------------------------------------
# 分类标签
# ---------------------------------------------------------------------------

CATEGORY_SCRIPT_HEALER = "脚本-定位器问题"
CATEGORY_SCRIPT_WAIT = "脚本-等待问题"
CATEGORY_PRODUCT_BUG = "产品缺陷"
CATEGORY_ENV = "环境问题"
CATEGORY_SYNTAX = "脚本-语法/导入"
CATEGORY_UNKNOWN = "未知-待人工分析"

# 六类标签的完整集合: 方案层按它把标签投影成 blame, 少一类就会在那里判成"未知"
CATEGORIES = (CATEGORY_SCRIPT_HEALER, CATEGORY_SCRIPT_WAIT, CATEGORY_PRODUCT_BUG,
              CATEGORY_ENV, CATEGORY_SYNTAX, CATEGORY_UNKNOWN)

# LLM 的常见说法 -> 六类标签。按顺序匹配, 命中第一个就停 (与 Phase2 历史行为一致)
CATEGORY_KEYWORDS: Dict[str, str] = {
    "定位器": CATEGORY_SCRIPT_HEALER,
    "等待": CATEGORY_SCRIPT_WAIT,
    "语法": CATEGORY_SYNTAX,
    "导入": CATEGORY_SYNTAX,
    "环境": CATEGORY_ENV,
    "session": CATEGORY_ENV,
    "产品": CATEGORY_PRODUCT_BUG,
    "缺陷": CATEGORY_PRODUCT_BUG,
    "未知": CATEGORY_UNKNOWN,
}


def normalize_category(raw: str) -> str:
    """把 LLM 报的分类归一到六类标签; 认不出来原样返回, 由调用方决定要不要用

    Phase2 和方案层的 blame 判定都吃这一张表 —— 关键词表只维护一份, 两边口径
    不会各说各话。
    """
    text = str(raw or "")
    for keyword, category in CATEGORY_KEYWORDS.items():
        if keyword in text:
            return category
    return text


# ---------------------------------------------------------------------------
# Phase 1: 规则表 (从上到下先命中先返回)
# ---------------------------------------------------------------------------

# 规则表 / 关键词表 改动就把版本 +1: 评测结果里要记它, 回归时才说得清"这一条基线
# 是哪一版规则判的"。方案层还叠着自己的 blame 投影版本, 两段一起写进 rule_version。
RULES_VERSION = "rules.v1"

_RULES: List = [
    (["SyntaxError", "ModuleNotFoundError", "ImportError"],
     CATEGORY_SYNTAX,
     "Python 语法或导入失败",
     "检查文件语法 / 依赖是否安装, 一般是刚改完代码留下的低级错误",
     "SyntaxError"),

    (["no such window", "target window already closed",
      "Connection refused", "Connection reset",
      "SessionNotCreatedException", "InvalidSessionIdException",
      "Error communicating with the remote browser"],
     CATEGORY_ENV,
     "浏览器 session 断了",
     "关闭所有 Edge/Chrome 进程, 检查浏览器版本与 selenium 驱动是否匹配, 重跑",
     "Session/Crash"),

    (["StaleElementReferenceException",
      "element is not attached to the page document"],
     CATEGORY_SCRIPT_WAIT,
     "元素 DOM 引用过期 (页面刷新/重新渲染导致)",
     "定位后立即操作, 或每次操作前重新 find_element",
     "Stale"),

    (["TimeoutException", "timed out waiting", "Timeout waiting"],
     CATEGORY_SCRIPT_WAIT,
     "显式等待超时",
     "加 WebDriverWait + EC 条件; 检查元素选择器是否正确",
     "Timeout"),

    (["NoSuchElementException", "Unable to locate element",
      "ElementNotInteractableException", "ElementClickInterceptedException",
      "element not interactable", "click intercepted",
      "Other element would receive the click",
      # runtime 原语把等待超时统一包成 _Missing("找不到元素: ..."), 这样它也能
      # 归到定位器而不是掉进"未知"
      "找不到元素"],
     CATEGORY_SCRIPT_HEALER,
     "定位器失效或元素被遮挡",
     "检查定位器 / Element Plus wrapper / 弹窗残留遮挡",
     "Locator/Intercept"),

    (["errors=\\[\\]", "新增被拦截: errors=\\[\\]", "弹窗已关但查不到"],
     CATEGORY_PRODUCT_BUG,
     "后端拒绝但前端无明确错误提示",
     "检查后端接口返回; 可能是字段校验被静默拒绝",
     "SilentReject"),

    (["AssertionError"],
     CATEGORY_PRODUCT_BUG,
     "断言不匹配 (功能行为与预期不一致)",
     "看断言具体内容; 如果是 errors=[] 但返回 False 见上一条",
     "Assertion"),
]


def _rule_match(text: str) -> Optional[FailureDiagnosis]:
    """Phase 1: 纯规则匹配"""
    for patterns, category, summary, suggestion, evidence_label in _RULES:
        hits = [p for p in patterns if re.search(p, text, re.IGNORECASE)]
        if hits:
            confidence = 0.7 + 0.1 * len(hits)
            confidence = min(confidence, 1.0)
            return FailureDiagnosis(
                category=category,
                confidence=confidence,
                summary=summary,
                evidence=[evidence_label] + hits,
                suggestion=suggestion,
                source="rule",
            )
    return FailureDiagnosis(
        category=CATEGORY_UNKNOWN,
        confidence=0.0,
        summary="规则未命中, 需人工查看 traceback",
        suggestion="贴完整 traceback 和最后 30 行日志给 AI 分析",
        source="rule",
    )


# ---------------------------------------------------------------------------
# Phase 2: LLM 精准归因
# ---------------------------------------------------------------------------

LLM_SYSTEM_PROMPT = """你是一个专业的 Selenium UI 自动化测试失败分析专家.
用户可能同时提供:
- traceback 文本
- 日志文本
- 失败时的页面截图 (图片)

如果有截图, 请结合图片内容分析 (比如: 看到了遮挡弹窗/红色 is-error 字段/元素是否可见).
根据所有信息判断失败属于以下 6 类之一, 并给出置信度 (0~1) 和具体建议:

1. 脚本-定位器问题: 元素找不到 / 被遮挡 / 被其他元素拦截点击
2. 脚本-等待问题: 超时 / 元素还没渲染 / DOM 引用过期
3. 脚本-语法/导入: SyntaxError / ImportError / 依赖缺失
4. 环境问题: session 崩了 / 浏览器版本不匹配 / 网络断开
5. 产品缺陷: 功能行为与预期不符 / 后端静默拒绝 / 前端无错误提示
6. 未知: 无法判断

输出严格 JSON 格式 (不要 markdown 代码块):
{"category": "...", "confidence": 0.xx, "summary": "一句话根因", "suggestion": "具体处理步骤"}
"""


def matched_categories(text: str) -> Tuple[str, ...]:
    """命中了哪些分类 (按规则表顺序去重)

    与 diagnose() 的区别只有一点: diagnose 只报第一名, 这里报"还像哪些" —— 方案层
    用它判歧义 (定位器 / 等待 同时命中时, 两个结论都该被评审接受)。不是第二套口径,
    用的还是同一张 _RULES, 不会两边各说各话。
    """
    seen: List[str] = []
    for patterns, category, *_rest in _RULES:
        if category not in seen and any(re.search(p, text, re.IGNORECASE)
                                        for p in patterns):
            seen.append(category)
    return tuple(seen)


def diagnose_with_llm(traceback_text: str, log_text: str = "", screenshot_base64: Optional[str] = None) -> Optional[FailureDiagnosis]:
    """
    Phase 2: 调 LLM 精准归因. 失败返回 None.
    """
    try:
        from ai_kit.client import chat_json, chat_json_with_image, llm_available
    except Exception:
        try:
            from ai_kit.client import chat_json, chat_json_with_image, llm_available
        except Exception:
            return None

    if not llm_available():
        return None

    # 裁剪长度, 避免超出 token limit
    tb_trimmed = traceback_text[-2000:] if len(traceback_text) > 2000 else traceback_text
    log_trimmed = log_text[-1000:] if len(log_text) > 1000 else log_text

    user_msg = f"=== Traceback ===\n{tb_trimmed}\n\n=== Log ===\n{log_trimmed}"
    # 出境前脱敏: traceback 的 assert 消息里常带着被测页面填的值
    # ("13812345678" != "13812345670"), log 同理。截图无法文本脱敏, 见 README 数据安全。
    user_msg = sanitize(user_msg)
    if screenshot_base64:
        print(f"  [分析-多模态] 检测到截图 ({len(screenshot_base64)//1024}KB base64), 调 LLM Vision...")
        result = chat_json_with_image(LLM_SYSTEM_PROMPT, user_msg, screenshot_base64)
    else:
        result = chat_json(LLM_SYSTEM_PROMPT, user_msg)
    if not result:
        return None

    # 对齐分类标签 (关键词表在模块级, 方案层也用同一张)
    category = normalize_category(result.get("category", CATEGORY_UNKNOWN))
    confidence = float(result.get("confidence", 0.5))
    summary = str(result.get("summary", ""))
    suggestion = str(result.get("suggestion", ""))

    return FailureDiagnosis(
        category=category,
        confidence=max(0.0, min(1.0, confidence)),
        summary=summary,
        evidence=[f"LLM: {category}"],
        suggestion=suggestion,
        source="llm",
    )


# ---------------------------------------------------------------------------
# 主入口: diagnose() — Phase 1 → Phase 2 自动降级
# ---------------------------------------------------------------------------

def diagnose(traceback_text: str, log_text: str = "",
             use_llm: bool = True, screenshot_base64: Optional[str] = None) -> FailureDiagnosis:
    """
    对一次失败做分类.
    Phase 1 规则匹配 → confidence < 0.8 或 UNKNOWN 时, 如果 use_llm=True 自动调 LLM
    LLM 挂了 → 降级回 Phase 1 结果
    """
    # 1) Phase 1 规则
    rule_diag = _rule_match(f"{traceback_text}\n{log_text}")

    if not use_llm:
        return rule_diag

    # 2) 判断是否需要 LLM 补充
    need_llm = (
        rule_diag.confidence < 0.8
        or rule_diag.category == CATEGORY_UNKNOWN
    )
    if not need_llm:
        return rule_diag

    # 3) 调 LLM
    print("  [分析] Phase 1 置信度低, 调 Phase 2 LLM 精准归因...")
    llm_diag = diagnose_with_llm(traceback_text, log_text, screenshot_base64=screenshot_base64)
    if llm_diag is None:
        print("  [分析] LLM 不可用, 降级回 Phase 1 规则结果")
        return rule_diag

    # LLM 结果更准 → 但如果 LLM 置信度比规则还低, 还是用规则
    if llm_diag.confidence >= rule_diag.confidence:
        print(f"  [分析] LLM 结果置信度更高 ({llm_diag.confidence:.2f} vs {rule_diag.confidence:.2f}), 采用 LLM 结果")
        return llm_diag
    print(f"  [分析] 规则结果置信度更高 ({rule_diag.confidence:.2f} vs {llm_diag.confidence:.2f}), 采用规则结果")
    return rule_diag


# ---------------------------------------------------------------------------
# 从 Allure 结果目录批量分析
# ---------------------------------------------------------------------------

def analyze_allure_results(allure_dir: str, max_failures: int = 5,
                           use_llm: bool = True) -> List[FailureDiagnosis]:
    import json as _json
    results: List[FailureDiagnosis] = []
    if not os.path.isdir(allure_dir):
        return results

    files = [f for f in os.listdir(allure_dir) if f.endswith("-result.json")]
    for fname in files[:20]:
        try:
            with open(os.path.join(allure_dir, fname), encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            continue
        if data.get("status") != "failed":
            continue
        tb_parts = []
        for det_key in ("statusTrace", "statusMessage"):
            d = data.get(det_key, "")
            if d:
                tb_parts.append(str(d))

        def scan_steps(steps):
            for s in steps or []:
                if s.get("status") == "failed":
                    for k in ("statusTrace", "statusMessage"):
                        v = s.get(k, "")
                        if v:
                            tb_parts.append(str(v))
                    scan_steps(s.get("steps", []))
        scan_steps(data.get("steps", []))

        tb_text = "\n".join(tb_parts)
        diag = diagnose(tb_text, use_llm=use_llm)
        diag.summary = f"[{data.get('name', '?')}] {diag.summary}"
        results.append(diag)
        if len(results) >= max_failures:
            break

    return results
