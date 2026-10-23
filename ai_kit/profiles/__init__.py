# -*- coding: utf-8 -*-
"""
规则包 — 把"框架/站点知识"从代码里搬出来

    loader.py       : JSON 加载 + schema 校验 (stdlib json, 不引 PyYAML)
    builtin/        : 内置通用规则包, 按框架提示自动匹配
                      generic.json / element_plus.json / antd.json
    sites/          : 站点 profile (含敏感字段的必须进 .gitignore)
                      example_shop.json

profile 承载: L1 定位器坑表 / 语义候选 / 意图词表 / 等待特征 / 页面原型特征等。
"""
