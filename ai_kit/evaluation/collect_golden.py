# -*- coding: utf-8 -*-
"""
generator 黄金采集器 — 真页面 -> collector 清单 -> 脱敏冻结 -> 黄金集

为什么不手编 HTML: 手写 page_structure 只会写"我想象中页面有的东西", 采集器拿到
的才是真实控件形状 (el-table 的包裹层、动态 id、行内按钮折叠)。黄金集的输入应该
来自真链路, 而不是对链路的想象。

两步分离 (collect 要登录、要浏览器; freeze 纯离线):
    python -m ai_kit.evaluation.collect_golden collect  --base "$BASE_URL" --out _collected.json
    python -m ai_kit.evaluation.collect_golden freeze   --in _collected.json [--scrub 账号名]

数据安全 (冻结环节保证, 一条都靠自觉):
- 表单值 JS 侧本来就不采 (collector 硬约束); sample_rows 是表格裸文本, 清成空行;
- url 只留路由 (域名换 https://app.invalid), title 换成 page_name;
- 所有字符串字段过 utils.sanitize (手机号/身份证/字段名+值);
- --scrub 传入的凭据词 (登录账号等) 一律替换成 [USER];
- 落盘前做零残留扫描: 手机号/18 位身份证/凭据词 命中即拒绝写出。

账号只从环境变量读 (默认 LOGIN_USER / LOGIN_PASS), 不落盘、不回显。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

from ai_kit.evaluation import golden
from ai_kit.perception import collector
from ai_kit.utils import sanitize

FAKE_ORIGIN = "https://app.invalid"      # 域名是内部信息, 黄金里一律换成合成域名
USER_PLACEHOLDER = "[USER]"
RULE_VERSION = "generator.v2"

# 采集菜单: route 是 SPA hash; click 非空 = 先点这个按钮 (打开弹窗) 再采。
# 想要别的页面用 --pages 覆盖 (同一套脱敏, 不另开口子)。
# 注: #/login 在已登录会话下会被路由守卫弹回默认页, 所以 login 页在登录前采 (collect 里特判);
#     #/system/users 当前账号无权限 (弹回 /usage/board), 采不到就别采, 拿假数据凑数更糟。
DEFAULT_PAGES: List[Dict[str, Any]] = [
    {"key": "login",     "page_name": "登录页",     "route": "#/login",            "login": False},
    {"key": "operators", "page_name": "操作员列表", "route": "#/system/operators", "login": True},
    {"key": "operators_add", "page_name": "操作员新增弹窗", "route": "#/system/operators",
     "login": True, "click": "新增操作员"},
    {"key": "device",    "page_name": "设备列表",   "route": "#/device",           "login": True},
    {"key": "history",   "page_name": "历史查询",   "route": "#/usage/history",    "login": True},
    {"key": "operators_edit", "page_name": "操作员编辑弹窗", "route": "#/system/operators",
     "login": True, "click": "编辑"},
    {"key": "board",     "page_name": "使用看板",   "route": "#/usage/board",      "login": True},
    {"key": "board_add", "page_name": "看板新增记录弹窗", "route": "#/usage/board",
     "login": True, "click": "新增记录"},
]

# 登录页控件: 通用形状 (placeholder 含用户名/账号, password 输入框, 文案含登录的按钮)
USER_INPUT = ("css selector", "input[placeholder*='用户'], input[placeholder*='账号'], input[type='text']")
PASS_INPUT = ("css selector", "input[type='password']")
LOGIN_BUTTON = ("xpath",
                "//button[contains(normalize-space(.),'登录')]"
                " | //span[contains(normalize-space(.),'登录')]/ancestor::button[1]"
                " | //*[self::button or contains(@class,'button')][contains(normalize-space(.),'登 录')]")

# 零残留扫描: 手机号 / 18 位身份证 / 15 位老身份证
RESIDUAL_PATTERNS = [
    re.compile(r"1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}"),
    re.compile(r"\d{17}[\dXx]"),
    re.compile(r"\d{15}"),
]


# ---------------------------------------------------------------------------
# collect: 登录 + 逐页采集 (联网, 本机一次性)
# ---------------------------------------------------------------------------

def _find(driver, by: str, value: str):
    from selenium.webdriver.common.by import By
    return driver.find_element(getattr(By, by.upper().replace(" ", "_")), value)


def _wait_page(driver, seconds: float = 8.0) -> None:
    """SPA 渲染要等: readyState + 至少出现一个可交互元素, 等不到也到点就走
    (空清单是有效信号 —— 这页没采出来, freeze 环节会点名, 不假装成功)"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            ready = driver.execute_script("return document.readyState")
            n = len(driver.find_elements("css selector",
                                         "input, button, a[href], .el-table, .el-button"))
        except Exception:  # noqa: BLE001 — 导航瞬间元素树抖动属正常
            ready, n = "loading", 0
        if ready == "complete" and n > 0:
            time.sleep(1.2)          # 框架异步填充再留一点
            return
        time.sleep(0.4)
    time.sleep(0.5)


