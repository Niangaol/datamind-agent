# 交付清单（官方提交物 × 当前状态 × 文件路径 × 缺什么）

**前端交付补充（2026-10-10）**：旧版 `web/` 根目录前端已退役删除；交付脚本现纳入 `frontend/` Vue 源码与现有 `web/vue/` 产物，排除依赖目录和开发缓存。首次运行需按 [README](../README.md) 构建 Vue。下文既有 ZIP 计划、交付件和 UI 验收是历史快照，本次未重新生成 ZIP 或 Office 交付件；新版页面范围见 [前端评估](FRONTEND_NEXT_STEPS_20261010.md)。

| 字段 | 值 |
|---|---|
| **WP** | WP10「交付材料成套化」 |
| **日期** | 第一轮 2026-10-09；第二轮（修复轮）交付同步 2026-10-09 23:4x；第三轮（收口轮）交付同步 2026-10-10 08:35 CST；**第四轮（收口轮）交付一致性校对 2026-10-10 09:03 CST**（机器时钟 `date "+%Y-%m-%d %H:%M %Z"` → `2026-10-10 09:03 CST`） |
| **基线提交** | `e4e2ebf`（`cd "/Users/caicai/DataMind Agent" && git rev-parse --short HEAD`，只读） |
| **API 版本** | `0.39.0`（`GET http://127.0.0.1:8000/openapi.json` → `info.version`；`datamind/api.py:198`） |
| **状态词** | 已具备 / 部分 / 未做 / 无法判定（不出现自评分数） |
| **第三轮改动范围** | 只重写 04 创新自述（DOCX + PDF，**4 页**）以纳入第三轮新证据（跨源 P2–P4 规范语料、跨源 live、三臂消融的独立复核状态、F1 档位判断），并同步本清单、`交付/DELIVERY_MANIFEST.json`、`reports/office_checks.json`。01 设计报告 / 02 技术说明书 / 03 答辩演示的**内容未改动**，sha256 与本清单第一轮/第二轮逐个一致（`docx1`/`pdf1`/`docx2`/`pdf2`/`pptx` 五键三轮同一） |
| **第四轮改动范围** | ① **修掉被第三轮独立复核证伪的主张**：`data/knowledge_sources/build_norm_corpus.py --check` 的 `deterministic: true` 曾在 17 次运行中 4 次为 false（根因 `/ID` 只匹配 `<hex><hex>`，PyMuPDF 的 `(...)` 字面串被静默放行）→ 改为解析 trailer `/ID` 数组并显式写回内容派生 id，修复后 **12/12 次 `deterministic: true`**，重建 14 份语料与 `manifest.json` 与冻结 sha256 **逐字节一致**（未改 `data/evaluation/cross_source_norm_v1.json`，`eval_cross_source_norm.py --check` 仍 exit 0）。② **交付件一致性校对**：只读复核 04（DOCX 46809 B / `365b47c9…`；PDF 4 页 / 517145 B / `a6457cc3…`）——F1「不主张 6 分档」、三臂消融 `plain 0.8 → full 0.0 / meets true`、P2–P4 28/28 均与仓库现状一致，**04 内容未改**；更正本清单与 `交付/DELIVERY_MANIFEST.json` 中已过期的回归分母（1497/31）与「不得写语料逐字节可复现」两处边界，并更正清单中 04 DOCX 的第二轮遗留 bytes/sha256。③ 本轮**未跑无参数全量回归**（由主协调者统一跑），因此不背书「当前版本全绿」 |

> 本文件只做「官方提交物 ↔ 仓库现有文件」的逐项核对，不给任何官方得分结论。
> 所有路径相对仓库根目录。分值一律引自官方赛题 PDF 第 8–12 页，未改写。

---

## 1. 初赛提交物（官方赛题 PDF 第 8–10 页）

