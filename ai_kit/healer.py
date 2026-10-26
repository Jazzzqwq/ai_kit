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
from dataclasses import dataclass
from functools import partial
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
# profile 规则包 (可选) — 站点知识覆盖框架内置规则
#
# 约定是"宽松 dict": 缺键、类型不对、整个 profile 不是 dict 一律当没有,
# 绝不因为规则包写错而让定位/评测链路上抛。支持三个键:
#     l1_rules            {坏定位器: {by, locator, reason} 或 [by, locator, reason]}
#     semantic_candidates {语义: [css, ...]}
#     intent_keywords     {confirm|cancel|close: [同义词, ...]}
# ---------------------------------------------------------------------------

def _profile_get(profile: Any, key: str) -> Any:
    """从 profile 取一个键 (dict 或带 .get 的对象), 任何异常/缺失都返回 None"""
    if profile is None:
        return None
    try:
        if hasattr(profile, "get"):
            return profile.get(key)
        return getattr(profile, key, None)
    except Exception:  # noqa: BLE001 — profile 是外部数据
        return None


def _profile_l1_rule(profile: Any, broken_locator: str) -> Optional[Tuple[str, str, str]]:
    """站点坑表里该坏定位器的 (by, locator, reason); 没有/畸形返回 None"""
    rules = _profile_get(profile, "l1_rules")
    if not isinstance(rules, dict) or broken_locator not in rules:
        return None
    rule = rules[broken_locator]
    if isinstance(rule, dict):
        new_by = str(rule.get("by") or By.CSS_SELECTOR)
        new_loc = rule.get("locator") or rule.get("selector")
        reason = str(rule.get("reason") or "profile 规则")
    elif isinstance(rule, (list, tuple)) and len(rule) >= 2:
        new_by = str(rule[0])
        new_loc = rule[1]
        reason = str(rule[2]) if len(rule) > 2 else "profile 规则"
    else:
        return None
    if not isinstance(new_loc, str) or not new_loc.strip():
        return None
    return (new_by, new_loc.strip(), reason)


def _profile_semantic_candidates(profile: Any, semantic: str) -> List[str]:
    """站点 profile 给该语义的 CSS 候选 (已去空/非字符串)"""
    table = _profile_get(profile, "semantic_candidates")
    if not isinstance(table, dict):
        return []
    items = table.get(semantic)
    if not isinstance(items, (list, tuple)):
        return []
    return [s.strip() for s in items if isinstance(s, str) and s.strip()]


