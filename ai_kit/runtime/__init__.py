# -*- coding: utf-8 -*-
"""
运行层 — standalone 模式用例的运行时原语

定位: 计划里只有 action / eid / value, 没有定位串。standalone 模式靠两样东西把它
跑起来 ——

    loc("e12")   从随计划生成的 locators.json 查定位串 (eid -> 定位策略)
    page.<原语>  统一显式等待 + 失败自动截图 + 调 proposal.diagnoser

换一个网站只需要换 profile 和计划: 运行时代码 (.py 产物) 里不出现任何站点的
选择器, 所以「任意网页可用」靠的是这一层, 不是模板。

设计约束 (与 5.7 的 ③ AST 白名单互相咬合):
- 产物只准写 `page.fill(loc("e12"), "x")` 这种形状; 白名单认得 page / loc /
  use_locators, 裸 selenium 调用会被拦下。
- 每个公开原语都带失败上下文 (动作 / eid / 截图 / 归因)。失败抛 ActionFailure
  (它是 AssertionError 的子类), pytest 里就是一条干净的失败, 不是异常堆栈。
- 找不到元素时按 alternatives 依次试 (采集器给了多策略定位, 这里要用上)。

环境变量:
    AI_KIT_WAIT       显式等待秒数 (默认 10)
    AI_KIT_ARTIFACTS  失败截图目录 (默认 ./artifacts)

proposal.diagnoser 的契约: `diagnose(context: dict) -> str`。模块还没实现时这一
步静默跳过 —— 归因是加分项, 不能让原语本身挂掉。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

RUNTIME_VERSION = 1
LOCATORS_VERSION = 1
DEFAULT_TIMEOUT = 10.0
POLL_FREQUENCY = 0.2
# 页面上没有可观察信号时 (翻页后表格重绘) 给的一小段自更新时间 —— 全模块唯一的 sleep
SETTLE_SECONDS = 0.15

LOCATORS_ENV = "AI_KIT_LOCATORS"
ARTIFACTS_ENV = "AI_KIT_ARTIFACTS"
WAIT_ENV = "AI_KIT_WAIT"


# ---------------------------------------------------------------------------
# 异常 / 定位串
# ---------------------------------------------------------------------------

class RuntimeNotBound(RuntimeError):
    """page 还没绑定 driver: 告诉人怎么绑, 而不是抛 AttributeError"""


class LocatorNotKnown(LookupError):
    """eid 不在当前 locators 表里: 计划与 locators.json 不配套, 或忘了 use_locators"""


class ActionFailure(AssertionError):
    """一个原语失败 (带动作 / eid / 截图 / 归因), pytest 看作断言失败"""

    def __init__(self, message: str, *, action: str = "", eid: str = "",
                 screenshot: str = "", diagnosis: str = "") -> None:
        super().__init__(message)
        self.action = action
        self.eid = eid
        self.screenshot = screenshot
        self.diagnosis = diagnosis


class _Missing(Exception):
    """内部「找不到」信号: 由 _guarded 转成带上下文的 ActionFailure"""


@dataclass(frozen=True)
class Locator:
    """一个 eid 展开出来的定位方式 (主策略 + 备选 + 所属表格)

    table 只对 c1.column:名称 / c1.row:删除 这类引用有意义: 断言排序列时要先找
    到表格根, 从 eid 前缀再查一次 loc() 比在 Locator 里存 WebElement 靠谱。
    """

    eid: str
    by: str = "css selector"
    value: str = ""
    alternatives: Tuple[Tuple[str, str], ...] = ()
    label: str = ""
    kind: str = "element"
    table: str = ""

    def pairs(self) -> Tuple[Tuple[str, str], ...]:
        primary = ((self.by, self.value),) if self.value else ()
        return primary + tuple(self.alternatives)

    def __str__(self) -> str:
        return f"{self.eid} ({self.by}={self.value})"


# ---------------------------------------------------------------------------
# locators.json: 计划与页面之间的那一份「地址簿」
# ---------------------------------------------------------------------------

_LOCATORS: Dict[str, Locator] = {}
_SOURCE = ""

PAGE_REF = "page"
_PAGE_LOCATOR = Locator(PAGE_REF, "tag name", "html", kind="page")


def _entry_locator(eid: str, raw: Any) -> Optional[Locator]:
    """单条 JSON 记录 -> Locator; 脏记录返回 None (不让一条坏数据毁整张表)"""
    key = str(eid or "").strip()
    if not key:
        return None
    if isinstance(raw, str):
        return Locator(key, "css selector", raw)
    if not isinstance(raw, Mapping):
        return None
    by = str(raw.get("by") or "css selector")
    value = str(raw.get("value") or "")
    if not value:
        return None
    alternatives: List[Tuple[str, str]] = []
    for item in raw.get("alternatives") or ():
        if isinstance(item, (list, tuple)) and len(item) == 2:
            alternatives.append((str(item[0]), str(item[1])))
        elif isinstance(item, Mapping) and item.get("value"):
            alternatives.append((str(item.get("by") or "css selector"), str(item["value"])))
    return Locator(key, by, value, tuple(alternatives),
                   label=str(raw.get("label") or ""),
                   kind=str(raw.get("kind") or "element"),
                   table=str(raw.get("table") or ""))


def _entries_of(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping) and isinstance(payload.get("locators"), Mapping):
        return payload["locators"]
    return payload if isinstance(payload, Mapping) else {}


def use_locators(source: Any) -> Dict[str, Locator]:
    """装载定位表: 可以给 locators.json 的路径, 也可以直接给 dict

    dict 的两种形状都收: renderer.build_locators 的整份产物 (带 "locators" 键),
    或 eid -> 记录 的平表 (测试里最顺手)。
    """
    global _SOURCE
    table: Dict[str, Locator] = {}
    if source is None or source == "":
        _LOCATORS.clear()
        _SOURCE = ""
        return dict(_LOCATORS)
    payload = source
    if isinstance(source, (str, Path)):
        path = Path(str(source))
        if not path.is_file():
            raise LocatorNotKnown(
                f"locators 文件不存在: {path} —— 它是随计划一起生成的, "
                f"确认渲染时写了 {path.name}, 或设置环境变量 {LOCATORS_ENV}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        _SOURCE = str(path)
    for eid, raw in _entries_of(payload).items():
        item = _entry_locator(str(eid), raw)
        if item is not None:
            table[item.eid] = item
    _LOCATORS.clear()
    _LOCATORS.update(table)
    return dict(_LOCATORS)


def locators() -> Mapping[str, Locator]:
    """当前定位表 (只读快照)"""
    return dict(_LOCATORS)


def loc(eid: Any) -> Locator:
    """eid -> Locator。查不到时把「有几条、从哪来的」一起说清楚"""
    key = str(eid or "").strip()
    if not key:
        raise LocatorNotKnown("loc() 收到空 eid")
    if key == PAGE_REF:
        return _PAGE_LOCATOR
    found = _LOCATORS.get(key)
    if found is None:
        hint = f"当前表来自 {_SOURCE}" if _SOURCE else \
            f"还没装载定位表: 先 use_locators(locators.json) 或设置 {LOCATORS_ENV}"
        raise LocatorNotKnown(
            f"loc({key!r}) 查不到: 表里有 {len(_LOCATORS)} 条; {hint}. "
            f"计划与 locators.json 必须出自同一份元素清单")
    return found


def _as_locator(target: Any) -> Locator:
    if isinstance(target, Locator):
        return target
    if isinstance(target, str):
        return Locator(target, "css selector", target)
    if isinstance(target, (list, tuple)) and len(target) == 2:
        return Locator(str(target[1]), str(target[0]), str(target[1]))
    raise TypeError(f"需要 loc() 的结果或定位串, 拿到 {type(target).__name__}")


# ---------------------------------------------------------------------------
# 失败上下文: 截图 + 归因
# ---------------------------------------------------------------------------

def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")


def _slug(text: str, limit: int = 40) -> str:
    value = re.sub(r"[^0-9A-Za-z_\-]+", "_", str(text or "")).strip("_")
    return (value or "shot")[:limit]


def screenshot(driver: Any, name: str = "failure") -> str:
    """失败截图; 任何一步失败都返回空串 —— 截图是附加证据, 不是主流程"""
    if driver is None:
        return ""
    folder = Path(os.environ.get(ARTIFACTS_ENV) or "artifacts")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_stamp()}_{_slug(name)}.png"
        if driver.save_screenshot(str(path)):
            return str(path)
    except (OSError, WebDriverException):
        return ""
    return ""


def _diagnose(context: Mapping[str, Any]) -> str:
    """调 proposal.diagnoser; 模块没落地 / 抛异常都只降级成空串"""
    try:
        from ai_kit.proposal import diagnoser      # 惰性: 归因不该是硬依赖
    except Exception:                              # noqa: BLE001
        return ""
    diagnose = getattr(diagnoser, "diagnose", None)
    if not callable(diagnose):
        return ""
    try:
        return str(diagnose(dict(context)) or "")
    except Exception as exc:                       # noqa: BLE001
        return f"(diagnoser 调用失败: {exc})"


def _guarded(action: str):
    """公开原语的统一保护: 失败 -> 截图 -> 归因 -> ActionFailure"""

    def decorate(fn):
        @wraps(fn)
        def inner(self: "Page", *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except (ActionFailure, RuntimeNotBound):
                raise
            except Exception as exc:               # noqa: BLE001 — 统一转成带上下文
                target = next((item for item in args if isinstance(item, Locator)), None)
                raise self._failure(action, target, exc) from exc

        return inner

    return decorate


# ---------------------------------------------------------------------------
# 页面门面
# ---------------------------------------------------------------------------

_CONDITIONS = {
    "visible": EC.visibility_of_element_located,
    "present": EC.presence_of_element_located,
    "clickable": EC.element_to_be_clickable,
}

_TABLE_SELECTORS = ("table", ".el-table", ".ant-table", "[role=table]", "[role=grid]")
_ROW_SELECTORS = ("tbody tr", "[role=row]", ".el-table__row")
_HEADER_SELECTORS = ("thead th", "[role=columnheader]", "th")
_CELL_SELECTORS = "td, [role=cell]"
_DIALOG_SELECTORS = (".el-dialog", ".ant-modal", "[role=dialog]", ".modal", "[class*=dialog]")
_LOADING_SELECTORS = (".el-loading-mask", ".ant-spin-spinning", "[role=progressbar]",
                      "[class*=loading-overlay]", "[class*=loading-mask]")
_ERROR_SELECTORS = (".el-message--error", ".el-notification--error", ".ant-message-error",
                    ".ant-notification-notice-error", ".ant-form-item-explain-error",
                    ".el-form-item__error", "[role=alert][class*=error]",
                    "[class*=toast][class*=error]", "[class*=message][class*=error]")
_FIELD_ERROR_SELECTORS = (".el-form-item__error", ".ant-form-item-explain-error",
                          "[class*=error]", "[class*=invalid]", "[role=alert]")
_EMPTY_WORDS = ("暂无", "无数据", "没有数据", "空列表", "nothing here", "no data", "empty")
_CLICK_KEYS = {
    "enter": Keys.ENTER, "esc": Keys.ESCAPE, "escape": Keys.ESCAPE, "tab": Keys.TAB,
    "backspace": Keys.BACK_SPACE, "space": Keys.SPACE, "delete": Keys.DELETE,
    "up": Keys.ARROW_UP, "down": Keys.ARROW_DOWN, "left": Keys.ARROW_LEFT,
    "right": Keys.ARROW_RIGHT, "home": Keys.HOME, "end": Keys.END,
}


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _norm_lower(text: Any) -> str:
    return _norm(text).lower()


def _visible(element: Any) -> bool:
    try:
        return bool(element.is_displayed())
    except (StaleElementReferenceException, WebDriverException):
        return False


def _try_text(element: Any) -> str:
    try:
        return _norm(element.text)
    except (StaleElementReferenceException, WebDriverException):
        return ""


class Page:
    """standalone 模式的唯一门面: 一次 bind, 用例里只剩下 page.<原语>(loc(eid), ...)"""

    def __init__(self) -> None:
        self._driver: Any = None
        self._timeout: float = DEFAULT_TIMEOUT
        self._last_click: Optional[Tuple[str, int]] = None
        self._nav_at: float = 0.0

    # --- 绑定 ---

    def bind(self, driver: Any, *, timeout: Optional[float] = None) -> "Page":
        self._driver = driver
        if timeout is not None:
            self._timeout = max(0.5, float(timeout))
        self._last_click = None
        self._nav_at = time.monotonic()
        return self

    def unbind(self) -> None:
        self._driver = None
        self._last_click = None

    @property
    def bound(self) -> bool:
        return self._driver is not None

    @property
    def driver(self) -> Any:
        if self._driver is None:
            raise RuntimeNotBound(
                "page 还没绑定 driver: 在 fixture 里 page.bind(driver), "
                "或在测试里先 page.bind(driver) 再调原语")
        return self._driver

    @property
    def timeout(self) -> float:
        raw = os.environ.get(WAIT_ENV)
        if raw:
            try:
                return max(0.5, float(raw))
            except ValueError:
                pass
        return self._timeout

    # --- 查找 / 等待 ---

    def _wait(self, by: str, value: str, state: str, timeout: float) -> Any:
        condition = _CONDITIONS[state]
        return WebDriverWait(self.driver, timeout, poll_frequency=POLL_FREQUENCY).until(
            condition((by, value)))

    def _find(self, target: Any, state: str = "visible",
              timeout: Optional[float] = None) -> Any:
        wanted = _as_locator(target)
        pairs = wanted.pairs()
        if not pairs:
            raise _Missing(f"{wanted.eid} 没有定位串")
        total = self.timeout if timeout is None else float(timeout)
        per = max(0.5, total / len(pairs))
        for by, value in pairs:
            try:
                return self._wait(by, value, state, per)
            except TimeoutException:
                continue
        raise _Missing(f"找不到元素: {wanted} (试了 {len(pairs)} 个定位串, 共等 {total:g}s)")

    def _gone(self, target: Any, timeout: Optional[float] = None) -> bool:
        wanted = _as_locator(target)
        total = self.timeout if timeout is None else float(timeout)
        per = max(0.5, total / max(1, len(wanted.pairs())))
        for by, value in wanted.pairs():
            try:
                WebDriverWait(self.driver, per, poll_frequency=POLL_FREQUENCY).until(
                    EC.invisibility_of_element_located((by, value)))
                return True
            except TimeoutException:
                continue
        return False

    def _all(self, by: str, value: str) -> List[Any]:
        try:
            return [item for item in self.driver.find_elements(by, value) if _visible(item)]
        except (StaleElementReferenceException, WebDriverException):
            return []

    def _pause(self) -> None:
        """没有可观察信号时给页面一小段自更新时间; 全模块只有这一处 sleep"""
        time.sleep(SETTLE_SECONDS)

    def _wait_ready(self, timeout: Optional[float] = None) -> None:
        budget = self.timeout if timeout is None else float(timeout)
        try:
            WebDriverWait(self.driver, budget, poll_frequency=POLL_FREQUENCY).until(
                lambda drv: drv.execute_script("return document.readyState") == "complete")
        except TimeoutException:
            pass
        self._nav_at = time.monotonic()

    # --- 失败上下文 ---

    def _failure(self, action: str, target: Optional[Locator], exc: Exception) -> ActionFailure:
        eid = target.eid if target is not None else ""
        shot = screenshot(self._driver, f"{action}_{eid or 'page'}")
        context = {
            "action": action, "eid": eid, "locator": str(target) if target else "",
            "error": f"{type(exc).__name__}: {exc}", "screenshot": shot,
            "url": "", "driver": self._driver,
        }
        try:
            context["url"] = self._driver.current_url if self._driver is not None else ""
        except WebDriverException:
            pass
        diagnosis = _diagnose(context)
        lines = [f"{action} 失败: {target if target is not None else '整页'}",
                 f"原因: {type(exc).__name__}: {exc}"]
        if shot:
            lines.append(f"截图: {shot}")
        if diagnosis:
            lines.append(f"归因: {diagnosis}")
        return ActionFailure("\n".join(lines), action=action, eid=eid,
                             screenshot=shot, diagnosis=diagnosis)

    def _body_text(self) -> str:
        try:
            return str(self.driver.execute_script(
                "return document.body ? document.body.innerText : ''") or "")
        except WebDriverException:
            try:
                return str(self.driver.page_source or "")
            except WebDriverException:
                return ""

    def _error_texts(self, selectors: Sequence[str] = _ERROR_SELECTORS) -> List[str]:
        texts: List[str] = []
        for selector in selectors:
            for element in self._all(By.CSS_SELECTOR, selector):
                text = _try_text(element)
                if text and text not in texts:
                    texts.append(text)
        return texts

    # ------------------------------------------------------------------
    # 流程
    # ------------------------------------------------------------------

    @_guarded("goto")
    def goto(self, url: Any) -> None:
        self.driver.get(str(url))
        self._wait_ready()

    @_guarded("reload")
    def reload(self) -> None:
        self.driver.refresh()
        self._wait_ready()

    @_guarded("back")
    def back(self) -> None:
        self.driver.back()
        self._wait_ready()

    # ------------------------------------------------------------------
    # 元素动作
    # ------------------------------------------------------------------

    @_guarded("fill")
    def fill(self, target: Any, text: Any) -> None:
        element = self._find(target, "visible")
        element.clear()
        element.send_keys("" if text is None else str(text))

    @_guarded("clear")
    def clear(self, target: Any) -> None:
        self._find(target, "visible").clear()

    @_guarded("click")
    def click(self, target: Any) -> None:
        element = self._find(target, "clickable")
        self._remember_click()
        try:
            element.click()
        except ElementClickInterceptedException:
            # 被浮层挡住时先滚到可视区再点一次; 还不行就如实失败 (不拿 JS 点到假通过)
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", element)
            element.click()
        self._pause()

    @_guarded("double_click")
    def double_click(self, target: Any) -> None:
        element = self._find(target, "clickable")
        self._remember_click()
        ActionChains(self.driver).double_click(element).perform()
        self._pause()

    def _remember_click(self) -> None:
        try:
            self._last_click = (self.driver.current_url, len(self.driver.page_source or ""))
        except WebDriverException:
            self._last_click = None

    @_guarded("check")
    def check(self, target: Any) -> None:
        element = self._find(target, "clickable")
        if not element.is_selected():
            element.click()
            self._pause()

    @_guarded("uncheck")
    def uncheck(self, target: Any) -> None:
        element = self._find(target, "clickable")
        if element.is_selected():
            element.click()
            self._pause()

    @_guarded("select_option")
    def select_option(self, target: Any, text: Any) -> None:
        element = self._find(target, "clickable")
        label = str(text if text is not None else "")
        tag = (element.tag_name or "").lower()
        if tag == "select":
            Select(element).select_by_visible_text(label)
            self._pause()
            return
        # 组件库的下拉: 点开再按文案点选项 (采集时看到的是容器, 选项是动态挂载的)
        element.click()
        option = None
        for element_option in self._all(By.XPATH,
                                        f'//*[self::li or @role="option" or contains(@class, "option")]'
                                        f'[normalize-space()="{label}"]'):
            option = element_option
            break
        if option is None:
            raise _Missing(f"下拉里没有文案为 {label!r} 的选项")
        option.click()
        self._pause()

    @_guarded("press_key")
    def press_key(self, target: Any, key: Any) -> None:
        name = _norm_lower(key)
        element = self._find(target, "visible")
        element.send_keys(_CLICK_KEYS.get(name, str(key if key is not None else "")))
        self._pause()

    @_guarded("get_text")
    def get_text(self, target: Any) -> str:
        return _try_text(self._find(target, "visible"))

    @_guarded("get_value")
    def get_value(self, target: Any) -> str:
        element = self._find(target, "present")
        try:
            value = element.get_attribute("value")
        except (StaleElementReferenceException, WebDriverException):
            value = None
        return _norm(value if value is not None else element.text)

    # ------------------------------------------------------------------
    # 表格 / 分页 / 排序
    # ------------------------------------------------------------------

    def _table_root(self, target: Any = None) -> Any:
        if target is not None:
            return self._find(target, "visible")
        for selector in _TABLE_SELECTORS:
            for element in self._all(By.CSS_SELECTOR, selector):
                return element
        raise _Missing("页面上没有可见的表格")

    def _rows(self, root: Any) -> List[Any]:
        for selector in _ROW_SELECTORS:
            rows = []
            for row in root.find_elements(By.CSS_SELECTOR, selector):
                if not _visible(row):
                    continue
                if row.find_elements(By.CSS_SELECTOR, "th"):
                    continue          # 表头行不算数据行
                rows.append(row)
            if rows:
                return rows
        return []

    def _column_index(self, root: Any, name: str) -> int:
        headers: List[Any] = []
        for selector in _HEADER_SELECTORS:
            headers = root.find_elements(By.CSS_SELECTOR, selector)
            if headers:
                break
        for index, header in enumerate(headers):
            if _norm(_try_text(header)) == _norm(name):
                return index
        raise _Missing(f"表格里没有列 {name!r}")

    def _cell_texts(self, root: Any, index: int) -> List[str]:
        texts: List[str] = []
        for row in self._rows(root):
            cells = row.find_elements(By.CSS_SELECTOR, _CELL_SELECTORS)
            if index < len(cells):
                texts.append(_try_text(cells[index]))
        return texts

    def _row_texts(self, root: Any) -> List[str]:
        return [_try_text(row) for row in self._rows(root)]

    def row_count(self, target: Any = None) -> int:
        """行数 (供原语内部与测试直接用; 不单独做断言)"""
        return len(self._rows(self._table_root(target)))

    @_guarded("paginate")
    def paginate(self, target: Any, page_number: Any) -> None:
        self._table_root(target)
        value = _norm_lower(page_number)
        control = self._pager_control(value) if value in ("first", "last", "next", "prev") \
            else self._pager_item(str(page_number))
        control.click()
        self._pause()
        self._wait_ready()

    def _pager_items(self) -> List[Any]:
        items: List[Any] = []
        for selector in (".el-pager li", ".el-pagination li", ".ant-pagination-item",
                         "[class*=pagination] li", "[class*=pagination] a",
                         "[class*=pagination] button"):
            for element in self._all(By.CSS_SELECTOR, selector):
                if element not in items:
                    items.append(element)
        return items

    def _pager_item(self, label: str) -> Any:
        available: List[str] = []
        for element in self._pager_items():
            text = _try_text(element)
            available.append(text)
            if text == label:
                return element
        raise _Missing(f"分页里没有第 {label} 页 (可见页码: {available[:10]})")

    def _pager_control(self, name: str) -> Any:
        tokens = {"prev": ("prev", "previous", "上一页"), "next": ("next", "下一页"),
                  "first": ("first", "首页"), "last": ("last", "末页", "尾页")}[name]
        for element in self._pager_items():
            blob = " ".join((element.get_attribute("class") or "",
                             element.get_attribute("aria-label") or "",
                             _try_text(element))).lower()
            if any(token in blob for token in tokens):
                return element
        raise _Missing(f"分页里没有 {name} 控件")

    @_guarded("set_page_size")
    def set_page_size(self, target: Any, size: Any) -> None:
        self._table_root(target)
        label = str(size)
        native = [item for item in self.driver.find_elements(
            By.CSS_SELECTOR, "[class*=pagination] select") if _visible(item)]
        if native:
            Select(native[0]).select_by_visible_text(label)
            self._pause()
            return
        triggers = [item for item in self.driver.find_elements(
            By.CSS_SELECTOR, ".el-pagination .el-select, [class*=pagination] .el-select")
            if _visible(item)]
        if not triggers:
            raise _Missing("分页区域没有每页条数控件")
        triggers[0].click()
        option = None
        for element in self._all(By.XPATH,
                                 f'//*[self::li or @role="option" or contains(@class, "option")]'
                                 f'[normalize-space()="{label}"]'):
            option = element
            break
        if option is None:
            raise _Missing(f"每页条数下拉里没有 {label!r}")
        option.click()
        self._pause()

    @_guarded("sort")
    def sort(self, target: Any, direction: Any = "asc") -> None:
        self._find(target, "clickable").click()
        self._pause()

    @_guarded("assert_sorted")
    def assert_sorted(self, target: Any, direction: Any = "asc") -> None:
        wanted = _as_locator(target)
        column = wanted.label or wanted.eid.split(".column:")[-1]
        root = self._table_root(loc(wanted.table) if wanted.table else None)
        values = [text for text in self._cell_texts(root, self._column_index(root, column))
                  if text]
        if len(values) < 2:
            raise _Missing(f"列 {column!r} 只有 {len(values)} 个非空值, 排不了序")
        numbers = _as_numbers(values)
        ordered = sorted(numbers) if numbers is not None else sorted(values)
        if _norm_lower(direction) == "desc":
            ordered.reverse()
        if (numbers if numbers is not None else values) != ordered:
            raise AssertionError(
                f"列 {column!r} 没有按 {direction} 排序, 当前: {values[:6]}")

    # ------------------------------------------------------------------
    # 弹窗
    # ------------------------------------------------------------------

    def _visible_dialog(self) -> Optional[Any]:
        for selector in _DIALOG_SELECTORS:
            for element in self._all(By.CSS_SELECTOR, selector):
                if _norm(_try_text(element)) or element.find_elements(
                        By.CSS_SELECTOR, "button, [role=button]"):
                    return element
        return None

    def _dialog_button(self, root: Any, label: str,
                       fallback: Sequence[str] = ()) -> Any:
        labels = [label] + [item for item in fallback if item != label]
        texts: List[str] = []
        for wanted in labels:
            for element in root.find_elements(
                    By.XPATH,
                    f'.//button | .//a | .//*[@role="button"]'):
                text = _try_text(element)
                texts.append(text)
                if text == wanted:
                    return element
        raise _Missing(f"弹窗里没有按钮 {label!r} (可见按钮: {texts[:8]})")

    @_guarded("confirm_dialog")
    def confirm_dialog(self, label: Any = None) -> None:
        root = self._visible_dialog()
        if root is None:
            raise _Missing("页面上没有可见弹窗")
        self._dialog_button(root, str(label or "确定"),
                            fallback=("确定", "确认", "OK", "Ok", "保存", "提交")).click()
        self._pause()

    @_guarded("cancel_dialog")
    def cancel_dialog(self, label: Any = None) -> None:
        root = self._visible_dialog()
        if root is None:
            raise _Missing("页面上没有可见弹窗")
        self._dialog_button(root, str(label or "取消"),
                            fallback=("取消", "关闭", "Cancel", "Close")).click()
        self._pause()

    # ------------------------------------------------------------------
    # 导航 / 元素断言
    # ------------------------------------------------------------------

    @_guarded("assert_visible")
    def assert_visible(self, target: Any) -> None:
        self._find(target, "visible")

    @_guarded("assert_hidden")
    def assert_hidden(self, target: Any) -> None:
        if not self._gone(target):
            raise AssertionError(f"{_as_locator(target)} 还是可见的")

    @_guarded("assert_inactive")
    def assert_inactive(self, target: Any) -> None:
        element = self._find(target, "present")
        if not _visible(element):
            return
        if element.get_attribute("disabled") is not None:
            return
        if _norm_lower(element.get_attribute("aria-disabled")) == "true":
            return
        if "disabled" in _norm_lower(element.get_attribute("class") or ""):
            return
        raise AssertionError(f"{_as_locator(target)} 还是可用的 (没置灰也没隐藏)")

    @_guarded("assert_text")
    def assert_text(self, target: Any, text: Any) -> None:
        actual = _try_text(self._find(target, "visible"))
        expected = _norm(text)
        if expected not in actual:
            raise AssertionError(f"{_as_locator(target)} 的文本是 {actual!r}, 不含 {expected!r}")

    @_guarded("assert_value")
    def assert_value(self, target: Any, text: Any) -> None:
        actual = self.get_value(target)
        expected = _norm(text)
        if actual != expected:
            raise AssertionError(f"{_as_locator(target)} 的值是 {actual!r}, 不等于 {expected!r}")

    @_guarded("assert_count")
    def assert_count(self, target: Any, count: Any) -> None:
        wanted = _as_locator(target)
        pairs = wanted.pairs()
        total = len(self.driver.find_elements(*pairs[0])) if pairs else 0
        if total != int(count):
            raise AssertionError(f"{wanted} 有 {total} 个, 不等于 {count}")

    @_guarded("assert_attr")
    def assert_attr(self, target: Any, name: Any, value: Any) -> None:
        element = self._find(target, "present")
        actual = element.get_attribute(str(name))
        if _norm(actual) != _norm(value):
            raise AssertionError(
                f"{_as_locator(target)} 的 {name} 是 {actual!r}, 不等于 {value!r}")

    @_guarded("assert_field_error")
    def assert_field_error(self, target: Any, text: Any = None) -> None:
        element = self._find(target, "present")
        texts: List[str] = []
        for depth in ("..", "../.."):
            try:
                container = element.find_element(By.XPATH, depth)
            except WebDriverException:
                continue
            for selector in _FIELD_ERROR_SELECTORS:
                for node in container.find_elements(By.CSS_SELECTOR, selector):
                    if _visible(node) and _try_text(node):
                        texts.append(_try_text(node))
        wanted = _norm(text) or "必填"
        if not texts:
            raise AssertionError(f"{_as_locator(target)} 下面没有任何校验提示")
        if wanted not in " ".join(texts):
            raise AssertionError(f"{_as_locator(target)} 的提示是 {texts[:3]}, 不含 {wanted!r}")

    @_guarded("assert_masked")
    def assert_masked(self, target: Any = None) -> None:
        if target is None:
            if any(item.get_attribute("type") == "password"
                   for item in self._all(By.CSS_SELECTOR, 'input[type="password"]')):
                return
            raise AssertionError("页面上没有 type=password 的输入框, 敏感值没有被脱敏的机会")
        element = self._find(target, "present")
        if _norm_lower(element.get_attribute("type")) == "password":
            return
        value = str(element.get_attribute("value") or "")
        if value and set(value) <= set("*•·● "):
            return
        raise AssertionError(f"{_as_locator(target)} 的值没有被遮罩: {value!r}")

    @_guarded("assert_autocomplete")
    def assert_autocomplete(self, target: Any, value: Any) -> None:
        element = self._find(target, "present")
        actual = element.get_attribute("autocomplete")
        if _norm(actual) != _norm(value):
            raise AssertionError(
                f"{_as_locator(target)} 的 autocomplete 是 {actual!r}, 不等于 {value!r}")

    # ------------------------------------------------------------------
    # 页面级断言
    # ------------------------------------------------------------------

    @_guarded("assert_no_error_toast")
    def assert_no_error_toast(self) -> None:
        texts = self._error_texts()
        if texts:
            raise AssertionError(f"出现了报错提示: {texts[:3]}")

    @_guarded("assert_no_alert")
    def assert_no_alert(self) -> None:
        try:
            alert = self.driver.switch_to.alert
        except WebDriverException:
            return
        try:
            text = alert.text
            alert.accept()          # 不关掉会卡住后续步骤; 但这条断言已经失败
        except WebDriverException:
            text = ""
        raise AssertionError(f"弹出了原生 alert: {text!r}")

    @_guarded("assert_loading")
    def assert_loading(self) -> None:
        seen = False
        deadline = time.monotonic() + min(5.0, self.timeout)
        while time.monotonic() < deadline:
            if any(self._all(By.CSS_SELECTOR, selector) for selector in _LOADING_SELECTORS):
                seen = True
                break
            time.sleep(0.1)
        if not seen:
            raise AssertionError("没有看到 loading (断言的前提就不成立)")
        for selector in _LOADING_SELECTORS:
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    WebDriverWait(self.driver, self.timeout,
                                  poll_frequency=POLL_FREQUENCY).until(
                        EC.invisibility_of_element(element))
                except TimeoutException as exc:
                    raise AssertionError(f"loading 一直没消失: {selector}") from exc

    @_guarded("assert_page_ready_within")
    def assert_page_ready_within(self, milliseconds: Any) -> None:
        budget = float(milliseconds)
        elapsed = self._page_elapsed_ms()
        if elapsed > budget:
            raise AssertionError(f"页面可用耗时 {elapsed:.0f}ms, 超过预算 {budget:.0f}ms")

    def _page_elapsed_ms(self) -> float:
        try:
            timing = self.driver.execute_script(
                "var n=(performance.getEntriesByType&&performance.getEntriesByType('navigation')[0])"
                "||null; if(!n) return null; return [n.domContentLoadedEventEnd||0,"
                " n.loadEventEnd||0, n.duration||0];")
        except WebDriverException:
            timing = None
        if isinstance(timing, (list, tuple)):
            values = [float(item or 0) for item in timing]
            if max(values) > 0:
                return max(values)
        if self._nav_at:
            return (time.monotonic() - self._nav_at) * 1000
        self._wait_ready(timeout=min(2.0, self.timeout))
        return (time.monotonic() - self._nav_at) * 1000

    @_guarded("assert_feedback")
    def assert_feedback(self) -> None:
        if self._last_click is None:
            raise _Missing("assert_feedback 要跟在 click/double_click 之后 (没有可对比的点击前状态)")
        before_url, before_size = self._last_click
        after_url = self.driver.current_url
        try:
            after_size = len(self.driver.page_source or "")
        except WebDriverException:
            after_size = before_size
        changed = (before_url != after_url or abs(after_size - before_size) > 0
                   or self._visible_dialog() is not None or bool(self._error_texts()))
        if not changed:
            raise AssertionError("点击后页面没有任何变化 (URL / DOM / 弹窗 / 提示都没动)")

    @_guarded("assert_persisted")
    def assert_persisted(self, value: Any) -> None:
        marker = str(value)
        self.reload()
        if marker not in self._body_text():
            raise AssertionError(f"重新加载后页面上找不到 {marker!r}")

    @_guarded("assert_not_persisted")
    def assert_not_persisted(self, value: Any) -> None:
        marker = str(value)
        self.reload()
        if marker in self._body_text():
            raise AssertionError(f"重新加载后页面上仍然有 {marker!r}")

    @_guarded("assert_login_error")
    def assert_login_error(self) -> None:
        body = self._body_text()
        on_login = ("login" in self.driver.current_url.lower()
                    or "signin" in self.driver.current_url.lower()
                    or bool(self._all(By.CSS_SELECTOR, 'input[type="password"]')))
        if not on_login:
            raise AssertionError("已经不在登录页了, 登录失败无从谈起")
        hints = self._error_texts() + [word for word in ("错误", "失败", "不正确", "无效",
                                                         "不存在", "锁定")
                                       if word in body]
        if not hints:
            raise AssertionError("还停在登录页, 但页面上没有任何失败提示")

    @_guarded("assert_logged_in")
    def assert_logged_in(self) -> None:
        url = self.driver.current_url.lower()
        if "login" in url or "signin" in url:
            raise AssertionError(f"还停在登录页: {self.driver.current_url}")
        if self._all(By.CSS_SELECTOR, 'input[type="password"]'):
            raise AssertionError("页面上还有密码输入框, 看起来没登录成功")

    @_guarded("assert_locked")
    def assert_locked(self, attempts: Any = None) -> None:
        body = self._body_text()
        words = [word for word in ("锁定", "禁用", "验证码", "频繁", "稍后再试") if word in body]
        if not words:
            raise AssertionError(f"页面上没有锁定/验证码提示 (试了 {attempts or '?'} 次)")

    @_guarded("assert_submit_failed")
    def assert_submit_failed(self) -> None:
        hints = self._error_texts()
        body = self._body_text()
        if not hints and not any(word in body for word in ("失败", "错误", "请检查", "不能为空")):
            raise AssertionError("提交没有给出失败提示, 也没看到可重试的错误")

    @_guarded("assert_empty_state")
    def assert_empty_state(self, target: Any = None) -> None:
        root = self._table_root(target)
        rows = self._rows(root)
        if rows:
            raise AssertionError(f"表格还有 {len(rows)} 行, 不是空态")
        blob = (_norm(_try_text(root)) + " " + self._body_text()).lower()
        if not any(word in blob for word in _EMPTY_WORDS):
            raise AssertionError("表格是空的, 但页面上没有空态文案")

    @_guarded("assert_rows_match")
    def assert_rows_match(self, target: Any, text: Any) -> None:
        root = self._table_root(target)
        rows = self._row_texts(root)
        if not rows:
            raise AssertionError("没有可校验的行")
        wanted = _norm(text)
        bad = [row for row in rows if wanted not in row]
        if bad:
            raise AssertionError(f"{len(bad)}/{len(rows)} 行不含 {wanted!r}: {bad[:2]}")

    @_guarded("assert_row_count")
    def assert_row_count(self, target: Any, count: Any) -> None:
        actual = len(self._rows(self._table_root(target)))
        if actual != int(count):
            raise AssertionError(f"行数是 {actual}, 不等于 {count}")

    @_guarded("assert_row_count_lte")
    def assert_row_count_lte(self, target: Any, count: Any) -> None:
        actual = len(self._rows(self._table_root(target)))
        if actual > int(count):
            raise AssertionError(f"行数是 {actual}, 超过上限 {count}")

    @_guarded("assert_row_count_gte")
    def assert_row_count_gte(self, target: Any, count: Any) -> None:
        actual = len(self._rows(self._table_root(target)))
        if actual < int(count):
            raise AssertionError(f"行数是 {actual}, 少于下限 {count}")

    @_guarded("assert_no_duplicate_rows")
    def assert_no_duplicate_rows(self, target: Any = None) -> None:
        rows = [row for row in self._row_texts(self._table_root(target)) if row]
        seen: Dict[str, int] = {}
        for row in rows:
            seen[row] = seen.get(row, 0) + 1
        duplicated = [row for row, count in seen.items() if count > 1]
        if duplicated:
            raise AssertionError(f"有 {len(duplicated)} 组重复行: {duplicated[:2]}")

    @_guarded("assert_count_matches_rows")
    def assert_count_matches_rows(self, target: Any = None) -> None:
        root = self._table_root(target)
        rows = len(self._rows(root))
        texts = [_try_text(root), self._body_text()]
        total = None
        for text in texts:
            matched = re.search(r"共\s*(\d+)\s*条", text)
            if matched:
                total = int(matched.group(1))
                break
        if total is None:
            raise AssertionError("页面上没有『共 N 条』这样的计数")
        if total != rows:
            raise AssertionError(f"『共 {total} 条』与表格 {rows} 行不一致")

    def _selection_count(self, root: Any) -> int:
        total = 0
        for box in root.find_elements(By.CSS_SELECTOR,
                                      'input[type="checkbox"], [role=checkbox]'):
            checked = False
            try:
                if box.tag_name.lower() == "input":
                    checked = box.is_selected()
                else:
                    checked = _norm_lower(box.get_attribute("aria-checked")) == "true"
            except (StaleElementReferenceException, WebDriverException):
                continue
            if checked:
                total += 1
        return total

    @_guarded("assert_selection_count")
    def assert_selection_count(self, target: Any, count: Any) -> None:
        actual = self._selection_count(self._table_root(target))
        if actual != int(count):
            raise AssertionError(f"选中 {actual} 行, 不等于 {count}")

    @_guarded("assert_selection_count_gte")
    def assert_selection_count_gte(self, target: Any, count: Any) -> None:
        actual = self._selection_count(self._table_root(target))
        if actual < int(count):
            raise AssertionError(f"选中 {actual} 行, 少于 {count}")

    # ------------------------------------------------------------------
    # 等待 (给不想直接断言的场合)
    # ------------------------------------------------------------------

    @_guarded("wait_visible")
    def wait_visible(self, target: Any) -> Any:
        return self._find(target, "visible")

    @_guarded("wait_clickable")
    def wait_clickable(self, target: Any) -> Any:
        return self._find(target, "clickable")

    @_guarded("wait_text")
    def wait_text(self, target: Any, text: Any) -> Any:
        wanted = _as_locator(target)
        expected = _norm(text)
        for by, value in wanted.pairs():
            try:
                element = WebDriverWait(self.driver, self.timeout,
                                        poll_frequency=POLL_FREQUENCY).until(
                    EC.text_to_be_present_in_element((by, value), expected))
                if element:
                    return self._find(wanted, "visible")
            except TimeoutException:
                continue
        raise _Missing(f"{wanted} 在 {self.timeout:g}s 内没有出现文本 {expected!r}")

    @_guarded("wait_gone")
    def wait_gone(self, target: Any) -> None:
        if not self._gone(target):
            raise _Missing(f"{_as_locator(target)} 在 {self.timeout:g}s 内没有消失")


def _as_numbers(values: Sequence[str]) -> Optional[List[float]]:
    """全是数字就按数字排, 否则交给字符串排序 (日期/中文都能用字典序)"""
    numbers: List[float] = []
    for text in values:
        try:
            numbers.append(float(str(text).replace(",", "")))
        except ValueError:
            return None
    return numbers


# 用例产物直接 `from ai_kit.runtime import loc, page, use_locators`
page = Page()

__all__ = [
    "ARTIFACTS_ENV",
    "ActionFailure",
    "DEFAULT_TIMEOUT",
    "LOCATORS_ENV",
    "LOCATORS_VERSION",
    "Locator",
    "LocatorNotKnown",
    "PAGE_REF",
    "Page",
    "RUNTIME_VERSION",
    "RuntimeNotBound",
    "WAIT_ENV",
    "loc",
    "locators",
    "page",
    "screenshot",
    "use_locators",
]
