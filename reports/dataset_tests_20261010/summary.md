# 现有赛题数据测试记录（2026-10-10）

本次使用项目中已有的 Chinook PostgreSQL 数据和 OHR-Bench_v2 文本页样本。Chinook 有 11 张表、15,607 行；OHR 有 14 页，覆盖 7 个领域，现有索引含 62 个切片。测试模型为 `deepseek-flash`，本次实际调用模型 28 次。

| 测试 | 通过 / 总数 | 通过率 | 每题平均耗时 | 判断依据 |
| --- | --- | --- | --- | --- |
| Chinook 真实 NL2SQL | 14/14 | 100.0% | 2.11 秒 | 生成 SQL 经生产门禁、参数绑定和只读执行；完整行列与 gold SQL 结果严格一致 |
| OHR 真实检索问答 | 13/14 | 92.9% | 1.79 秒 | 固定事实检查、正确来源和页码、连续引用与内容摘要；实际 HTTP 文件下载摘要一致 |

SQL 首次模型草稿执行结果为 **14/14**；全部题目均无需模型修复。覆盖单表过滤/计数、时间筛选、多表关联、分组聚合、Top-3、LEFT JOIN 与空播放列表。两条原有问题结果分别为 USA / 13 位客户、Iron Maiden / 140 首销量。2010 年账单数为 0，按预先冻结的零计数边界保留。

OHR 的 13 个内容问题通过 **12/13**；另外 1 题在问题中明确指定 `ohr-005`，通过 **1/1**，生产检索器据此限定文档。默认检索实际使用 13 次 hybrid、1 次 lexical。题目与事实/证据标签在模型测试前已保存并记录 SHA-256，全部 14 题的预期连续证据均经原文验证；本次没有按模型输出修改标签。

## 失败用例

`existing-ohr-006`：询问 CO-OPERATIVE FEDERATION OF VICTORIA LTD 的 2004 年 Grant Income，预期值为 **11,621**。默认混合检索没有把正确文档放入前 5 个片段，模型返回 `INSUFFICIENT_EVIDENCE` 并拒答，计为失败。

只读诊断显示：正确片段的词法排名为第 1，但经 RRF 融合后降至第 15；双路均命中的泛相关片段占据前 5。诊断没有重新调用模型，原首轮报告仍为 13/14。

## 原文与格式噪声对照

额外用同一批 14 个问题，对全部 14 份 `gt_text` 和 `noise_text` 分别运行现有切片与词法排序函数；未建新数据库索引。去掉问题中的 `ohr-XXX`，标题置空，排除样本编号和标题对召回的直接帮助。

| 离线词法检索范围 | 原文 GT | 格式噪声文本 |
| --- | --- | --- |
| 13 个内容问题，Top-5 正确文档命中 | 13/13 | 13/13 |
| 全部 14 题，Top-5 正确文档命中 | 13/14 | 13/14 |
| 所有冻结 GT 连续证据在整页文本中的字面保留 | 14/14 | 2/14 |

原含样本 ID 的财务问题，去除 ID 后只问“文档讨论的主题和年份”，缺少定位内容，因此两组均未进入前 5。格式噪声使切片从 62 增至 79；连续证据字面保留率下降反映格式/标点变化，不能解释为语义答案错误。该对照没有调用模型，也没有运行图片 OCR，不能计作噪声条件下的问答准确率。

## 产物与复跑

- [Chinook 逐题结果](chinook.json)
- [OHR 逐题结果](ohrbench.json)
- [失败题检索诊断](ohrbench_q6_retrieval_diagnostic.json)
- [噪声检索对照](ohr_noise_retrieval.json)与[独立重放验证](ohr_noise_retrieval_verification.json)
- [测试前输入清单](input_inventory.json)、[测试后完整性检查](input_integrity_after.json)与[OHR 标签核验](ohr_label_validation.json)

在项目根目录执行下列命令。输出路径须不存在，以保留每次首次结果；SQL/问答命令会真实调用当前配置的模型。两个真实评测器遇到失败题返回退出码 1，仍保存逐题结果。运行需要 PostgreSQL/Qdrant 可达、模型配置可用；OHR 引用核验另需 API 在 `127.0.0.1:8000` 就绪。

```bash
.venv/bin/python scripts/eval_chinook_existing.py --output reports/dataset_tests_rerun/chinook.json
.venv/bin/python scripts/eval_ohrbench_existing.py --output reports/dataset_tests_rerun/ohrbench.json
.venv/bin/python scripts/eval_ohr_noise_retrieval.py --output reports/dataset_tests_rerun/ohr_noise_retrieval.json
```

## 适用范围

这些成绩来自当前现有样本上的自建冻结问题，不是完整 OHR 官方基准，也不能外推为通用 NL2SQL、OCR 或跨领域准确率。OHR 输入是已有文本 JSON，缺少原页图片/真实几何定位；本次验证了文件来源与文本引用，未评估视觉定位。

测试后原始输入 16 个文件的摘要、Chinook 11 张表的完整数据指纹、OHR 14 份文档版本与 62 个切片摘要均保持一致；冻结标签与实际评测代码摘要也通过复核。测试完成时本地 API 已启动，可通过 `http://127.0.0.1:8000/` 继续使用。

生成时间：2026-10-10T18:32:06.229307+08:00。