def _profile_intent_keywords(profile: Any) -> Dict[str, List[str]]:
    """站点 profile 的意图同义词表 (键/值都已 lower 化)"""
    table = _profile_get(profile, "intent_keywords")
    if not isinstance(table, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for bucket, words in table.items():
        if not isinstance(words, (list, tuple)):
            continue
        cleaned = [w.strip().lower() for w in words
                   if isinstance(w, str) and w.strip()]
        if cleaned:
            out[str(bucket).strip().lower()] = cleaned
    return out


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


# 意图分组 -> 内置关键词; profile.intent_keywords 可给同一组追加站点同义词
_INTENT_BUCKETS: Dict[str, Tuple[str, ...]] = {
    "confirm": ("确定", "confirm", "delete"),
    "cancel": ("取消", "cancel"),
    "close": ("关闭", "close"),
}

_INTENT_SELECTORS: Dict[str, str] = {
    "confirm": (".el-message-box__btns button.el-button--danger, "
                ".el-message-box__btns button.el-button--primary, "
                ".el-message-box__btns button:last-child"),
    "cancel": ".el-message-box__btns button.el-button--default",
    "close": ".el-message-box__headerbtn, .el-dialog__close",
}


def _l2_candidates(original_by: str, original_loc: str,
                   intent: str = "", profile=None) -> List[Tuple[str, str, float]]:
    """返回 [(by, locator, confidence)] 候选列表; profile 为 None 时与旧版逐字一致"""
    results: List[Tuple[str, str, float]] = []
    sem = _guess_semantic(original_loc)
    results.append((original_by, original_loc, 0.1))

    if sem and sem in SEMANTIC_CANDIDATES:
        # 站点 profile 的候选更贴近真实 DOM, 排在框架内置候选之前
        for i, cand in enumerate(_profile_semantic_candidates(profile, sem)):
            results.append((By.CSS_SELECTOR, cand, 0.95 - i * 0.05))
        for i, cand in enumerate(SEMANTIC_CANDIDATES[sem]):
            conf = 0.9 - i * 0.05
            results.append((By.CSS_SELECTOR, cand, conf))

    # intent 辅助 (内置关键词 + profile 同义词)
    intent_lower = intent.lower()
    keywords = _profile_intent_keywords(profile)
    for bucket in ("confirm", "cancel", "close"):
        words = _INTENT_BUCKETS[bucket] + tuple(keywords.get(bucket, ()))
        if any(word in intent_lower for word in words):
            results.append((By.CSS_SELECTOR, _INTENT_SELECTORS[bucket], 1.0))

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
# 纯函数: rank_candidates — 不改生产 try_heal, 不写缓存, 不返回元素
# 汇总 L1→L2→L3(→L4) 候选定位器, 合并去重并按置信度降序, 供评测算 top-1/top-3
# 与方案层共用; 同一个函数不产生任何副作用, 同样输入连调多次结果完全一致
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """一个候选定位器 (纯数据, 永不包含 WebElement)

    confidence 是相对排序权重 (可 >1 表示规则硬命中), 只在同一次 rank_candidates
    的结果内部可比; 跨 case 不可比。frozen 是为了让调用方无法篡改共享结果。
    """
    by: str
    locator: str
    confidence: float
    layer: str                  # L1 / L2 / L3 / L4
    reason: str = ""            # 可选: 为什么给这个候选 (L1 规则理由等)

    def as_tuple(self) -> Tuple[str, str, float, str]:
        """(by, locator, confidence, layer) — 报告/日志用的紧凑形式"""
        return (self.by, self.locator, self.confidence, self.layer)


_LAYER_ORDER = {"L1": 0, "L2": 1, "L3": 2, "L4": 3}


def rank_candidates(driver: Optional[WebDriver] = None,
                    by: Optional[str] = None,
                    locator: Optional[str] = None,
                    intent: str = "",
                    profile=None,
                    llm_client=None) -> List[Candidate]:
    """
    把一个"坏定位器"展开成有序候选, 纯函数, 评测与方案层共用。

    返回 List[Candidate], 按置信降序 (同置信按层序 L1<L2<L3<L4); 同一
    (by, locator) 只保留置信最高的那条, 并标出它来自哪一层。

    契约 (单测锁死):
        - 不写缓存: 不碰 _load_cache / _save_cache
        - 不返回 WebElement: 结果只有 (by, locator, confidence, layer, reason)
        - 不抛异常: driver / profile / llm_client 畸形或抛错都只降级, 不中断
        - 可复现: 同样输入连调多次结果完全一致

    参数:
        driver     : 可选; 只为 L3 (相对 XPath) 与 L4 (容器 HTML) 提供 DOM,
                     None 时这两层不参与。
        by, locator: 坏定位器的定位方式与表达式; locator 为空 → 返回 []。
        intent     : 元素用途/动作描述 (如 "确定删除"), 用于 L2 意图候选。
        profile    : 可选规则包 (见 ai_kit.profiles), 支持键:
                       l1_rules            {坏定位器: {by, locator, reason}}
                       semantic_candidates {语义: [css, ...]}
                       intent_keywords     {confirm|cancel|close: [同义词]}
                     站点规则优先于框架内置; 缺键/畸形一律忽略。
        llm_client : 可选 LLM 客户端, 形如 (system, user) -> dict|None;
                     不给则不产 L4 候选 (评测默认离线可复现)。
    """
    orig_loc = str(locator or "").strip()
    if not orig_loc:
        return []
    orig_by = str(by or "").strip() or By.CSS_SELECTOR
    intent_text = str(intent or "")

    ranked: List[Candidate] = []

    # L1: profile 站点坑表优先, 否则内置规则
    l1_rule = _profile_l1_rule(profile, orig_loc)
    if l1_rule is None and orig_loc in L1_RULES:
        new_by, new_loc, reason = L1_RULES[orig_loc]
        l1_rule = (new_by, new_loc, reason)
    if l1_rule is not None:
        ranked.append(Candidate(l1_rule[0], l1_rule[1], 1.1, "L1", l1_rule[2]))

    # L2: 语义候选 + 意图候选 (内部已含原始定位器 0.1 兑底)
    for cand_by, cand_loc, conf in _l2_candidates(orig_by, orig_loc, intent_text, profile):
        ranked.append(Candidate(cand_by, cand_loc, conf, "L2"))

    # L3: 相对 XPath, 需要活 driver
    if driver is not None:
        try:
            l3 = _l3_xpath_candidates(driver)
        except Exception:  # noqa: BLE001 — driver 是外部对象
            l3 = []
        ranked.extend(Candidate(b, loc, conf, "L3") for b, loc, conf in l3)

    # L4: 只有显式注入 llm_client 才参与 (离线评测永远不跑)
    if driver is not None and llm_client is not None:
        try:
            l4 = _l4_llm_candidates(driver, orig_loc, intent_text, llm_client=llm_client)
        except Exception:  # noqa: BLE001
            l4 = []
        ranked.extend(Candidate(b, loc, conf, "L4") for b, loc, conf in l4)

    return _dedupe_sorted(ranked)


def _dedupe_sorted(candidates: List[Candidate]) -> List[Candidate]:
    """按 (by, locator) 去重 (留置信最高; 平手留层序靠前的), 再按置信降序"""
    best: Dict[Tuple[str, str], Candidate] = {}
    for cand in candidates:
        key = (cand.by, cand.locator)
        current = best.get(key)
        if current is None or _outranks(cand, current):
            best[key] = cand
    return sorted(best.values(),
                  key=lambda c: (-c.confidence, _LAYER_ORDER.get(c.layer, 9)))


def _outranks(cand: Candidate, other: Candidate) -> bool:
    """cand 是否应取代 other: 置信更高, 或置信平手但层序更靠前"""
    if cand.confidence != other.confidence:
        return cand.confidence > other.confidence
    return _LAYER_ORDER.get(cand.layer, 9) < _LAYER_ORDER.get(other.layer, 9)


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
                       intent: str = "",
                       llm_client=None) -> List[Tuple[str, str, float]]:
    """Phase 2 L4: capture visible container HTML, ask LLM for selectors.

    llm_client: (system_prompt, user_prompt) -> dict|None。为 None 时用内置
    ai_kit.client.chat_json (先判 llm_available()); 注入时由调用方负责可用性,
    客户端抛错/返回怪数据都只降级成 [] 候选。
    """
    if llm_client is None:
        try:
            from ai_kit.client import chat_json, llm_available
        except Exception:  # noqa: BLE001 — 没装 requests 等
            return []
        if not llm_available():
            return []
        llm_client = partial(chat_json, max_retries=1)

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
    try:
        result = llm_client(_LLM_HEAL_SYSTEM, user_msg)
    except Exception as ex:  # noqa: BLE001 — 注入的客户端可能任意抛
        print(f"  [Healer] L4 LLM 调用失败, 跳过 ({ex})")
        return []
    if not result:
        print("  [Healer] L4 LLM returned None")
        return []

    candidates = result.get("candidates", []) if isinstance(result, dict) else []
    if not isinstance(candidates, list):
        return []
    out: List[Tuple[str, str, float]] = []
    for cand in candidates[:3]:
        if not isinstance(cand, dict):
            continue
        sel = cand.get("selector")
        if not isinstance(sel, str) or not sel.strip():
            continue
        sel = sel.strip()
        try:
            conf = float(cand.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        out.append((By.CSS_SELECTOR, sel, max(0.5, min(1.0, conf))))
    print(f"  [Healer] L4 LLM gave {len(out)} candidates")
    return out
