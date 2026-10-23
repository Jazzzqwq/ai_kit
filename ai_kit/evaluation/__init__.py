# -*- coding: utf-8 -*-
"""
AI 评测模块 (Evaluation) — 可移植、自包含

设计原则:
1. 只依赖 ai_kit.* 公开函数 + stdlib + selenium + requests, 绝不 import 具体项目的 pages/config/common
2. 黄金数据自带 (静态 HTML 夹具 + JSON 用例), 不联网/不登录, 随包分发
3. 默认 0 成本可进 CI; --llm 开关才调真实大模型

入口: python -m ai_kit.evaluation.run_eval [--llm] [--suite healer,analyzer,generator]
"""
