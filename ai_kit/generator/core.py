# -*- coding: utf-8 -*-
"""
Phase 3 AI 用例生成器 — Test Generator

两步生成法:
  Step 1: 爬页面 DOM 结构 + 读取 Page Object 方法签名 (本地)
  Step 2: LLM 生成用例清单 → 套 pytest 模板 → 落盘

使用方式 (CLI):
  python -m ai_kit.generator.core --page "用户管理" \
      --po-module pages.user_management_page --po-class UserManagementPage \
      --fixture user_management_page
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 动态 import Phase 2 模块
# ---------------------------------------------------------------------------

def _try_import(name: str, alt_name: Optional[str] = None):
    for mod_name in [name, alt_name or f"pages.{name}"]:
        try:
            return importlib.import_module(mod_name)
        except Exception:
            continue
    return None


_llm_client = (_try_import("llm_client") or _try_import("ai_kit.client"))
_gen_prompts = (_try_import("generator_prompts") or _try_import("ai_kit.generator.prompts"))

if _llm_client is None:
    raise ImportError("Phase 3 需要 LLM 客户端可用 (ai_kit.client 或 llm_client)")

chat_json = _llm_client.chat_json
llm_available = _llm_client.llm_available

DOM_SCRAPE_JS = (_gen_prompts.DOM_SCRAPE_JS if _gen_prompts else "")


# ---------------------------------------------------------------------------
# Step 1: 页面结构提取
# ---------------------------------------------------------------------------

def scrape_page_structure(driver, page_name: str = "") -> Dict[str, Any]:
    """在已打开的页面上执行 JS, 返回可操作元素摘要"""
    if not DOM_SCRAPE_JS:
        return {"title": page_name, "inputs": [], "buttons": [], "tables": [], "selects": []}
    try:
        raw = driver.execute_script(DOM_SCRAPE_JS)
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
    except Exception as e:
        print(f"  [Generator] scrape_page_structure JS 执行失败: {e}")
    return {"title": page_name, "inputs": [], "buttons": [], "tables": [], "selects": []}


def extract_po_methods(po_class_or_instance) -> str:
    """从 Page Object 类提取公开方法签名"""
    methods = []
    for name in dir(po_class_or_instance):
        if name.startswith("_"):
            continue
        attr = getattr(po_class_or_instance, name)
        if callable(attr):
            try:
                import inspect
                sig = inspect.signature(attr)
                methods.append(f"  def {name}{sig}:")
            except (ValueError, TypeError):
                methods.append(f"  def {name}(...):")
    return "\n".join(methods[:60])  # 上限 60 个方法


# ---------------------------------------------------------------------------
# Step 2: LLM 生成用例清单
# ---------------------------------------------------------------------------

GENERATE_PLAN_SYSTEM = """你是一个资深 QA 测试工程师. 根据页面结构和 Page Object 方法, 生成该模块的 pytest 测试用例.