| # | 官方要求（页码） | 状态 | 文件路径 | 缺什么 |
|---|---|---|---|---|
| D1 | 设计报告：Word + PDF，建议 ≤20 页，第 1–2 页「项目核心亮点预览页」（p.8） | **已具备** | [交付/01_设计报告_DataMind_Agent.docx](../交付/01_设计报告_DataMind_Agent.docx)、[.pdf](../交付/01_设计报告_DataMind_Agent.pdf) | 未做逐页人工视觉检查（会话模型不接受图像输入）；决赛深化稿未做 |
| D2 | 技术实现说明书：Word + PDF，建议 ≤30 页，覆盖架构/NL2SQL/RAG/多源融合/可解释性/中级任务/测试评估（p.9） | **已具备** | [交付/02_技术实现说明书_DataMind_Agent.docx](../交付/02_技术实现说明书_DataMind_Agent.docx)、[.pdf](../交付/02_技术实现说明书_DataMind_Agent.pdf) | 同上；Mermaid 以文字 + 代码块给出，Word 内不渲染 |
| D3 | 完整源代码 + 清晰目录结构说明 + README | **已具备** | 源码：`datamind/`、`ingestion/`、`frontend/`、`web/vue/`、`scripts/`、`tests/`、`deploy/`；说明：[README.md](../README.md)、[PROGRESS.md](../PROGRESS.md) | README/PROGRESS 的回归分母已在第四轮同步为 **1578/1578**（`grep -c 1497 PROGRESS.md README.md` → 0）；**是否全绿只认 `reports/latest_regression.json` 的 `failures`/`errors`**，该文件会被任何 `--pattern` 运行覆写（见 §4 第 1 条） |
| D4 | 数据库 schema 定义文件 | **已具备** | `db/migrations/001–019`（19 个；019 仍为未跟踪 `??`）、`db/chinook/Chinook_PostgreSql.sql`、`deploy/init/*.sql` | `db/migrations` 与 `deploy/init` 两套 DDL 的**空库等效性未验证**（本轮禁止重建容器/库） |
| D5 | 知识库文档样本 ≥10 份、涵盖不同类型文档格式 | **已具备（打包前需确认纳入方式）** | `data/knowledge_base/*.pdf`（12 份 + 2 份原生版）、`data/ohr_bench_samples/*.json`（15 份）、`data/qa_samples/01–11`（原生 PDF 2 / 扫描 PDF 2 / DOCX 2 / XLSX 2 / PPTX 2 / DXF 1）、`data/evaluation/extensible_field_v1/materials/`、**第三轮新增 `data/knowledge_sources/`（5 PDF + 9 DOCX，30 条条款）** | `data/qa_samples/`、`extensible_field_v1/materials/` 与 `data/knowledge_sources/` 均为**未跟踪文件**（`git ls-files` 无输出），需拍板是否纳入版本控制；`data/uploads/development/` 的上传临时件**不进包** |
| D6 | 模型权重文件（若适用） | **按官方「列明获取/缓存/网络依赖」提供清单** | [docs/MODEL_WEIGHTS.md](MODEL_WEIGHTS.md) | 仓库不含权重（`.cache/` 被忽略）；不得把「只列下载地址」等同于官方接受的权重交付 |
| D7 | 运行环境配置说明（依赖包列表及版本号） | **已具备** | `requirements.txt`、`requirements.lock`（65 个固定版本）、`requirements-{sql,rag,ocr,formula,office,cad}.txt`、守护测试 `tests/test_dependency_lock.py` | 无 |
| D8 | 可执行 Demo 的快速启动脚本 | **已具备** | `scripts/start_local.sh`、`scripts/start_api_background.py` | **未在干净空库环境演练启动**（本轮禁止重建容器/库），不主张「新环境已验证可启动」 |
| D9 | 答辩 PPT：≤20 页，不得出现单位/人名（p.11） | **已具备** | [交付/03_答辩演示.pptx](../交付/03_答辩演示.pptx)（实测 20 张，sha256 三轮未变） | 主办方正式模板另由主办方提供（本包未使用）；匿名检查方法见 §3 |
| D10 | 决赛版技术文档：≤50 页深化稿（p.11–12） | **未做** | — | 决赛深化稿（含初赛反馈响应）整体未做；本轮只交付初赛口径的两份 DOCX/PDF |
| D11 | 决赛版技术数据 ZIP：完善后的全部代码与数据（p.12） | **部分** | 计划脚本：[scripts/build_delivery_zip.py](../scripts/build_delivery_zip.py)（dry-run）；机读计划：`交付/DELIVERY_MANIFEST.json` 的 `zip` 段 | **本轮仍不打 ZIP**（避免把 `.venv`/`.cache`/临时产物打进包）；机读段记录 `planned_file_count 462` / `planned_total_bytes 11 620 175`（11.08 MiB），**该数字随工作区并发写入变化**，引用须带生成时间；本轮未重跑 dry-run |

---

## 2. 申报加分提交物（官方赛题 PDF 第 7、12、14 页）

