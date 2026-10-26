# -*- coding: utf-8 -*-
"""
AI 自愈定位器 — Locator Healer (Phase 1 本地版)
分层自愈策略: L1 规则映射 → L2 多语义 CSS → L3 相对 XPath
不依赖 LLM, 所有规则本地可解释可调试

规则全部外置在 ai_kit/profiles/builtin/*.json (generic 打底 + element_plus 覆盖),
站点差异用 profile 覆盖。本模块只负责"怎么用规则", 不再持有"有哪些规则"。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from functools import lru_cache, partial
from typing import List, Optional, Tuple, Dict, Any

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from ai_kit.profiles import default_rules, effective_rules


# ---------------------------------------------------------------------------
# 规则读法 — 数据在 ai_kit/profiles/builtin/*.json, 这里只管怎么解释它
#   规则包写错一律当没有 (缺键/类型不对/单条畸形), 绝不让自愈链路上抛
# ---------------------------------------------------------------------------

_DEFAULT_RULES: Dict[str, Any] = default_rules()


def _normalize_l1_rule(rule: Any) -> Optional[Tuple[str, str, str]]:
    """把 {by, locator, reason} / [by, locator, reason] 归一成三元组; 畸形返回 None"""
    if isinstance(rule, dict):
        new_by = str(rule.get("by") or By.CSS_SELECTOR)
        new_loc = rule.get("locator") or rule.get("selector")
        reason = str(rule.get("reason") or "内置规则")
    elif isinstance(rule, (list, tuple)) and len(rule) >= 2:
        new_by = str(rule[0])
        new_loc = rule[1]
        reason = str(rule[2]) if len(rule) > 2 else "内置规则"
    else:
        return None
    if not isinstance(new_loc, str) or not new_loc.strip():
        return None
    return (new_by, new_loc.strip(), reason)


def _l1_rule(rules: Dict[str, Any], broken_locator: str) -> Optional[Tuple[str, str, str]]:
    """规则包里该坏定位器的 (by, locator, reason); 没收录或写畸形返回 None"""
    table = rules.get("l1_rules")
    if not isinstance(table, dict) or broken_locator not in table:
        return None
    return _normalize_l1_rule(table[broken_locator])


def _l1_tuple_view(rules: Dict[str, Any]) -> Dict[str, Tuple[str, str, str]]:
    """{坏定位器: (by, locator, reason)} 只读快照 (历史上 L1_RULES 的样子)"""
    table = rules.get("l1_rules")
    if not isinstance(table, dict):
        return {}
    view: Dict[str, Tuple[str, str, str]] = {}
    for broken, rule in table.items():
        norm = _normalize_l1_rule(rule)
        if norm is not None:
            view[broken] = norm
    return view


# 向后兼容视图: 老代码可能 `from ai_kit.healer import L1_RULES`。
# 数据源已经是默认规则包, 这里只是历史三元组格式的只读快照, 不要改它。
L1_RULES: Dict[str, Tuple[str, str, str]] = _l1_tuple_view(_DEFAULT_RULES)


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
# L2: 多语义 CSS — 语义提示 / 语义候选 / 意图词表全部来自规则包
# ---------------------------------------------------------------------------

def _hint_pairs(rules: Dict[str, Any]) -> Tuple[Tuple[str, str], ...]:
    """规则包里的 (正则源码, 语义) 对, 已滤掉畸形条目"""
    hints = rules.get("semantic_hints")
    if not isinstance(hints, (list, tuple)):
        return ()
    pairs: List[Tuple[str, str]] = []
    for item in hints:
        if (isinstance(item, (list, tuple)) and len(item) >= 2
                and isinstance(item[0], str) and isinstance(item[1], str)
                and item[0].strip() and item[1].strip()):
            pairs.append((item[0], item[1]))
    return tuple(pairs)


@lru_cache(maxsize=16)
def _compile_hints(pairs: Tuple[Tuple[str, str], ...]) -> Tuple[Tuple[re.Pattern, str], ...]:
    """正则按 pair 元组缓存只编译一次; 写错的正则跳过, 不影响其他提示"""
    compiled: List[Tuple[re.Pattern, str]] = []
    for pattern, semantic in pairs:
        try:
            compiled.append((re.compile(pattern, re.I), semantic))
        except re.error:
            continue
    return tuple(compiled)


def _guess_semantic(locator: str,
                    rules: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """按规则包的语义提示判断定位器属于哪个语义; 认不出来返回 None"""
    active = rules if isinstance(rules, dict) else _DEFAULT_RULES
    for pat, hint in _compile_hints(_hint_pairs(active)):
        if pat.search(locator):
            return hint
    return None


# 向后兼容视图: 老代码可能直接读这两个常量。数据源已是默认规则包。
SEMANTIC_HINTS: List[Tuple[re.Pattern, str]] = list(_compile_hints(_hint_pairs(_DEFAULT_RULES)))
SEMANTIC_CANDIDATES: Dict[str, List[str]] = _DEFAULT_RULES["semantic_candidates"]


def _l2_candidates(original_by: str, original_loc: str, intent: str = "",
                   rules: Optional[Dict[str, Any]] = None
                   ) -> List[Tuple[str, str, float]]:
    """返回 [(by, locator, confidence)] 候选列表

    候选顺序: 站点规则 (合并时已排在前面) → 框架规则 → 原始定位器 0.1 兜底;
    语义候选置信从 0.9 起按位置递减, 保证单调且不与意图命中 (1.0) 抢位置。
    """
    active = rules if isinstance(rules, dict) else _DEFAULT_RULES
    results: List[Tuple[str, str, float]] = [(original_by, original_loc, 0.1)]

    sem = _guess_semantic(original_loc, active)
    table = active.get("semantic_candidates")
    items = table.get(sem) if isinstance(table, dict) and sem else None
    if isinstance(items, (list, tuple)):
        for i, cand in enumerate(items):
            if isinstance(cand, str) and cand.strip():
                results.append((By.CSS_SELECTOR, cand.strip(), 0.9 - i * 0.05))

    # 意图辅助: 关键词命中就把该组的选择器顶到最前 (1.0)
    intent_lower = str(intent or "").lower()
    keywords = active.get("intent_keywords")
    selectors = active.get("intent_selectors")
    if intent_lower and isinstance(keywords, dict) and isinstance(selectors, dict):
        for bucket, words in keywords.items():
            if not isinstance(words, (list, tuple)):
                continue
            selector = selectors.get(bucket)
            if not isinstance(selector, str) or not selector.strip():
                continue
            if any(str(w).lower() in intent_lower for w in words if str(w).strip()):
                results.append((By.CSS_SELECTOR, selector.strip(), 1.0))
    return results


def _l3_xpath_candidates(driver: WebDriver,
                         containers: Optional[List[str]] = None
                         ) -> List[Tuple[str, str, float]]:
    """基于当前可见容器 (规则包 l3_containers) 生成相对 XPath"""
    candidates: List[Tuple[str, str, float]] = []
    if containers is None:
        containers = _DEFAULT_RULES.get("l3_containers") or []
    for sel in containers:
        # 只吃 .class 形式: 下面的 XPath 靠 @class 包含匹配, [role=dialog] 之类会生成垃圾
        if not isinstance(sel, str) or not sel.startswith(".") or len(sel) < 2:
            continue
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
             intent: str = "",
             profile=None) -> Optional[WebElement]:
    """
    尝试用 L1→L2→L3(→L4) 分层策略找到原始定位器本应找到的元素.
    找到 → 返回 WebElement + 写入缓存. 全部失败 → 返回 None.

    profile: 可选站点规则包 (dict / 路径 / 包名), 覆盖内置规则; 不传则只用
             内置包 (generic + element_plus), 与历史上硬编码规则的行为一致。
    """
    rules = effective_rules(profile)
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
    l1_rule = _l1_rule(rules, original_locator)
    if l1_rule is not None:
        new_by, new_loc, reason = l1_rule
        try:
            el = driver.find_element(new_by, new_loc)
            if _is_valid_heal(el, expected_kind):
                print(f"  [Healer] L1规则命中 {original_locator} → {new_loc}")
                _record_heal(cache, cache_key, new_by, new_loc, reason)
                return el
        except Exception:
            pass

    # 2. L2 多语义
    cands = _l2_candidates(original_by, original_locator, intent, rules)
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
    for by, loc, conf in _l3_xpath_candidates(driver, rules.get("l3_containers")):
        try:
            el = driver.find_element(by, loc)
            if _is_valid_heal(el, expected_kind):
                print(f"  [Healer] L3 XPath命中 → {loc}")
                _record_heal(cache, cache_key, by, loc, f"L3: {loc}")
                return el
        except Exception:
            continue

    # 4. L4 LLM 辅助自愈 (Phase 2)
    l4_cands = _l4_llm_candidates(driver, original_locator, intent,
                                  containers=rules.get("l4_containers"))
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
        profile    : 可选规则包, 覆盖内置包 (generic + element_plus)。
                     可以是 dict / JSON 路径 / 包名 ("antd", "example_shop");
                     支持键 (详见 ai_kit.profiles.loader):
                       l1_rules            {坏定位器: {by, locator, reason}}
                       semantic_hints      [[正则, 语义]]
                       semantic_candidates {语义: [css, ...]}
                       intent_keywords     {confirm|cancel|close: [同义词]}
                       intent_selectors    {confirm|cancel|close: css}
                       l3_containers / l4_containers  [css, ...]
                     站点规则优先于框架内置; 缺键/畸形一律忽略。
        llm_client : 可选 LLM 客户端, 形如 (system, user) -> dict|None;
                     不给则不产 L4 候选 (评测默认离线可复现)。
    """
    orig_loc = str(locator or "").strip()
    if not orig_loc:
        return []
    orig_by = str(by or "").strip() or By.CSS_SELECTOR
    intent_text = str(intent or "")
    rules = effective_rules(profile)

    ranked: List[Candidate] = []

    # L1: 规则包坑表 (站点覆盖已由 effective_rules 合并)
    l1_rule = _l1_rule(rules, orig_loc)
    if l1_rule is not None:
        ranked.append(Candidate(l1_rule[0], l1_rule[1], 1.1, "L1", l1_rule[2]))

    # L2: 语义候选 + 意图候选 (内部已含原始定位器 0.1 兜底)
    for cand_by, cand_loc, conf in _l2_candidates(orig_by, orig_loc, intent_text, rules):
        ranked.append(Candidate(cand_by, cand_loc, conf, "L2"))

    # L3: 相对 XPath, 需要活 driver
    if driver is not None:
        try:
            l3 = _l3_xpath_candidates(driver, rules.get("l3_containers"))
        except Exception:  # noqa: BLE001 — driver 是外部对象
            l3 = []
        ranked.extend(Candidate(b, loc, conf, "L3") for b, loc, conf in l3)

    # L4: 只有显式注入 llm_client 才参与 (离线评测永远不跑)
    if driver is not None and llm_client is not None:
        try:
            l4 = _l4_llm_candidates(driver, orig_loc, intent_text,
                                    llm_client=llm_client,
                                    containers=rules.get("l4_containers"))
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
                       llm_client=None,
                       containers: Optional[List[str]] = None
                       ) -> List[Tuple[str, str, float]]:
    """Phase 2 L4: capture visible container HTML, ask LLM for selectors.

    containers: 规则包 l4_containers (默认内置包)。通用模式 ([role=dialog] /
    .modal / [class*=dialog]) 排在框架容器之后, 只在前面的都没抓到时才兜底,
    这样 Element Plus 页面的抓取顺序与历史上硬编码的容器列表一致。
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

    if containers is None:
        containers = _DEFAULT_RULES.get("l4_containers") or []

    # 抓可见容器 innerHTML
    containers_html = []
    for sel in containers:
        if not isinstance(sel, str) or not sel.strip():
            continue
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