严格约束:
1. 只能使用提供的 Page Object 方法, 禁止硬编码 .click() / .find_element() / execute_script
2. 所有测试用户必须用提供的前缀 (避免污染原数据, 方便 teardown 统一清理)
3. 每个 test 方法必须 try/finally 自我清理 (teardown 用 batch_cleanup 兜底)
4. severity 四档: BLOCKER > CRITICAL > NORMAL > MINOR
5. 用中文写 allure.story 标题
6. 输出严格 JSON, 不要 markdown 代码块标记
"""


def generate_case_plan(page_structure: Dict[str, Any],
                       po_methods: str,
                       page_name: str,
                       fixture_name: str,
                       prefixes: List[str],
                       max_cases: int = 8,
                       generate_boundary: bool = True,
                       generate_negative: bool = True) -> Optional[Dict]:
    """调 LLM 生成用例清单 JSON"""
    if not llm_available():
        print(f"  [Generator] ❌ LLM 未配置, 无法生成用例")
        print(f"  [Generator] 请先在 ai_kit/config.py 填 API Key")
        return None

    # 精简页面结构 (避免 token 爆炸)
    trimmed = {
        "title": page_structure.get("title", page_name),
        "inputs": page_structure.get("inputs", [])[:15],
        "buttons": page_structure.get("buttons", [])[:20],
        "tables": page_structure.get("tables", [])[:3],
        "selects": page_structure.get("selects", [])[:5],
    }

    user_msg = (
        f"## 页面名称\n{page_name}\n\n"
        f"## 页面结构\n```json\n{json.dumps(trimmed, ensure_ascii=False, indent=2)}\n```\n\n"
        f"## Page Object 方法签名 (只能用这些)\n```\n{po_methods}\n```\n\n"
        f"## fixture\n- driver\n- {fixture_name}: Page Object 实例 (已登录)\n\n"
        f"## 用例规则\n"
        f"- 测试用户前缀: {', '.join(prefixes)}\n"
        f"- 每个类最多 {max_cases} 个用例\n"
        f"- 边界值测试: {'是' if generate_boundary else '否'}\n"
        f"- 逆向测试: {'是' if generate_negative else '否'}\n\n"
        f"## 输出 JSON 格式\n"
        f'{{"classes": [{{"class_name": "Test{Module}{Category}", "category": "", '
        f'"cases": [{{"method": "test_xxx", "title": "", "severity": "", '
        f'"steps": ["..."], "expected": ["..."]}}]}}]}}'
    )

    print(f"  [Generator] 🧠 调 LLM 生成用例清单 (约 5~10s)...")
    plan = chat_json(GENERATE_PLAN_SYSTEM, user_msg, max_retries=1)
    if plan is None:
        print(f"  [Generator] ❌ LLM 返回 None")
        return None

    classes = plan.get("classes", [])
    total_cases = sum(len(c.get("cases", [])) for c in classes)
    print(f"  [Generator] ✅ LLM 返回 {len(classes)} 个类, {total_cases} 个用例")
    return plan


# ---------------------------------------------------------------------------
# Step 3: 清单 → pytest 代码
# ---------------------------------------------------------------------------

def plan_to_pytest_code(plan: Dict,
                        page_name: str,
                        po_module: str,
                        po_class: str,
                        fixture_name: str,
                        short_name: str = "") -> str:
    """把 LLM 生成的清单转成可运行的 pytest 代码"""
    short = short_name or po_class.replace("Page", "").lower()

    class_blocks: List[str] = []
    for cls in plan.get("classes", []):
        class_name = cls.get("class_name", f"Test{po_class}")
        # 确保 Test 前缀
        if not class_name.startswith("Test"):
            class_name = "Test" + class_name

        cases_code: List[str] = []
        for case in cls.get("cases", []):
            method = case.get("method", "test_unnamed")
            if not method.startswith("test_"):
                method = "test_" + method
            title = case.get("title", "")
            severity = case.get("severity", "NORMAL")
            steps = case.get("steps", [])
            expected = case.get("expected", [])

            steps_str = "\n".join(f"            # step: {s}" for s in steps[:5])
            expected_str = "\n".join(f"            # expect: {e}" for e in expected[:5])

            # 生成一个最小骨架 — 让用户填充 Page Object 方法调用
            case_block = f'''
    @allure.story({title!r})
    @allure.severity(allure.severity_level.{severity})
    def {method}(self, {fixture_name}):
        """{title}"""
        try:
{steps_str}
            # TODO: 在这里填 Page Object 方法调用
            # fixture_name.add_user(...) / fixture_name.query_by_username(...) / ...
{expected_str}
            # TODO: 在这里填断言
            # assert xxx, "说明"
            pass
        finally:
            pass  # teardown 由 class-level batch_cleanup 统一处理
'''
            cases_code.append(case_block)

        class_block = f'''
class {class_name}:
    """{page_name} — {cls.get("category", "all")}"""
{"".join(cases_code)}
'''
        class_blocks.append(class_block)

    # 组装最终文件
    boundary_label = "含边界值" if _gen_prompts and _gen_prompts.GENERATE_BOUNDARY_CASES else "不含边界值"
    negative_label = "含逆向测试" if _gen_prompts and _gen_prompts.GENERATE_NEGATIVE_CASES else "不含逆向测试"

    header = f'''# -*- coding: utf-8 -*-
"""
AI 自动生成的测试用例 — {page_name}
生成时间: {time.strftime("%Y-%m-%d %H:%M:%S")}
生成规则: Phase 3 ({boundary_label}, {negative_label})
⚠️ 注意: 方法体里有 TODO, 需要你手动填 Page Object 调用和断言
"""
import time
import uuid

import allure
import pytest

from pages.{po_module} import {po_class}


def _fresh_{short}(page, prefix):
    """创建一个新用户并确保表格里能查到"""
    suffix = uuid.uuid4().hex[:3]
    username = f"{{prefix}}_{{suffix}}"
    page.add_user(username, "Test@123", phone="13900000000")
    time.sleep(0.2)
    page.query_by_username(username)
    row = page.find_row_by_username(username)
    return username, row


'''
    return header + "\n".join(class_blocks) + "\n"


# ---------------------------------------------------------------------------
# Step 4: 落盘 + 校验
# ---------------------------------------------------------------------------

def save_generated_code(code: str, tests_dir: str, filename: str) -> str:
    """保存生成的测试代码, 返回文件路径"""
    gen_dir = os.path.join(tests_dir, "generated")
    os.makedirs(gen_dir, exist_ok=True)
    fpath = os.path.join(gen_dir, filename)

    # 语法校验: 用 compile() 对代码字符串做纯内存语法编译(不落盘)
    try:
        compile(code, fpath, "exec")
    except SyntaxError as e:
        print(f"  [Generator] ⚠️ 语法校验警告: {e}")
        print(f"  [Generator] 代码可能有问题, 请手动检查: {fpath}")

    with open(fpath, "w", encoding="utf-8") as f:
        f.write(code)
    print(f"  [Generator] ✅ 已保存: {fpath}")
    print(f"  [Generator] 运行: pytest tests/generated/{filename} -v -s")
    return fpath


# ---------------------------------------------------------------------------
# 主入口 (命令行 + 编程调用)
# ---------------------------------------------------------------------------

def generate(driver=None,
             page_name: str = "",
             po_module: str = "",
             po_class: str = "",
             fixture_name: str = "",
             tests_dir: str = "",
             use_llm: bool = True) -> Optional[str]:
    """
    一步生成 pytest 测试文件.
    driver: Selenium WebDriver (可选, 传了就爬 DOM)
    page_name: 中文页面名
    po_module / po_class: Page Object 类位置
    fixture_name: conftest 里的 fixture 名
    tests_dir: 测试目录 (默认 web_test_framework/tests)
    """
    # 默认值
    if not tests_dir:
        tests_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "tests")
    if not fixture_name:
        fixture_name = po_class.replace("Page", "_page").lower() if po_class else "page"

    print(f"\n{'='*60}")
    print(f"🧠 Phase 3 AI 用例生成器")
    print(f"{'='*60}")
    print(f"  页面: {page_name}")
    print(f"  Page Object: {po_module}.{po_class}")
    print(f"  Fixture: {fixture_name}")
    print(f"  LLM 可用: {llm_available()}")

    # Step 1: 页面结构 + PO 方法
    page_structure = {}
    if driver is not None:
        print(f"\n  [Step 1] 爬页面 DOM 结构...")
        page_structure = scrape_page_structure(driver, page_name)
        print(f"    inputs={len(page_structure.get('inputs', []))}, "
              f"buttons={len(page_structure.get('buttons', []))}, "
              f"tables={len(page_structure.get('tables', []))}")
    else:
        print(f"\n  [Step 1] 没传 driver, 跳过 DOM 爬取 (LLM 只能用 Page Object 方法签名生成)")

    print(f"  [Step 1] 提取 Page Object 方法签名...")
    po_instance = None
    try:
        po_mod = importlib.import_module(po_module)
        po_cls = getattr(po_mod, po_class)
        po_methods = extract_po_methods(po_cls)
        print(f"    {len(po_methods.splitlines())} 个公开方法")
    except Exception as e:
        print(f"    ❌ 加载 Page Object 失败: {e}")
        po_methods = "# (无法加载, 请手动填 Page Object 方法签名)"

    # Step 2: LLM 生成清单
    if not use_llm or not llm_available():
        print(f"\n  [Step 2] 跳过 LLM (不可用), 生成最小骨架")
        plan = _minimal_plan(po_module, po_class)
    else:
        plan = generate_case_plan(
            page_structure=page_structure,
            po_methods=po_methods,
            page_name=page_name,
            fixture_name=fixture_name,
            prefixes=_gen_prompts.TEST_USER_PREFIXES if _gen_prompts else ["gen_"],
            max_cases=_gen_prompts.MAX_CASES_PER_CLASS if _gen_prompts else 8,
            generate_boundary=_gen_prompts.GENERATE_BOUNDARY_CASES if _gen_prompts else True,
            generate_negative=_gen_prompts.GENERATE_NEGATIVE_CASES if _gen_prompts else True,
        )
        if plan is None:
            print(f"  [Step 2] ❌ LLM 生成失败, 生成最小骨架")
            plan = _minimal_plan(po_module, po_class)

    # Step 3: 清单 → pytest 代码
    print(f"\n  [Step 3] 清单 → pytest 代码...")
    short_name = po_class.replace("Page", "").lower() if po_class else "module"
    code = plan_to_pytest_code(
        plan=plan,
        page_name=page_name,
        po_module=po_module,
        po_class=po_class,
        fixture_name=fixture_name,
        short_name=short_name,
    )
    print(f"    {len(code)} 字符, {code.count('class Test')} 个类")

    # Step 4: 落盘
    print(f"\n  [Step 4] 落盘...")
    filename = f"test_generated_{short_name}.py"
    fpath = save_generated_code(code, tests_dir, filename)

    print(f"\n{'='*60}")
    print(f"✅ 生成完成!")
    print(f"   文件: {fpath}")
    print(f"   注意: 方法体里有 TODO, 需要手动填 Page Object 调用")
    print(f"{'='*60}\n")
    return fpath


def _minimal_plan(po_module: str, po_class: str) -> Dict:
    """LLM 不可用时的最小骨架"""
    return {
        "classes": [{
            "class_name": f"Test{po_class}Generated",
            "category": "all",
            "cases": [
                {"method": "test_smoke", "title": "冒烟测试",
                 "severity": "CRITICAL", "steps": ["打开页面"], "expected": ["页面加载成功"]},
                {"method": "test_query_all", "title": "查询-全部",
                 "severity": "NORMAL", "steps": ["点击查询"], "expected": ["返回结果"]},
            ],
        }],
    }


# ---------------------------------------------------------------------------
# CLI (可选, 让你能命令行直接跑)
# ---------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(description="Phase 3 AI 用例生成器")
    parser.add_argument("--page", required=True, help="页面中文名, 如 '用户管理'")
    parser.add_argument("--po-module", required=True, help="Page Object 模块名, 如 'pages.user_management_page'")
    parser.add_argument("--po-class", required=True, help="Page Object 类名, 如 'UserManagementPage'")
    parser.add_argument("--fixture", required=True, help="fixture 名, 如 'user_management_page'")
    parser.add_argument("--tests-dir", default="", help="tests 目录路径")
    args = parser.parse_args()

    generate(
        driver=None,  # CLI 模式不连浏览器, LLM 只靠 PO 方法签名生成
        page_name=args.page,
        po_module=args.po_module,
        po_class=args.po_class,
        fixture_name=args.fixture,
        tests_dir=args.tests_dir,
        use_llm=True,
    )


if __name__ == "__main__":
    _cli()
