# 变更记录

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号沿用 `pyproject.toml`。
首个版本已打 tag `v0.1.0`（对应 Gitee Releases），未发布 PyPI。

## [0.1.0] - 2026-10-28

首个可用版本：可移植的 Web UI 测试 AI 工具箱，依赖只有 `selenium` + `requests`。

### 模块

- `healer`：L1-L4 分层定位自愈，top-1 87.5% / top-3 100%（24 条黄金样本；L4 仅 `--llm` 产出候选）。
- `analyzer`：失败归因规则 + 可选 LLM，严格准确率 96.7%、含容错 100%，带信心校准（gap/ECE）。
- `perception` / `generator`：清单 → 用例计划 → pytest 代码，eid 解析率 100%、可执行率 92.3%。
- `proposal`：诊断 / 方案 / 人工确认 HTML 报告 / 六步应用（备份、白名单、单 nodeid 验证、回滚、记账）。
- `evaluation`：黄金集 + 指标 + 成本 → md/json 报告，默认离线 0 成本。
- 数据安全：出境点清单 + `sanitize`，采集侧 `value` 在页面内就换 `[REDACTED]`。

### 新增（本次收口）

- `utils.sanitize_structure`：递归把嵌套 `dict`/`list`/`tuple` 里的每个字符串过 `sanitize`。
  JSON 里的值没有标签和字段名提示，对整段 JSON 跑正则会误伤 eid 与行数，所以逐字符串过筛。
- `utils.dom_diff(before, after, limit=20, redact=True)`：只比两层 —— 可见文本行 + 标签签名
  （`tag` + `role/type/name`，`class` 刻意不进）。输出 markdown 两段带 `(+n -n ~n)` 统计，
  每类最多 20 条并报剩余条数；无变化时返回一句说明而不是空串（空串在下游会被当成"没采集"）。
  产物进报告与提示词，所以默认脱敏。**目前没有自动生产者**：`PageObservation` 不含 HTML，
  全项目没有"操作前快照"钩子，`proposal/report.build` 与 `solutions.propose` 的 `dom_diff`
  参数仍由调用方提供。
- 评测指标 v2 两项：`metrics.healing_stability`（重复调用一致 / 命中冗余 / 排名稳固）与
  `metrics.cross_page_generalization`（短板页覆盖率 + 按原型分桶），进 `run_eval` 报告。

### 变更

- 七处 LLM 出境点全部接上脱敏：`generator/core.py`、`perception/prompts.py`、
  `perception/archetype.py`、`proposal/diagnoser.py`、`proposal/solutions.py`
  （`healer` L4 与 `analyzer` 早前已接）。
- `generator` 表格样例行改为整格抹除（`_redact_rows`）：格子里孤零零一个"张三"没有字段名提示，
  正则判不出来，就不猜；行列形状与列名保留，模型仍能看出"几行几列、列名是什么"。
- `ai_kit` 顶层惰性导出增加 `sanitize_structure` 与 `dom_diff`。

### 未变（有证据）

- 黄金集 8 页冻结时就已脱敏，接线后 `--llm` 的 prompt **逐字节不变**，并有守门测试钉住；
  接线前后同口径跑 `run_eval`：healer top-1 87.5% / top-3 100%、generator py_compile 100% /
  召回 29.2%、元素覆盖率 23.4%（66/282）、eid 解析率 100%（260/260）、可执行率 92.3%（108/117）、
  analyzer 规则准确率 96.7%，逐项一致。
- 全量回归 `1145 passed, 2 warnings in 136.13s`；`run_eval` 离线约 12 秒、0 成本。
