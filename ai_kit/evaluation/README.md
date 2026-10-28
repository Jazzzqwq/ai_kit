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
├── metrics.py           # 纯函数指标库 (top_k/layer/accuracy/gap/ECE/py_compile/coverage/dup)
├── golden.py            # 黄金集 schema 加载/归一/校验 + 规模底线 (7.2)
├── driver_provider.py   # 可插拔无头浏览器 (Edge→Chrome, 拿不到→自愈套件 SKIPPED)
├── cost_tracker.py      # token→元 成本累计 (每次 / 每页)
├── eval_healer.py       # 套件1: 坏定位器→正确定位器, top1/top3 + L1-L4 分层对照
├── eval_analyzer.py     # 套件2: 失败场景→根因, 准确率(严格/含容错) + 过度欠自信 + ECE
├── eval_generator.py    # 套件3: page_structure→用例, py_compile率/覆盖率/重复率 + 感知链两项率
├── eval_proposal.py     # 套件4: 方案层人决策 (接受率/验证成功率, 读本地 decisions.jsonl)
├── collect_golden.py    # 真页面采集 → 脱敏冻结 golden (两步式: collect / freeze)
├── run_eval.py          # CLI 入口, 汇总 → md + json 报告 (+ --smoke 冒烟)
└── golden/
    ├── healer_cases.json / analyzer_cases.json / generator_cases.json
    ├── flywheel_positive.json    # 6.6 数据飞轮导出的正样本 (存在才并入评测)
    ├── flywheel_negative.json    # 6.6 负样本 (反面清单, 不进分母)
    └── fixtures/*.html   # 静态 DOM 夹具
```

## run_eval 与 CI (7.3)
```powershell
# 零 LLM 零浏览器也出分 (离线规则/骨架, 0 成本, 确定性 → CI 友好)
python -m ai_kit.evaluation.run_eval

# 含真实 LLM: Phase-2 归因对比 / L4 自愈 / 真实用例生成 + 每次成本 (花钱/慢)
python -m ai_kit.evaluation.run_eval --llm

# 只跑部分套件
python -m ai_kit.evaluation.run_eval --suite healer,analyzer

# 退回手写黄金集基线 (不并入数据飞轮样本), 便于与上一版数字对比
python -m ai_kit.evaluation.run_eval --suite analyzer --no-flywheel

# --llm 冒烟: analyzer / generator 各 1 条, 验 usage 采集与成本换算
python -m ai_kit.evaluation.run_eval --smoke

# 黄金集规模/schema 校验 (样本不够或字段缺失 → 退出码 1)
python -m ai_kit.evaluation.golden

# metrics 算法自检 (在项目根执行)
pytest tests/test_metrics.py tests/test_golden.py tests/test_eval_report.py
```
报告默认输出到**当前工作目录** `ai_evaluation_report.md` / `.json`（可用 `--out/--json` 或 `AI_KIT_REPORT_DIR` 覆盖）。

`--smoke` 只有三条都过才算成功：真的调到了模型 / usage 采到了 / 价目表认识这个型号；任一条不过就退出码 1，CI 会红 —— 免得“绿着但链路是断的”。

**CI（`.github/workflows/eval.yml`）**：`windows-latest`（自带 Edge，selenium-manager 自动下驱动 → healer 真跑）+ 每周一 cron + 改动 push；跑完把报告 **commit 回仓库**，git 历史就是指标趋势看板。报告文件默认在 `.gitignore` 里（本地跑评测不打脏工作区），CI 用 `git add -f` 强制入库；配了 `QIANWEN_API_KEY` secret 才跑冒烟，没配自动跳过。

> 该 workflow 是 GitHub Actions 语法，仓库现托管 Gitee，不会自动执行；Gitee 上的等价做法是本地手动跑
> `python -m ai_kit.evaluation.run_eval`（含 `--llm --smoke`），或后续接 Gitee Go。

> Windows 控制台若中文乱码，用 `python -X utf8 -m ai_kit.evaluation.run_eval`。落盘文件始终为 UTF-8。

## 指标总表 (7.1)
“状态”只有两种：✅ 真跑在报告里；— 还没做（写进表里是为了不假装覆盖）。

| 模块 | 指标 | 口径 | 版本 | 状态 | 落点 |
|------|------|------|------|------|------|
| analyzer | 分类准确率 + 混淆矩阵 | 严格 `pred == expected.category`；另报含容错（`pred ∈ category ∪ acceptable_categories`），**分母同一个** | v1 | ✅ | `report.rule.accuracy` / `accuracy_tolerant` / `confusion_matrix` |
| analyzer | 过度/欠自信 | `mean(confidence) − accuracy`（标题指标，替代裸 ECE；\|gap\|≤0.02 算校准良好） | v1 | ✅ | `metrics.confidence_gap` → `report.rule.confidence` |
| analyzer | ECE（5 箱） | Σ(箱权重 × \|平均置信−箱内准确\|)，只做细节表 | v1 | ✅ | `metrics.expected_calibration_error(n_bins=5)` → `report.rule.calibration` |
| healer | top-1 / top-3 命中率 | `rank_candidates` 前 1/3 候选能否在夹具里解析到 `data-eval-target` | v1 | ✅ | `metrics.top_k_rates` → `report.top1/top3` |
| healer | 分层命中率 L1-L4 | 首个命中候选所属层占比 + 各层候选命中 case 数，对照 README 30/50/10/10 | v1 | ✅ | `metrics.layer_breakdown` → `report.layers` |
| cost | 每次成本 | 总成本 ÷ LLM 调用次数（token × 价目表快照） | v1 | ✅ | `cost_tracker.summary().per_call` |
| cost | 每页成本 | 总成本 ÷ 页数（generator 一条 case 一页） | v1 | ✅ | `cost_tracker.summary(units=).per_page` |
| generator | 去重率 | 步骤 Jaccard>0.8 近重复对占比（+ 方法名碰撞） | v2 | ✅ | `metrics.duplication_rate` → `report.avg_step_near_dup_rate` |
| generator | 元素覆盖率 | ≥1 条用例覆盖的交互元素 ÷ 总交互元素 | v2 | — | 现只有 `coverage_recall`（关键词召回，口径不同，不冒充） |
| generator | eid 解析率 | `plan` 里全部非空 `step.eid`（含 cleanup / 复合引用）能在采集清单 `resolve_ref` 到的比例 | v2 | ✅ | `metrics.perception_chain_metrics` → `report.chain.metrics.eid_parse_rate` |
| generator | 可执行率 | `render` 出码且 `verify` 后未标 `needs_review` 的用例占比 | v2 | ✅ | 同上 → `report.chain.metrics.executable_rate` |
| perception | 元素抽取 P/R | vs 人工标注（抽样 10 页一次性基准） | v2 | — | 待 v2 |
| proposal | 人工接受率 | `accept ÷ (accept+reject)`，defer 未决定不进分母 | v2 | ✅ | `metrics.proposal_decision_metrics` → `overall.acceptance_rate`（+ `by_type` 分列） |
| proposal | 验证成功率 | `accept 且 verify=passed ÷ accept`；一条都没采纳时显 n/a 而不是 0% | v2 | ✅ | 同上 → `overall.verify_success_rate` |

口径说明：
- **未知 ↔ 未知算对**：黄金集里 `expected.category = 未知-待人工分析` 的条目，规则答“未知”也计为对 —— 该说不说时不乱猜，是这个分类的存在意义。
- **容错只由人写**：`acceptable_categories` 来自黄金集人工标注，不用规则自报的歧义（否则规则说什么都对）。规则自报的歧义单列在 `rule_acceptable`，只作审计。
- **感知链两项率的分母**：eid 解析率按「引用」计（`n_refs`）、可执行率按「用例」计（`n_cases`）；分母为 0 时渲染层显 n/a 而不是 0%。
  离线是确定性规划器、无幻觉，解析率预期接近 100%（真能打破它的是 `--llm` 引用幻觉，本期未接）；
  被标 `needs_review` 的多是 `set_network` 这类需 CDP 断网的动作，渲染器诚实拒收。

## 黄金集 (7.2) — `golden/`
一份 schema，三套件共用（加载与校验在 `golden.py`）：

```json
{
  "id": "A21_prod_silent_reject",
  "module": "analyzer",
  "rule_version": "rules.v1",
  "source": "manual",
  "input": {"traceback": "...", "log": "...", "screenshot": "form_error.html"},
  "expected": {"category": "产品缺陷", "acceptable_categories": []},
  "note": "命中静默拒绝规则"
}
```

- `module`：这条 case 喂哪个套件（`analyzer` / `healer` / `generator`；飞轮的负面清单是 `proposal`）。
- `input` / `expected` 按模块不同：healer 是「夹具 + 坏定位器 + 意图」→「定位器 + 目标元素」；generator 是「页面结构 + PO 签名」→「应覆盖分类/关键词」。
- `rule_version`：标注那天的规则版本（analyzer 跟 `RULES_VERSION` 走），回归时先看它。
- `source`：`manual` / `flywheel` / `collected`（后者由 `collect_golden.py` 从真页面采集后脱敏冻结，见该模块 docstring）；报告里分开报 `n_manual` / `n_flywheel`，就按它数。
- 老平铺格式（`traceback` / `expected_category`）加载时自动归一 —— 6.6 已经导出过老形状的文件，存量数据不该因为改 schema 就读不了。
- **规模底线**：analyzer ≥30、healer ≥20、generator ≥8（当前样本 30 / 24 / 10）。`python -m ai_kit.evaluation.golden` 低于底线或字段缺失直接退出 1。
- **fixtures**：`golden/fixtures/*.html` 本地静态页（Element Plus 风格 + 通用表单各几份），评测不依赖真实站点。
- **所有报告带 n**：`analyzer.n` / `healer.n` / `generator.n` / 成本表也有 `样本 n`。

手写条目要点：
- healer case：夹具目标元素要打 `data-eval-target`，`expected.locator/target` 是“正确定位器与它该命中的元素”。
- analyzer case：`expected.acceptable_categories` 只在“另一个评审也会点头”时填，且不能重复自己。
- generator case：`page_structure/po_methods` 自带，`--llm` 才产真实用例。

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
- **标签出处写在样本里**：正样本的 `expected.category` 取自报告里的 `diagnosis`（机器给的），
  `category_source` 记着哪版规则；人只确认了动作（采纳 + 补丁通过）。真要当归因黄金用，先人工复核分类。

导出是幂等的（重复跑只追加新条目，既有条目一个字都不改）；缺 `report.json` 的决策跳过并点名 ——
现场没了就补不出样本，宁可少一条，不编一条。

## 换到目标网站
把 `golden/fixtures/` 里的 HTML 换成目标站点真实 DOM 快照、更新对应 case 的坏/正确定位器即可，评测逻辑与依赖不变。
