# -*- coding: utf-8 -*-
"""
规则包 — 把"框架/站点知识"从代码里搬出来

    loader.py       : JSON 加载 + schema 校验 + 合并 (stdlib json, 不引 PyYAML)
    builtin/        : 内置通用规则包, 按框架提示自动匹配
                      generic.json / element_plus.json / antd.json
    sites/          : 站点 profile (含敏感字段的必须进 .gitignore)
                      example_shop.json

profile 承载: L1 定位器坑表 / 语义提示与候选 / 意图词表 / L3-L4 容器选择器等。

用法:
    from ai_kit.profiles import default_rules, effective_rules, load_profile

    default_rules()                 # 内置包 (generic + element_plus), 只读
    effective_rules("antd")         # 内置 + antd 包, 返回新 dict
    load_profile("example_shop")    # 严格读 sites/example_shop.json, 写错会抛
"""
from ai_kit.profiles.loader import (
    BUILTIN_DIR,
    DEFAULT_BUILTIN,
    RULE_KEYS,
    SCHEMA_VERSION,
    SITES_DIR,
    ProfileError,
    builtin_names,
    default_rules,
    effective_rules,
    load_profile,
    merge_profiles,
    resolve_profile,
    site_names,
    validate_profile,
)

__all__ = [
    "BUILTIN_DIR",
    "DEFAULT_BUILTIN",
    "RULE_KEYS",
    "SCHEMA_VERSION",
    "SITES_DIR",
    "ProfileError",
    "builtin_names",
    "default_rules",
    "effective_rules",
    "load_profile",
    "merge_profiles",
    "resolve_profile",
    "site_names",
    "validate_profile",
]
