# 可扩展要素提取（B2）：新增领域的成本清单与实测

**日期**：2026-10-10（北京时间）
**基线提交**：`e4e2ebf`（`git rev-parse --short HEAD` 只读读取）
**API 版本**：0.39.0（见 `/openapi.json`，本文件不改动运行时代码）
**对应官方要求**：中级任务难题②「可扩展要素提取：不依赖 Few-Shot 大提示词，新增领域可低成本扩展」（赛题 PDF 第 5 页）
**机读报告**：reports/extensible_fields_v1.json
**结论卡**：reports/cards/wp5a_extensible.md

---

## 0. 一页结论

| 问题 | 实测 | 证据 |
|---|---|---|
| 新增领域要改引擎代码行数 | **0 行**（`datamind/catalog.py` 无 diff；新增迁移 0 个） | 报告 `cost.engine_code_change_evidence` |
| 新增领域要写多少配置 | **273 行** manifest（字段定义 97 + 材料/解析声明 131 + 其余 45） | 报告 `cost.config_lines` |
| 新增领域要标多少样本 | **20 条记录 / 120 条字段断言**（849 行 `labels.json`） | 报告 `cost` |
| 抽取质量 | **120/120 = 100.0%**（6 字段 × 20 记录，含 3 条「材料里没有该字段」的 null 期望） | 报告 `results.declarative` |
| 是否靠示例堆 | 声明式臂 **0/3/10 示例三档均为 120/120，极差 0.0 个百分点** | 报告 `results.tier_spread_pp` |
| 对照：只给示例不给配置 | stub 臂 **2.5% → 52.5% → 100.0%**（极差 97.5 个百分点） | 报告 `results.tiers` |
| 原领域是否受影响 | 三个快照表数 **6/11/2**、评测用例 **14/12/4** 均未变；v1 Schema Linking 汇总不变 | 报告 `original_domain_guard` |

> **一句话**：在本实验范围内，新增一个未参与开发的领域只需**声明式配置 + 材料**，引擎零改动；
> 声明式臂在三档示例下完全不变（不靠示例堆），而「只给示例」的对照臂强烈依赖示例数量。

---

## 1. 领域选择与材料

- **领域**：设备维保工单（`equipment maintenance work orders`），**未参与本项目开发**，此前不在任何注册表、提示词或评测集里。
- **字段**（6 个）：设备编号 `equipment_id`、故障代码 `fault_code`、维保日期 `service_date`、工时 `labor_hours`（单位「小时」）、备件号 `spare_part_no`、维保结论 `conclusion`（枚举：正常/需更换备件/需复检/观察）。
- **材料**（6 份、3 种格式、20 条记录，均由 `build_suite.py` 确定性生成）：

| 材料 | 格式/解析模式 | 记录数 | 标签拼写（用于考别名） |
|---|---|---|---|
| `m01_txt_labeled.txt` | txt / text_blocks（`：`） | 3 | 设备编号/故障代码/维保日期/工时/备件号/维保结论 |
| `m02_docx_labeled.docx` | docx / text_blocks（`----` 分段） | 3 | 设备号/故障码/服务日期/维修工时/备件编号/处理结论 |
| `m03_xlsx_table.xlsx` | xlsx / xlsx_table | 4 | 设备编号/故障代码/维保日期/工时/备件号/维保结论 |
| `m04_docx_table.docx` | docx / docx_table | 3 | 设备ID/故障编码/维保时间/人工工时/更换备件/维保结果 |
| `m05_txt_delimited.txt` | txt / text_blocks（`\|` 分隔） | 3 | 设备编号/故障代码/维保日期/工时/备件号/维保结论 |
| `m06_xlsx_two_sheets.xlsx` | xlsx / xlsx_table（2 个 sheet） | 4 | 设备号/故障代码/服务日期/工时/备件编号/处理结论 |

- 材料里**故意留了 3 处缺字段**（m02 一条无备件号、m04 一条无备件号、m06 一条无工时）：期望值为 `null`，抽取器补值即算失败。
- 标注纪律：`labels.json`（120 条断言）由 `build_suite.py` 从**作者手写的 SPEC** 生成，与抽取器输出无关；`--check` 会在材料或标签被改动时以退出码 1 失败。

