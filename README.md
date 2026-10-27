# ai_kit

可移植的 Web UI 测试 AI 工具箱：定位器自愈、失败归因、用例生成、离线评测。
依赖只有 `stdlib + selenium + requests`，包内不 import 被测项目的 `pages/config/common`。

- 版本：`0.1.0`　Python：`>=3.9`　测试：`python -m pytest -q`
- 默认 0 成本（只跑规则层），加 `--llm` 才调大模型，每次调用都记账

## 它解决什么

| 测试现场的老问题 | ai_kit 的做法 |
| --- | --- |
| 定位器一改就崩，靠人肉 F12 猜 | `healer` 四层自愈：L1 规则映射 → L2 多语义 CSS → L3 相对 XPath → L4 LLM 兜底，命中结果落缓存 |
| 挂了说不清是谁的锅 | `proposal/blame` + `analyzer` 六分类归因（5 类规则可判 + 未知兜底），规则不够准才降级 LLM，输出产品缺陷 / 环境 / 测试代码 / 未知四路 |
| 用例一条条手写 | `perception` 采集 DOM → 元素清单 → 页面原型 → 策略 → 用例计划 → 渲染 pytest 代码；`generator` 两步法补业务断言 |
| "AI 生成得好不好"没有度量 | `evaluation` 黄金集 + 指标（top-k / 分层 / 校准 / 编译通过率 / 重复率）+ 成本台账，报告可进 CI |

## 模块地图

```
perception/  DOM 采集 → 元素清单 → 页面原型 → 策略 → 用例计划 → pytest 渲染
runtime/     standalone 运行原语: Page 的 59 个操作 + loc()，统一显式等待
proposal/    失败诊断 → 方案类型 → 确认报告 → 应用/回滚/验证 → 账本反哺黄金集
evaluation/  黄金集 + 指标 + 成本 → md/json 报告（详见 ai_kit/evaluation/README.md）
profiles/    规则包: 内置 generic / element_plus / antd + 站点 profile，全部 JSON
utils/       sanitize: 送 LLM 前的 PII 脱敏
client.py    LLM 客户端（OpenAI Chat Completions 兼容，多模态，usage 回填）
config.py    provider 配置（密钥只走环境变量）+ 单价表
ledger.py    预算上限 + 每次调用记账
analyzer.py  失败归因门面（规则优先 → LLM 降级）
healer.py    分层自愈定位器
```

## 快速开始

```bash
pip install -e ".[test]"

# 1) 全量测试（Windows 上建议先设 PYTHONUTF8=1，避免子进程按 GBK 输出）
python -m pytest -q

# 2) 黄金集规模体检（样本不够直接退出码 1）
python -m ai_kit.evaluation.golden

# 3) 离线评测：0 成本，产出 md + json 同款报告
python -m ai_kit.evaluation.run_eval --out ai_evaluation_report.md --json ai_evaluation_report.json

# 4) 可选：真实 LLM 冒烟（会花钱，每次调用记账）
python -m ai_kit.evaluation.run_eval --llm --smoke --suite analyzer,generator
```

跑真实归因/自愈需要 provider 配置：`AI_ACTIVE_PROVIDER` + 对应 `*_API_KEY`（见 `.env.example` /
`ai_kit/config.py`），密钥永远不落盘、不进提示词。

## 数据安全

这个包会把页面内容送到外部大模型，所以出境点写了明文清单，代码改到这里请同步本表。

### 出境点清单

| 出境点 | 送出去的内容 | 处理 |
| --- | --- | --- |
| `healer` L4 | 最多 3 段可见容器 `outerHTML`（每段截断 4000 字符） | **已脱敏**：拼提示词前过 `ai_kit.utils.sanitize` |
| `analyzer` 文本 | traceback 末 2000 字符 + log 末 1000 字符 | **已脱敏**：同一函数，出境前过 `sanitize` |
| `analyzer` 多模态 | 失败截图 base64（1 张） | **不做文本脱敏**：像素里的身份证/病历号无法用正则抹。要合规就裁切，或者不传 `screenshot_base64` |
| 评测黄金集 | `golden/*.json` + `golden/fixtures/*.html` | 全部手写合成页，不含真实业务数据；`--llm` 跑评测时送出去的也是这些合成样本 |
| `perception` 采集 / 用例生成 | 元素清单（`text/attrs/constraints/html`） | `value` 在页面内 JS 里就换成 `[REDACTED]`（`collector.py`）；元素可见文本**未过** `sanitize`，见下方边界 |

### 脱敏做什么

`ai_kit.utils.sanitize`（纯 stdlib 正则、无状态、幂等）会抹掉：

- HTML 的 `value` 属性、`<textarea>` 内容（`option/li` 的 value 是选项标识，保留）
- `class/id/name/data-*` 带 `patient-name`、`idCard`、`phone`、`mrn` 之类提示的元素内文
- `姓名：X`、`<label>姓名</label>…X`、`病历号：X` 这类"字段名 + 值"里的值
- 手机号（含 `138-1234-5678` 分隔写法）、身份证（18 位含 X / 老式 15 位）

刻意保留：标签名、属性名、class、可见按钮文案（`删除患者` 这种）。LLM 靠这些推选择器，
全抹掉等于把自愈能力废掉 —— 脱敏的目标是"值不出境"，不是"页面不可读"。

### 边界（不要当成万能合规方案）

- 不认识**没有字段名提示的裸文本**：表格里孤零零一个"张三"，正则分不出它是姓名还是科室名。
  这类靠 `value` / `class` / 字段名前缀覆盖，漏了就漏了，宁可漏也不乱猜；
- **`perception` / `generator` 的链路只做了 value 脱敏**：元素清单里的可见文本（`text`）没接 `sanitize`，
  医疗类页面表格里的姓名仍可能随用例生成出境。接法同 L4（拼提示词前包一层），但会改变生成质量，
  需要先在黄金集上对比，所以没有顺手改；
- 不处理**图片**：截图见上表；
- 不做**去标识化关联**：抹掉的是值本身，不是把它换成稳定假名，所以"同一患者"在两次调用间不可追踪。

要接新的出境点，先加 `sanitize(...)`，再在 `tests/test_utils_sanitize.py` 里补一条"合成数据 → 断言零残留"。

## 现状与缺口

- `utils.dom_diff` 只有声明，尚未实现（现在能用的只有 `sanitize`）；
- 评测指标的 v2 六项（跨页泛化 / 稳定性等）在报告里标 `—`，没跑就是没跑；
- 真实 LLM 全链路（`--llm` / CI 冒烟）尚未在本机用真 key 跑通，报告里的数字都是离线规则层实测。