| # | 官方要求 | 状态 | 文件路径 | 缺什么 |
|---|---|---|---|---|
| C1 | 《突破与创新性自述》独立 PDF，篇幅 ≤5 页，含定位声明 / 核心贡献 / 举证 / 评审价值（p.7） | **已具备（第三轮重写）** | [交付/04_突破与创新性自述.pdf](../交付/04_突破与创新性自述.pdf)（实测 **4 页**）、DOCX 源 [交付/04_突破与创新性自述_DataMind_Agent.docx](../交付/04_突破与创新性自述_DataMind_Agent.docx) | 自述已逐份点名证据文件真实状态（见 §2.1）并给出 **F1 档位判断（不主张 6 分档）**；无逐页人工视觉判定（会话模型不接受图像输入），仅 4 页渲染 + 167 文本块越界 0 的部分替代 |
| C2 | 技术突破领先性（0~10，p.14） | **无法判定 / 不申报** | — | 无公开基准成绩、无与公开最优方案的对比实验、无真实视觉模型对比；自述已声明不申报该方向，并写明「不得主张业界领先」 |
| C3 | 实用创新与转化价值（0~10，p.14） | **部分（材料已备，判定权在评委）** | 自述 §2/§3/§5、[reports/binding_ablation.json](../reports/binding_ablation.json)、[reports/cross_source_offline.json](../reports/cross_source_offline.json)、[reports/vcar_ablation.json](../reports/vcar_ablation.json) | 三臂消融（`plain / bind_only / full`）硬判定 `meets=true`（错误绑定率 32/40 = 0.800 → 0/17 = 0.000）；其**冻结集与门槛算术已由第三轮独立复核复跑 `--check`（exit 0、problems []、pin_matches true、40 例）确认**，但仍是**自建冻结集 + 确定性 stub**（`model_calls=0`）；无商业化路径材料 |

### 2.1 加分举证所引证据的真实状态（逐份点名；第三轮核对时刻 2026-10-10T08:35+08:00）

> 第一轮复查已 refuted「两份专项报告均缺失」这类**未逐份点名**的整句表述
> （`reports/review_round1_evidence.md` §6.1 C130/C131）。下表取代该表述，并在第三轮把
> 「`cross_source_live.json` 未产出」这一**已过期**的条目更新为实测值（撤回-3）。

| 文件 | 真实状态 | 规模 | 复核判定 |
|---|---|---|---|
| [reports/cross_source_offline.json](../reports/cross_source_offline.json) | **存在**（13 798 B） | 21 用例 / 161 断言 / `model_calls=0` | **confirmed**（C01–C10；第三轮复跑逐例 verdict 差异 **0**）；注入 fixture 内部一致性，不是泛化准确率 |
| [reports/binding_ablation.json](../reports/binding_ablation.json) | **存在**（219 605 B，sha256 前 16 位 `d086dd2e24014965`，`generated_at 2026-10-09T15:36:52+00:00`） | 40 例陷阱 / 3 臂 / 68 条 arm-case 失败 / `model_calls=0` | **冻结集与门槛算术已独立复跑**（第三轮 `scripts/eval_binding.py --check` → exit 0、`problems []`、`pin_matches true`、40 例）；作答器仍是 stub，**不是模型成绩** |
| [reports/cross_source_live.json](../reports/cross_source_live.json) | **存在**（`ran_at 2026-10-09T19:07:04Z`） | P1 **5/6**；P2/P3/P4 `answerable_cases = 0`；`model_calls 17`；`checks 55/60` | **未经独立重跑**（需真实模型）；内部一致（`by_structure` 求和 = 21） |
| [reports/cross_source_norm_live.json](../reports/cross_source_norm_live.json) | **存在**（第三轮新增） | P2 12/12、P3 5/5、P4 6/6 = **28/28**；**`model_calls = 0`** | **管线已独立复现**（第三轮复跑逐例 `answer` 文本差异 **0**）；**不是模型成绩**，文档侧为自建语料 |
| [reports/faithfulness_v1.json](../reports/faithfulness_v1.json) | **存在**（第三轮新增） | 26 例 → 逐结论 **19/21 = 0.9048**、矛盾 0、引用覆盖 17/21、拒答 5/5 | **未经独立重跑**；标签自检 exit 0；报告未记录上游模型标识 |
| [reports/robustness_v2.json](../reports/robustness_v2.json) | **存在**（第三轮新增） | 21 例 → **20/21 = 0.9524**、5xx 0/21 | **未经独立重跑**；标签自检 exit 0 |
| [reports/multifile_v1.json](../reports/multifile_v1.json) | **存在**（第三轮新增） | 12 例 → 可答正确 **5/9 = 0.5556**、引用覆盖 3/7、拒答 3/3、上游错误 4/12 | **未经独立重跑**；**F5 未达 70% 档** |
| [reports/vcar_ablation.json](../reports/vcar_ablation.json) | **存在** | 16 例合成机制消融 | **confirmed**（C132）；视觉载荷替身，`不调用任何模型`，不得改写成跨源绑定成绩 |
| [data/evaluation/binding_traps_v1.json](../data/evaluation/binding_traps_v1.json) | **存在**（`dataset_file_sha256` 与报告字段一致） | 40 例（四类各 10） | 第三轮 `--check` exit 0（摘要 `1fdfa89b…`） |

