# -*- coding: utf-8 -*-
"""
LLM Provider 配置 — 密钥只从环境变量 / 本地 .env 读取

铁律 (A-1 密钥剥离):
    1. 本文件不出现任何明文 key —— PROVIDERS 里根本没有 api_key 字段,
       key 只在运行时从环境变量 {PROVIDER大写}_API_KEY 解析 (通用兜底 AI_API_KEY)。
    2. .env 只放本机, 已进 .gitignore; 可提交的模板写在 .env.example。
    3. model / base_url 也支持 {PROVIDER大写}_MODEL / {PROVIDER大写}_BASE_URL 覆盖,
       换 endpoint / 换模型不用改代码。

配置方式 (二选一):
    # a) 环境变量 (PowerShell)
    $env:QIANWEN_API_KEY = "sk-xxxx"
    $env:AI_ACTIVE_PROVIDER = "qianwen"

    # b) 本地 .env (把 .env.example 复制过去再填)
    QIANWEN_API_KEY=sk-xxxx

自检:
    python -m ai_kit.config            # 打印解析后的配置 (key 掩码) + 单价, 不联网
    python -m ai_kit.config --ping     # 真调一次 LLM 做连通性测试 (消耗极少 token)

价目表:
    PRICE_TABLE 按 **model** 索引 (元 / 1K token), 快照月份见 PRICE_TABLE_SNAPSHOT。
    未收录的 model 不报错, get_price() 返回 (None, None), 评测把成本记成 n/a。
"""

import os
from typing import Any, Dict, List, Optional


def _load_dotenv() -> None:
    """加载本地 .env (不覆盖已有环境变量).

    查找顺序: AI_KIT_ENV_FILE -> 当前目录 .env -> 包上级目录 .env (源码树)。
    仅用 stdlib 实现, 不为读 .env 引入 python-dotenv。
    """
    candidates = [
        os.environ.get("AI_KIT_ENV_FILE") or "",
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
    ]
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    if line.startswith("export "):
                        line = line[len("export "):].lstrip()
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if key and key not in os.environ:
                        os.environ[key] = value.strip().strip('"').strip("'")
        except Exception:
            continue


_load_dotenv()


def _env(name: str) -> str:
    """读环境变量, 去首尾空白; 未设返回空串"""
    return (os.environ.get(name) or "").strip()


# ====== 当前使用的 provider (可用环境变量 AI_ACTIVE_PROVIDER 覆盖) ======
ACTIVE_PROVIDER = _env("AI_ACTIVE_PROVIDER") or "qianwen"

# 静态档案: 只有 endpoint / model / timeout, **不含任何 key, 也不含价目**
# (价目按 model 查下方 PRICE_TABLE —— 同一家换 model 单价就变)。
# requires_api_key=False 表示本地服务 (ollama) 无鉴权, 只要 base_url + model 就绪即可用。
PROVIDERS: Dict[str, Dict[str, Any]] = {
    # --- DeepSeek ---
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "timeout": 15,
    },

    # --- 豆包 (火山方舟: model 填你创建的 endpoint id, ep-xxxxx) ---
    "doubao": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "",
        "timeout": 15,
    },

    # --- 本地 Ollama (无鉴权) ---
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5:7b",
        "timeout": 60,          # 本地模型可能慢, 给多点时间
        "requires_api_key": False,
    },

    # --- qianwen ---
    "qianwen": {
        "base_url": "https://maas.qianwenaiapi.com/compatible-mode/v1",
        "model": "qwen-plus",
        "timeout": 30,
    },
}


# ---------------------------------------------------------------------------
# 价目表 (元 / 1K token, 按 model 索引)
# ---------------------------------------------------------------------------
# 为什么按 model 而不按 provider: 同一家换 model 单价就变 (qwen-plus vs qwen-max),
# 而 {PROVIDER}_MODEL 本来就允许用环境变量覆盖 —— 按 provider 记价会在换模型后算错钱。
# 字段: "in" = 输入 (缓存未命中), "out" = 输出; 单位元 / 1000 token。

