# -*- coding: utf-8 -*-
"""
通用 LLM 客户端 — OpenAI Chat Completions 兼容接口
支持 DeepSeek / 豆包 / 本地 Ollama, 统一走 requests.post
如果 LLM 没配置或挂了, 返回 None, 不阻塞 Phase 1 规则匹配流程
"""
from __future__ import annotations

import json
import time
from typing import List, Optional, Dict, Any

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    from ai_kit.config import get_active_config, is_configured
except Exception:  # 允许把 client.py 单独拷进别的项目使用
    get_active_config = None
    is_configured = None


# ---------------------------------------------------------------------------
# Token 用量回填 (供评测算成本; 向后兼容, 不影响 chat/chat_json 返回值)
# ---------------------------------------------------------------------------
_LAST_USAGE: Dict[str, Any] = {}          # 最近一次 chat 的 usage
_CUMULATIVE_USAGE: Dict[str, int] = {     # 进程内累计 (评测按增量算每条 case 成本)
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "calls": 0,
}


def get_last_usage() -> Dict[str, Any]:
    """最近一次 chat 返回的 usage (无则空 dict)"""
    return dict(_LAST_USAGE)


def get_cumulative_usage() -> Dict[str, int]:
    """进程内累计 token 用量 + 调用次数 (评测按前后差值算单次成本)"""
    return dict(_CUMULATIVE_USAGE)


def _record_usage(data: Dict[str, Any]) -> None:
    """从 chat completions 响应体里提取 usage, 写入 last + cumulative"""
    global _LAST_USAGE
    usage = (data or {}).get("usage") or {}
    _LAST_USAGE = dict(usage)
    if usage:
        _CUMULATIVE_USAGE["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
        _CUMULATIVE_USAGE["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
        _CUMULATIVE_USAGE["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
        _CUMULATIVE_USAGE["calls"] += 1
    else:
        # 有响应但无 usage 字段: 至少记一次调用, 成本记为不可换算
        _CUMULATIVE_USAGE["calls"] += 1


# ---------------------------------------------------------------------------
# 统一 chat 接口
# ---------------------------------------------------------------------------

def chat(messages: List[Dict[str, str]],
         temperature: float = 0.1,
         max_retries: int = 2) -> Optional[str]:
    """
    调 LLM 对话, 返回文本回答. 任何异常返回 None.
    - messages: [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
    - temperature: 0 更保守 (适合代码生成/分类), 1 更发散
    """
    if not _HAS_REQUESTS:
        return None
    if is_configured is None or not is_configured():
        return None

    try:
        cfg = get_active_config()
    except Exception as e:
        # 当前 ACTIVE_PROVIDER 未填 key/model 等 → 优雅降级, 不抛异常(符合"LLM 不可用返回 None"约定)
        print(f"  [LLM] 未配置或配置不完整, 跳过 LLM: {e}")
        return None
    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
    }
    if cfg.get("max_tokens"):
        payload["max_tokens"] = cfg["max_tokens"]

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                url, headers=headers, json=payload,
                timeout=cfg.get("timeout", 15))
            if resp.status_code == 200:
                data = resp.json()
                _record_usage(data if isinstance(data, dict) else {})
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    return content.strip()
                return None
            # 429/5xx 重试
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            last_err = str(e)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
    if last_err:
        print(f"  [LLM] 调用失败: {last_err}")
    return None


# ---------------------------------------------------------------------------
# 结构化输出 (要求 LLM 返回 JSON, 解析失败回 None)
# ---------------------------------------------------------------------------

def chat_json(system_prompt: str, user_prompt: str,
              max_retries: int = 2) -> Optional[Dict[str, Any]]:
    """
    要求 LLM 返回 JSON, 自动提取 + 解析. 返回 dict 或 None.
    """
    msgs = [
        {"role": "system",
         "content": system_prompt + "\n\n请严格只返回 JSON, 不要 markdown 代码块标记 (```json)。"},
        {"role": "user", "content": user_prompt},
    ]
    raw = chat(msgs, temperature=0.1, max_retries=max_retries)
    if not raw:
        return None

    # 尝试直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # fallback: 从 markdown ```json ... ``` 里提取
    import re
    m = re.search(r"```json\s*([\s\S]*?)```", raw)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # fallback: 找第一个 { ... } 块
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    print(f"  [LLM] JSON 解析失败, 原始返回: {raw[:200]}")
    return None


# ---------------------------------------------------------------------------
# 测试连通性
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 多模态: 带图片调用
# ---------------------------------------------------------------------------

def chat_with_image(system_prompt: str,
                    user_text: str,
                    image_base64: str,
                    image_mime: str = "image/png",
                    max_retries: int = 2) -> Optional[str]:
    """
    多模态调用: text + 1 张图片 (base64). 失败返回 None.
    image_base64: 纯 base64 字符串 (不含 data:image/xxx;base64, 前缀)
    """
    if not _HAS_REQUESTS:
        return None
    if is_configured is None or not is_configured():
        return None

    data_url = f"data:{image_mime};base64,{image_base64}"
    msgs = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
    ]
    return chat(msgs, temperature=0.2, max_retries=max_retries)


def chat_json_with_image(system_prompt: str,
                         user_text: str,
                         image_base64: str,
                         image_mime: str = "image/png",
                         max_retries: int = 2) -> Optional[dict]:
    """多模态 + JSON 结构化输出. 返回 dict 或 None."""
    raw = chat_with_image(system_prompt, user_text, image_base64, image_mime, max_retries)
    if not raw:
        return None

    import re as _re
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = _re.search(r"```json\s*([\s\S]*?)```", raw)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    m = _re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    print(f"  [LLM-Vision] JSON parse failed, raw[:200]={raw[:200]}")
    return None


def test_connection() -> bool:
    """发一条最简单的消息, 返回 True 表示 LLM 可用"""
    result = chat([
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Reply with just: ok"},
    ], max_retries=0)
    return result is not None


def llm_available() -> bool:
    """检测 LLM 是否可用 (不真调, 只看配置)"""
    return _HAS_REQUESTS and is_configured and is_configured()