---

## 3. 交付物的生成与检查方式（可复现）

| 环节 | 命令 / 方法 | 结果 |
|---|---|---|
| 生成 | `交付/_build/make_01_design_report.py`、`make_02_tech_spec.py`、`make_03_deck.py`；04 本轮由第三轮同结构生成器产出（沿用 `交付/_build/docx_kit.py`） | 四份源文件均真实生成，退出码 0 |
| DOCX → PDF | DSH LibreOffice Kit：`convert --input <a.docx> --output <a.pdf>` | 三份 PDF 生成成功，`missingFonts: []`；**第三轮 04 转换先写到 `tmp/review3/` 新路径再以 `cp -f` 覆盖交付件**（Kit 拒绝覆盖已存在输出；未删除任何文件） |
| 结构检查 | `<office-skill>/../scripts/check_office.py <file> --contains <text> --out <json>` | **4/4 通过**（两个 DOCX、PPTX、自述 DOCX，均 `exit 0` / `verdict=pass`）；结果存 `reports/office_checks.json`（第三轮重跑，前两轮字段保留在 `checks_round1` / `checks_round2`） |
| 页数 | `pymupdf` 读 PDF 实际页数 | 设计报告 **13** 页、技术说明书 **12** 页、创新自述 **4** 页（≤5） |
| 幻灯片数 | `python-pptx` 读 `slides._sldIdLst` 长度 | **20** 张（≤20） |
| sha256 / 字节 | `hashlib.sha256` + `Path.stat().st_size` | 逐文件写入 `reports/office_checks.json` 的 `sha256` / `measured`，并回填 `交付/DELIVERY_MANIFEST.json` |
| **PPT 匿名检查** | `交付/_build/make_03_deck.py` 内置：读每页所有 `text_frame` 与表格单元格文本，对单位名黑名单（`中电云计算技术有限公司`、`中电云计算`、`中电云`、`中国电子`、`中国电子信息产业集团`、`网信产业应用赛道`、`赛题八`）与称谓提示词（`先生/女士/老师/博士/教授/总经理/总监/经理/同志/队长`）做正则扫描 | 黑名单命中 **0**、称谓提示词命中 **0**（PPTX sha256 未变，结果沿用）；明细存 `交付/_build/anonymity_scan.json` |
| 匿名检查的边界 | 不覆盖**嵌入图片内文字**，不覆盖母版/主题文本（本实现已扫描文本框与表格） | 因此不能声称「图片内绝对无单位名」 |
| 视觉检查 | LibreOffice Kit `render --pages 1,2,3,4` 于第三轮重跑 | 4 页渲染成功（`missingFonts: []`）；`pymupdf` 文本块边界检查：4 页 **167** 块，越界 **0**；渲染图 `tmp/review3/preview04/` 供人眼复核。**人工逐页视觉判定仍未做**（会话模型不接受图像输入），版式美观/分页裁定未做 |
| ZIP | `python scripts/build_delivery_zip.py`（默认 dry-run，不落盘） | 计划数字见 `交付/DELIVERY_MANIFEST.json` 的 `zip` 段（随工作区变化，引用须带生成时间）；本轮未重跑 |

---

## 4. 仍缺项汇总（按「卡提交资格」程度排序）

