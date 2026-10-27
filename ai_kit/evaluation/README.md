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
    ├── flywheel_positive.json    # 6.6 数据飞轮导出的正样本 (存在才并入评测)
    ├── flywheel_negative.json    # 6.6 负样本 (反面清单, 不进分母)
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

# 退回手写黄金集基线 (不并入数据飞轮样本), 便于与上一版数字对比
python -m ai_kit.evaluation.run_eval --suite analyzer --no-flywheel

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
| 分类准确率 + 混淆矩阵 | 归因 | 预测根因 == 黄金根因（手写 + 飞轮正样本）|
| 置信度校准 (ECE) | 归因 | 置信度分 10 箱, Σ(箱权重 × \|平均置信-箱内准确\|) |
| 每次成本 | 归因/生成 | LLM token × `ai_kit/config.py` 价目表 (`--llm`) |
| py_compile 通过率 / 覆盖率 / 重复率 | 生成 | stdlib compile / 对"应覆盖清单"召回 / 方法名碰撞 + 步骤 Jaccard |

## 扩充黄金集
手写条目:
- healer case: `fixture, broken_by, broken_locator, intent, expected_locator, expected_target`（夹具目标元素要打 `data-eval-target`）。
- analyzer case: `traceback, log, expected_category`（可选 `screenshot_fixture`，`--llm` 时渲染成截图走多模态）。
- generator case: `page_structure, po_methods, expected_categories, expected_keywords`。

## 数据飞轮（6.6）：让黄金集随使用增长
`decisions.jsonl` 里的每条决策本身就是带标签的数据 —— 每周导一次，`evaluation/golden/`
就多出一批真实现场的样本，不需要人再手写 traceback：

```powershell
# 先看这一周会加什么（不写文件）
python -m ai_kit.proposal.flywheel --decisions decisions.jsonl --reports artifacts/ `
    --since 2026-10-20 --dry-run
# 确认后去掉 --dry-run
```

| 决策 | 落点 | 用途 |
|------|------|------|
| accept + 重跑通过（verified） | `golden/flywheel_positive.json` | 正样本；与手写 analyzer case 同形，**默认并入归因评测的分母** |
| 人拒 / 采纳但验证不过（rejected） | `golden/flywheel_negative.json` | 负样本，带 `rejected_reason`；**不进任何分母** |

两条约束：
- **正负必须分开**：reject 只说“这个方案不对”，不说明“归因错了”。把被拒的补丁塞进分类准确率，等于把正确的归因算成错。
- **标签出处写在样本里**：正样本的 `expected_category` 取自报告里的 `diagnosis`（机器给的），
  `category_source` 记着哪版规则；人只确认了动作（采纳 + 补丁通过）。真要当归因黄金用，先人工复核分类。

导出是幂等的（重复跑只追加新条目，既有条目一个字都不改）；缺 `report.json` 的决策跳过并点名 ——
现场没了就补不出样本，宁可少一条，不编一条。

## 换到目标网站
把 `golden/fixtures/` 里的 HTML 换成目标站点真实 DOM 快照、更新对应 case 的坏/正确定位器即可，评测逻辑与依赖不变。
