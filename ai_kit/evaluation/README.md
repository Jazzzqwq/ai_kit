# AI 评测模块 (ai_kit/evaluation)

把 README 里的**估算数字**（自愈命中率 30/50/10/10、置信度 0.80 vs 0.97）变成**可复现的实测指标**。

## 设计前提：可移植、自包含
本模块已随 `ai_kit` 独立成包、可用于任意网站，因此：
- 只依赖 `ai_kit.*` 公开函数 + stdlib + selenium + requests，**不 import** 项目的 `pages/`、`config.py`、`common/`。
- 黄金数据全部自带（静态 HTML 夹具 + JSON 用例），**不联网、不登录** → 100% 可复现。
- **默认 0 成本、可进 CI**：只跑 Phase-1 规则 / 本地自愈层；`--llm` 才调真实大模型。
- **不引新依赖**：ECE、Jaccard、py_compile 全 stdlib 手写。
- **不改生产逻辑**：`ai_kit/healer.py` 只新增了纯函数 `rank_candidates`，`try_heal`/缓存不动。

## 目录
```
ai_kit/evaluation/
├── metrics.py           # 纯函数指标库 (top_k/accuracy/ECE/py_compile/coverage/dup)
├── driver_provider.py   # 可插拔无头浏览器 (Edge→Chrome, 拿不到→自愈套件 SKIPPED)
├── cost_tracker.py      # token→元 成本累计
├── eval_healer.py       # 套件1: 坏定位器→正确定位器, top1/top3 + L1-L4 分层归因
├── eval_analyzer.py     # 套件2: 失败场景→根因, 准确率 + 置信度校准 (+可选 LLM 对比/成本)
├── eval_generator.py    # 套件3: page_structure→用例, py_compile率/覆盖率/重复率
├── run_eval.py          # CLI 入口, 汇总 → md + json 报告
└── golden/
    ├── healer_cases.json / analyzer_cases.json / generator_cases.json
    └── fixtures/*.html   # 静态 DOM 夹具
```

## 运行
```powershell
# 默认: 离线规则/骨架, 0 成本, 确定性 (CI 友好)
python -m ai_kit.evaluation.run_eval

# 含真实 LLM: 测 Phase-2 归因对比 / L4 自愈 / 真实用例生成 + 每次成本 (花钱/慢)
python -m ai_kit.evaluation.run_eval --llm

# 只跑部分套件
python -m ai_kit.evaluation.run_eval --suite healer,analyzer

# metrics 算法自检 (在项目根执行)
pytest tests/test_metrics.py
```
报告默认输出到**当前工作目录** `ai_evaluation_report.md` / `.json`（可用 `--out/--json` 或 `AI_KIT_REPORT_DIR` 覆盖）。

> Windows 控制台若中文乱码，用 `python -X utf8 -m ai_kit.evaluation.run_eval`。落盘文件始终为 UTF-8。

## 指标口径
| 指标 | 来源 | 算法 |
|------|------|------|
| top-1 / top-3 命中率 | 自愈 | `rank_candidates` 前 1 / 前 3 候选能否在夹具里解析到 `data-eval-target` 目标元素 |
| 分层命中率 (L1-L4) | 自愈 | 首个命中候选所属层归因 → 对照 README 30/50/10/10 |
| 分类准确率 + 混淆矩阵 | 归因 | 预测根因 == 黄金根因 |
| 置信度校准 (ECE) | 归因 | 置信度分 10 箱, Σ(箱权重 × \|平均置信-箱内准确\|) |
| 每次成本 | 归因/生成 | LLM token × `ai_kit/config.py` 价目表 (`--llm`) |
| py_compile 通过率 / 覆盖率 / 重复率 | 生成 | stdlib compile / 对"应覆盖清单"召回 / 方法名碰撞 + 步骤 Jaccard |

## 扩充黄金集
只需往 `golden/*.json` 加条目 +（healer）配一份 HTML 夹具，无需改评测代码。
- healer case: `fixture, broken_by, broken_locator, intent, expected_locator, expected_target`（夹具目标元素要打 `data-eval-target`）。
- analyzer case: `traceback, log, expected_category`（可选 `screenshot_fixture`，`--llm` 时渲染成截图走多模态）。
- generator case: `page_structure, po_methods, expected_categories, expected_keywords`。

## 换到目标网站
把 `golden/fixtures/` 里的 HTML 换成目标站点真实 DOM 快照、更新对应 case 的坏/正确定位器即可，评测逻辑与依赖不变。