1. **全量回归待最终收敛（最高优先）**：权威全量 **1578 执行 / 1574 通过 / 4 失败 / 0 错误，exit 1**（`tmp/regression_final_a.log`）。第四轮已消除其中两类红：3 条 `test_status_docs_sync` 的文档漂移（`PROGRESS.md`/`README.md` 已写明 1578 分母、不再出现 1497）与 1 条 `test_agent_api` 跨源路由契约回归（`--pattern test_agent_api.py` → 5/5、exit 0，实测）。但 `reports/latest_regression.json` 仍会被单文件 `--pattern` 运行覆写为小分母（第四轮实测被覆写为 21、31、5），因此**仍需主协调者跑一次无参数全量**，并以该次写入的记录文件为准。
2. **决赛版深化技术文档（≤50 页）与决赛 ZIP**：初赛稿已备，决赛稿与真包未做；ZIP 脚本已就绪但本轮明确不打。
3. **干净环境演练（≥2 次完整启动 + 演示彩排）**：官方与提升计划均要求；本轮硬性禁止重建容器/库，**未做**，因此不主张「新环境可启动」。
4. **素材纳入版本控制**：`data/qa_samples/`、`data/evaluation/extensible_field_v1/materials/`、**`data/knowledge_sources/`** 均为未跟踪文件，需在打包前决定是否入库（否则 ZIP 的样本清单会指向工作区而非常规提交内容）。
5. **逐页人工视觉判定**：四份交付物均只过结构检查；本轮已渲染 04 的 4 页并做文本块边界检查（167 块 / 越界 0），但受会话模型不接受图像输入所限，**未做人工逐页视觉判定**，版式美观风险未消除。
6. **答辩 PPT 正式模板**：主办方模板未提供，当前用自建版式；页数 ≤20 与匿名要求已自检满足。
7. **加分举证的限定条件（第三轮更新）**：`cross_source_offline.json` 与 `binding_ablation.json` 的**冻结集/门槛算术**已由第三轮独立复跑确认；但两者 `model_calls` 均为 **0**（注入 fixture / deterministic-stub），`cross_source_norm_live.json` 亦为 `model_calls=0`。引用这些数字时必须同时说明「自建冻结集 + 确定性 stub / 规则编译，不是模型成绩」，且 `cross_source_live.json`、`faithfulness_v1.json`、`robustness_v2.json`、`multifile_v1.json` **未经独立重跑**。
8. **F1 档位**：第三轮实测 4 种结构可复现（P1 5/6 + P2 12/12 + P3 5/5 + P4 6/6），但 3/4 结构 `model_calls=0` 且文档侧为自建语料 → **仍不能计入官方 6 分档**（详见 [reports/review_round3_final.md](../reports/review_round3_final.md) §4）。

---

## 4.1 证据索引（第一轮 → 第三轮）

逐文件的真实字节数与 sha256 见 [交付/DELIVERY_MANIFEST.json](../交付/DELIVERY_MANIFEST.json) 的
`round2_evidence_index` 与 `round3_evidence_index` 两节；此处只列角色。

> **口径（第四轮补充）**：这两个索引记录的是**各自轮次建索引当时的 sha256 快照**。其中
> `reports/round1_improvement_report_20261010.md` 与 `reports/cross_source_norm_live.json`
> 在其后被重写/重跑，故与当前磁盘值不同——历史快照不是现值，**当前交付物的现值一律以
> `files[]` 段与磁盘为准**；本轮未回写历史快照（避免伪造历史）。