def login(driver, base_url: str, user: str, password: str) -> None:
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as ec
    from selenium.webdriver.common.by import By

    driver.get(base_url)
    _wait_page(driver)
    wait = WebDriverWait(driver, 15)
    by = lambda spec: (getattr(By, spec[0].upper().replace(" ", "_")), spec[1])
    wait.until(ec.visibility_of_element_located(by(USER_INPUT))).send_keys(user)
    wait.until(ec.visibility_of_element_located(by(PASS_INPUT))).send_keys(password)
    wait.until(ec.element_to_be_clickable(by(LOGIN_BUTTON))).click()
    _wait_page(driver)


def _click_text(driver, text: str) -> bool:
    """按可见文案点按钮 (弹窗入口); 点不到返回 False, 该页照常采主视图"""
    from selenium.webdriver.common.by import By
    try:
        nodes = driver.find_elements(By.XPATH,
                                     f"//button[contains(normalize-space(.),'{text}')]"
                                     f" | //span[contains(normalize-space(.),'{text}')]/ancestor::button[1]")
        for node in nodes:
            if node.is_displayed():
                node.click()
                time.sleep(1.0)
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def collect(pages: List[Dict[str, Any]], base_url: str, *,
            profile: str = "element_plus", headless: bool = True) -> List[Dict[str, Any]]:
    """登录一次, 逐页采集。返回 [{key, page_name, route, inventory, notes}]"""
    from selenium import webdriver
    from ai_kit.evaluation.driver_provider import _make_edge, _make_chrome

    def _driver():
        for factory in (_make_edge, _make_chrome):
            try:
                return factory()
            except Exception:  # noqa: BLE001
                continue
        raise RuntimeError("Edge/Chrome 都拉不起来, 采集没法跑")

    origin = base_url.split("#")[0].rstrip("/")
    if origin.endswith("/"):
        origin = origin[:-1]
    user = os.environ.get("LOGIN_USER", "")
    password = os.environ.get("LOGIN_PASS", "")

    drv = _driver()
    out: List[Dict[str, Any]] = []
    try:
        # 登录页要在未登录状态采 (登录后守卫会弹走), 所以先采 login 再登录
        pre = [p for p in pages if not p.get("login", True)]
        post = [p for p in pages if p.get("login", True)]
        for spec in pre:
            out.append(_collect_one(drv, spec, origin, profile))
        if post:
            if not (user and password):
                raise SystemExit("需要登录但 LOGIN_USER/LOGIN_PASS 未设置 (脚本不落盘凭据)")
            login(drv, base_url, user, password)
            for spec in post:
                out.append(_collect_one(drv, spec, origin, profile))
    finally:
        try:
            drv.quit()
        except Exception:  # noqa: BLE001
            pass
    return out


def _collect_one(drv, spec: Dict[str, Any], origin: str,
                 profile: str = "element_plus") -> Dict[str, Any]:
    """导航 -> 落地校验 -> 可选点击 -> 采集。路由不对就地点名, 不采错东西假装没事"""
    url = origin + "/" + spec["route"]
    drv.get(url)
    _wait_page(drv)
    notes = []
    landed = drv.current_url.split("#")[-1].split("?")[0].rstrip("/")
    want = spec["route"].split("?")[0].rstrip("/")
    if want.startswith("#"):
        want = want[1:]              # spec 里写的是 #/xxx, 比对时只比 hash 后面的路径
    if not want.startswith("/"):
        want = "/" + want          # current_url 的 hash 部分带前导斜杠, 归一后再比
    if landed != want:
        notes.append(f"路由被弹到 {landed!r}, 采的不是目标页 (期望 {want!r}, 实际 URL {drv.current_url!r})")
        print(f"[采集] 跳过 {spec['key']}: {notes[-1]}")
        return {"key": spec["key"], "page_name": spec["page_name"],
                "route": spec["route"], "inventory": None, "notes": notes}
    if spec.get("click"):
        ok = _click_text(drv, spec["click"])
        if not ok:
            notes.append(f"点击『{spec['click']}』失败, 采的是主视图")
    inv = collector.collect(drv, profile, page_name=spec["page_name"])
    print(f"[采集] {spec['key']:<14} 交互 {inv['counts']['interactive']:<4} "
          f"表格 {inv['counts']['tables']}  -> {spec['route']}")
    return {"key": spec["key"], "page_name": spec["page_name"],
            "route": spec["route"], "inventory": inv, "notes": notes}


