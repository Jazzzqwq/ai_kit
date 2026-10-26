# -*- coding: utf-8 -*-
"""
密钥泄漏护栏 — 工作区 / git 全历史都不许出现明文 key, 且 .env 必须被忽略。

与 tests/test_config.py 的分工:
    test_config.py          : 解析行为对不对 (key 只从环境来)
    test_no_plaintext_keys.py: 静态护栏 —— 现在就干净, 以后也别想混进来 (可进 CI)

运行:
    pytest tests/test_no_plaintext_keys.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 真实 key 长这样; 刻意不匹配 sk-xxxxxx 这类文档占位符, 避免误报
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)api[_-]?key[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_\-]{16,}[\"']"),
    re.compile(r"(?i)(secret|token|passwd|password)[\"']?\s*[:=]\s*[\"'][^\s\"']{16,}[\"']"),
]

# 明确允许出现的占位符/测试杜撰值 (真实 key 不会包含这些词)
ALLOWED = re.compile(
    r"(?i)sk-xxxx|sk-your|sk-test|fake|your-|xxxx|example|placeholder|"
    r"endpoint_id|ep-xxx|changeme|<[^>]+>"
)

SCAN_SUFFIXES = (".py", ".json", ".md", ".toml", ".txt", ".yaml", ".yml", ".ini", ".cfg", ".html")


def _repo_files():
    """该提交的文本文件 = 已跟踪 + 未跟踪但未被忽略 (本机 .env / 产物自动排除)"""
    if shutil.which("git") is not None and _git("rev-parse", "--git-dir").returncode == 0:
        listed = (_git("ls-files").stdout + "\n" +
                  _git("ls-files", "--others", "--exclude-standard").stdout)
        rels = [p.strip().replace("\\", "/") for p in listed.splitlines() if p.strip()]
    else:
        rels = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames
                           if d not in {".git", ".venv", "__pycache__", ".pytest_cache", ".idea"}
                           and not d.endswith(".egg-info")]
            for fn in filenames:
                rels.append(os.path.relpath(os.path.join(dirpath, fn), ROOT).replace("\\", "/"))
    for rel in sorted(set(rels)):
        base = os.path.basename(rel)
        # 密钥类文件即使不是常规代码后缀也要扫内容 (曾出现 " .env" 带空格逃逸)
        if not (rel.endswith(SCAN_SUFFIXES) or base.strip().endswith(".env")):
            continue
        path = os.path.join(ROOT, *rel.split("/"))
        if os.path.isfile(path):
            yield rel, path


def _matches(text: str):
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(line):
                if ALLOWED.search(m.group(0)):
                    continue
                hits.append((lineno, _mask(m.group(0))))
    return hits


def _mask(value: str) -> str:
    return (value[:4] + "****") if len(value) > 4 else "****"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 1. 仓库内容: 该提交的文件里不能有明文 key
# ---------------------------------------------------------------------------

def test_working_tree_has_no_plaintext_keys():
    offenders = []
    scanned = 0
    for rel, path in _repo_files():
        scanned += 1
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for lineno, masked in _matches(text):
            offenders.append(f"{rel}:{lineno} -> {masked}")
    assert scanned > 0, "没扫到任何文件, 检查扫描范围"
    assert not offenders, "仓库内容发现疑似明文密钥:\n" + "\n".join(sorted(offenders))


# ---------------------------------------------------------------------------
# 2. config.py 结构约束: key 字段根本不存在, 只能来自环境
# ---------------------------------------------------------------------------

def test_config_source_never_hardcodes_credentials():
    src = open(os.path.join(ROOT, "ai_kit", "config.py"), encoding="utf-8").read()
    assert not re.search(r"api_key[\"']?\s*[:=]\s*[\"'][^\"']+[\"']", src), \
        "config.py 里不许出现非空 api_key 字面量"
    assert "os.environ" in src or "_env(" in src, "config.py 必须从环境变量读 key"


def test_env_template_credentials_left_blank():
    """模板里 *_API_KEY / AI_API_KEY 必须留空 (非敏感项如 provider 选择可以给默认值)"""
    path = os.path.join(ROOT, ".env.example")
    assert os.path.isfile(path), "缺少 .env.example 模板"
    secret_keys = []
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().upper()
        if key == "AI_API_KEY" or key.endswith("_API_KEY"):
            secret_keys.append(key)
            assert not value.strip(), f".env.example 的 {key} 必须留空"
    assert secret_keys, ".env.example 里至少要列出各家 provider 的 key 变量"


# ---------------------------------------------------------------------------
# 3. git: .env 被忽略 / 未被跟踪; 模板可提交
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_dotenv_is_gitignored_and_untracked():
    # " .env" 这类带前导空格的变体也是另一个文件名, 必须一并忽略
    for name in (".env", " .env", ".env.local", "prod.env"):
        assert _git("check-ignore", "-q", "--", name).returncode == 0, \
            f"{name!r} 未被 .gitignore 忽略 (A-1 曾因 ' .env' 带空格逃逸过一次)"
    assert _git("ls-files", "--error-unmatch", ".env").returncode != 0, \
        ".env 已被 git 跟踪, 必须 git rm --cached"
    assert _git("check-ignore", "-q", ".env.example").returncode != 0, \
        ".env.example 是要提交的模板, 不该被忽略"


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_no_tracked_secret_or_ide_files():
    """兜底: 索引里 basename 去空白后以 .env 结尾的、以及 .idea/ 都不许入库"""
    tracked = [p for p in _git("ls-files").stdout.splitlines() if p.strip()]
    offenders = [p for p in tracked
                 if os.path.basename(p.strip()).strip().endswith(".env")
                 and not p.strip().endswith(".env.example")]
    assert not offenders, f"这些密钥类文件被 git 跟踪了: {offenders}"
    ide = [p for p in tracked if p.strip().startswith(".idea/")]
    assert not ide, f".idea/ 是本地 IDE 配置, 不该入库: {ide}"


# ---------------------------------------------------------------------------
# 4. git 全历史: 任一版本的文件内容里都没混进过明文 key
# ---------------------------------------------------------------------------

# git grep 用 POSIX ERE, 不支持 \s / \S
_HISTORY_REGEXES = [
    r"sk-[A-Za-z0-9_-]{20,}",
    r"api[_-]?key[[:space:]]*[:=][[:space:]]*[\"'][A-Za-z0-9_-]{16,}[\"']",
]


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_git_history_has_no_plaintext_keys():
    """逐提交扫描树内容 (不用 pickaxe: -S 会把"删除 key 的提交"也当成命中, 误报)"""
    if _git("rev-parse", "--git-dir").returncode != 0:
        pytest.skip("不是 git 仓库")
    revs = _git("rev-list", "--all").stdout.split()
    offenders, scanned = [], 0
    for sha in revs:
        for pattern in _HISTORY_REGEXES:
            r = _git("grep", "-I", "-n", "-E", pattern, sha, "--", ".")
            if r.returncode != 0 or not r.stdout.strip():
                continue
            for line in r.stdout.splitlines():
                # 只保留 sha:path:lineno, 不输出疑似密钥内容
                parts = line.split(":", 3)
                where = ":".join(parts[:3]) if len(parts) >= 3 else line[:60]
                if ALLOWED.search(parts[3] if len(parts) > 3 else ""):
                    continue
                offenders.append(where + "  (匹配 " + pattern + ")")
        scanned += 1
    assert scanned > 0, "没扫到任何提交"
    assert not offenders, (
        "git 全历史发现疑似明文密钥:\n" + "\n".join(sorted(offenders)) +
        "\n处理: 立即在服务商后台吊销该 key, 或用 git filter-repo 清历史"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  PASS {name}")
    print("\n[密钥护栏自检] 全部通过")
