# -*- coding: utf-8 -*-
"""
可插拔无头浏览器供应器 — 供自愈评测加载静态 HTML 夹具 (file://)

可移植要求:
- 不 import 项目的 common/driver_factory 或 config.py (那些绑定了 BASE_URL/账号)
- 只用 selenium 原生; 依次尝试 Edge → Chrome, 都拉不起来则返回 None (调用方 skip)
- 全程无头, 不联网 (只加载本地 file://), 静默失败不抛栈
"""
from __future__ import annotations

from typing import Optional


def get_driver() -> Optional[object]:
    """
    返回一个可用的无头 WebDriver, 失败返回 None。
    调用方拿到 None 应把自愈套件标为 SKIPPED (可移植环境降级)。
    """
    for factory in (_make_edge, _make_chrome):
        try:
            drv = factory()
            if drv is not None:
                return drv
        except Exception:  # noqa: BLE001 — 环境不具备该浏览器属正常, 静默换下一个
            continue
    return None


def _make_edge():
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options as EdgeOptions
    opts = EdgeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    # Selenium 4.15+ 的 selenium-manager 自动匹配驱动
    return webdriver.Edge(options=opts)


def _make_chrome():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(options=opts)


def to_file_url(abs_path: str) -> str:
    """把本地绝对路径转成 file:// URL (Windows 反斜杠也要处理)"""
    import os
    from urllib.parse import quote
    p = os.path.abspath(abs_path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    # 保留盘符冒号, 其余按 URL 编码
    return "file://" + quote(p, safe="/:")


def quit_driver(driver) -> None:
    """安全关闭 driver"""
    try:
        if driver is not None:
            driver.quit()
    except Exception:  # noqa: BLE001
        pass