---

## 2. 新增领域「要改什么」：可核对清单

| 项 | 本次新增量 | 核对方式 |
|---|---|---|
| 元数据库表结构 / DDL | **0**（无新迁移、无 ALTER） | `git status --porcelain db/migrations` → 空；`manifest.registry_contract.ddl_changes=0` |
| 注册表写入 | **0**（`schema_tables/_columns` 无 insert/update） | `registry_contract.registry_writes=0`；本 WP 全部 SQL 只读 |
| 引擎代码 | **0 行**（`datamind/catalog.py` 无 diff） | `git diff --numstat HEAD -- datamind/catalog.py` → 空；文件哈希 `1b5ab88f…` |
| 声明式配置 | **273 行** `manifest.json` | 报告 `cost.config_lines` |
| — 其中字段定义 | 97 行（6 个字段，属性集与 `schema_columns` 对齐） | `manifest.fields` |
| — 其中材料/解析声明 | 131 行（6 份材料的格式、分隔符、表头行、标签表） | `manifest.materials` |
| 标注样本 | **20 条记录 / 120 条断言** | `labels.json`、`cost.assertion_count` |
| 抽取器（新增脚手架，非引擎） | `scripts/eval_extensible_fields.py`（460 行） | 新增文件，不改既有模块 |
| 原领域回归 | 不受影响（只读计数与 v1 汇总均不变） | `original_domain_guard`；`tests/test_schema_linking_v2.py::V1UnchangedTest` |

**属性契约复用**：manifest 的字段属性刻意使用与 `schema_columns` 相同的名字
（`business_name`→`label`、`aliases`、`enum_map`、`semantic_role`、`sensitivity`、`unit`）。
新领域**没有**新增表结构，只是同一属性契约的另一份声明式实例。

**引擎复用证据**：`scripts/eval_extensible_fields.py` 用 `Catalog.__new__` 注入上述声明式行，
调用**未修改的** `Catalog.retrieve()` 完成「问题 → 字段定位」；WP6 的 v2 fixture 用的是同一注入手法。

---

## 3. 三档对照：不靠示例堆

口径：同一冻结集（120 条断言）、同一分母；`0_examples / 3_examples / 10_examples` 的示例记录 id 在
`manifest.example_tiers` 中显式声明。两条臂都不调用上游模型：

- **`declarative`（本 WP 主张的机制）**：字段标签/别名/取值模式全部来自 manifest，示例只作对照变量。
- **`stub_fewshot`（对照替身）**：**不使用** manifest 的别名与取值模式，只从示例（含示例真值）反推
  「标签拼写 → 字段」，再用学到的拼写去抽取其余材料。它不是真实 few-shot LLM，只回答「示例买到了什么」。

| 档位 | declarative | stub_fewshot |
|---|---:|---:|
| 0 示例 | 120/120 = 100.0% | 3/120 = 2.5% |
| 3 示例 | 120/120 = 100.0% | 63/120 = 52.5% |
| 10 示例 | 120/120 = 100.0% | 120/120 = 100.0% |
| **极差** | **0.0 个百分点** | **97.5 个百分点** |

**判定**：声明式臂极差 0.0 ≤ 5 个百分点 → 可以陈述「该实现不靠示例堆」。
**同时如实报告反向结论**：示例确实买到东西——它买到的是**标签拼写覆盖面**（m01/m03/m05 用标准标签，
3 条示例即可覆盖；m02/m04/m06 用别名拼写，只有示例里出现过才学得到）。这正是把拼写写进配置（别名表）而不是
写进示例的理由。

按维度分解（declarative 臂）：格式 `docx 36/36`、`txt 36/36`、`xlsx 48/48`；
6 个字段各自 `20/20`；带定位信息的取值 `117/117`（另 3 条期望为 null，无取值）。

---

## 4. 失败样本

本集 declarative 臂 **0 失败**。为避免「零失败」被误读，明确列出本集**包含**的边界与失败：

