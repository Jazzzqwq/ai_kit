# -*- coding: utf-8 -*-
"""
Phase 3 AI 用例生成 — Prompt 模板 + 可自定义规则
你可以改这里的任何常量来调整生成风格 / 用例数量 / 命名前缀

页面结构采集不在这个文件: 已迁到 ai_kit.perception.collector (一次 JS 往返拿回
裁剪过的 JSON 清单), generator 只负责"清单 -> 用例"。
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
# 角色 System Prompt (硬约束)
# ============================================================

GENERATE_PLAN_SYSTEM = """你是一个资深 QA 测试工程师. 根据页面结构和 Page Object 方法, 生成该模块的 pytest 测试用例.

严格约束:
1. 只能使用提供的 Page Object 方法, 禁止硬编码 .click() / .find_element() / execute_script
2. 所有测试用户必须用提供的前缀 (避免污染原数据, 方便 teardown 统一清理)
3. 每个 test 方法必须 try/finally 自我清理 (teardown 用 batch_cleanup 兜底)
4. severity 四档: BLOCKER > CRITICAL > NORMAL > MINOR
5. 用中文写 allure.story 标题
6. 输出严格 JSON, 不要 markdown 代码块标记
"""


# ============================================================
# 📋 生成清单模板 (LLM 先生成这个, 再套 pytest)
# 模板用 string.Template 语法: $name 是占位符, 正文里的裸 { } 不需要转义
# ============================================================

CASE_PLAN_TEMPLATE = """## 页面名称
$page_name

## 页面结构
```json
$page_structure
```

## Page Object 方法签名 (只能用这些)
```
$po_methods
```

## fixture
- driver
- $fixture_name: Page Object 实例 (已登录)

## 用例规则
- 测试用户前缀: $prefixes
- 每个类最多 $max_cases 个用例
- 边界值测试: $boundary_rule
- 逆向测试: $negative_rule

## 输出 JSON 格式
{"classes": [{"class_name": "Test{Module}{Category}", "category": "",
"cases": [{"method": "test_xxx", "title": "", "severity": "",
"steps": ["..."], "expected": ["..."]}}]}]}
"""


# ============================================================
# 🧪 pytest 代码模板 (按清单生成)
# ============================================================

PYTEST_CODE_TEMPLATE = '''# -*- coding: utf-8 -*-
"""
AI 自动生成的测试用例 — $page_name
生成时间: $timestamp
生成规则: Phase 3 ($boundary_label, $negative_label)
⚠️ 注意: 方法体里有 TODO, 需要你手动填 Page Object 调用和断言
"""
import time
import uuid

import allure
import pytest

from pages.$po_module import $po_class


def _fresh_$short(page, prefix):
    """创建一个新用户并确保表格里能查到"""
    suffix = uuid.uuid4().hex[:3]
    username = f"{prefix}_{suffix}"
    page.add_user(username, "Test@123", phone="13900000000")
    time.sleep(0.2)
    page.query_by_username(username)
    row = page.find_row_by_username(username)
    return username, row


$test_classes
'''

BOUNDARY_LABEL = "含边界值" if GENERATE_BOUNDARY_CASES else "不含边界值"
NEGATIVE_LABEL = "含逆向测试" if GENERATE_NEGATIVE_CASES else "不含逆向测试"
