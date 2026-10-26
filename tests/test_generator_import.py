# -*- coding: utf-8 -*-
"""
任务 2 回归测试: generator 死代码 / 导入路径 / CLI 入口

守四件事:
1. core.py 只 import 包内真模块 (ai_kit.client / ai_kit.generator.prompts),
   不再回落到 legacy 顶层名 llm_client / generator_prompts, 也不再猜 pages.*
2. `python -m ai_kit.generator` 与 `python -m ai_kit.generator.core` 的 --help 都能跑
3. LLM 清单模板能真正渲染 (旧版 f-string 里的 {Module}{Category} 会直接 NameError,
   导致唯一的真实生成路径必然崩溃)
4. 语法校验用 compile(code, path, "exec"), 不用 py_compile.compile("", code)
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import subprocess
import sys
from string import Template

import pytest

from ai_kit import client as ai_client
from ai_kit.generator import core as gcore
from ai_kit.generator import prompts

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GEN_DIR = os.path.join(REPO_ROOT, "ai_kit", "generator")

# 可移植硬约束: 本包绝不 import 具体项目的包
FORBIDDEN_TOPS = {"pages", "common", "llm_client", "generator_prompts"}


def _gen_sources():
    for name in sorted(os.listdir(GEN_DIR)):
        if name.endswith(".py"):
            with open(os.path.join(GEN_DIR, name), encoding="utf-8") as f:
                yield name, f.read()


# ---------------------------------------------------------------------------
# 1. 导入路径
# ---------------------------------------------------------------------------

def test_core_has_no_legacy_import_fallbacks():
    src = inspect.getsource(gcore)
    assert "_try_import" not in src, "legacy _try_import 兜底应已删除"
    # 词边界匹配: 不能把合法的 _llm_client 变量名当成 legacy 模块名
    assert not re.search(r"(?<![\w.])llm_client\b", src), "不应再回落 legacy 顶层模块 llm_client"
    assert "generator_prompts" not in src, "不应再回落 legacy 顶层模块 generator_prompts"


def test_core_binds_canonical_modules():
    assert gcore._llm_client is ai_client
    assert gcore._prompts is prompts
    assert gcore.chat_json is ai_client.chat_json
    assert gcore.llm_available is ai_client.llm_available


def test_generator_package_imports_only_allowed_modules():
    """AST 逐 import 检查: 不 import 项目私有包 / legacy 顶层名 (模板字符串里的 import 不算)"""
    for name, src in _gen_sources():
        tree = ast.parse(src, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tops = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                tops = [(node.module or "").split(".")[0]]
            else:
                continue
            for top in tops:
                assert top not in FORBIDDEN_TOPS, f"{name} 里出现了禁止的 import: {top}"


# ---------------------------------------------------------------------------
# 2. CLI 入口
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mod", ["ai_kit.generator", "ai_kit.generator.core"])
def test_cli_help_exits_zero(mod):
    proc = subprocess.run(
        [sys.executable, "-m", mod, "--help"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Phase 3 AI 用例生成器" in proc.stdout
    assert "--po-module" in proc.stdout


# ---------------------------------------------------------------------------
# 3. 模板渲染 (旧版 NameError 的护栏)
# ---------------------------------------------------------------------------

def test_prompts_constants_are_live_not_copied():
    assert gcore.TEST_USER_PREFIXES is prompts.TEST_USER_PREFIXES
    assert gcore.MAX_CASES_PER_CLASS == prompts.MAX_CASES_PER_CLASS
    assert gcore.GENERATE_PLAN_SYSTEM is prompts.GENERATE_PLAN_SYSTEM
    assert isinstance(gcore.CASE_PLAN_TEMPLATE, Template)
    assert isinstance(gcore.PYTEST_CODE_TEMPLATE, Template)


def test_generate_case_plan_renders_prompt_without_name_error(monkeypatch):
    captured = {}

    def fake_chat_json(system, user, max_retries=2):
        captured["system"], captured["user"] = system, user
        return {"classes": [{"class_name": "TestX", "category": "add", "cases": [
            {"method": "test_x", "title": "t", "severity": "NORMAL",
             "steps": ["s"], "expected": ["e"]}]}]}

    monkeypatch.setattr(gcore, "llm_available", lambda: True)
    monkeypatch.setattr(gcore, "chat_json", fake_chat_json)
    plan = gcore.generate_case_plan({"title": "X"}, "  def add_user(u):",
                                    "页面", "fx", ["gen_"])

    assert plan is not None, "LLM 路径不应再抛 NameError (旧版 {Module}{Category})"
    prompt = captured["user"]
    assert "页面" in prompt and "gen_" in prompt
    assert "边界值测试: 是" in prompt and "逆向测试: 是" in prompt
    assert "$page_name" not in prompt and "$po_methods" not in prompt
    # JSON 输出格式里的裸大括号必须原样保留 (string.Template 不需要转义)
    assert '"classes": [{"class_name"' in prompt


def test_minimal_plan_to_code_compiles_and_has_no_placeholder():
    code = gcore.plan_to_pytest_code(
        gcore._minimal_plan("user_management_page", "UserManagementPage"),
        page_name="用户管理", po_module="user_management_page",
        po_class="UserManagementPage", fixture_name="user_management_page",
    )
    compile(code, "<generated>", "exec")     # 语法必须合法
    assert "from pages.user_management_page import UserManagementPage" in code
    assert "def _fresh_usermanagement(page, prefix)" in code
    assert "$" not in code, "模板占位符没被替换干净"
    assert "class TestUserManagementPageGenerated:" in code


# ---------------------------------------------------------------------------
# 4. 语法校验用 compile() 而非 py_compile
# ---------------------------------------------------------------------------

def test_syntax_check_uses_compile_with_path():
    src = inspect.getsource(gcore.save_generated_code)
    assert 'compile(code, fpath, "exec")' in src
    assert "py_compile" not in src


def test_save_generated_code_writes_file_and_reports_syntax_error(tmp_path, capsys):
    path = gcore.save_generated_code("def f(:\n", str(tmp_path), "broken.py")
    out = capsys.readouterr().out
    assert "语法校验失败" in out
    assert os.path.isfile(path), "语法有问题也要落盘, 由人工判断"