1. **stub 臂失败（示例不足）**：0 示例档 117 条 `missed_value`；3 示例档 57 条 `missed_value`。
   完整清单在报告 `results.tiers[].stub_learned_labels` 与逐条 `failures`（declarative 臂）中；
   本集 stub 失败集中在 m02/m04/m06（别名拼写）。
2. **非法取值被拒绝而非补值**：抽取器对不符合声明模式的取值不产出字段（`invalid_value_rejections` 本集为 0 条；
   单测 `tests/test_extensible_fields.py::ExtractionBoundaryTest::test_invalid_value_is_rejected_not_fabricated`
   用 `SP-99X` 构造了一条被拒样本，断言它进入 `value_failed_declared_pattern` 而不是变成字段值）。
3. **缺字段不补值**：3 条 null 期望必须命中 null；补值会被记为 `fabricated_value`（本集 0 条）。

> 「0 失败」的适用范围只有这 6 份材料、20 条记录。它**不能**说明抽取器在真实工单系统上准确率 100%。

---

## 5. 复现命令（实测退出码）

```bash
cd "/Users/caicai/DataMind Agent"

# 1) 生成 + 冻结校验（确定性、离线）
.venv/bin/python data/evaluation/extensible_field_v1/build_suite.py --write   # exit 0
.venv/bin/python data/evaluation/extensible_field_v1/build_suite.py --check   # exit 0
# 篡改任一材料后 --check 退出码 1（由测试覆盖）

# 2) 评测（确定性规则抽取，不调用上游模型）
.venv/bin/python scripts/eval_extensible_fields.py                            # exit 0
# → declarative: 120/120 = 100.0%

# 3) 隔离回归（只跑本 WP 的测试文件）
.venv/bin/python scripts/test_isolated.py --pattern test_extensible_fields.py # exit 0
# → Ran 17 tests ... OK；"failures": 0, "errors": 0, "metadata_isolated": true,
#   "source_database_modified": false, "commit": "e4e2ebf"
```

---

## 6. 诚实边界（不能据此声称什么）

1. **不是大模型 few-shot 实验**：抽取器是确定性规则解释器；本文件不度量任何模型的 few-shot 能力，
   也不能用来论证「大模型不需要示例」。
2. **「引擎 0 行改动」限定在本实验范围**（离线要素提取 + 字段定位复用 `Catalog.retrieve`）。
   把这些声明式配置**写入运行时注册表**所需的 loader、权限登记、发布流程**未实现**，
   不计入这个 0；端到端生产接入的改动量**未测量**。
3. **stub 臂是替身**：它证明「示例只买到拼写覆盖」，不等于真实 few-shot 提示词的收益。
4. **自建冻结小集**：6 份材料、20 条记录、3 种格式，均为本项目合成材料；
   不能外推为真实工单系统的准确率或官方评测成绩。
5. **确定性依赖归一化**：`.docx/.xlsx` 的 zip 时间戳与 `docProps/core.xml` 的 `modified` 被统一改写为
   2026-01-01，才有逐字节可复现；未归一化时同一天内重复生成也会得到不同字节（已在文档与测试中记录）。
6. **原领域守卫是只读计数比对**：工作区同时存在其他工作包的改动（例如 `datamind/industrial_rules.py`），
   本文件不评价它们的正确性。

---

## 7. 评分联动

| 官方项 | 本文件能提供的材料 | 不能提供的 |
|---|---|---|
| 中级任务难题②（E8/F8 第三项） | 一个新领域的成本清单 + 三档对照 + 冻结集 + 失败清单 + 复现命令 | 「低成本」的官方主观判定 |
| E8/F8 质量加分 | 引擎 0 行改动、原领域不受影响的可核对证据 | 官方分数 |
| F4（复杂 NL2SQL 支撑） | 字段定位复用同一 `Catalog.retrieve` 的证据 | 跨库泛化结论 |

> 所有数字来自 reports/extensible_fields_v1.json 的实测输出；
> 本文件不声明任何官方分值。
