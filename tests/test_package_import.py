# -*- coding: utf-8 -*-
"""
包化守门: ai_kit 必须是自洽的独立包 (任务卡验收标准固化为测试)

    `python -c "import ai_kit"` 干净 —— 不拉起任何项目模块
    依赖预算 —— stdlib + selenium + requests, 不多一个

为什么值得写成测试而不是人工检查一次: 只要有人在 healer 里顺手写一行
`from pages.login_page import LoginPage`, 包就重新绑回具体项目了, 而
"import 能过" 这种检查在本地环境里永远是绿的 (项目就在旁边)。

运行: pytest tests/test_package_import.py
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from functools import lru_cache
from importlib import metadata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = REPO_ROOT / "ai_kit"

SELF = "ai_kit"
BUDGET_THIRD_PARTY = {"selenium", "requests"}

pytestmark = pytest.mark.skipif(
    not hasattr(sys, "stdlib_module_names"),
    reason="需要 Python 3.10+ 的 sys.stdlib_module_names 才能区分 stdlib 与项目模块",
)


def _stdlib() -> set:
    return set(sys.stdlib_module_names)


def _iter_sources():
    for dirpath, dirnames, filenames in os.walk(PKG_DIR):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name


def _top_level_imports(path: Path) -> set:
    """收集一个源文件里所有绝对 import 的顶层模块名 (相对 import 天然包内闭环, 跳过)"""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # from . import x / from ..runtime import y
                continue
            if node.module:
                found.add(node.module.split(".")[0])
    return found


def _run_in_foreign_cwd(tmp_path: Path, statement: str,
                       repo_root: Path = REPO_ROOT) -> dict:
    """在临时目录里跑一段 import 语句, 返回 {模块名: __file__}。

    cwd 故意挪走: 否则 `import ai_kit` 能靠当前目录命中, 就验证不出
    "装到任意环境也能导入" 这件事; 仓库根只通过 PYTHONPATH 暴露。
    """
    code = ("import sys, json\n" + statement +
            "\nprint(json.dumps({k: getattr(v, '__file__', None)"
            " for k, v in list(sys.modules.items())}))")
    env = dict(os.environ, PYTHONPATH=str(repo_root))
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"子进程导入失败:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _foreign_modules(loaded: dict) -> dict:
    """预算外的顶层模块: {模块名: [它的发行包... 或说明]}

    "预算内" = stdlib ∪ selenium/requests 的**传递**依赖闭包 —— 光看顶层包名不够,
    import ai_kit.client 会顺带拉起 requests 自己的 urllib3 / certifi / charset_normalizer。

    没有发行包归属的顶层模块分两种，必须分开处理:
      - 住在 site-packages 里（C 扩展造的 cython_runtime 之类）→ 第三方内部产物，放行
      - 住在别处（仓库根下的 pages / common）→ 就是宿主项目模块，报红
    后者才是这张卡真正要防的东西。
    """
    stdlib = _stdlib()
    dist_map = metadata.packages_distributions()
    allowed = _allowed_distributions()

    foreign = {}
    for name in loaded:
        top = name.split(".")[0]
        if name.startswith("_") or top in stdlib or top == SELF:
            continue
        dists = dist_map.get(top)
        if not dists:
            path = (loaded.get(top) or "").replace("\\", "/")
            if path and "site-packages" not in path and "dist-packages" not in path:
                foreign[top] = ["无发行包归属, 也不在 site-packages 里 (宿主项目模块?)"]
            continue
        bad = [dist for dist in dists if _canonical(dist) not in allowed]
        if bad:
            foreign[top] = bad
    return foreign


def _canonical(name: str) -> str:
    """发行包名归一: charset_normalizer / charset-normalizer / Charset.Normalizer 等价"""
    return re.sub(r"[-_.]+", "-", name).lower()


@lru_cache(maxsize=1)
def _allowed_distributions(roots: tuple = ("selenium", "requests")) -> frozenset:
    """从依赖预算的根包出发, 递归收集整棵依赖树 (含 extra 里的可选依赖)"""
    seen = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        key = _canonical(name)
        if key in seen:
            continue
        seen.add(key)
        try:
            specs = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            continue
        for spec in specs:
            dep = spec.split(";")[0].strip()          # 去掉 extra / python_version 条件
            if not dep:
                continue
            dep_name = re.split(r"[<>=!~\[ (]", dep, maxsplit=1)[0].strip()
            if dep_name:
                stack.append(dep_name)
    return frozenset(seen)


# ---------------------------------------------------------------------------
# 静态: 源码里不许出现预算外的顶层 import
# ---------------------------------------------------------------------------

def test_sources_import_only_stdlib_and_budget_deps():
    allowed = _stdlib() | BUDGET_THIRD_PARTY | {SELF}
    offenders = {}

    for path in _iter_sources():
        bad = _top_level_imports(path) - allowed
        if bad:
            offenders[str(path.relative_to(REPO_ROOT))] = sorted(bad)

    assert not offenders, (
        "ai_kit 只能依赖 stdlib + "
        f"{sorted(BUDGET_THIRD_PARTY)}; 以下文件越界: {offenders}")


def test_sources_never_reach_into_a_host_project():
    """常见的项目包名一眼就能认出来 —— 专门给一条直白的断言, 报错更好懂"""
    project_like = {"pages", "page_objects", "common", "conftest", "base_page",
                    "business", "fixtures", "utils_project"}
    offenders = {}

    for path in _iter_sources():
        bad = _top_level_imports(path) & project_like
        if bad:
            offenders[str(path.relative_to(REPO_ROOT))] = sorted(bad)

    assert not offenders, f"可移植包里不能 import 宿主项目模块: {offenders}"


# ---------------------------------------------------------------------------
# 动态: 真跑一次 `import ai_kit`
# ---------------------------------------------------------------------------

def test_import_ai_kit_pulls_no_project_modules(tmp_path):
    loaded = _run_in_foreign_cwd(tmp_path, "import ai_kit")

    foreign = _foreign_modules(loaded)
    assert not foreign, f"import ai_kit 拉起了预算外发行包: {foreign}"

    assert "ai_kit" in loaded


def test_top_level_package_stays_lazy(tmp_path):
    """`import ai_kit` 只该付出读一个 __init__ 的代价。

    一旦顶层文件 import 了 healer/client, 每个只想用 metrics 的调用方都得先
    把 selenium 装上、把规则包 JSON 读一遍。要补顶层 API 再导出时, 请改成
    在函数内部延迟导入, 或者明确修改这条测试并说明代价。
    """
    loaded = _run_in_foreign_cwd(tmp_path, "import ai_kit")

    pulled = {name.split(".")[0] for name in loaded} & BUDGET_THIRD_PARTY
    assert not pulled, f"顶层包不该拉起第三方依赖: {sorted(pulled)}"

    submodules = [name for name in loaded if name.startswith(SELF + ".")]
    assert not submodules, f"顶层包不该顺带导入子模块: {submodules}"


def test_documented_submodules_import_with_budget_deps_only(tmp_path):
    """每个文档里承诺的模块都得能独立 import (只靠预算内依赖)"""
    modules = [
        "ai_kit.client",
        "ai_kit.config",
        "ai_kit.analyzer",
        "ai_kit.healer",
        "ai_kit.evaluation.metrics",
        "ai_kit.evaluation.cost_tracker",
        "ai_kit.evaluation.driver_provider",
        "ai_kit.evaluation.eval_healer",
        "ai_kit.evaluation.eval_generator",
        "ai_kit.evaluation.eval_analyzer",
        "ai_kit.evaluation.run_eval",
        "ai_kit.generator.core",
        "ai_kit.generator.prompts",
        "ai_kit.profiles",
        "ai_kit.perception",
        "ai_kit.perception.prompts",
        "ai_kit.proposal",
        "ai_kit.proposal.diagnoser",
        "ai_kit.proposal.solutions",
        "ai_kit.runtime",
        "ai_kit.utils",
    ]

    loaded = _run_in_foreign_cwd(
        tmp_path, "import importlib\n" + "\n".join(
            f"importlib.import_module({name!r})" for name in modules))

    foreign = _foreign_modules(loaded)
    assert not foreign, f"子模块 import 拉起了预算外发行包: {foreign}"


# ---------------------------------------------------------------------------
# 门面元信息
# ---------------------------------------------------------------------------

def test_package_exposes_version_and_all():
    import ai_kit

    assert isinstance(ai_kit.__version__, str) and ai_kit.__version__.strip()
    assert isinstance(ai_kit.__all__, list)


def test_package_dir_is_discoverable_without_the_repo_cwd(tmp_path):
    """包得是"可发现的": 只靠 PYTHONPATH 指向仓库根就能找见 (等价于安装后被 find)"""
    loaded = _run_in_foreign_cwd(tmp_path, "import ai_kit")
    assert "ai_kit" in loaded


# ---------------------------------------------------------------------------
# 惰性导出 (PEP 562) + py.typed
# ---------------------------------------------------------------------------

def test_public_names_resolve_to_the_same_objects():
    """`from ai_kit import try_heal` 拿到的必须是 healer 里那一个, 不是副本; 清单不得漂移"""
    import importlib as _importlib

    import ai_kit

    assert ai_kit.__all__ == sorted(ai_kit._LAZY), "__all__ 与 _LAZY 键不一致"

    for name in ai_kit.__all__:
        module_name, attr = ai_kit._LAZY[name]
        module = _importlib.import_module(module_name)
        assert getattr(ai_kit, name) is getattr(module, attr), \
            f"ai_kit.{name} 与 {module_name}.{attr} 不是同一个对象"


def test_unknown_attribute_raises_attribute_error():
    import ai_kit

    with pytest.raises(AttributeError) as excinfo:
        ai_kit.does_not_exist  # noqa: B018

    message = str(excinfo.value)
    assert "ai_kit" in message and "does_not_exist" in message, \
        f"AttributeError 应该能直接定位到包名和属性名: {message}"


def test_dir_lists_lazy_public_names():
    """惰性名字还没进 globals(), __dir__ 得把它们补上 (IDE 补全 / help() 依赖它)"""
    import ai_kit

    for name in ai_kit.__all__:
        assert name in dir(ai_kit), f"dir(ai_kit) 漏了惰性导出的 {name}"


def test_lazy_exports_delay_and_cache_submodule_imports(tmp_path):
    """顶层访问前一个子模块都不加载; 访问后只加载被需要的那一个, 且结果被缓存"""
    statement = (
        "import ai_kit\n"
        "assert 'ai_kit.healer' not in sys.modules, 'import ai_kit 就加载了 healer'\n"
        "assert 'ai_kit.client' not in sys.modules, 'import ai_kit 就加载了 client'\n"
        "first = ai_kit.try_heal\n"
        "assert 'ai_kit.healer' in sys.modules, '访问 try_heal 后 healer 仍未加载'\n"
        "assert ai_kit.try_heal is first, '第二次访问拿到的不是同一个对象'\n"
        "assert ai_kit.try_heal is sys.modules['ai_kit.healer'].try_heal\n"
        "assert 'ai_kit.client' not in sys.modules, '访问 healer 顺带加载了 client'\n"
    )
    _run_in_foreign_cwd(tmp_path, statement)


def test_py_typed_marker_ships_with_the_package():
    """PEP 561 标记: 文件在包内, 且 package-data 会把它带进 wheel"""
    marker = PKG_DIR / "py.typed"
    assert marker.is_file(), "缺 ai_kit/py.typed, 下游 mypy 会把 ai_kit 当成无类型包"

    tomllib = pytest.importorskip("tomllib", reason="需要 Python 3.11+ 的 tomllib")
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = data["tool"]["setuptools"]["package-data"]

    assert "py.typed" in package_data.get("ai_kit", []), \
        f"package-data 没声明 py.typed, wheel 里不会有它: {package_data.get('ai_kit')}"


# ---------------------------------------------------------------------------
# 守门测试自检: 真的发生"import 宿主项目"时, 上面两条断言必须报红
# ---------------------------------------------------------------------------

def test_guard_detects_a_smuggled_host_project_import(tmp_path):
    """造一个 import pages 的假包 —— 如果它依然绿, 说明整套守门是摆设"""
    fake_repo = tmp_path / "repo"
    pkg = fake_repo / "ai_kit"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("import pages\npages.ready()\n", encoding="utf-8")
    host = fake_repo / "pages"
    host.mkdir()
    (host / "__init__.py").write_text("def ready():\n    return True\n", encoding="utf-8")

    allowed = _stdlib() | BUDGET_THIRD_PARTY | {SELF}
    assert _top_level_imports(pkg / "__init__.py") - allowed == {"pages"}, \
        "静态白名单没拦住 import pages"

    loaded = _run_in_foreign_cwd(tmp_path, "import ai_kit", repo_root=fake_repo)
    assert "pages" in _foreign_modules(loaded), "动态预算检查没拦住 import pages"