# ---------------------------------------------------------------------------
# freeze: 脱敏 + 合成黄金 case (纯离线, 可单测)
# ---------------------------------------------------------------------------

def _scrub(value: Any, words: List[str]) -> Any:
    """字符串先过 sanitize (手机号/身份证/字段名+值), 再替换凭据词"""
    if isinstance(value, str):
        text = sanitize(value)
        for word in words:
            if word:
                text = text.replace(word, USER_PLACEHOLDER)
        return text
    if isinstance(value, list):
        return [_scrub(v, words) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, words) for k, v in value.items()}
    return value


def sanitize_inventory(inv: Dict[str, Any], route: str,
                       page_name: str, scrub_words: List[str]) -> Dict[str, Any]:
    """冻结一条采集清单: 域名/标题合成化, 裸文本清空, 全字段过 sanitize"""
    out = json.loads(json.dumps(inv, ensure_ascii=False))  # 深拷贝, 不劳烦 copy 语义之争
    out["url"] = FAKE_ORIGIN + "/" + route
    out["title"] = page_name
    for bucket in ("inputs", "buttons", "selects", "tables"):
        for item in out.get(bucket) or ():
            item["html"] = ""                       # outerHTML 是裸文本重灾区, 结构信息 attrs/locator 已够
            for key in ("sample_rows",):
                if key in item:
                    item[key] = []                  # 表格单元格是业务数据, 行数 row_count 才是结构
    for dialog in out.get("dialogs") or ():
        dialog["text"] = dialog.get("text", "")[:40]
    out = _scrub(out, scrub_words)
    out["frozen_at"] = time.strftime("%Y-%m-%d")
    out["freeze"] = {"domain": "fake", "sample_rows": "cleared",
                     "html": "dropped", "sanitize": True}
    return out


_CATEGORY_VERBS = [
    ("查询", "query"), ("搜索", "query"), ("重置", "reset"), ("新增", "add"),
    ("编辑", "edit"), ("删除", "delete"), ("导出", "export"), ("详情", "detail"),
    ("确定", "submit"), ("提交", "submit"), ("保存", "submit"),
]

def _derive_expected(inv: Dict[str, Any]) -> Dict[str, List[str]]:
    """期望值从采集结构里推 (不是手编): 按钮/行内动作动词 -> 分类, 按钮/列名/字段名 -> 关键词"""
    texts: List[str] = []
    for item in (inv.get("buttons") or []) + (inv.get("inputs") or []) \
            + (inv.get("selects") or []):
        label = (item.get("text") or item.get("attrs", {}).get("placeholder")
                 or item.get("name") or "").strip()
        if label:
            texts.append(label)
    for table in inv.get("tables") or ():
        # 行内动作 (删除/编辑) 同样代表应覆盖的场景, 只当关键词漏掉了分类
        for action in table.get("row_actions") or ():
            t = str(action.get("text") or "").strip()
            if t:
                texts.append(t)
    categories, keywords = [], []
    for word, cat in _CATEGORY_VERBS:
        if any(word in t for t in texts) and cat not in categories:
            categories.append(cat)
            if word not in keywords:
                keywords.append(word)
    for table in inv.get("tables") or ():
        for action in table.get("row_actions") or ():
            t = str(action.get("text") or "").strip()
            if t and t not in keywords:
                keywords.append(t)
    return {"categories": categories or ["query"],
            "keywords": list(dict.fromkeys(keywords))[:12]}


def freeze_case(raw: Dict[str, Any], scrub_words: List[str]) -> Dict[str, Any]:
    """一次采集结果 -> 一条 generator 黄金 case"""
    inv = sanitize_inventory(raw["inventory"], raw.get("route", ""),
                             raw["page_name"], scrub_words)
    expected = _derive_expected(inv)
    return {
        "id": f"G_{raw['key']}",
        "module": "generator",
        "rule_version": RULE_VERSION,
        "source": "collected",
        "input": {
            "page_name": raw["page_name"],
            "fixture_name": raw["key"],
            "po_module": raw["key"],
            "po_class": "".join(p.capitalize() for p in raw["key"].split("_")) + "Page",
            "po_methods": "",
            "inventory": inv,
        },
        "expected": expected,
        "note": (f"{time.strftime('%Y-%m-%d')} 由 collect_golden 从真页面采集并脱敏冻结 "
                 f"(collector v{inv.get('collector_version', 1)}); "
                 f"interactive={inv['counts']['interactive']}"),
    }