PRICE_TABLE_SNAPSHOT = "2026-10"        # 价格快照月份 (YYYY-MM), 涨价/降价后请同步改
PRICE_TABLE_SOURCE = (
    "阿里云百炼 (qwen-plus) / DeepSeek 开放平台 (deepseek-chat) 官方定价页快照; "
    "数值迁移自本仓库原有内嵌价目, 未逐条向厂商复核——只用于成本估算, 不要拿来对账"
)

PRICE_TABLE: Dict[str, Dict[str, float]] = {
    # --- 阿里云百炼 ---
    "qwen-plus":     {"in": 0.0008, "out": 0.002},
    # --- DeepSeek ---
    "deepseek-chat": {"in": 0.001,  "out": 0.002},
    # --- 本地 Ollama: 自己跑, 不计费 ---
    "qwen2.5:7b":    {"in": 0.0,    "out": 0.0},
}

# 模型名的版本后缀分隔符: qwen-plus-2025-04-28 / qwen2.5:7b-instruct-q4_K_M
_MODEL_SUFFIX_SEPS = ("-", ":", "/", "@")


def price_for_model(model: str) -> Optional[Dict[str, float]]:
    """按 model 名查价目 (元/1K), 未收录返回 None。

    支持带版本后缀的 model 名 (取最长前缀):
        qwen-plus-2025-04-28          -> qwen-plus
        qwen2.5:7b-instruct-q4_K_M    -> qwen2.5:7b
    """
    name = (model or "").strip().lower()
    if not name:
        return None
    if name in PRICE_TABLE:
        return dict(PRICE_TABLE[name])
    hits = [key for key in PRICE_TABLE
            if any(name.startswith(key + sep) for sep in _MODEL_SUFFIX_SEPS)]
    if not hits:
        return None
    return dict(PRICE_TABLE[max(hits, key=len)])


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def active_provider() -> str:
    """当前 provider 名 (调用时读环境, 便于运行时切换)"""
    return _env("AI_ACTIVE_PROVIDER") or ACTIVE_PROVIDER


def _profile(name: str) -> Dict[str, Any]:
    if name not in PROVIDERS:
        raise ValueError(
            f"未知 provider: {name!r}. 可选: {', '.join(sorted(PROVIDERS))} "
            f"(改环境变量 AI_ACTIVE_PROVIDER)"
        )
    return PROVIDERS[name]


def resolve_api_key(provider: Optional[str] = None) -> str:
    """key 只来自环境: {PROVIDER大写}_API_KEY 优先, 其次通用 AI_API_KEY"""
    name = (provider or active_provider()).strip()
    return _env(f"{name.upper()}_API_KEY") or _env("AI_API_KEY")


def get_active_config(provider: Optional[str] = None) -> dict:
    """返回解析后的配置字典 (含 api_key, 来自环境)。

    key / model / base_url 缺失则抛 ValueError, 报错信息只提变量名、绝不回显 key 值。
    """
    name = (provider or active_provider()).strip()
    cfg = dict(_profile(name))
    cfg["provider"] = name
    cfg["api_key"] = resolve_api_key(name)
    cfg["model"] = _env(f"{name.upper()}_MODEL") or cfg.get("model", "")
    cfg["base_url"] = _env(f"{name.upper()}_BASE_URL") or cfg.get("base_url", "")

    if cfg.get("requires_api_key", True) and not cfg["api_key"]:
        raise ValueError(
            f"[{name}] api_key 未配置. 请设环境变量 {name.upper()}_API_KEY "
            f"(或通用的 AI_API_KEY), 可写进本地 .env — 切勿提交到仓库"
        )
    if not cfg.get("base_url"):
        raise ValueError(
            f"[{name}] base_url 未填写. 可用环境变量 {name.upper()}_BASE_URL 提供"
        )
    if not cfg.get("model"):
        raise ValueError(
            f"[{name}] model 未填写 (豆包要填 endpoint id ep-xxxxx). "
            f"可用环境变量 {name.upper()}_MODEL 提供"
        )
    return cfg


