# -*- coding: utf-8 -*-
"""
计划缓存 — 同一份页面 + 同一个规则包 + 同一个模型, 就不再花第二次钱

缓存键 (卡片 5.9) 由 planner.plan_cache_key 给: 指纹 + profile + model +
strategy/planner 版本 + 原型。四个成分缺一个都会串味:
    换页面      -> 指纹变, 旧计划的 eid 在新页面上根本不存在;
    换规则包    -> 风险词/容器提示变, 该测什么跟着变;
    换 model    -> 产出的步骤可能完全不同, 复用它等于拿 A 的答案交 B 的卷;
    换模板版本  -> 步骤模板改了却复用旧计划, 比重新生成一次贵得多。

文件放 plans/<fingerprint>.json (卡片指定的路径)。**文件名只用指纹, 键却不止指纹**
—— 所以文件里存了完整 cache_key, 读的时候比对: 对不上就当未命中, 顺手覆盖。
把四个成分都塞进文件名会让「指纹 -> 计划」这条人找文件的路走不通, 而碰撞本来
就是靠文件内的键来判的, 不靠文件名。

一个容易写错的地方: TestPlan.to_dict() 故意不含 needs_review 和 diagnostics
(schema 是 ⑥ 的契约, 「这条还没验收」是流水线状态)。如果缓存只存 to_dict、
读回来直接 plan_from_dict, 那么一条 needs_review 的用例命中缓存后会**悄悄变干净**,
报告显示一切正常 —— 这正是 5.8 刚修掉的那类 bug。所以缓存额外存 review 名单
与 diagnostics, 读回时原样还原。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Tuple

from ai_kit.ledger import Budget, RunLedger, meter
from ai_kit.perception.inventory import PageInventory, from_inventory
from ai_kit.perception.planner import (
    DUP_THRESHOLD,
    PLANNER_VERSION,
    TestPlan,
    plan_cache_key,
    plan_from_dict,
    plan_verified,
)

CACHE_VERSION = "plan-cache.v1"
PLANS_DIR_ENV = "AI_KIT_PLANS_DIR"
DEFAULT_PLANS_DIR = "plans"

# 指纹只该是 sha1 hex; 其它形状一律再 hash 一遍 —— 手写清单的 fingerprint 字段
# 是外部输入, 直接拼进路径就是拿缓存键做路径穿越 ("../../x")。
_HEX_RE = re.compile(r"^[0-9a-f]{8,64}$")
_UNSAFE_RE = re.compile(r"[^0-9a-z_.-]+")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plans_dir(root: Any = None) -> Path:
    """缓存目录: 显式传 > AI_KIT_PLANS_DIR > ./plans"""
    if root is not None:
        return Path(root)
    return Path(os.environ.get(PLANS_DIR_ENV) or DEFAULT_PLANS_DIR)


def cache_filename(fingerprint: str) -> str:
    """指纹 -> 文件名; 非法形状先 hash, 不认识的字符一律换掉"""
    text = str(fingerprint or "").strip().lower()
    if _HEX_RE.match(text):
        return f"{text}.json"
    if not text:
        text = "unknown"
    safe = _UNSAFE_RE.sub("-", text).strip("-.") or "unknown"
    # 兜底残留的 ".." 之类不能留: 上面已经把 / \ 换掉了, 这里只防 ".."
    return f"{hashlib.sha1(safe.encode('utf-8')).hexdigest()}.json"


def plan_cache_path(fingerprint: str, root: Any = None) -> Path:
    """plans/<fingerprint>.json"""
    return plans_dir(root) / cache_filename(fingerprint)


def cache_key(inventory: Any, archetype: str = "", *, profile: str = "", model: str = "",
              prompt_version: str = "") -> str:
    """缓存键的唯一入口 (转发 planner), 让 cache 与 planner 永远同一口径"""
    return plan_cache_key(inventory, archetype, profile=profile, model=model,
                          prompt_version=prompt_version)


def _review_ids(plan_: TestPlan) -> list:
    """needs_review 的 case_id 名单 (case 级标记 + diagnostics 两边取并集)"""
    ids = [case.case_id for case in plan_.cases if case.needs_review]
    pending = plan_.diagnostics.get("needs_review")
    if isinstance(pending, Mapping):
        ids.extend(str(case_id) for case_id in pending)
    return list(dict.fromkeys(ids))


def plan_payload(plan_: TestPlan, inventory: Any, *, archetype: str = "") -> dict:
    """TestPlan -> 缓存文件内容 (计划本体 + 读不回来的那部分状态)"""
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    return {
        "cache_version": CACHE_VERSION,
        "cache_key": cache_key(inv, archetype, profile=plan_.profile, model=plan_.model,
                               prompt_version=plan_.prompt_version),
        "fingerprint": inv.fingerprint,
        "profile": plan_.profile,
        "model": plan_.model,
        "prompt_version": plan_.prompt_version,
        "saved_at": _utcnow(),
        "review": _review_ids(plan_),
        "diagnostics": dict(plan_.diagnostics),
        "plan": plan_.to_dict(),
    }


def save_plan(plan_: TestPlan, inventory: Any, *, archetype: str = "", root: Any = None
              ) -> Tuple[Optional[Path], str]:
    """写缓存; 返回 (路径, 说明)。写不进去返回 (None, 原因), 不抛

    先写 .tmp 再 replace: 中途断电留下半个 JSON, 读回时会被当成未命中并覆盖 ——
    这比让半个文件冒充缓存好, 但能原子替换就别留这个窗口。
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    path = plan_cache_path(inv.fingerprint, root)
    payload = plan_payload(plan_, inv, archetype=archetype)
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as exc:
        # diagnostics 里混进了不可序列化的东西 (如 driver): 保计划, 丢诊断摘要
        payload["diagnostics"] = {"problems": [str(item) for item in plan_.problems]}
        try:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return None, f"计划序列化不了: {exc}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        return None, f"写不进去 ({path}): {exc}"
    return path, f"已写入 {path}"