def residual_hits(text: str, words: List[str]) -> List[str]:
    hits = [p.pattern for p in RESIDUAL_PATTERNS if p.search(text)]
    hits += [w for w in words if w and w in text]
    return hits


def write_golden(cases: List[Dict[str, Any]], directory: str) -> Dict[str, Any]:
    """按 id 合并进 generator_cases.json (手写 case 一个字不动), 先体检后落盘"""
    path = os.path.join(directory, golden.FILES["generator"])
    existing = golden.load("generator", directory=directory)
    by_id = {c["id"]: c for c in existing}
    for case in cases:
        by_id[case["id"]] = case
    merged = sorted(by_id.values(), key=lambda c: str(c.get("id")))
    problems = golden.check(merged, "generator")
    if problems:
        raise SystemExit("黄金体检不过, 拒绝落盘:\n  " + "\n  ".join(problems))
    payload = {
        "_comment": ("生成黄金集 (generator.v2): source=collected 的 input.inventory 是 "
                     "collector 从真页面采集并脱敏冻结的清单 (域名合成/表格裸文本清零/html 丢弃/"
                     "sanitize+scrub 零残留扫描), 不手编 HTML; G1/G2 为手写管道自检。"
                     "eval_generator 对带 inventory 的 case 走 plan->render->verify 链, "
                     "评 eid 解析率/可执行率。"),
        "cases": merged,
    }
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {"path": path, "n_total": len(merged), "n_new": len(cases)}


def _parse_pages(arg: str) -> List[Dict[str, Any]]:
    if not arg:
        return DEFAULT_PAGES
    by_key = {p["key"]: p for p in DEFAULT_PAGES}
    picked = []
    for key in (k.strip() for k in arg.split(",") if k.strip()):
        if key not in by_key:
            raise SystemExit(f"未知页面 {key!r}: 认得 {'/'.join(by_key)}")
        picked.append(by_key[key])
    return picked


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("action", choices=("collect", "freeze"))
    parser.add_argument("--base", default=os.environ.get("BASE_URL", ""),
                        help="登录页完整 URL (默认读 BASE_URL 环境变量)")
    parser.add_argument("--pages", default="", help="逗号分隔的页面 key (默认全套菜单)")
    parser.add_argument("--profile", default="element_plus")
    parser.add_argument("--out", default="_collected.json", help="collect 的中间产物 (本地, 不进 git)")
    parser.add_argument("--in", dest="src", default="_collected.json", help="freeze 的输入")
    parser.add_argument("--golden-dir", default=golden.DEFAULT_DIR)
    parser.add_argument("--scrub", action="append", default=[],
                        help="凭据词 (可重复传/逗号分隔), 冻结时替换为 [USER]")
    args = parser.parse_args(argv)

    words = [w.strip() for chunk in args.scrub for w in chunk.split(",") if w.strip()]
    if args.action == "collect":
        if not args.base:
            raise SystemExit("--base 或 BASE_URL 必须给一个")
        raw = collect(_parse_pages(args.pages), args.base, profile=args.profile)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        print(f"[采集] {len(raw)} 页 -> {args.out} (中间产物, 别提交)")
        return 0

    with open(args.src, encoding="utf-8") as f:
        raw_pages = json.load(f)
    cases = []
    for raw in raw_pages:
        if not raw.get("inventory"):      # collect 环节就没采到的页 (路由被弹走), 不合成假 case
            print(f"[冻结] 跳过 G_{raw['key']}: 采集阶段 inventory 为空 ({'; '.join(raw.get('notes') or ()) or '无清单'})")
            continue
        case = freeze_case(raw, words)
        text = json.dumps(case, ensure_ascii=False)
        hits = residual_hits(text, words)
        if hits:
            raise SystemExit(f"{case['id']} 冻结后仍残留 {hits}, 不落盘")
        cases.append(case)
        print(f"[冻结] {case['id']:<18} 分类 {case['expected']['categories']}")
    result = write_golden(cases, args.golden_dir)
    print(f"[冻结] {result['n_new']} 条 -> {result['path']} (合并后共 {result['n_total']} 条)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
