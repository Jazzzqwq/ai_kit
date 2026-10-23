# -*- coding: utf-8 -*-
"""
Phase 2 AI LLM 配置 — 请填写你用的 LLM 服务的 API Key / Endpoint / Model

填写说明:
1. 只填你想用的那家 (三家都填也行, 用哪个改 ACTIVE_PROVIDER)
2. API_KEY 格式一般是 sk-xxxxxx 或 直接 token
3. BASE_URL 要带 /v1 或完整路径, 例如 DeepSeek 是 https://api.deepseek.com/v1
4. 填完保存即可, 随时可以改

常用选项参考 (按需选):
┌──────────┬──────────────────────────────────────┬─────────────────────────┐
│ Provider │ BASE_URL                             │ MODEL (推荐)            │
├──────────┼──────────────────────────────────────┼─────────────────────────┤
│ DeepSeek │ https://api.deepseek.com/v1          │ deepseek-chat           │
│ 豆包      │ https://ark.cn-beijing.volces.com    │ 你自己创建的 endpoint_id │
│ 本地ollama │ http://localhost:11434/v1          │ qwen2.5:7b / llama3:8b  │
└──────────┴──────────────────────────────────────┴─────────────────────────┘
"""

import os


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
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if key and key not in os.environ:
                        os.environ[key] = value.strip().strip('"').strip("'")
        except Exception:
            continue


_load_dotenv()

# ====== 选一个填 ======
# 可用环境变量 AI_ACTIVE_PROVIDER 覆盖当前 provider
ACTIVE_PROVIDER = os.environ.get("AI_ACTIVE_PROVIDER", "qianwen")

# 成本价目表 (单位: 元 / 1K token) — 供评测模块算"每次成本"。不填则成本记为 n/a
PROVIDERS = {
    # --- DeepSeek  ---
    "deepseek": {
        "api_key": "",          # 在这里填你的 API Key
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "timeout": 15,          # 秒
        "price_per_1k_input": 0.001,
        "price_per_1k_output": 0.002,
    },

    # --- 豆包  ---
    "doubao": {
        "api_key": "",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "",            # 这里填你在火山引擎创建的 endpoint id (ep-xxxxx)
        "timeout": 15,
    },

    # --- 本地 Ollama  ---
    "ollama": {
        "api_key": "ollama",    # ollama 不需要 key, 随便写
        "base_url": "",
        "model": "qwen2.5:7b",  # 先 ollama pull qwen2.5:7b
        "timeout": 60,          # 本地模型可能慢, 给多点时间
        "price_per_1k_input": 0.0,   # 本地免费
        "price_per_1k_output": 0.0,
    },
    # --- qianwen ---
    "qianwen": {
        "api_key": os.environ.get("QIANWEN_API_KEY", ""),   # 从环境变量读取, 切勿硬编码进仓库
        "base_url": "https://maas.qianwenaiapi.com/compatible-mode/v1",
        "model": "qwen-plus",
        "timeout": 30,
        "price_per_1k_input": 0.0008,
        "price_per_1k_output": 0.002,
    }
}


def get_active_config() -> dict:
    """返回当前 ACTIVE_PROVIDER 的配置字典。
    api_key 支持用环境变量 {PROVIDER大写}_API_KEY 覆盖（写入本地 .env 也生效），
    环境变量优先级高于文件内明文。"""
    cfg = dict(PROVIDERS.get(ACTIVE_PROVIDER, {}))
    env_key = os.environ.get(f"{ACTIVE_PROVIDER.upper()}_API_KEY")
    if env_key:
        cfg["api_key"] = env_key
    if not cfg.get("api_key"):
        raise ValueError(
            f"[{ACTIVE_PROVIDER}] api_key 未配置. "
            f"请设环境变量 {ACTIVE_PROVIDER.upper()}_API_KEY 或写入本地 .env"
        )
    if not cfg.get("model"):
        raise ValueError(
            f"[{ACTIVE_PROVIDER}] model 未填写. 例如豆包要填 endpoint id (ep-xxxxx)"
        )
    return cfg


def is_configured() -> bool:
    """检测是否至少配置了一个 provider 的 api_key（含环境变量 / .env）"""
    for p, cfg in PROVIDERS.items():
        if cfg.get("api_key") or os.environ.get(f"{p.upper()}_API_KEY"):
            return True
    return False


def get_price() -> tuple:
    """
    返回当前 ACTIVE_PROVIDER 的 (每1K输入元, 每1K输出元)。
    未配置价目 → 返回 (None, None), 评测会把成本标记为 n/a。
    """
    cfg = PROVIDERS.get(ACTIVE_PROVIDER, {})
    pin = cfg.get("price_per_1k_input")
    pout = cfg.get("price_per_1k_output")
    if pin is None or pout is None:
        return (None, None)
    return (float(pin), float(pout))
