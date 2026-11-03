# 版本化证据绑定的度量与三臂对照消融（WP3）

> 一页结论：在 **40 例自建冻结陷阱集** 上，`full` 臂的错误绑定率 **0/17 = 0.0%**，`plain` 臂
> **32/40 = 80.0%**，相对下降 **100%**、绝对值 **0% ≤ 5%** → **达到**预设的机制有效性门槛。
> 但本消融的作答器是**确定性 stub（model_calls = 0）**、陷阱集是**自建**的，所以它只能支撑
> 「机制在冻结定义下有效」的**工程**举证，**不能**单独支撑「技术领先」的判断。

数据与复现：`data/evaluation/binding_traps_v1.json`（sha256 `1fdfa89b…`）、
`datamind/binding_eval.py`、`scripts/eval_binding.py`、`reports/binding_ablation.json`、
`tests/test_binding_eval.py`。

---

## 1. 度量定义（分母分子同给，零分母报 N/A）

| 指标 | 定义 | 分母 |
|---|---|---|
| 绑定准确率 | 正确绑定的答案声称 / 全部答案声称（每例一条答案声称） | 40（冻结例数） |
| **错误绑定率** | **绑定错误却仍以 `confidence=high` 输出的声称 / 全部 high 置信声称** | high 置信声称数（plain 40 / bind_only 35 / full 17） |
| 冲突精确率 / 召回率 | 以「两侧同 `entity+metric+version` 却无法归约为同一声称」为真值：TP/(TP+FP)、TP/(TP+FN) | 真值冲突 25 例 / 无冲突 15 例 |
| 误报率 | FP/(FP+TN) | 无冲突例数 15 |
| 修复账本 | `attempts`（记账的尝试）/ `changed`（发生改写）/ `succeeded` / `rejected` / `misrepair` | 成功率 = 成功/发生改写；**误修复率 = 误修复/发生改写**；零分母报 N/A |
| 成本 | `cost_ms` = 该臂在整 40 例上的墙钟 CPU 时间（每次运行会浮动，表内为冻结报告那次的值）；`model_calls = 0`、`tokens = 0`（stub 不调用模型，token 成本不适用） | — |

补充口径（防止分母缩水掩盖错误）：报告同时给出 `misbinding_rate_all_cases`（错误/全部例）、
`binding_field_accuracy`（entity/version/unit/time_scope 四个字段，分母 = 17 例可作答）、
`asserted_high` / `abstained_or_low`、`resolution_accuracy`。

**不可作答例的判定**：当两侧不可比或矛盾无权威时，「正确」= 检出差异 **且** 不以 high 断言；
若该例本来就没有竞争性数值声称（只是绑定/核验缺口），则「不以 high 断言」即正确。断言即错。

## 2. 三臂差异（同一冻结集、同一 stub 作答器）

| 臂 | 绑定 | 版本适用性核验 | 单位可比性核验 | 跨源比对/冲突门 | 受控修复 | 置信度策略 |
|---|---|---|---|---|---|---|
| `plain` | 不做，原样取用 SQL 行的 unit/version/scope | 否 | 否 | 否 | 否 | 一律 high |
| `bind_only` | 做：approved 别名 → entity_id、单位规范化、时间口径解析、取声明/快照版本 | 否 | 否 | 否 | 否 | 绑定七字段完整才 high |
| `full` | 做 | 做：按**登记版本有效期窗口**判定，必要时由窗口取适用版本 | 做：不可精确归一的换算必须拒绝 | 做：无权威登记时保留双方、标记待裁决 | 最多 1 次，全部记账 | 通过全部门才 high |

## 3. 冻结集与标签纪律

- 40 例 = 四类陷阱各 10 例：同对象跨版本、单位不同/不可归一（英寸/毫米、degF→degC 非终止换算、
  自定义单位）、时间口径不同（自然月/季度/生产批次期间）、文档与库数值相反；另含 9 例负向对照
  （单位等价、口径一致、数值一致、声明版本已正确）用于约束误报与过度拒答。
- 每例先冻结 `expected_binding{entity_id, version, unit, time_scope}`、`expected_answerable`、
  `expected_conflict`、`expected_resolution ∈ {resolved_by_authority, unresolved, needs_review}` 与
  `trap_kind`、`provenance`，再实现任何臂。
