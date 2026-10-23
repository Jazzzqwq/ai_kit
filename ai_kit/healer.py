# -*- coding: utf-8 -*-
"""
AI 自愈定位器 — Locator Healer (Phase 1 本地版)
分层自愈策略: L1 规则映射 → L2 多语义 CSS → L3 相对 XPath
不依赖 LLM, 所有规则本地可解释可调试
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import List, Optional, Tuple, Dict, Any

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement


# ---------------------------------------------------------------------------
# L1: 规则映射表 — 积累你踩过的坑, 命中直接替换
# ---------------------------------------------------------------------------
L1_RULES: Dict[str, Tuple[str, str, str]] = {
    # Element Plus 弹窗: body 直接子元素是 __wrapper, 不是 .el-message-box 本身
    "body > div.el-message-box": (
        By.CSS_SELECTOR, ".el-message-box",
        "Element Plus 弹窗 DOM: body > __wrapper > .el-message-box"),
    "body > div.el-dialog": (
        By.CSS_SELECTOR, ".el-dialog",
        "Element Plus dialog DOM: body > __wrapper > .el-dialog"),
    "body > .el-message-box": (
        By.CSS_SELECTOR, ".el-message-box",
        "body 子元素不是 .el-message-box"),
    "body > .el-dialog": (
        By.CSS_SELECTOR, ".el-dialog",
        "body 子元素不是 .el-dialog"),
    # 删除确认弹窗按钮: 可能是 danger (红色) 不是 primary
    ".el-message-box__btns button.el-button--primary": (
        By.CSS_SELECTOR, ".el-message-box__btns button.el-button--danger, "
                         ".el-message-box__btns button.el-button--primary",
        "删除确认弹窗用 danger 按钮, 保存用 primary, 都试试"),
}


def _cache_path() -> str:
    """自愈缓存路径: 优先 AI_KIT_CACHE_DIR, 默认 ~/.ai_kit。

    独立包不能把运行产物写进自身目录 (site-packages 可能只读),
    因此不再探测项目里的 pages 目录。"""
    base = os.environ.get("AI_KIT_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".ai_kit")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        import tempfile
        base = tempfile.gettempdir()
    return os.path.join(base, "locator_heal_cache.json")


def _load_cache() -> Dict[str, Dict[str, Any]]:
    p = _cache_path()
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    try:
        with open(_cache_path(), "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# L2: 多语义 CSS — 基于语义类别枚举候选选择器
# ---------------------------------------------------------------------------

SEMANTIC_HINTS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"message[-_]?box|el-message-box", re.I), "message_box"),
    (re.compile(r"\.el-dialog", re.I), "dialog"),
    (re.compile(r"el-table", re.I), "table"),
    (re.compile(r"el-select|el-dropdown", re.I), "select"),
]

SEMANTIC_CANDIDATES: Dict[str, List[str]] = {
    "message_box": [
        ".el-message-box",
        ".el-message-box__wrapper .el-message-box",
        "body > .el-message-box__wrapper > .el-message-box",
    ],
    "dialog": [
        ".el-dialog",
        ".el-dialog__wrapper .el-dialog",
    ],
    "table": [
        ".el-table__body-wrapper .el-table__body",
        ".el-table .el-table__body",
        "table.el-table__body",
    ],
    "select": [
        ".el-select__wrapper",
        ".el-select .el-input__inner",
    ],
}


def _guess_semantic(locator: str) -> Optional[str]:
    for pat, hint in SEMANTIC_HINTS:
        if pat.search(locator):
            return hint
    return None


def _l2_candidates(original_by: str, original_loc: str,
                   intent: str = "") -> List[Tuple[str, str, float]]:
    """返回 [(by, locator, confidence)] 候选列表"""
    results: List[Tuple[str, str, float]] = []
    sem = _guess_semantic(original_loc)
    results.append((original_by, original_loc, 0.1))

    if sem and sem in SEMANTIC_CANDIDATES:
        for i, cand in enumerate(SEMANTIC_CANDIDATES[sem]):
            conf = 0.9 - i * 0.05
            results.append((By.CSS_SELECTOR, cand, conf))

    # intent 辅助
    intent_lower = intent.lower()
    if intent and ("确定" in intent or "confirm" in intent_lower or "delete" in intent_lower):
        results.append((By.CSS_SELECTOR,
                        ".el-message-box__btns button.el-button--danger, "
                        ".el-message-box__btns button.el-button--primary, "
                        ".el-message-box__btns button:last-child", 1.0))
    if intent and ("取消" in intent or "cancel" in intent_lower):
        results.append((By.CSS_SELECTOR,
                        ".el-message-box__btns button.el-button--default", 1.0))
    if intent and ("关闭" in intent or "close" in intent_lower):
        results.append((By.CSS_SELECTOR,
                        ".el-message-box__headerbtn, .el-dialog__close", 1.0))

    return results


def _l3_xpath_candidates(driver: WebDriver) -> List[Tuple[str, str, float]]:
    """基于当前可见容器生成相对 XPath"""
    candidates: List[Tuple[str, str, float]] = []
    for sel in [".el-message-box", ".el-dialog"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            if _is_visible(el):
                candidates.append(
                    (By.XPATH, f"//*[contains(@class,'{sel[1:]}')]//button[contains(text(),'确定')]", 0.85))
                candidates.append(
                    (By.XPATH, f"//*[contains(@class,'{sel[1:]}')]//button[contains(text(),'取消')]", 0.85))
                candidates.append(
                    (By.XPATH, f"//*[contains(@class,'{sel[1:]}')]//*[contains(@class,'btns')]/button[last()]", 0.80))
        except Exception:
            continue
    return candidates


def _is_visible(el: WebElement) -> bool:
    try:
        return el.is_displayed() and el.size.get("width", 0) > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 候选元素类型校验 — 防止"把按钮愈合到输入框"这类假成功自愈
#   原定位器/意图推断"期望类型" -> 每个候选必须先匹配类型 + 可见(按钮还需可点击)才算命中
# ---------------------------------------------------------------------------

def _classify_element(el: WebElement) -> str:
    """把真实元素归类: button / input / select / link / other / unknown"""
    try:
        tag = (el.tag_name or "").lower()
        role = (el.get_attribute("role") or "").lower()
        itype = (el.get_attribute("type") or "").lower()
        cls = (el.get_attribute("class") or "").lower()
    except Exception:
        return "unknown"
    if tag == "button" or role == "button" or itype in ("submit", "button", "reset") or "el-button" in cls:
        return "button"
    if tag in ("input", "textarea"):
        return "input"
    if tag == "select" or role == "combobox" or "el-select" in cls:
        return "select"
    if tag == "a":
        return "link"
    return "other"


def _infer_expected_kind(original_locator: str, intent: str = "") -> Optional[set]:
    """从原定位器(优先)或意图推断期望元素类型集合; None 表示不限制"""
    loc = (original_locator or "").lower()
    if "button" in loc or "[type='submit']" in loc or '[type="submit"]' in loc:
        return {"button"}
    if "input" in loc or "el-input__inner" in loc or "textarea" in loc:
        return {"input"}
    if "el-select" in loc or "select" in loc or "option" in loc:
        return {"select"}
    if "//a" in loc or re.search(r"(^|[\s,>+~])a[\.\[#]", loc):
        return {"link"}
    # 定位器看不出类型时, 退化用意图: 点击类动作至少排除纯输入框
    intent_lower = (intent or "").lower()
    if intent_lower and any(k in intent_lower for k in
                            ("点击", "查询", "提交", "确定", "取消", "关闭", "按钮",
                             "click", "submit", "confirm", "button")):
        return {"button", "link", "other"}
    return None


def _is_valid_heal(el: WebElement, expected_kind: Optional[set]) -> bool:
    """愈合候选的有效性判据: 可见 + 类型匹配; 若期望按钮还需 enabled"""
    if not _is_visible(el):
        return False
    kind = _classify_element(el)
    if expected_kind is not None and kind not in expected_kind:
        return False
    if kind == "button":
        try:
            if not el.is_enabled():
                return False
        except Exception:
            return False
    return True


# ---------------------------------------------------------------------------
# 主入口: 自愈尝试
# ---------------------------------------------------------------------------

def try_heal(driver: WebDriver,
             original_by: str,
             original_locator: str,
             intent: str = "") -> Optional[WebElement]:
    """
    尝试用 L1→L2→L3 分层策略找到原始定位器本应找到的元素.
    找到 → 返回 WebElement + 写入缓存. 全部失败 → 返回 None.
    """
    cache = _load_cache()
    cache_key = f"{original_by}::{original_locator}"
    expected_kind = _infer_expected_kind(original_locator, intent)

    # 0. 先查缓存
    if cache_key in cache:
        try:
            el = driver.find_element(cache[cache_key]["by"],
                                     cache[cache_key]["locator"])
            if _is_valid_heal(el, expected_kind):
                print(f"  [Healer] 命中缓存 → {cache[cache_key]['locator']}")
                return el
            else:
                print(f"  [Healer] 缓存命中但类型不符(期望{expected_kind}), 丢弃并重新自愈")
        except Exception:
            pass

    # 1. L1 规则映射
    if original_locator in L1_RULES:
        new_by, new_loc, reason = L1_RULES[original_locator]
        try:
            el = driver.find_element(new_by, new_loc)
            if _is_valid_heal(el, expected_kind):
                print(f"  [Healer] L1规则命中 {original_locator} → {new_loc}")
                _record_heal(cache, cache_key, new_by, new_loc, reason)
                return el
        except Exception:
            pass

    # 2. L2 多语义
    cands = _l2_candidates(original_by, original_locator, intent)
    for by, loc, conf in sorted(cands, key=lambda x: -x[2]):
        try:
            el = driver.find_element(by, loc)
            if _is_valid_heal(el, expected_kind):
                print(f"  [Healer] L2语义命中 {original_locator} → {loc} (conf={conf:.2f})")
                _record_heal(cache, cache_key, by, loc, f"L2: {loc}")
                return el
        except Exception:
            continue

    # 3. L3 相对 XPath
    for by, loc, conf in _l3_xpath_candidates(driver):
        try:
            el = driver.find_element(by, loc)
            if _is_valid_heal(el, expected_kind):
                print(f"  [Healer] L3 XPath命中 → {loc}")
                _record_heal(cache, cache_key, by, loc, f"L3: {loc}")
                return el
        except Exception:
            continue

    # 4. L4 LLM 辅助自愈 (Phase 2)
    l4_cands = _l4_llm_candidates(driver, original_locator, intent)
    for by, loc, conf in sorted(l4_cands, key=lambda x: -x[2]):
        try:
            el = driver.find_element(by, loc)
            if _is_valid_heal(el, expected_kind):
                print(f"  [Healer] L4 LLM命中 -> {loc}")
                _record_heal(cache, cache_key, by, loc, f"L4-LLM: {loc}")
                return el
        except Exception:
            continue

    print(f"  [Healer] 全部失败 (L1->L4): {original_by}::{original_locator}")
    return None


def _record_heal(cache: Dict, key: str, by: str, loc: str, reason: str) -> None:
    cache[key] = {
        "by": by, "locator": loc, "reason": reason,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_cache(cache)


# ---------------------------------------------------------------------------
# 评测专用纯函数: rank_candidates — 不改生产 try_heal, 不写缓存, 不返回元素
# 汇总 L1->L4 候选定位器, 合并去重并按置信度降序, 供评测算 top-1/top-3 + 分层归因
# ---------------------------------------------------------------------------

def rank_candidates(driver: Optional[WebDriver],
                    original_by: str,
                    original_locator: str,
                    intent: str = "",
                    use_llm: bool = False) -> List[Tuple[str, str, float, str]]:
    """
    返回有序候选列表: [(by, locator, confidence, layer), ...], layer ∈ {L1,L2,L3,L4}。
    - L1: 精确规则映射 (最高置信)
    - L2: 多语义 CSS 枚举 (含原始定位器兑底)
    - L3: 相对 XPath (需活 driver 判断容器可见)
    - L4: LLM 辅助 (仅 use_llm=True 时参与)
    不执行 find_element 命中判定, 不改任何状态, 保证评测可复现。
    """
    ranked: List[Tuple[str, str, float, str]] = []

    # L1 规则映射
    if original_locator in L1_RULES:
        new_by, new_loc, _reason = L1_RULES[original_locator]
        ranked.append((new_by, new_loc, 1.1, "L1"))

    # L2 多语义 (内部已含原始定位器 0.1 兑底)
    for by, loc, conf in _l2_candidates(original_by, original_locator, intent):
        ranked.append((by, loc, conf, "L2"))

    # L3 相对 XPath (需活 driver)
    if driver is not None:
        try:
            for by, loc, conf in _l3_xpath_candidates(driver):
                ranked.append((by, loc, conf, "L3"))
        except Exception:
            pass

    # L4 LLM 辅助 (仅 --llm)
    if use_llm and driver is not None:
        try:
            for by, loc, conf in _l4_llm_candidates(driver, original_locator, intent):
                ranked.append((by, loc, conf, "L4"))
        except Exception:
            pass

    # 去重: (by, locator) 保留最高置信 (首次 layer 标签)
    best: Dict[Tuple[str, str], Tuple[float, str]] = {}
    for by, loc, conf, layer in ranked:
        key = (by, loc)
        if key not in best or conf > best[key][0]:
            best[key] = (conf, layer)

    out = [(by, loc, info[0], info[1]) for (by, loc), info in best.items()]
    out.sort(key=lambda x: -x[2])
    return out


# ---------------------------------------------------------------------------
# 诊断辅助: 打印当前页面的所有弹窗/对话框
# ---------------------------------------------------------------------------

def dump_page_diagnostics(driver: WebDriver) -> None:
    """失败时打印页面上可见的 dialog / message-box / 错误提示 摘要"""
    print("\n  [Healer-诊断] === 当前页面元素摘要 ===")
    for sel, label in [(".el-message-box", "message-box"),
                       (".el-dialog", "dialog"),
                       (".el-message", "toast"),
                       (".el-message-box__btns button", "btn"),
                       (".el-form-item.is-error", "form-error")]:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            visible = [e for e in els if _is_visible(e)]
            print(f"    .{label}: {len(visible)} visible / {len(els)} total")
            for e in visible[:3]:
                cls = (e.get_attribute("class") or "").strip()
                txt = (e.text or "").strip()[:40]
                print(f"      → class={cls!r} text={txt!r}")
        except Exception as ex:
            print(f"    .{label}: scan failed ({ex})")



# ---------------------------------------------------------------------------
# L4 (Phase 2): LLM 辅助自愈 - 抓容器 innerHTML -> 让 LLM 给候选
# ---------------------------------------------------------------------------

_LLM_HEAL_SYSTEM = (
    "You are a Selenium UI automation element locator expert.\n"
    "Below is the HTML of a visible container in a web page (Element Plus dialog/"
    "message-box/form/table) and the intent of the element you need to find.\n\n"
    "Please output the 3 most likely CSS selectors in confidence order.\n"
    "Rules:\n"
    "- selectors must work within THIS container (no global scan)\n"
    "- prefer semantic classes (.el-button--danger, .el-dialog__close)\n"
    "- avoid nth-child/index; use text() or aria-label if possible\n"
    "- output ONLY pure JSON, NO markdown code blocks\n\n"
    "Output format:\n"
    '{"candidates": [{"selector": ".xxx", "confidence": 0.9, "reason": ""}, ...]}'
)


def _l4_llm_candidates(driver: WebDriver,
                       original_locator: str,
                       intent: str = "") -> List[Tuple[str, str, float]]:
    """Phase 2 L4: capture visible container HTML, ask LLM for selectors."""
    try:
        from ai_kit.client import chat_json, llm_available
    except Exception:
        try:
            from ai_kit.client import chat_json, llm_available
        except Exception:
            return []

    if not llm_available():
        return []

    # 抓可见容器 innerHTML
    containers_html = []
    for sel in [".el-message-box", ".el-dialog", ".el-message-box__wrapper",
                ".filter-card", "table.el-table__body"]:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els[:2]:
                if _is_visible(el):
                    html = el.get_attribute("outerHTML") or ""
                    if len(html) > 4000:
                        html = html[:4000] + "...[TRUNCATED]"
                    containers_html.append(f"<!-- {sel} -->\n{html}")
        except Exception:
            continue
        if len(containers_html) >= 3:
            break

    if not containers_html:
        return []

    html_text = "\n\n".join(containers_html)
    user_msg = (
        f"Failed locator: {original_locator}\n"
        f"Intent: {intent or 'unknown'}\n\n"
        f"=== Container HTML ===\n{html_text}"
    )
    print("  [Healer] L4 asking LLM for candidate selectors...")
    result = chat_json(_LLM_HEAL_SYSTEM, user_msg, max_retries=1)
    if not result:
        print("  [Healer] L4 LLM returned None")
        return []

    candidates = result.get("candidates", [])
    out: List[Tuple[str, str, float]] = []
    for cand in candidates[:3]:
        sel = cand.get("selector", "").strip()
        conf = float(cand.get("confidence", 0.5))
        if sel:
            out.append((By.CSS_SELECTOR, sel, max(0.5, min(1.0, conf))))
    print(f"  [Healer] L4 LLM gave {len(out)} candidates")
    return out
