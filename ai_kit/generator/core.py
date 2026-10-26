# -*- coding: utf-8 -*-
"""
Phase 3 AI 用例生成器 — Test Generator

两步生成法:
  Step 1: 爬页面 DOM 结构 + 读取 Page Object 方法签名 (本地)
  Step 2: LLM 生成用例清单 → 套 pytest 模板 → 落盘

使用方式 (CLI, 两个入口等价):
  python -m ai_kit.generator --page "用户管理" \
      --po-module pages.user_management_page --po-class UserManagementPage \
      --fixture user_management_page
  python -m ai_kit.generator.core ...   # 同上

规则 / 模板 / 提示词全部在 prompts.py, 本模块只负责流程。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import time
from string import Template
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 包内依赖 (按模块全名 import, 不再猜 legacy 模块名)
# ---------------------------------------------------------------------------

def _require(module_name: str):
    """导入包内必需模块; 失败时抛指名的 ImportError, 不再静默吞掉原因"""
    try:
        return importlib.import_module(module_name)
    except Exception as e:  # noqa: BLE001 — 统一转成带上下文的 ImportError
        raise ImportError(
            f"ai_kit.generator 依赖模块 {module_name!r} 导入失败: {e}") from e


_llm_client = _require("ai_kit.client")
_prompts = _require("ai_kit.generator.prompts")
_collector = _require("ai_kit.perception.collector")

chat_json = _llm_client.chat_json
llm_available = _llm_client.llm_available

# 规则 / 模板 / 提示词全部从 prompts 取, 本模块不再留副本 (改 prompts.py 即生效)
TEST_USER_PREFIXES = _prompts.TEST_USER_PREFIXES
MAX_CASES_PER_CLASS = _prompts.MAX_CASES_PER_CLASS
GENERATE_BOUNDARY_CASES = _prompts.GENERATE_BOUNDARY_CASES
GENERATE_NEGATIVE_CASES = _prompts.GENERATE_NEGATIVE_CASES
GENERATE_PLAN_SYSTEM = _prompts.GENERATE_PLAN_SYSTEM
CASE_PLAN_TEMPLATE = Template(_prompts.CASE_PLAN_TEMPLATE)
PYTEST_CODE_TEMPLATE = Template(_prompts.PYTEST_CODE_TEMPLATE)


# ---------------------------------------------------------------------------
# Step 1: 页面结构提取
# ---------------------------------------------------------------------------

def scrape_page_structure(driver, page_name: str = "") -> Dict[str, Any]:
    """在已打开的页面上采集可操作元素清单, 原样返回 inventory。

    实现在 perception.collector: 一次 JS 往返拿回裁剪过的 JSON 清单, 带多策略
    定位 / 语义角色 / 表单约束 / 表格行内按钮 / 风险标记。driver=None / 页面崩 /
    返回脏数据都由 collect 内部降级成空骨架 + notes, 这里不重复兜底。

    不要在这里自己拼 execute_script —— 老 DOM_SCRAPE_JS 是语句形式的 IIFE,
    selenium 把它当函数体执行, 永远返回 undefined, 于是每次采集都静默返回空页面,
    而 LLM 就在"这页什么都没有"的前提下生成用例。
    """
    return _collector.collect(driver, page_name=page_name)


# LLM 只需要"这页有哪些控件", 不需要 outerHTML / 备选定位串: 65 个元素各带
# 2000 字符 HTML 会把提示词直接烧穿。压成一行摘要再进模板。
_SLIM_KEYS = ("eid", "text", "tag", "role", "name", "container", "risk")
_SLIM_BLOCK_KEYS = ("constraints", "options", "actions")


def _as_dict(value: Any) -> Dict[str, Any]:
    """宽容取 dict —— slim 会被喂各种半成品, 类型不对就当空"""
    return value if isinstance(value, dict) else {}


def _as_items(value: Any) -> List[Any]:
    """宽容取 list —— 字符串有 [:n] 切片, 不拦住会被逐字符当成元素"""
    return value if isinstance(value, list) else []


def _slim_element(element: Any) -> Dict[str, Any]:
    """单个元素 -> 一行摘要 (丢 html / alternatives / aria)"""
    element = _as_dict(element)
    slim = {key: element[key] for key in _SLIM_KEYS if element.get(key)}
    for key in _SLIM_BLOCK_KEYS:
        if element.get(key):
            slim[key] = element[key]
    locator = element.get("locator") or {}
    if locator.get("value"):
        slim["locator"] = locator["value"]
    return slim


def _slim_table(table: Any) -> Dict[str, Any]:
    """表格 -> 列名 + 行数 + 行内按钮 (样例行只留 2 行)"""
    table = _as_dict(table)
    slim = _slim_element(table)
    slim["columns"] = _as_items(table.get("columns"))
    slim["row_count"] = table.get("row_count", 0)
    slim["sample_rows"] = _as_items(table.get("sample_rows"))[:2]
    slim["row_actions"] = [
        {"text": action.get("text"), "eid": action.get("eid")}
        for action in (_as_dict(a) for a in _as_items(table.get("row_actions"))[:8])
        if action.get("text")]
    return slim


def slim_page_structure(inventory: Dict[str, Any], page_name: str = "",
                        max_inputs: int = 15, max_buttons: int = 20,
                        max_tables: int = 3,
                        max_selects: int = 5) -> Dict[str, Any]:
    """采集清单 -> LLM 友好版: 按类别截断, 保留全局摘要与风险统计"""
    inventory = _as_dict(inventory)
    slim: Dict[str, Any] = {
        "title": inventory.get("title") or page_name,
        "url": inventory.get("url") or "",
        "counts": _as_dict(inventory.get("counts")),
        "risks": _as_dict(inventory.get("risks")),
        "containers": _as_dict(inventory.get("container_summary")),
        "dialogs": [{"text": d.get("text") or "", "buttons": _as_items(d.get("buttons"))}
                    for d in (_as_dict(x) for x in _as_items(inventory.get("dialogs"))[:2])],
        "inputs": [_slim_element(e) for e in _as_items(inventory.get("inputs"))[:max_inputs]],
        "buttons": [_slim_element(e) for e in _as_items(inventory.get("buttons"))[:max_buttons]],
        "selects": [_slim_element(e) for e in _as_items(inventory.get("selects"))[:max_selects]],
        "tables": [_slim_table(t) for t in _as_items(inventory.get("tables"))[:max_tables]],
    }
    if inventory.get("truncated"):
        slim["truncated"] = True
        slim["groups"] = _as_items(inventory.get("groups"))
    notes = _as_items(inventory.get("notes"))
    if notes:
        slim["notes"] = notes
    return slim


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
        print(f"  [Generator] 请先在 .env 里配好 *_API_KEY (见 .env.example)")
        return None

    # 精简页面结构 (避免 token 爆炸)
    trimmed = slim_page_structure(page_structure, page_name=page_name)

    # 模板在 prompts.CASE_PLAN_TEMPLATE (string.Template 语法, JSON 大括号无需转义)
    user_msg = CASE_PLAN_TEMPLATE.substitute(
        page_name=page_name,
        page_structure=json.dumps(trimmed, ensure_ascii=False, indent=2),
        po_methods=po_methods,
        fixture_name=fixture_name,
        prefixes=", ".join(prefixes),
        max_cases=max_cases,
        boundary_rule="是" if generate_boundary else "否",
        negative_rule="是" if generate_negative else "否",
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

    # 文件头 + 工具函数在 prompts.PYTEST_CODE_TEMPLATE, 边界/逆向标签也在 prompts 里算
    return PYTEST_CODE_TEMPLATE.substitute(
        page_name=page_name,
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
        boundary_label=_prompts.BOUNDARY_LABEL,
        negative_label=_prompts.NEGATIVE_LABEL,
        po_module=po_module,
        po_class=po_class,
        short=short,
        test_classes="\n".join(class_blocks),
    )


# ---------------------------------------------------------------------------
# Step 4: 落盘 + 校验
# ---------------------------------------------------------------------------

def save_generated_code(code: str, tests_dir: str, filename: str) -> str:
    """保存生成的测试代码, 返回文件路径"""
    gen_dir = os.path.join(tests_dir, "generated")
    os.makedirs(gen_dir, exist_ok=True)
    fpath = os.path.join(gen_dir, filename)

    # 语法校验: compile() 对代码字符串做纯内存语法编译(不落盘), fpath 用于报错定位
    try:
        compile(code, fpath, "exec")
    except SyntaxError as e:
        print(f"  [Generator] ⚠️ 语法校验失败: {e.msg} (line {e.lineno})")
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
    po_module / po_class: Page Object 类位置 (po_module 写成可 import 的全名, 如 'pages.user_management_page')
    fixture_name: conftest 里的 fixture 名
    tests_dir: 测试目录 (默认当前目录下的 tests)
    """
    # 默认值: 落到调用方 cwd 下的 tests, 不往包安装目录里写
    if not tests_dir:
        tests_dir = os.path.join(os.getcwd(), "tests")
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
            prefixes=TEST_USER_PREFIXES,
            max_cases=MAX_CASES_PER_CLASS,
            generate_boundary=GENERATE_BOUNDARY_CASES,
            generate_negative=GENERATE_NEGATIVE_CASES,
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
# CLI (命令行入口; python -m ai_kit.generator / python -m ai_kit.generator.core 都走这里)
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3 AI 用例生成器")
    parser.add_argument("--page", required=True, help="页面中文名, 如 '用户管理'")
    parser.add_argument("--po-module", required=True, help="Page Object 模块全名, 如 'pages.user_management_page'")
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
    main()