- 生成器确定性：`datamind/binding_eval.py::generate_dataset()` 是唯一来源；
  `scripts/eval_binding.py --check` 重新生成并逐字节比对 + 用 `validate_labels()` **从 fixture 独立
  重推** `expected_conflict`/`answerable`，任一标签被放宽即 `BINDING_TRAPS_LABELS_LOOSENED`（exit 1）。
  数据集摘要被钉在 `TRAPS_SHA256`（文件被手改则评测拒跑，`BINDING_TRAPS_DATASET_CHANGED`）。

## 4. 结果与硬判定

| 臂 | 绑定准确率 | **错误绑定率** | high 断言 | 冲突 P | 冲突 R | 误报率 | 修复 attempts/changed | 成功/误修复 | cost_ms |
|---|---|---|---|---|---|---|---|---|---|
| plain | 0.200 (8/40) | **0.800 (32/40)** | 40 | N/A（无检出） | 0.0 (0/25) | 0.0 (0/15) | 0 / 0 | N/A | 0.34 |
| bind_only | 0.200 (8/40) | **0.771 (27/35)** | 35 | N/A | 0.0 (0/25) | 0.0 (0/15) | 0 / 0 | N/A | 0.45 |
| **full** | **1.000 (40/40)** | **0.000 (0/17)** | 17 | 1.0 (25/25) | 1.0 (25/25) | 0.0 (0/15) | 15 / 9 | 1.0 / 0.0 | 8.90 |

字段级绑定（分母 = 17 例可作答）：plain `entity 15/17, version 9/17, unit 17/17, time 16/17`；
bind_only `entity 17/17, version 9/17, unit 17/17, time 16/17`；full 四项均 **17/17**。
→ 结论：**只绑定不核验不够**（别名规范化只把 entity 修好，版本适用性 9/17 原地不动）。

**硬判定**：`full` 相对 `plain` 错误绑定率下降 `(0.8-0.0)/0.8 = 100% ≥ 50%`，绝对值 `0% ≤ 5%`
→ **达到预设的机制有效性门槛**。对外表述须同时写明第 5 节边界；
若外部不接受 stub/自建集作为有效性证据，则只保留定性说明、不给出量级结论。**`plain` 并非
"几乎不犯错"**（80% 高置信错误绑定），因此本项不落入「基线无差异」的例外。

**within-full 敏感性探针**（同一冻结集，逐个拆掉子机制）：

| 变体 | 绑定准确率 | 错误绑定率 | high 断言 | 说明 |
|---|---|---|---|---|
| full（基线） | 1.000 | 0.000 | 17 | 全机制 |
| 去掉版本适用性核验+版本修复 | 0.775 | **0.500 (9/18)** | 18 | 过期版本被高置信输出 |
| 去掉冲突门 | 0.450 | **0.541 (20/37)** | 37 | 未裁决冲突照答 |
| 去掉修复 | 0.800 | 0.000 | 9 | 改为过度拒答（可作答例只断言 9 条） |

## 5. 诚实边界（对外表述必须同步带上）

1. **stub 不等于真实模型准确率**：作答器是确定性桩（`model_calls = 0`），不调用任何上游模型，
   因此本报告**不是**模型准确率，也不能外推为业务准确率；token 成本不适用。
2. **自建冻结集**：40 例由本仓库编写并冻结，`plain` 的失败来自陷阱的设计。相对下降是
   **机制对照**结果，不是泛化能力，也不外推为业界领先。
3. **`full` 40/40 不构成泛化能力证据**：它是「陷阱集与机制同源设计」的结果；能说明机制有效的是
   敏感性探针（拆掉任一子机制立刻出现 9–20 条高置信错误绑定）。
4. **不是端到端 live 结果**：不衡量真实演示库查询、真实检索或前端表现。
5. **冲突口径**：只统计 `source_kind` 不同的同 `(entity, metric, version)` 数值对；不可比
   （单位/口径/版本适用性）记为 `needs_review` 而非 `unresolved`；「检出冲突」不等于判对了数值。
6. **依赖会漂移**：`datamind/binding.py`、`evidence_graph.py`、`cross_source.py` 在本轮被其他 WP
   持续修改；报告记录了本次数值对应的文件 sha256（`dependency_digests`），复跑前应先比对。
   本轮 `--check`/评测在 `datamind/binding.py` 新增 `degF`（理性精确换算）之后完成。
7. **零分母纪律**：所有零分母一律 `null`（N/A），不用 0 冒充。
