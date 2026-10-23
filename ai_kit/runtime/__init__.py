# -*- coding: utf-8 -*-
"""
运行层 — standalone 模式用例的运行时原语

    actions : click / fill / get_text / assert_visible / assert_text ... (带失败上下文)
    waits   : 统一显式等待封装 (visibility / clickable / text / gone), 替代 time.sleep

设计: 与项目 Page Object 解耦, 只依赖 selenium; 供 renderer 的 standalone 模板调用。
"""