def load_plan(inventory: Any, *, archetype: str = "", profile: str = "", model: str = "",
              prompt_version: str = "", root: Any = None
              ) -> Tuple[Optional[TestPlan], str]:
    """读缓存; 返回 (计划, 说明)。读不出来返回 (None, 原因) —— 原因要带出去,
    否则「为什么没命中」永远只能靠猜
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    path = plan_cache_path(inv.fingerprint, root)
    if not path.is_file():
        return None, f"没有缓存文件 ({path})"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"缓存读不出来 ({path}): {exc}"
    if not isinstance(raw, Mapping):
        return None, f"缓存不是 JSON object ({path})"
    if raw.get("cache_version") != CACHE_VERSION:
        return None, f"缓存格式变了 ({raw.get('cache_version')!r} != {CACHE_VERSION})"
    expected = cache_key(inv, archetype, profile=profile, model=model,
                         prompt_version=prompt_version)
    stored = str(raw.get("cache_key") or "")
    if stored != expected:
        return None, "缓存键变了 (页面/规则包/模型/提示词版本之一), 重新生成"
    plan_, problems = plan_from_dict(raw.get("plan"))
    if plan_ is None:
        return None, f"缓存里的计划读不回来: {'; '.join(problems) or '结构不对'}"

    diagnostics = raw.get("diagnostics")
    restored = dict(diagnostics) if isinstance(diagnostics, Mapping) else {}
    if problems:
        restored.setdefault("cache_problems", list(problems))
    review = {str(case_id) for case_id in (raw.get("review") or ()) if case_id}
    pending = restored.get("needs_review")
    if isinstance(pending, Mapping):
        review.update(str(case_id) for case_id in pending)
    cases = tuple(replace(case, needs_review=True) if case.case_id in review else case
                  for case in plan_.cases)
    return replace(plan_, cases=cases, diagnostics=restored), f"命中 {path}"


def plan_cached(inventory: Any, *, render: Any = None, ask: Any = None,
                budget: Optional[Budget] = None, ledger: Optional[RunLedger] = None,
                root: Any = None, archetype: str = "", prompt_version: str = PLANNER_VERSION,
                max_retries: int = 2, mode: str = "", po_methods: Iterable[str] = (),
                po_signatures: Iterable[str] = (),
                driver: Any = None, dedupe: bool = True, threshold: float = DUP_THRESHOLD,
                **kwargs: Any) -> TestPlan:
    """带缓存 + 预算的计划生成: 命中直接复用, 未命中才算钱

    ask 会被 meter 包一层 (查预算 -> 计时 -> 记账), 所以这里的 ask 是「已经带账的
    ask」; 传 None 就是纯规则路径, 不会碰网络也不会写账本。
    diagnostics["cache"] 一定会有一行: 命中/未命中各自的原因 —— 缓存沉默不语时,
    人只会以为「今天模型特别快」。
    """
    inv = inventory if isinstance(inventory, PageInventory) else from_inventory(inventory)
    cached, why = load_plan(inv, archetype=archetype, root=root,
                            prompt_version=prompt_version)
    if cached is not None:
        diagnostics = dict(cached.diagnostics)
        diagnostics["cache"] = why
        return replace(cached, diagnostics=diagnostics)

    metered_ask = None
    if ask is not None:
        metered_ask = meter(ask, tag="plan", prompt_version=prompt_version,
                            budget=budget, ledger=ledger)
    fresh = plan_verified(inv, ask=metered_ask, render=render, max_retries=max_retries,
                          mode=mode, po_methods=po_methods,
                          po_signatures=po_signatures, driver=driver, dedupe=dedupe,
                          threshold=threshold, prompt_version=prompt_version, **kwargs)
    path, note = save_plan(fresh, inv, archetype=archetype, root=root)
    diagnostics = dict(fresh.diagnostics)
    diagnostics["cache"] = f"未命中 ({why}); " + (note if path or note else "未写入")
    return replace(fresh, diagnostics=diagnostics)
