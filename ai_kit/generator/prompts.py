# -*- coding: utf-8 -*-
"""
Phase 3 AI 用例生成 — Prompt 模板 + 可自定义规则
你可以改这里的任何常量来调整生成风格 / 用例数量 / 命名前缀
"""
from __future__ import annotations

from typing import List, Dict


# ============================================================
# 🎨 用例规则 (改这里控制生成风格)
# ============================================================

# 测试用户前缀 — 所有自动生成的测试用户必须用这些前缀
TEST_USER_PREFIXES: List[str] = [
    "auto_",   # 自动化基础用例
    "del_",    # 删除用例
    "keep_",   # 种子用户 (teardown 不删, 后续用例复用)
    "gen_",    # Phase 3 自动生成的用例专用前缀
]

# 每个类生成的最多用例数 (太多会超 token, 生成变慢)
MAX_CASES_PER_CLASS: int = 8

# 是否生成边界值测试 (空输入 / 超长输入 / 特殊字符)
GENERATE_BOUNDARY_CASES: bool = True

# 是否生成逆向测试 (必填项为空 / 非法参数)
GENERATE_NEGATIVE_CASES: bool = True

# fixture 命名规则 — Phase 2 已有的, Phase 3 生成时复用
EXISTING_FIXTURES: List[str] = [
    "driver",
    "logged_in_driver",
    "user_management_page",
    "history_page",
    "device_list_page",
    "operator_list_page",
    "telemetry_page",
    "edit_patient_page",
]

# 允许跳过的用例前缀 (比如弹窗复杂 / 需要上传文件, 暂时跳过)
SKIP_CASE_PATTERNS: List[str] = [
    "upload",       # 文件上传
    "download",     # 文件下载 (需要处理下载目录)
    "iframe",       # iframe 内部
]


# ============================================================
# 📋 生成清单模板 (LLM 先生成这个, 再套 pytest)
# ============================================================

CASE_PLAN_TEMPLATE = """
你是一个专业的测试工程师. 根据页面结构, 生成该模块的测试用例清单 (步骤 + 预期).

## 页面名称
{page_name}

## Page Object 方法签名 (只能用这些方法, 不能硬编码 CSS)
{po_methods}

## fixture
- driver: webdriver
- {fixture_name}: Page Object 实例 (已登录)

## 用例规则
- 必须用这些测试用户前缀: {prefixes}
- 每个类最多 {max_cases} 个用例
{"- 生成边界值测试 (空/超长/特殊字符)" if GENERATE_BOUNDARY_CASES else "- 不生成边界值测试"}
{"- 生成逆向测试 (非法参数)" if GENERATE_NEGATIVE_CASES else "- 不生成逆向测试"}

## 输出格式 (严格 JSON)
{{
  "classes": [
    {{
      "class_name": "Test{ModuleName}{Category}",
      "category": "add|query|edit|delete|reset|export",
      "cases": [
        {{
          "method": "test_{action}_{desc}",
          "title": "中文故事标题",
          "severity": "BLOCKER|CRITICAL|NORMAL|MINOR",
          "steps": ["步骤1", "步骤2"],
          "expected": ["预期结果1", "预期结果2"]
        }}
      ]
    }}
  ]
}}
"""


# ============================================================
# 🧪 pytest 代码模板 (按清单生成)
# ============================================================

PYTEST_CODE_TEMPLATE = '''# -*- coding: utf-8 -*-
"""
AI 自动生成的测试用例 — {page_name}
生成时间: {timestamp}
生成规则: Phase 3 ({boundary_label}, {negative_label})
"""
import time
import uuid

import allure
import pytest

from pages.{po_module} import {po_class}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _fresh_{short}(page, prefix):
    """创建一个新用户并确保表格里能查到"""
    suffix = uuid.uuid4().hex[:3]
    username = f"{{prefix}}_{{suffix}}"
    page.add_user(username, "Test@123", phone="13900000000")
    time.sleep(0.2)
    page.query_by_username(username)
    row = page.find_row_by_username(username)
    return username, row


# ===========================================================================
{test_classes}
'''

BOUNDARY_LABEL = "含边界值" if GENERATE_BOUNDARY_CASES else "不含边界值"
NEGATIVE_LABEL = "含逆向测试" if GENERATE_NEGATIVE_CASES else "不含逆向测试"


# ============================================================
# Step 1: 爬页面结构的 JS 模板
# ============================================================

DOM_SCRAPE_JS = """
// 返回当前页面的可操作元素摘要 (给 LLM 生成用例用)
(function() {
    var result = { title: document.title, url: location.href };
    
    // 1) 表单输入
    var inputs = document.querySelectorAll('input[type=text], input:not([type]), input[type=password], textarea, select');
    result.inputs = Array.from(inputs).map(function(el) {
        return {
            name: el.name || '',
            placeholder: el.placeholder || '',
            type: el.type || el.tagName.toLowerCase(),
            id: el.id || '',
            class: (el.className || '').toString().slice(0, 60),
            required: el.required || false
        };
    });
    
    // 2) 按钮 (有文字的)
    var btns = document.querySelectorAll('button, [role=button], a.btn, .el-button');
    result.buttons = Array.from(btns).filter(function(b) {
        return (b.textContent || '').trim().length > 0 && (b.offsetWidth > 0 || b.offsetHeight > 0);
    }).map(function(b) {
        return {
            text: (b.textContent || '').trim().slice(0, 40),
            class: (b.className || '').toString().slice(0, 80),
            id: b.id || ''
        };
    });
    
    // 3) 表格
    var tables = document.querySelectorAll('table, .el-table');
    result.tables = Array.from(tables).map(function(t) {
        var headers = t.querySelectorAll('th, thead th, .el-table th .cell');
        return {
            class: (t.className || '').toString().slice(0, 80),
            headers: Array.from(headers).map(function(h) { return (h.textContent || '').trim().slice(0, 30); }).filter(Boolean)
        };
    });
    
    // 4) 下拉/选择器
    var selects = document.querySelectorAll('select, .el-select, .el-dropdown');
    result.selects = Array.from(selects).map(function(s) {
        return {
            class: (s.className || '').toString().slice(0, 80),
            options: Array.from(s.querySelectorAll('option, .el-select-dropdown__item')).slice(0, 10).map(function(o) {
                return (o.textContent || '').trim().slice(0, 30);
            }).filter(Boolean)
        };
    });
    
    return JSON.stringify(result, null, 2);
})();
"""