| 文件 | 角色 | 轮次 |
|---|---|---|
| [reports/round1_improvement_report_20261010.md](../reports/round1_improvement_report_20261010.md) | **第三轮定稿总报告**（三句话结论 / 工作包清单 / 三分口径实测 / 官方逐项对照 / 三轮复查汇总 / 失败样本 / 回归与安全 / 下一步 / 证据索引） | 三轮（第三轮重写） |
| [reports/review_round3_final.md](../reports/review_round3_final.md) | **第三轮独立最终复核**（确定性命令逐条复跑、F1 档位判定、L4 下沉核对、回归口径核对、风险分级） | 第三轮 |
| [reports/review_round1_evidence.md](../reports/review_round1_evidence.md) | 独立复查：证据真实性（123 条数字主张：confirmed 107 / refuted 2 / unverifiable 14） | 第一轮 |
| [reports/review_round1_regression.md](../reports/review_round1_regression.md) | 独立复查：全量隔离回归（1497 项执行 / 6 失败，逐条列出） | 第一轮 |
| [reports/review_round1_security.md](../reports/review_round1_security.md) | 独立复查：安全（阻断 0 / 高 2 / 中 4 / 低 5） | 第一轮 |
| [reports/cross_source_offline.json](../reports/cross_source_offline.json) | 跨源离线评测（21/21、161/161、`model_calls=0`） | 第一轮 |
| [reports/binding_ablation.json](../reports/binding_ablation.json) | 三臂绑定消融（40 例冻结陷阱集；第三轮独立复跑 `--check`） | 第二/三轮 |
| [reports/cross_source_live.json](../reports/cross_source_live.json) | 跨源真实链路（P1 5/6、`model_calls 17`） | 第二轮 |
| [reports/cross_source_norm_live.json](../reports/cross_source_norm_live.json) | 规范语料 P2/P3/P4（28/28、`model_calls=0`、自建语料） | 第三轮 |
| [reports/faithfulness_v1.json](../reports/faithfulness_v1.json) / [robustness_v2.json](../reports/robustness_v2.json) / [multifile_v1.json](../reports/multifile_v1.json) | 单文档忠实度 19/21 / 鲁棒性 20/21 / 多文档 5/9（真实模型，未经独立重跑） | 第三轮 |
| [reports/scale_v2.json](../reports/scale_v2.json) | 工具链规模 + 并发 + 六段端到端（新 section `profile_live`） | 第一/三轮 |
| [data/knowledge_sources/](../data/knowledge_sources/) | 自建规范语料（5 PDF + 9 DOCX、30 条条款、确定性生成器 + manifest） | 第三轮 |
| [reports/cards/](../reports/cards/) | 工作包结论卡（`wp1a`–`wp10`、`wp3_binding`、`wp2_cross_source_live`、`r2_security_fixes`、`r2_delivery_sync`、`r3_cross_source_norm`、`r3_clarification_sink`、`r3_scoring_gaps`、`wp_fix2_regression_sync` 等） | 三轮 |
| [reports/office_checks.json](../reports/office_checks.json) | 交付物结构检查 / 页数 / 幻灯片数 / sha256 / 渲染边界检查（三轮字段并列保留） | 三轮 |

---

## 5. 诚实边界

- 本清单是**提交物形态与路径**核对，**不是**官方得分、排名或晋级判断；官方无缺交材料的统一扣分公式，本文件不编造。
- 「已具备」仅表示仓库内存在该文件且通过结构检查，**不表示**其内容已被评委认可。
- 页数/幻灯片数为 PDF/PPTX 实测值；LibreOffice 分页可能与 Microsoft Word/PowerPoint 不完全一致。
- **不得主张业界领先**：无公开基准对比、无真实视觉模型对比；绑定消融是自建冻结集上的机制对照。
- **离线 stub / 规则编译不等于模型准确率**：`cross_source_offline.json`、`binding_ablation.json`、`cross_source_norm_live.json` 的 `model_calls` 均为 **0**，不得外推为业务准确率。
- `cross_source_live.json`、`faithfulness_v1.json`、`robustness_v2.json`、`multifile_v1.json`、`scale_v2.json`.`profile_live` 是真实模型/运行中服务实测，但**未经独立重跑**，引用须带此限定；且三份报告未记录上游模型标识（第三轮复核 §3.3）。
- **「语料逐字节可复现」在第四轮已修复、可以主张（必须带口径）**：第三轮 17 次独立运行曾 4 次报 `deterministic: false`（第三轮复核 §3.1；根因 `/ID` 正则只认十六进制形态）；第四轮改为解析 trailer `/ID` 数组并按内容派生 id 显式写回（覆盖 `<hex>` 与 `(...)` 两种形态，找不到即硬报错），修复后 **12/12 次 `deterministic: true`**、重建 14 份语料与 `manifest.json` 与冻结 sha256 一致（证据 `reports/cards/r4_determinism_delivery.md`）。该主张只覆盖**语料字节可复现性**，仍不得外推为模型或业务准确率。
- **不得写「当前版本回归全绿」**：权威全量 1578/1574、4 失败、exit 1（见 §4 第 1 条）。
- 本文件（第三轮）只改动 04 交付物，并同步本清单、`交付/DELIVERY_MANIFEST.json`、`reports/office_checks.json` 与总报告；01 设计报告、02 技术说明书、03 答辩演示的**内容未改动**（sha256 三轮一致），也未改动任何代码、测试或数据文件。
- 本文件（第四轮）**未改动任何交付物内容**（01/02/03/04 四份的 sha256 与第三轮一致）与 `data/evaluation/cross_source_norm_v1.json`；改动限于：`data/knowledge_sources/build_norm_corpus.py`（确定性修复）、`tests/test_cross_source_norm.py`（在既有用例内新增两种 `/ID` 形态的确定性断言，用例总数仍为 21）、本清单、`交付/DELIVERY_MANIFEST.json`、`reports/office_checks.json`、`reports/cards/r4_determinism_delivery.md`。语料重建结果为**字节不变**。