def is_configured(provider: Optional[str] = None) -> bool:
    """provider 给定 → 只判该 provider 是否可用;
    未给定 → 当前 provider 可用, 或至少还有一家配了 key。

    无鉴权的本地服务 (ollama) 只在"被选中"时才算可用, 避免没配任何东西时
    llm_available() 也返回 True, 让离线/CI 评测白跑网络重试。
    """
    if provider:
        return _usable(provider)
    if _usable(active_provider()):
        return True
    return any(resolve_api_key(name) for name in PROVIDERS)


def _usable(name: str) -> bool:
    try:
        get_active_config(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def available_providers() -> List[str]:
    """列出当前环境解析得通的 provider (供诊断/报告)"""
    return [name for name in sorted(PROVIDERS) if _usable(name)]


def _model_of(provider_name: str) -> str:
    """该 provider 解析后的 model (含 {PROVIDER}_MODEL 环境覆盖); 未知 provider 返回空串"""
    try:
        cfg = _profile(provider_name)
    except Exception:  # noqa: BLE001
        return ""
    return _env(f"{provider_name.upper()}_MODEL") or cfg.get("model", "")


def get_price(provider: Optional[str] = None) -> tuple:
    """
    返回该 provider 当前 model 的 (每1K输入元, 每1K输出元)。

    价格按 model 查 PRICE_TABLE, 所以 {PROVIDER}_MODEL 覆盖后单价会跟着换
    (旧版把价目记在 provider 上, 换模型后仍按旧价算, 会算错钱)。
    未收录的 model / 未知 provider → (None, None), 评测把成本标记为 n/a。
    """
    entry = price_for_model(_model_of((provider or active_provider()).strip()))
    if not entry:
        return (None, None)
    return (float(entry["in"]), float(entry["out"]))


def _price_label(provider_name: str) -> str:
    """自检输出用: 单价一行字"""
    pin, pout = get_price(provider_name)
    if pin is None:
        return "未收录(成本记 n/a)"
    return f"in={pin} out={pout} 元/1K"


def masked_config(provider: Optional[str] = None) -> dict:
    """安全打印用: 解析配置但把 key 掩码, 绝不外泄明文"""
    name = (provider or active_provider()).strip()
    cfg = dict(_profile(name))
    key = resolve_api_key(name)
    cfg["provider"] = name
    cfg["api_key"] = mask_key(key)
    cfg["model"] = _env(f"{name.upper()}_MODEL") or cfg.get("model", "")
    cfg["base_url"] = _env(f"{name.upper()}_BASE_URL") or cfg.get("base_url", "")
    return cfg


def mask_key(key: str) -> str:
    """sk-ab****yz (长度不足则整体打码)"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-2:]}"


# ---------------------------------------------------------------------------
# 自检入口: python -m ai_kit.config [--ping]
# ---------------------------------------------------------------------------

def _selfcheck(ping: bool = False) -> int:
    print(f"当前 provider : {active_provider()}")
    print(f".env 候选路径 : {os.path.join(os.getcwd(), '.env')} (存在: {os.path.isfile(os.path.join(os.getcwd(), '.env'))})")
    print(f"\n各 provider 解析状态 (key 掩码; 单价快照 {PRICE_TABLE_SNAPSHOT}):")
    for name in sorted(PROVIDERS):
        cfg = masked_config(name)
        state = "可用" if _usable(name) else "缺项"
        key_state = cfg["api_key"] or ("无需 key" if not cfg.get("requires_api_key", True) else "未配置")
        print(f"  - {name:<9} {state:<4} key={key_state:<14} model={cfg['model'] or '(未填)'}")
        print(f"    {'':<9} 单价={_price_label(name)}  base_url={cfg['base_url'] or '(未填)'}")

    if not ping:
        print("\n连通性测试: python -m ai_kit.config --ping")
        return 0

    print("\n[ping] 真实调用 LLM ...")
    from ai_kit.client import test_connection
    ok = test_connection()
    print(f"[ping] {'通过' if ok else '失败'} (provider={active_provider()})")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_selfcheck(ping="--ping" in sys.argv[1:]))
