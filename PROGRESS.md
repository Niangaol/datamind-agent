# DataMind Agent 项目进度

**前端退役说明（2026-10-10）**：旧版 `web/` 根目录的 HTML、JS、CSS 及其专用 UI 测试已移除，当前入口仅提供 `frontend/` 构建的 `web/vue/`。后端与数据服务保留。下文旧页面能力、URL、测试数量及历史报告仍按当时版本记录；1666 项为退役前的最近全量执行口径，本次不将它改写为新版回归结果。构建方式见 [README](README.md)，页面迁移计划见 [前端建议](docs/FRONTEND_NEXT_STEPS_20261010.md)。

## 当前状态（截至 2026-10-10）

| 项 | 值 |
|---|---|
| 代码 API | **0.39.0**；仓库只保留 `main` 分支，与远端同步 |
| 完整隔离回归 | **1658/1658**（**这是分母口径**：最近一次全量执行 1658 项；是否全绿只看 reports/latest_regression.json 的 `failures`/`errors`；源库未修改；由 `scripts/test_isolated.py` 在克隆库上执行）。**新增测试会改变这个分母，每次都要同步本表** |
| 验证基线 | 提交 `dab1233` 由 `scripts/test_isolated.py` 在克隆元数据库上运行 |
| 审计缺陷 | **原审计反例（F01–F12）已修复**（F10 曾延后，已于 0.36.0 修复；另含修复过程中自查发现的 F12 与我自己引入并修复的回归 R1），见 [docs/AUDIT_FIXES.md](docs/AUDIT_FIXES.md) 与 reports/audit_fix_verification.json。**这不表示缺陷类别已穷尽**：2026-10-09 的独立复查（reports/project_audit_20261009_561316c.md）在同一基线上又复现了 R01–R05（SQL 作用域/表达式校验、待复核契约匿名可读、任务代次、审批≠可检索），正按批次修复 |
| 模型接入 | **直连上游**（`https://api.deepseek.com/v1`，配置存于 `app_settings`）；**不依赖本地网关**，仓库内的 `gateway/`（vendored New API，997 个文件）与 `new-api-local` 容器已移除 |
| 未决外部依赖 | T6 授权真实输入；V-CAR 真实视觉载荷与模型规模对比（上游仅文本模型、无本地权重目录） |
| 设计主入口 | [V1.3 技术设计说明书](技术文档/DataMind_Agent_技术设计说明书_V1.3.md)（0.39.0 基线）；历史阶段记录保留在 [V1.2 说明书](技术文档/DataMind_Agent_技术设计说明书_V1.2.md) §18.12–18.33 |

**分母口径**：上表 1658/1658 是**当前唯一权威值**（分母）；它的分子/分母都等于执行数，**不等于"全绿"**——是否全绿看 `reports/latest_regression.json` 的 `failures`/`errors`（该文件是唯一记录源，**会被任何 `--pattern` 运行覆写**，因此权威值只认最后一次全量；本条随基线行收敛到此段）。各阶段历史分母与原始失败分别保留在对应报告中（例如 `reports/vcar_ablation_verification.json`、`reports/parse_contract_verification.json`、`reports/audit_fix_verification.json` 的 `previous_stage_tests` 链），**不跨轮拼接为模型成绩**，也不得把任一历史值当作当前值。

## 阶段流水（新→旧，详细实现见 V1.3 §18.x 与提交记录）
**本轮（第 5 项：真实材料评测与能力证据）**：三条线并行。**①多轮上下文**——`/v1/chat` 与 `/v1/chat/stream` 接受客户端自带 `history`（≤20 条、单条≤2000 字符、合计≤8000；**超限拒绝不截断**），历史以 `trust="untrusted"` 边界区块送入模型并附防注入声明，响应如实给出 `turns_used`；**不传历史时提示词与结果逐字节不变**（三种独立证明：提示词 sha256 常量复算、用 `git show HEAD:` 加载旧引擎并排对照、API 调用面精确断言）；实测把 `DROP TABLE orders` 放进历史后执行的 SQL 与参数**完全不变**。**②多规模性能**（官方 E7/F6）——`scripts/eval_scale.py` + `reports/scale_v1.json`：4 个 CAD 规模点（100/1000/5000/20000 实体）与 4 个检索规模点（选 1/2/5/8 份），**每规模独立子进程**测耗时与峰值内存，含 4 条免责。**③CAD 能力分层冻结验收集**——`data/evaluation/cad_capability_v1/`（31 份合成材料 + 先写后评的冻结标签 + 确定性生成器 + `--check`）：6 个能力层（unit/version/handle/entity/layout/rejected）**31/31 通过、365 条断言**；材料篡改→退出码 2 且不写报告；标签修订记录如实登记（首轮 28/31 失败**全是评测器自身缺陷**，只改评测器、标签只加强不放宽）。**并把两份真实图纸登记为开发样本**（`data/cad_demo/DEVELOPMENT_SAMPLES.md`）：它们被用来修复 CRLF、位置身份与 LEADER 崩溃，**不得**当泛化成绩；报告 `is_independent_factory_data=false`。**本轮由测量发现并修复的真实缺口**：块上限硬编码且**不在 `manifest.limits`** 里（消费方无法从结果得知上限）→ 改为可配置（参数 > `DATAMIND_CAD_MAX_BLOCKS` > 默认 2000，非法值 fail-safe 回退）并在**所有形态**（含 `unsupported`）暴露 `max_blocks`。另修我自己的两处测量缺陷（`ru_maxrss` 单位、检索「读全部再切片」）。官方要求映射据此更新：E7/F6 → 部分实现、E10 → 已实现（限定）。
**本轮（第 2 批 2a：审查 R05「审批≠可检索」）**：上传管道**不建切片**（`vector_index: deferred`），但 `approve` 只要契约块数 >0 就把资产置 `indexed`，而 `GET /v1/documents` 按 `indexed` 过滤、检索器只读 `chunks` → **审批后文档出现在"可选问答文件"里却永远检索不到**（审查实测 `eligible_chunks=0`、`all_covered=false`）。修法：新增**唯一判定函数** `upload_pipeline.searchability(status, needs_review, chunks)`，四个端点（`GET /v1/uploads`、详情、`approve`、`GET /v1/documents`）全部复用，杜绝两份判定漂移；语义 `searchable == (status==indexed AND NOT needs_review AND chunks>0)`，`index_state ∈ {indexed,no_chunks,not_approved}` + `index_note` 四个稳定码。`approve` 只在 `chunks>0` 时置 `indexed`，否则保持 `parsed` 并如实标记 `no_chunks` → 它自然不出现在选择器里；`GET /v1/documents` 的 WHERE **一字未改**（该端点同时服务文档状态视图），因此**过滤责任在前端**：选择器严格按 `searchable===true` 出勾选框，不可检索的单独列为"已解析，尚未建立问答索引（暂不可问答）"。**我另补了服务端复核**（前端指出的 TOCTOU）：`knowledge.py::_selected_rows` 原先只查 `indexed/public/needs_review` 而**不查 `chunks>0`**，选中 0 切片文档会得到无证据的成功答案；现**区分两件事**——「从未切片」→ `DOCUMENT_SELECTION_NOT_SEARCHABLE` 拒绝；「切片文本哈希失效（证据漂移/被篡改）」→ **照旧如实报告证据不完整**（这条区别是为不破坏既有 `MULTIFILE_EVIDENCE_INCOMPLETE` 语义，我第一版合并成一律拒绝，被既有测试抓出后细化）。实测：线上 `/v1/documents` 27 项中 26 可检索、1 不可检索（`doc-industrial-0ac6c55838c3`，`no_chunks`，工业事实源本来就不走切片）；0 切片文档被选时服务端拒绝、有切片文档反向对照仍接受。完整隔离回归 1163 → **1190/1190**。**本轮（第 1 批：审查 R01 + R02）**：**R01（P1）**——`sql_guard` 的 `cte_names` 是全局名字集合，`public.customers` 这类**带 schema 的真实表**若与某个 CTE 同名就被当作 CTE 跳过，受限列可读。改为 `cte_reference_table_ids()` **按作用域来源对象**判定：只有本作用域内解析为 `is_cte=True` 且**无 schema/catalog 限定**的表才算 CTE 引用；删除全局名字捷径与列检查里的同名豁免。反例由 ALLOWED 变为 `RESTRICTED_COLUMN`，未授权 schema 仍 `SCHEMA_NOT_ALLOWED`，合法 CTE/别名/排序别名/分组全部保持放行。**R02（P1）**——`_semantic_findings` 原先只要求 AST 里**存在**一个匹配的 SUM 节点，我实测"正确 SUM 再**乘 0**""**加常量**""把 SUM 挪进无关子查询而外层投影 `1`""WHERE 追加 **`OR TRUE`**"**四种变体全部判通过**。现改为：**顶层投影必须就是批准指标**（仅允许 Alias/ROUND/COALESCE/ABS/CAST 包装，且拒绝"正确列掩护被篡改列"）、指标查询**禁止 `OR`**、**时间条件必须是主查询 WHERE 里的真实谓词**（区间两侧都在）、**状态过滤按 AST 校验且并集恰为批准集合**、连接粒度按 catalog 外键方向判 `METRIC_JOIN_GRAIN_RISK`；参数↔占位符映射改用**文本序哨兵**（实测 sqlglot 的 `find_all(Placeholder)` 顺序不等于文本顺序，不能按遍历序猜值）。**诚实降级**：新增 `semantic_verification()` 并接入 `agent.py`（`sql_result` 白名单、confidence、claim 的 `verification`）：只有强契约（`retail_metric_ast_contract`／`industrial_frame_contract`）才算 `semantically_verified`；其余零售问题为 `time_scope_only` → **仍返回 COMPLETED 但降级为 `insufficient`**，理由 `SEMANTIC_CONTRACT_WEAK`。实测"2024年销售额是多少"= high + verified；"2024年一共有多少订单"= COMPLETED 但 **insufficient**（只降级不拒答）。另把一条断言"订单数问题也是 verified"的既有测试改为断言诚实标签，并新增"销售额问题在强契约下仍必须 verified"的**正向对照**，防止被"一律标 insufficient"伪修。完整隔离回归 1116 → **1163/1163**。**本轮（第 0 批：审查 R03 + 5.2）**：按 2026-10-09 复查报告修复两项。**R03（P1，权限）**——`/v1/documents/{id}/contract` 原先对"审批前上传"的资产回退放行，而该路由位于公开读前缀，导致**未复核正文可被匿名读取**（我实测匿名非回环客户端拿到 572/1307 个含正文的块）。现引入唯一发布谓词 `_published_document` 与共用判定 `_contract_visible`：已发布文档照旧公开可读；**待复核上传仅已认证 admin 可读**（复核流程依赖），匿名/viewer/reviewer 一律 404 且响应体与"不存在的 id"**逐字节相同**（不泄露存在性）；待复核分支加 `Cache-Control: no-store`。**5.2（P2，诚实性）**——`read_result` 原先各存储只取前 2000 条而 `block_count` 取本页长度、关系查询又不受同一上限，大文件会**静默截断**。现三存储统一 `LIMIT/OFFSET` + 稳定排序，新增 `totals`（库内总数）/`counts`（本页数量）/`page`/`store_pages`（各库窗口与 `truncated`/`has_more`），旧扁平键含义不变（仍为本页）；`limit`（默认 2000，1..5000）/`offset` 非法值返回 400 稳定码，不静默钳制。两项由我**独立复现验证**（匿名/viewer/admin 矩阵 + `limit=100` 触发截断：`page.total=572/1307`、`truncated=true`）。完整隔离回归 1103 → **1116/1116**。**本轮（0.39.0，真实工程图 J2 实测）**：用户提供的真实工程图 `J2_HE_8K_US_Aluminum_Plate-20260922.dxf`（677KB、**AC1021**、**CRLF**、`$INSUNITS=1`（英寸）、1303 个模型空间实体含 41 `MTEXT`/5 原生 `DIMENSION`/1 `LEADER`、8 图层含中文图层名、无命名块）暴露并修复一处**真实崩溃缺陷**：`_visited()` 假设 `_sub_entities(entity,'vertices')` 的每个成员都是实体，而 **`LEADER.vertices` 是纯坐标 tuple** → 抛 `AttributeError` 并被 dispatch 包装成通用 `PARSE_ADAPTER_UNSUPPORTED/handler_failed`，**整图解析失败**；现改为只读取真正的实体，`LEADER` 走"逐项声明不支持"（`CAD_ENTITY_TYPE_UNSUPPORTED:LEADER:36F8`），并加两条回归测试。**J2 实测（经真实上传链路）**：`partial_failure`、**1307 块 / 1307 定位**（`figure` 1252 + `paragraph` 50 + `dimension` 5）、身份 `source`（真源句柄）、单位 `in`；提取到真实图框文字（版本/日期/审核/制图/描述/材质/重量/品名/单位/料号/厂商 + 栅格 A–F、1–4）；5 个原生尺寸保留 `geometry_measurement`（318.8/130.5/1.5/3.5/2.0）与 `declared_text`（含 `%%P0.1` 公差），但 `declared_value` 按纪律为 `null`（标注是占位符 `<>`，**不把几何值当制造尺寸**），并声明 `CAD_NATIVE_DIMENSION_ASSOCIATION_NOT_RECOVERED:5`。v2 清单按授权再重算一次（仍声明为开发期记录）。完整隔离回归 1101 → **1103/1103**。
**本轮（0.39.0，位置身份 + 真实图纸实测）**：为用户提供的真实图纸 `test.dxf`（453KB、**纯 CRLF**、`$ACADVER=AC1003`、**`$HANDLING` 与 `$INSUNITS` 均不存在**、572 个 `POLYLINE` 全在图层 `0`）补齐两项能力：①**位置身份**——源文件无句柄时不再整图拒绝，改用确定性的 `entity_index`（ENTITIES 段主记录 1-based 序号）并在 `unsupported_features` 显式声明 `CAD_SOURCE_HANDLE_UNAVAILABLE`；**绝不伪造 handle**（positional 下 locator 无 `handle` 键，契约层"positional 夹带 handle"直接拒绝），句柄**存在但非法/重复/半缺**仍整图拒绝，positional 下块展开与 XDATA 关联**声明缺口而不编造身份**；source 模式快照与改动前**逐字节一致**（实测）。②**CRLF 修复**——读取器对 `\r\n` 做归一化（原 `io.StringIO` 不翻译换行导致 ENTITIES 段被忽略、读到 0 实体）。**实测你的文件**：经真实上传链路 `partial_failure`、`block_count=572`、`locator_count=572`、`locator_total=572`、`identity_mode=positional`、**0 个块带 handle**、`unit=unknown`、缺口 `CAD_SOURCE_HANDLE_UNAVAILABLE:572`；可提取的只有 572 条折线几何（无标题层/尺寸/块，如实为空）。**另发现并修复一处我把库搞坏的缺陷**：上传管道把 `object_uri` 写成仓库相对路径，而全库约定是绝对路径（回归跑器按 `/data/` 标记重定位）→ 导致隔离回归直接拒绝启动（`Clone source unavailable`）；已改为单一来源的绝对路径并加守护测试。完整隔离回归 1067 → **1101/1101**。
**本轮（0.39.0，上传并入库 + 真实图纸解析）**：把"上传自己的文件"做成**一个按钮**：`POST /v1/uploads` 一次调用完成暂存+入队（**`GET /v1/uploads/{id}` 已于 0.39.0 后期改为纯读**，任务由独立 worker 推进：波次硬时限默认 45s、**解析一律有界子进程**，超时如实返回 `running` 不伪造完成；无 worker 时 `worker.alive=false`，前端如实提示"任务不会自行推进"），`GET /v1/uploads` 列表（只读），`POST /v1/uploads/{id}/approve` 复核门；解析结果写**统一契约存储**，`/contract` 可读定位与关系。接受 `.dxf`≤8MiB、`.json`≤1MiB、`.docx/.pptx/.xlsx`≤16MiB、`.pdf`≤32MiB（DWG 明确拒绝）。**P2 真实图纸**：新增 `dxf-native-v2`（`datamind-2d-engineering-dxf-v2`），支持 `ARC`/`LWPOLYLINE`/`POLYLINE`/`INSERT` 块展开/`MTEXT`/原生 `DIMENSION`/旋转文本，采用**部分成功 + 逐项声明**（`CODE:TYPE:HANDLE`），上限参数化（默认 32MiB/20 万实体/深度 8）；**v1 的 8 个冻结文件哈希实测未变**并有守护测试每次复检。**前端**：单按钮「上传并入库」、同卡片逐份结果（含缺口与错误码中文解释）、「我的文件」列表与详情、复核按钮；`vector_index: deferred` 明写"未建立向量索引（暂不可用于问答检索）"。**集成期修正**：`/contract` 新增 `block_locator_count`/`locator_total` 三种口径（DXF 曾读作"8 块 0 定位"）；补 7 个上传族错误码到前端映射；API 版本与前端缓存版本同步到 0.39.0。**线上端到端实测**：上传→一次轮询即 succeeded（8 块/8 定位/1 关系）→ 复核门拦住在列表外（27 文档、详情 404）→ approve 后可见（28）且记录操作者。完整隔离回归 959 → **1067/1067**。
**本轮（0.38.0，复查后全量修复）**：按独立复查（4 份只读审查报告）发现的全部问题并行修复（6 个修复子代理 + 1 个测试适配子代理 + 协调者集成）：安全 F01–F04（跨源不继承密钥、免登录只读 socket 对端 + `--no-proxy-headers`、同源改固定白名单、中间件默认拒绝并把三条遗漏路由纳入）、F07（GET 不再写库）、health 暴露 `vector_store`；契约侧 CAD 块 id 定长摘要、`document_id ≤64` 失败关闭、块形状闭合（迁移 017 补 9 列）、`cad_entity` 保留源字段；摄取侧 OHR 走同一契约路径并声明 `locator_unavailable`、工业种子真正写入定位、**摄取改按 manifest 白名单**（修复知识库污染）、`--reset` 覆盖契约与事实三表；存储/CLI `import_contract` 建资产行、`read_locators` 覆盖两套存储、`/contract` 暴露 `parse_contract` 与 `unsupported_features`；前端清除网关死预设、缓存版本 `0.38.0`、`.gitignore` 知识库白名单；文档 D03–D17 全部更正并新增官方要求映射表与权重清单。安全项由协调者**独立复现攻击验证**（本机 sink 阳性对照：跨源无密钥 0 次外送、跨源自带密钥正常外送且不泄露已存密钥、伪造 Origin 403、非回环匿名 401）。重摄取后定位由 0 → **539 行**，工业种子 `claimed=actual=296`。完整隔离回归 851 → **959/959**。
**本轮（0.37.0，复查与污染修复）**：独立交付审查指出——并已由我逐项核实——另一个 agent（Codex）写入 `data/knowledge_base/` 的 3 份 `field-test-*.pdf` **被摄取进了被服务的知识库**（3 文档 / 15 切片 / 18 事实 / 15 向量），因为它被 `ingest.py` 的目录遍历 `KB_DIR.glob("*.pdf")` 收走；`27+3=30`、`169+15=184` 精确吻合。**我此前把该增量归因为 R1 修复的效果，属错误归因，已在 V1.3 §18.25 与报告 JSON 更正**。已修根因（摄取改为按 `manifest.json` 白名单，未声明文件报告但不摄取；抽成 `knowledge_base_jobs()` 并加 6 项守护测试），并清理全部污染与两处残留，计数回到 **27 文档 / 169 切片 / 23 事实 / 169 向量**。另清理未跟踪机器产物 1 份、安全审查取证留下的 2 次 run 及其证据边/节点/claims。
**本轮（0.37.0，运行验收）**：按文档化方式完整启动（`bash scripts/start_local.sh --background`）——依赖检查、容器、16 个迁移、目录/评测种子、真实摄取、工业种子全部通过，服务在 8000 就绪。**运行中发现并修复我自己实现的一处缺陷**：`GET /v1/documents/{id}/contract` 原先只读 `document_contract_blocks`（Office/DXF 路径），而 PDF 的已验证定位在 `document_block_locators`，导致「有 11 条定位的 PDF 返回 block_count: 0」——即对一个证据完整定位的文档谎报「什么都没存」。现 `read_result()` 同时读两套存储并分别以 `blocks`/`locators` 返回，接口 docstring 也改为如实描述；实机验证该文档返回 11 条 `pdf_region` 定位。完整隔离回归 844 → **845/845**（新增 1 项守护：PDF 路径的定位必须被返回）。
**本轮（0.37.0，移除本地网关）**：应用原本**默认就直连上游**（`DEFAULT_BASE_URL = https://api.deepseek.com/v1`），本地 4000 网关（vendored New API）并非必需。已把网关库中的上游渠道密钥迁移到 `app_settings`（base_url `https://api.deepseek.com/v1`、model `deepseek-flash`），**实测直连可用**后才删除：`gateway/`（997 个跟踪文件 + 未跟踪残留）与 `new-api-local` 容器已移除，4000 端口释放，`.gitignore` 的网关条目清理，README 改为直连说明。删除前后各跑一次真实 NL2SQL 端到端验证（`status=completed`、3 行、无 finding）。完整隔离回归仍为 844/844（本次未新增测试）。
**本轮（0.37.0，仓库治理）**：为 `PROGRESS.md` 建立唯一的权威当前状态块并修正陈旧口径（审计 §5 曾指出本文件分母互相矛盾）；新增 `requirements.lock`（65 个固定版本）与守护测试 `tests/test_dependency_lock.py`（固定格式、关键运行时依赖齐备、**锁与实际安装版本一致**、并断言该漂移检查非空转），README 补锁文件复现说明。本项不改变 API 行为，故版本仍为 0.37.0，但回归分母随之更新为 844/844。
**本轮（0.37.0）**：**前端呈现**（引用卡片渲染已存关系：后端词汇原样显示不翻译、无关系则完全不渲染；文档状态语义查证后确认该视图不可能出现非 indexed 文档，故只为"空列表"补明确中文说明而不臆造状态列；主代理补关系块样式、把运行状态映射抽成 `runStatusLabel()` 供两处共用、提升前端缓存版本 0.34.0→0.37.0）与 **profile 解析实现收敛**（新增唯一入口 `resolve_profile_values()`，`api.resolve_profile()` 与 `sql_engine.settings()` 都只是它的投影；并修"编辑非敏感头后探活丢敏感头→401"：**无条件**从已存配置补回敏感头）。**一处须记录的裁决**：子代理一度把补回限定为"注入 `stored=` 时保持替换语义"以兼容一条记录缺陷行为的冻结断言，我判定这会让同一操作有两种含义，改为无条件补回并**改写该断言**（并新增"两种调用方式等价"断言防折中回归）。完整隔离回归见下。上一轮并行推进三条线并收口：①**F10 摄取跨存储一致性**（`publish_document` 顺序协议：先置不可检索并提交 → 写 Qdrant → 再写 PG 并置 `indexed`，失败停在不可检索）；②**profile 读取侧脱敏**（`public_profile()` 敏感头只出键名、proxy 去 userinfo，并同步改保存语义"留空=保持不变"，避免脱敏值写回丢凭据）；③**契约关系首次被检索侧消费**（`read_relations` + 引用载荷 `relations`，纪律与 locator 一致；迁移 016 补 target 索引）；同时 **F03 已真正生效**（重建容器后仅绑定 127.0.0.1，数据完好）。**并诚实记录一处我自己引入的回归 R1**：把 `block_id` 赋值移进 `parse_pdf` 后，OHR-Bench JSON 分支的块失去 id、完整摄取会在该段 KeyError（子代理误归因为"预先存在"，我核对重构前代码后确认是自己引入），已用 `ensure_block_ids` 修复并加 6 项守护。完整隔离回归 **804/804**。上一轮**补齐 F06 剩余部分**（问答链路补上 proxy 传递：`settings()` 映射 `NEW_API_PROXY`、`model_profile()` 输出 `proxy`；传输层本就支持），并在写测试时**自查发现并修复 F12**——`make_client()` 原先把显式 proxy 置于本机直连判定之前，profile 一旦配 proxy 连 localhost 网关都会走代理，F06 把 proxy 带进问答链路后该风险面变大。完整隔离回归见下。上一轮按独立审计修复 **F01–F09、F11** 共 10 项缺陷（F10 明确延后）：F01 列权限改为按 AST scope 解析（堵住 `SELECT c AS c` 与 CTE 别名覆盖两条越权路径）、F02 指标口径按 `business_metrics` 注册表做结构比对且"非空"不再等于已验证、F03 存储端口默认仅回环、F04 deadline 覆盖 stdin 写入、F05 无密钥导出彻底脱敏、F06 api.py 内 profile 解析统一（问答链路待续）、F07 恢复状态绑定计划摘要、F08 前端请求代次防串屏、F09 Office 保真（block id 摘要化/正文顺序/公式无缓存声明）、F11 验证脚本不再污染真实库且去掉硬编码规模。完整隔离回归 **734/734**；本轮改动只按显式路径提交以避开并发 Codex 的产物。上一轮让**前端消费已验证定位**（引用卡片显示定位徽标；对未旋转的 `pdf_region` 提供区域高亮，旋转页与非页面定位**只显示标签、绝不猜测绘制**；`index.html` 缓存版本 0.14.0→0.33.0 以免回访浏览器继续用旧脚本；新增前端契约守护测试）；上一轮让**问答引用链路消费已验证定位**（`citation_from_hit`/`ask_documents` 接受 locator 查询并只附上存储中确有的定位，缺失即不附、**不从页码推导区域**；查询失败记入 `locator_lookup_error` 且不阻断回答；`agent` 注入基于只读 DSN 的实现）；上一轮新增**契约存储与读接口**（migration 015：`document_contract_blocks` 存非页面形状的 Office/DXF 契约块、`document_block_relations` 存关系且 evidence 非空/禁止自引用；`ingestion/contract_store.py` 写库前再次校验并按文档整体替换；`scripts/import_contract.py` 与 `GET /v1/documents/{id}/contract` 供消费）；上一轮把**原生 DXF 接入统一解析契约**（`ingestion/cad_adapter.py`：实体/尺寸/XDATA 引用 → `cad_entity` 定位与 `references` 关系；身份层为首块、LINE/CIRCLE 为 figure、尺寸块携带 nominal/公差/单位/关联状态；引用目标不在有界读取内时记 `dimension_target_not_included` 而不是编造关系；**DWG 不注册假适配器**，明确返回 `CAD_DWG_CONVERTER_UNAVAILABLE`；新增 `scripts/parse_drawing.py`）；上一轮新增 **Office（DOCX/PPTX/XLSX）适配器**（可选依赖 `requirements-office.txt`；OOXML 子类型按**包内索引**判定，其它 ZIP 仍报 `application/zip` 并拒绝；DOCX 标题/段落/整表、PPTX 文本框、XLSX 按行取值并保留**真实行号**，全部映射进契约并注册适配器；超上限时 `parse_status=partial` 且记录 `block_limit_reached`/`sheet_row_limit_reached`）；上一轮把**契约定位写入存储**（新增应用所有的 `document_block_locators(block_id, document_id, contract_version, locator)`，`ingest` 重建文档块时同步写入；块表保持历史 13 列不变，缺定位的块不写行，不编造历史定位；因基表 `document_blocks` 属主为 postgres 且既有迁移从不 ALTER 基表，遵循既有模式另建表）；上一轮加入**媒体类型探测与 Adapter 路由分发**（`detect_media_type` 按文件签名判断，未知返回 octet-stream、不可读报 `PARSE_MEDIA_UNREADABLE`；`register_adapter` 接受 handler；`dispatch` 按类型路由、调用 handler 并校验返回值；`parse_pdf` 经该路由执行并在质量明细回报 `adapter=pdf-native`）；上一轮把**统一解析契约接入生产摄取链路**（`ingestion/ingest.py::parse_pdf` 在返回前把真实块映射进契约并校验，结果写入质量明细 `parse_contract`：`parsed`/`failed`/`skipped` + 稳定错误码；契约违反不中断摄取但保持可见；真实原生/扫描/公式 PDF 均验证为 `parsed`）；上一轮**并行推进两条线**：按 §5.2/§5.4 新增**统一解析契约**（`datamind/parse_contract.py`：parse_status 与摄取状态机、七类 SourceLocator 校验、Block/Relation 约束、Adapter 能力登记、真实 PDF 块映射；`docs/PARSE_CONTRACT.md`），并按 §13 新增**CPU/内存限额与跨主机租约**（`cpu_seconds` 经 RLIMIT_CPU 生效；`memory_bytes` 在平台不支持时失败关闭；migration 013 的 `task_leases` 提供接管递增 fence 的协作式租约）；上一轮按 §6.4/§9.2 新增 **V-CAR 机制消融与修复/冲突指标**（`datamind/ablation.py`：四条机制臂在 16 例冻结合成集上对照，按 §9.2 报告修复成功率、误修复率、冲突 Precision/Recall、正确/错误/未消解与触发比例，零分母报告 N/A；实测 `local` 臂 2 例误修复、`full` 臂 0 误修复并保留 2 例未消解）；上一轮把 **V-CAR 节点接入统一任务预算与 HTTP**（`bounded_runners` 在独立进程组内执行节点、按工具占跨进程资源槽、取消可传播、节点时限取自全局剩余预算；`POST /v1/plans/execute` 目标由服务端白名单决定，未配置即 fail closed）；上一轮按技术文档 §6.1/§6.4/§15-P3 新增**通用 V-CAR 依赖计划**（`datamind/vcar.py`：节点契约与拓扑校验、独立节点并发、失败只重跑受影响子图并保留原始失败、统一全局预算 `deadline/max_steps/max_rows/max_tokens/max_repair`、无权威规则的冲突终止自动消解并返回双方证据、断点恢复）；上一轮为已发布私有 PDF 向量点的受控修复与孤立点回收（`/v1/pdf-knowledge/publications/{id}/repair`、`/v1/pdf-knowledge/orphans`、`/orphans/reclaim`；只按冻结计划补点、默认 dry-run、仅限 `pdf_private_*`、不改计划/发布行/当前头）；上一轮把扫描页 OCR 与公式会话接入统一任务预算（运行器泛化为可执行任意命令/独立解释器；`ocr.page_blocks_bounded` 与 `formula.recognize(dsn=…)` 在进程组内受 deadline/槽/取消约束，ingest 可选启用）；上一轮为统一任务预算原语（`datamind/task_budget.py`），再上一轮为受控私有模型回答。完整回归在隔离数据库副本验收；原检出和原 8000 服务未升级。设计主入口：[V1.3 技术设计说明书](技术文档/DataMind_Agent_技术设计说明书_V1.3.md)（0.39.0 基线），历史阶段记录保留在 [V1.2 说明书](技术文档/DataMind_Agent_技术设计说明书_V1.2.md) §18.12–18.33。私有知识发布提交 [`6913ccc`](https://github.com/cyc120/datamind-agent/commit/6913ccc9a459b7bd91a72f72ec642d56e92d8a24) 已随 PR #1/#2 并入 `main`。仓库：[cyc120/datamind-agent](https://github.com/cyc120/datamind-agent)，当前只保留 `main` 分支。

本次推进：把 V-CAR 节点接入统一任务预算与 HTTP（`datamind/vcar.py` 的 `bounded_runners` / `ExecutionContext` / `run_request`：节点在独立进程组内执行、节点时限取自全局剩余预算、按工具占跨进程资源槽、取消传播杀进程组；新增 `POST /v1/plans/execute`，目标由服务端白名单决定）。这一轮之前依次完成：通用 V-CAR 依赖计划原语、私有向量修复与孤立回收、统一任务预算与进程时限、扫描页 OCR/公式接入同一预算、受控私有模型回答。该轮完整隔离回归 **549/549**（历史值，当前值见文首「当前状态」）。历史分母（465/465、480/480、502/502、513/513、523/523、539/539）与原始失败全部保留，不跨轮拼接为模型成绩。

## 现阶段优先级（2026-10-07 确认）

1. **以技术实现为主**：按 [V1.3 技术设计说明书](技术文档/DataMind_Agent_技术设计说明书_V1.3.md) §15 的顺序，把文档中仍标记为「规划／待实现」的技术项落地；每次改动都要有测试、验证报告与文档同步，不破坏既有回归。
2. **赛题交付包（ZIP）等前置技术任务完成后再做**：在此之前不投入交付打包、答辩 PPT、官方专项核对与《突破与创新性自述》。
3. **授权真实输入单独验收**：真实模板、图纸、扫描件需外部材料，拿到后另立样本与分母，不复用合成材料成绩。

## 当前交付范围

| 能力 | 已完成 | 验证/限制 |
|---|---|---|
| 认证尺寸服务与页面 | 单条测量登记/版本、显式绑定、独立使用批准/撤销、当前核验、保存快照、分页历史回放；`/#dimensions` | 三角色页面与隔离回归通过；真实 ERP、计量校准与制造授权待扩展 |
| 多文件登记队列 | DXF/单测量 JSON 上传、逐文件状态、全局/批次限额、租约、幂等、失败重试、协作取消；`/#files` | 必须独立启动 CLI worker；PDF/OCR/Office 未并发化，成功文件不因同批失败回滚 |
| 显式多文档问答 | 1–8 份无重复公开文件、精确版本/源/解析器、每文件相关片段、生成结束重验、检索/实际引用覆盖 | 已索引文档库范围；不是全文审阅或通用跨文件语义裁决，真实模型评分未在本阶段重跑 |
| 原生 PDF 离线准备 | 按 SHA 固定源、版本化块/切片、独立 attempt/进程、封存重放与摘要校验、旋转/裁剪坐标和保守图片预算；可选 `--deadline` 在独立进程组内运行 | 31 项合成边界与 8 项 CLI 验证；不发布到知识库，不替代 OCR/表格/公式模型；仍非 CPU/内存 cgroup 硬隔离 |
| PDF 私有快照登记 | admin/CSRF/同源、当前头 CAS、同 owner 原请求恢复、固定版本原文件/页图/bundle、读取后权限重验、分页元数据 | 服务 27/27、HTTP 19/19；不索引、不裁定权威、不接上传/页面；两个验证槽仅单服务进程，5 秒仅单次 DB 锁等待 |
| PDF 私有知识发布 | 只入队 HTTP、冻结向量计划、独立认证 Qdrant、实际点确认、私有头 CAS、token/lease 栅栏、原 owner 重试、显式历史切片 | 全 worker 最多 2 个 running；无解析/OCR 或公开 RAG；发布后点损失可由受控修复入口补齐 |
| PDF 私有多文件检索 | 固定 publication/完整计划 SHA，1–4 份文件、同 embedding profile、本地 Dot、逐文件覆盖、完整点与 DB/文件复核 | 两个跨进程检索槽、总候选 512；本接口只返回证据（模型回答见下一行）；仍未接入 CPU/内存硬隔离与完整 deadline |
| PDF 私有模型回答 | 只用本次选定私有片段生成；原文连续片段与数字支持校验、最多一次修复、文件未覆盖即拒答；生成后重查身份与证据 | 15 项组件/契约测试；不接公开知识、不写向量；真实模型忠实度与准确率未评测 |
| 私有向量修复与孤立回收 | 按冻结计划补齐丢失点（不改计划/发布行/当前头）；孤立点扫描与默认 dry-run 回收，仅限 `pdf_private_*`，删除前后重读引用集 | 10 项组件/契约测试；测试用内存 Qdrant；无自动调度与告警，真实认证私有 Qdrant 烟测未重跑 |
| V-CAR 通用依赖计划 | 节点契约与拓扑校验；独立节点并发；失败只重跑受影响子图且保留原始失败；统一预算与两次修复上限；冲突终止自动消解；断点恢复 | 16 项单元测试；视觉载荷/局部重识别与机制消融待做 |
| V-CAR 受控节点执行 | 节点在独立进程组内执行；节点时限取自全局剩余预算；按工具占跨进程资源槽；取消传播杀进程组；目标由服务端白名单决定 | 10 项组件/契约测试；HTTP 需运维显式配置注册表；同主机执行，非同主机租约 |
| V-CAR 机制消融 | 四条机制臂在同一冻结集对照；修复成功率、误修复率（含占原正确输入份额）、冲突 P/R、正确/错误/未消解、触发比例；零分母 N/A | 18 项测试；合成 16 例、机制替身，非模型精度；真实视觉载荷与模型规模对比未做 |
| 统一解析契约 | parse_status 五态与摄取状态机；七类 SourceLocator 校验；Block/Relation 约束；Adapter 登记与 handler 分发；真实 PDF 块映射；parse_pdf 经路由产出并校验；定位写入 document_block_locators | 42 项测试；关系表与检索消费未接入；Chunk/DocumentFact/DrawingFeature 未纳入 |
| Office 适配器 | DOCX 标题/段落/整表、PPTX 文本框、XLSX 按行（真实行号）映射进契约；OOXML 子类型按包内索引判定；超限声明 partial 与原因；可选依赖缺失时明确拒绝 | 10 项测试（无 Office 库时跳过）；未解析样式/批注/修订/图表/备注/公式/合并单元格/隐藏项；Office 块未接入事实抽取与检索 |
| 图纸适配器（DXF） | 实体/尺寸/XDATA 引用映射进契约；`cad_entity` 定位；关系为 `references`+`native_reference`+`verified`；引用缺失与超限均声明 partial；DWG 明确拒绝 | 12 项测试；DWG/DGN 需外部转换器；图纸 PDF 区域路径未做 CAD 专项关联 |
| 契约存储与读接口 | Office/DXF 契约块（含 locator）与关系入库；写前再次校验、按文档整体替换、relation_id 可复现；import CLI 与读接口供消费 | 7 项测试；两个存储（PDF 与非 PDF）未合并 |
| 引用消费定位 | 引用与检索证据携带契约校验过的 `locators`；只取存储中确有的、查询失败不阻断回答并记录错误码 | 9 项测试；历史文档无定位行故引用不带区域 |
| 前端消费定位 | 引用卡片显示定位徽标；未旋转 `pdf_region` 可展开原页区域高亮；旋转页与非页面定位只显示标签；缓存版本同步更新 | 5 项契约守护测试（含 node 语法校验）；界面为静态资源，需浏览器刷新 |
| 审计缺陷修复（F01–F12） | 见 `docs/AUDIT_FIXES.md` 与 `reports/audit_fix_verification.json`：越权、口径、端口暴露、deadline 绕过、导出与读取侧脱敏、profile 解析与 proxy 传递、恢复不绑计划、前端串屏、Office 保真、验证污染、本机直连优先级、摄取跨存储一致性 | **12 项反例已修**；F03 已重建容器生效；残留：F02 强制面仍窄、profile 解析实现仍两套（行为已一致）、F10 仍有孤儿向量窗口。**同类外延见 2026-10-09 复查 R01–R05（修复进行中）** |
| 契约关系消费 | `read_relations` 按 source/target 两向命中；引用载荷附 `relations`（只附存储中确有的、失败不阻断、错误独立记录）；对端字段命名 `peer_block_id` 以免与 locator 的 `block_id` 语义混淆；迁移 016 补 target 索引 | 关系**不参与**排序/打分/重排/冲突消解 |
| 前端呈现 | 引用卡片渲染已存关系（后端词汇不翻译、无关系不渲染）；空文档列表给明确中文说明；运行状态中文映射两处共用；关系块最小样式；前端缓存版本随之提升 | 12 项结构化测试（含 node 语法）；**需浏览器人工验收**；测试未声称验证 DOM 运行时 |
| profile 解析收敛 | 唯一入口 `resolve_profile_values()`（请求 > 配置 > env > 默认），`api.resolve_profile()` 与 `sql_engine.settings()` 均为其投影；敏感头无条件补回，删除仅 `__clear__`；修正"无 api_key 的 stored 配置 + env 密钥可能把密钥发往 stored 端点"的优先级差异（选保守侧） | 19 项新测试（随机化等价性 210 组）+ 既有 65 项契约回归全绿；请求侧 proxy 仍为替换语义（已证无实际缺口）
| CPU/内存限额与跨主机租约 | cpu_seconds 经 RLIMIT_CPU 生效；memory_bytes 平台不支持时失败关闭；task_leases 协作式租约与接管递增 fence | 11 项测试；本机 macOS 不支持 RLIMIT_AS；非 cgroup 硬隔离、非分布式共识；租约未接入执行路径 |
| 统一任务预算与进程时限 | 跨进程资源槽、挂钟 deadline、进程组终止（含孙进程）、取消传播、输出上限与稳定超时码；登记 worker、PDF 准备 CLI、扫描页 OCR 与公式会话可选接入 | 34 项组件/契约测试；非 CPU/内存 cgroup 硬隔离；跨主机租约待接入 |
| 结构化单源问数 | Catalog、Schema Linking、SQL Guard、参数化只读执行、修复、Trace/SSE/回放 | 历史零售 14/14，鲁棒性 4/4；未在本次重跑模型评分 |
| 文档问答 | Hybrid/RRF 检索、真实模型生成、原文引用/定位与拒答 | 最近完整 RAG 10/12，单题上游错误复测 1/1 单列；语义蕴含未独立验证 |
| OCR/公式/字段 | 原像素 OCR、隔离公式 worker、表头与字段候选、文件/块/页坐标校验 | 历史扫描表头 5/9、字段 3/15；完整表格关系未达成 |
| 工业跨源规则 | 合成工业底座、17 页工艺/质检 PDF、单位/版本/逐测量核验和冲突持久化 | 13/13 开发规则；真实冲突保留双方原值，未登记权威时不选来源 |
| 工业模型问数 | 限定工业 Catalog、计数/温度聚合/明细、语义门禁与回放 | 8/8 开发题、2/2 模型前澄清；初始 7/8 评测类型错误及同输出复算均保留 |
| 新工业模板测试 | 4 页原生/扫描材料、56 字段区域标签、首轮冻结与逐项报告 | 28/56 字段，2/4 事实；原生/扫描各 14/28；不是独立工厂盲测 |
| 紧凑工业格式 v2 | 半角/全角冒号、标签内空格、工艺/质检上下文、原块定位与单位核验 | 单独开发 52/52 字段、4/4 事实；冻结后新样本首轮 104/104、8/8；合成材料 |
| 字段人工复核 | 登录账户、复核权限、原页定位、确认/存疑/退回、版本审计、历史、幂等/并发处理 | 历史未认证记录不补身份；复核只针对提取忠实度 |
| 来源权威规则 | 管理员登记、另一审批账户启用/不批准/撤销、精确范围/版本/生产记录/依据页绑定、独立证据与历史回放 | 仅本地合成温度数值冲突；依据内容的业务授权含义由人核对，未做真实工厂授权验收 |
| 尺寸绑定与独立使用批准契约 | 显式测量/批次/工序/工艺版次/方法、图纸/复核/批准哈希、有效期、独立角色声明与撤销回放 | 冻结 13 文件；契约阶段新增 16 项、隔离回归 195/195；纯引擎不验证真实认证，服务适配另列 |
| CAD 认证关联复核 | reviewer/admin 确认/存疑/退回、明确候选选择、版本/源/快照绑定、历史与幂等/并发门禁 | 复核阶段新增 12 项、隔离回归 179/179；换版不继承；原图与提取不覆盖；不代表正式图纸审批 |
| 图纸资产与版本查看 | 认证登记、固定源副本/快照、版本历史、实体/特征/原文件/预览接口及页面 | 资产阶段新增 14 项、隔离回归 167/167；v2 后 v1 原文件/预览/特征哈希不变；正式图纸审批待扩展 |
| 原生 DXF 与实体预览 | 二维 LINE/CIRCLE/TEXT 源读取、图层标题/显式 XDATA 引用、尺寸/单位/门禁、句柄 SVG/离线交互 | 冻结后新合成图 6/6 场景符合预期：2 可用、3 保留/排除、1 不支持拒绝；限定自定义交换 profile |
| CAD 契约与关联门禁 | 冻结源/图号/版次/句柄/模型坐标/单位、LINE/CIRCLE/TEXT 及直径/公差、对象关联与错版/矛盾门禁 | 合成手工快照 7 实体、1 尺寸；12 项契约检查；没有 DXF 读取/渲染或 CAD 准确率 |
| 独立字段审批 | 审批权限、另一账户批准/不批准/撤销、文件/提取/复核版本绑定、独立审计记录 | 本地字段审批；不覆盖原文声明、不裁定来源权威；不等于工厂审批系统 |

当前数据（2026-10-08 清理后实测）：27 文档资产、23 候选事实、169 切片/向量。**注意**：曾出现 30/184/41，那是另一个 agent 写入 `data/knowledge_base/` 的 3 份文件被目录遍历摄取所致的污染，已清理并修根因（见阶段流水）；30/184/41 属被污染时的读数，不是现状。工业底座有 3 零件、12 批次、13 测量及 13 质检登记。测试模板未混入运行知识库。上一阶段浏览器提交测试使用隔离副本，测试后已清理；本轮私有检索测试/真实烟测的 PDF 与私有产物也已清理。正式文档没有被代理复核。

本阶段没有修改原库的数据、模型配置或复核记录。完整回归在临时元数据库中把原 17 页工业文档重新提取为 v2，验证新版读取/复核和旧模块兼容；模板仅在离线评测中使用。测试库不复制模型设置、账户、会话、登录限流、权威规则、CAD 资产、关联复核、私有 PDF 快照或知识发布行，运行后删除。

## 本阶段验收与历史证据

本轮（0.35.0 → 0.36.0）三条线合并后完整隔离回归 **804/804**、失败/错误 0（首次运行出现 1 个瞬时错误，重跑全绿，未复现，与容器重建/并发干扰有关）。F10 的 11 项测试用 mock 记录语句顺序，覆盖"Qdrant 抛错/元数据写抛错/**终态 PG commit 抛错**时都不出现 indexed 提交"；读取侧脱敏 27 项含 `node --check` 与一次性真机端到端（脱敏视图原样回传后库中凭据存活）；关系消费 17 项含两向命中、空输入不查库、失败不阻断。另核实并记录：本机网关仅 `deepseek-flash`（纯文本）、无权重目录，故 **V-CAR 真实视觉载荷与模型规模对比不可执行**（外部依赖）。

本轮审计缺陷修复完整隔离回归 **734/734**（上一阶段 664 + 新增 70）、失败/错误 0、退出 0，见 `reports/audit_fix_verification.json` 与 `docs/AUDIT_FIXES.md`。执行方式：F01/F04/F07 由主代理实现；F02、F03+F11、F05+F06、F08+F09 由四个子代理并行实现，每个子代理被限定只能写指定文件、禁止仓库级格式化与 git 写操作，改动全部由主代理独立复核并跑测试。诚实边界：F10（摄取跨存储一致性）**明确延后**并给出 outbox 方案而不是塞入未验证改动；F06 只做到 api.py 内统一，问答链路（sql_engine/rag_engine/pdf_answer）仍不含 proxy；F03 只改了配置、需重建容器才真正收敛；F02 的口径强制面仍窄（仅销售额别名族），复合指标按"无法独立校验"记录而不阻断。

本轮前端消费定位完整隔离回归 **664/664**（上一阶段 659 + 新增 5）、失败/错误 0、退出 0，见 `reports/parse_contract_verification.json`。新增 5 项为前端接缝守护：前端读取的 locator 字段（`locator_kind`/`page_no`/`bbox`/`page_width`/`page_height`/`rotation_degrees`/`handle`/`sheet`）必须与后端实际产出一致；必须存在**旋转页拒绝绘制**的判断；非页面定位走标签分支；`node --check` 通过。另发现并修正一处交付隐患：`index.html` 的缓存版本仍停留在 `0.14.0`，回访浏览器可能继续使用旧脚本，已更新为 `0.33.0`。运行态确认：服务挂在 `/assets`，`/assets/app.js` 已包含新函数；服务此前已停止，已重启（PID 23137，API 0.33.0）。

本轮引用消费定位完整隔离回归 **659/659**（上一阶段 650 + 新增 9）、失败/错误 0、退出 0，见 `reports/parse_contract_verification.json`。新增 9 项覆盖：只附上存储中存在的定位（缺失即不附）、无查询时不编造 locator 键、查询只收到命中块的真实 id（排序去重且仅一次）、查询失败时回答仍完成并记录 `locator_lookup_error`（支持自定义 code）、未注入查询时行为与之前一致、`read_locators` 省略未知 id、空输入不查库、超 512 上限拒绝。**过程异常**：一次完整回归出现 30 个数据库连接错误，PostgreSQL 日志显示连接被管理命令终止且另有会话并发执行 `DELETE`——原因是**另一个 agent（Codex 桌面版 CLI）在同一 PostgreSQL 上并行跑测试**；重跑后 659/659 全部通过，与本次改动无关。同一 agent 写入 `reports/` 的审计产物曾被 `git add -A` 误并入提交，已 amend 移除并改为按显式路径提交。

本轮契约存储与读接口完整隔离回归 **650/650**（上一阶段 643 + 新增 7）、失败/错误 0、退出 0，见 `reports/parse_contract_verification.json`。新增 7 项覆盖：块/定位/关系写库与读回一致（8 块、1 关系，locator 为 `cad_entity` 且单位/句柄/坐标齐全）；重复导入按文档整体替换而非重复；`relation_id` 可复现且随文档变化；**无效结果在写库前被拒绝**（给 `cad_entity` 塞 bbox 返回 `PARSE_LOCATOR_INVALID` 且未写入任何行）；数据库 CHECK 拒绝空 `evidence` 与自引用；读接口返回块（含 locator）与关系，未知文档 404。另有真实端到端：`scripts/import_contract.py` 对真实 DXF 写入 8 块 + 1 关系，读回一致，随后清理。

本轮原生 DXF 接入统一契约完整隔离回归 **643/643**（上一阶段 632 + 新增 12 − 报告守护合并 1）、失败/错误 0、退出 0，见 `reports/parse_contract_verification.json`。新增 12 项覆盖：真实 DXF 与 DWG 版本头的签名探测；实体/尺寸成为带 `cad_entity` 定位的契约块；身份层为 `title`（`heading_level=1`）而 LINE/CIRCLE 为无文本 `figure`；尺寸块携带 nominal/公差上下限/单位/关联状态；关系使用源 DXF 的 `native_reference` 证据且状态 `verified`；**引用目标不在有界读取内时记 `dimension_target_not_included` 且不产生关系**；超 `MAX_BLOCKS` 记 `block_limit_reached`；DWG 返回 `CAD_DWG_CONVERTER_UNAVAILABLE` 且 `dispatch` 返回 `PARSE_ADAPTER_UNKNOWN`；CAD 告警进入 `quality_findings`；CLI 汇总与 DWG 退出码 1。另修正报告守护测试的误报（改为只对解析失败的文件提示 Python 字面量）。

本轮 Office 适配器完整隔离回归 **630/630**（上一阶段 620 + 新增 10）、失败/错误 0、退出 0，见 `reports/parse_contract_verification.json`。新增 10 项覆盖：DOCX/PPTX/XLSX 由**包内索引**识别为对应 OOXML 子类型；普通 ZIP 保持 `application/zip` 且 `dispatch` 拒绝为 `PARSE_ADAPTER_UNKNOWN`；缺库时 `PARSE_ADAPTER_UNSUPPORTED(library_missing)`；DOCX 标题（含 `heading_level`）/段落/整表（`table_cells` 二维）与 `office_object` locator；PPTX 文本框块与 slide/shape locator；XLSX 按行块、`sheet_range` 与**真实行号**（空行跳过但不重编号）；`MAX_BLOCKS` 与单表行数上限触发 `partial` 与明确原因；CLI 输出契约汇总、对普通 ZIP 返回 1。测试对可选 Office 库缺失时跳过而非失败。

本轮契约定位写入存储完整隔离回归 **620/620**（上一阶段 614 + 新增 6）、失败/错误 0、退出 0，见 `reports/parse_contract_verification.json`。新增 6 项覆盖：解析后每个块都带**校验过的** locator（`locator_kind`、`page_no`、`bbox` 与块一致）与契约版本；块表 INSERT 仍是历史 13 列（契约列不上基表）；定位行携带版本与 JSON locator；无定位的块**不写行**；`document_block_locators` 由迁移创建且属主为应用角色；以及在**真实库事务内**按实际列插入块与定位行再回滚，证明 SQL 与 schema 相符。

本轮媒体类型探测与 Adapter 路由完整隔离回归 **614/614**（上一阶段 607 + 新增 7）、失败/错误 0、退出 0，见 `reports/parse_contract_verification.json`。新增 7 项覆盖：PDF/PNG 按签名识别、未知内容返回 `application/octet-stream`、不可读路径报 `PARSE_MEDIA_UNREADABLE`；`dispatch` 路由到已登记 handler 并校验其输出；未登记类型与无 handler 适配器分别报 `PARSE_ADAPTER_UNKNOWN`/`PARSE_ADAPTER_UNSUPPORTED`；handler 输出非法、签名不符、抛普通异常与抛 `HTTPException` 四种情形的稳定错误码；handler 必须可调用。真实解析路径已改走该路由，质量明细中出现 `adapter: pdf-native` 作为**路由确实被走到**的证据。

本轮解析契约接入生产摄取完整隔离回归 **607/607**（上一阶段 603 + 新增 4）、失败/错误 0、退出 0，见 `reports/parse_contract_verification.json`。新增 4 项为摄取集成测试：真实原生 PDF 校验为 `parsed` 且块数与 locator 类型匹配；真实扫描页 OCR 块同样通过；越界块被记录为 `failed`（`PARSE_LOCATOR_INVALID`）而**不中断摄取**；无块时记为 `skipped` 而不编造结果。手工核对三份真实 PDF（原生制度、扫描巡检、公式说明）均返回 `validation: parsed`、locator 全为 `pdf_region`。

本轮统一解析契约与资源限额完整隔离回归 **603/603**（上一阶段 567 + 新增 36）、失败/错误 0、退出 0，见 `reports/parse_contract_verification.json`。新增 36 项为 25 项解析契约测试与 11 项资源/租约测试。平台事实：`RLIMIT_CPU` 受支持并实测生效（1 秒上限下 300 秒自旋被终止），`RLIMIT_AS` 在本机 macOS 被拒（`setrlimit` EINVAL），因此内存上限**失败关闭**而不是假装生效；独立进程无法取得已被占用的租约。过程记录：并行的子代理在实现期间对仓库做了越界自动改写（删除 `web/app.js` 一行行为代码、删除 `style.css` 的 `.step/.num` 规则、批量移除 import 与 requirements 条目），已中断该子代理并**逐项回退全部 33 个受影响文件**，只保留受控改动；其产出的 1234 行契约模块因与规范偏差较大且无测试而弃用，改为受控实现。租约跨进程测试首轮误用父进程原始 DSN，子进程在隔离回归中连到真实库并成功取租约；改用 `effective_dsn` 后通过，同时验证了该原语的必要性。

本轮 V-CAR 机制消融与修复指标完整隔离回归 **567/567**（上一阶段 549 + 新增 18）、失败/错误 0、退出 0，见 `reports/vcar_ablation_verification.json`；实测报告 `reports/vcar_ablation.json`。18 项覆盖：冻结数据集摘要固定与变化拒绝、畸形数据集拒绝、逐例失败保留、零分母报告 N/A、修复成功率/误修复率/误修复占原正确输入份额/冲突 P-R 与假阳性算术、冲突检测与标签解耦（同一来源相反标签得到相同检出）、臂行为（本地臂产生误修复、full 臂零误修复并把无规则冲突留作未消解并返回双方证据、ocr_text 在冻结集漏检全部真实冲突）、两次运行指标一致、脚本拒绝覆盖与数据集变化时拒绝运行。实测对比：`ocr_text` 0.1875／`structured` 0.6875／`local` 0.6875（**2 例误修复，14.29%**）／`full` **1.0（0 误修复，2 例正确未消解）**。开发中修正两处度量缺陷：机制臂原依据 defect 标签判定冲突（标签泄漏使 Precision 恒为 1），改为纯来源比对；「未察觉冲突」原被记为正确消解，现要求先检出才计分。

本轮 V-CAR 受控节点执行与 HTTP 入口完整隔离回归 **549/549**（上一阶段 539 + 新增 10）、149.719 秒、失败/错误 0、退出 0，见 `reports/vcar_bounded_verification.json`。10 项覆盖：节点在独立子进程运行且收到包装体载荷（`input` 与依赖 `inputs` 正确）；目标白名单与模块/函数名校验（未知工具、非二元组、路径穿越、命令注入样式、非法类型）；全局预算耗尽时节点被终止（2 秒预算下 `sleep 300` 在 20 秒内返回）；取消传播后子进程 pid 消失、未派发依赖标记 `skipped`；`rows`/`tokens` 累加触发 `budget_exhausted`；资源槽占满时节点失败码为 `TASK_BUDGET_BUSY`；HTTP 未登录 401/403、非法请求体 422、未配置注册表 503 fail closed、配置后真实子进程执行。开发中修掉一个真实缺陷：调用方漏传共享 context 时节点时限会静默退回默认 120 秒，现由 `execute` 自动绑定。

本轮 V-CAR 通用依赖计划完整隔离回归 **539/539**（上一阶段 523 + 新增 16）、152.249 秒、失败/错误 0、退出 0，见 `reports/vcar_verification.json`。16 项覆盖：拓扑排序与受影响子图、节点/预算/依赖/工具/权限/环/重复 id 校验、依赖节点消费前序结果、独立节点真实并发（`threading.Barrier` 验证重叠）、失败跳过依赖但不影响独立分支、**只重跑受影响子图**（独立节点恰好执行 1 次）、修复上限（attempts=3）、`max_steps`/`deadline`/`max_rows`/`max_tokens` 四类预算耗尽、未消解冲突（repairs=0）并返回双方证据、缺失执行器与非法结果、断点恢复与非法 state 拒绝。本阶段为纯编排原语，未调用模型或数据库。

本轮私有向量修复与孤立回收完整隔离回归 **523/523**（上一阶段 513 + 新增 10）、147.273 秒、失败/错误 0、退出 0，见 `reports/pdf_maintenance_verification.json`。10 项覆盖：删除已发布点后修复恢复点集且发布行/计划摘要/结果摘要不变；完整时报告 0；未知与路径式 id 拒绝；孤立点扫描只统计无引用点；dry-run 不删除、confirm 删除且引用点全保留；回收后再修复仍为 0；非私有集合与非法批次拒绝；请求体严格校验；HTTP 未登录 401/403 与 admin 全链路。测试使用内存 Qdrant，本阶段未重跑真实认证私有 Qdrant 烟测。

本轮 OCR/公式接入统一任务预算完整隔离回归 **513/513**（上一阶段 502 + 新增 11）、149.428 秒、失败/错误 0、退出 0，见 `reports/ocr_formula_budget_verification.json`。真实扫描页（`data/knowledge_base/07_门店巡检记录_2025年12月_扫描版.pdf` 第 1 页）子进程 OCR 与进程内路径的**块与 detail 关键字段完全一致**（16 块、27 行、1.79 秒）；同一页给 1 秒时限时进程组被终止并返回 `OCR_TIMEOUT`。公式会话经 `run_bounded_command` 在 `.venv-formula` 独立解释器中执行，可选占用跨进程槽并保留 `FORMULA_TIMEOUT` 等错误码。OCR 默认仍在解析进程内，只有显式 `--ocr-deadline` 才启用受控路径；本阶段未调用上游语言模型。原库计数与旧模块未变。

本轮统一任务预算与进程时限完整隔离回归 **502/502**（上一阶段 480 + 新增 22）、147.653 秒、失败/错误 0、退出 0，见 `reports/task_budget_verification.json`。新增 22 项为 17 项任务预算单元测试与 5 项登记接线/映射/取消测试。真实子进程（`sleep 60`）在 1 秒挂钟时限内被终止，`sh -c 'echo $$ > pid; sleep 60'` 的孙进程一并消失；10 万字节 stdout 被截到 4096 且无法伪造结果；三个并发槽中第三个立即 `503/TASK_BUDGET_BUSY`，异常退出后释放；`effective_dsn` 指向当前实际数据库；`cancel_event` 置位后运行中的子进程被立即终止并返回 `TASK_CANCELLED`。CLI 冒烟：真实原生 PDF 在 60 秒时限下准备成功、封存清单 `verified=true`，非法时限与孤立 `--grace` 均以 exit 2 拒绝，产物已清理。原库计数与旧模块未变。

本轮受控私有模型回答完整隔离回归 **480/480**（上一阶段 465 + 新增 15）、142.189 秒、失败/错误 0、退出 0，见 `reports/pdf_answer_verification.json`。新增 15 项为 12 项生成/校验单元测试与 3 项 HTTP 契约测试；检索结果与模型回复均为明确合成注入，未调用上游模型、未下载、未改原库。校验覆盖：只把本次选定私有片段交给模型、逐字连续片段引用、数字必须出自引用片段或标题、未知 chunk 拒绝、片段不匹配拒绝、最多一次修复、文件未覆盖即拒答、生成后证据变化返回 409、重复引用合并。原库计数与旧模块未变。

本轮私有 PDF 检索完整隔离回归 **465/465**（原 411 + profile/并发 14 + 联合 capture 8 + HTTP 32）、125.833 秒、失败/错误 0、退出 0，见 `reports/pdf_search_regression.txt`。专项使用明确合成向量和实际内存 Qdrant；跨进程 DB 槽、slot backend 失效、无长事务、admission 后真实计划扩容、最终联合读回及未更新 SHA 标签的 DB 原始 JSON 变化均通过。首轮 12 项中的 4 个错误是验证脚本误用已脱敏 `connection.dsn`，失败保留；改为原连接参数仅替换临时库名后 12/12，再补失效边界 14/14，未放宽生产认证。HTTP 27/27→30/30→32/32 和联合向量 8/8 分别保存。实际本地 BGE/认证 Qdrant 烟测一个场景通过，两文件/三次发布、固定历史响应与来源链接、错误内部 SHA/缺失缓存/错误 key 拒绝；未调用生成模型、下载或外部模型。原库/旧模块/11/13/8 冻结摘要保持，临时库/账号/产物/向量实例清理。核查与边界见 `reports/pdf_search_verification.json`、`pdf_search_review.md`、`pdf_search_real_local_verification.json`；未新增页面或问答准确率。

上一私有 PDF 知识发布完整隔离回归 **411/411**（338 + 控制面 31 + 向量 24 + HTTP 18）、进程退出 0，见 `reports/pdf_index_regression.txt`。控制面使用假 vector adapter 专注 SQL/认证/租约；向量与 HTTP 使用真实内存 Qdrant 和明确合成嵌入，另在独立真实 1.12.4 服务验证 7 项协议边界。真实服务器首轮发现 bbox float64 JSON 回读相差 1 ULP；payload 改成精确 `bbox_f64_hex`，来源 chunk 数值不改，诊断/失败与最终通过记录分列。实际本地 BGE/CLI 场景验证发布、失败 exit 1、原 owner 重试且缺失模型缓存时重放冻结计划、v2 后 v1 字节不变。该场景最初两次因验证脚本只接受 403 和 macOS `/var` 路径别名失败，修正脚本后通过，未放宽冻结源门禁；原始日志保留。原库计数及旧模块、11/13/8 冻结摘要保持，临时库/账号/产物/向量实例已清理，无下载或外部模型调用。核查见 `reports/pdf_index_verification.json`、`pdf_index_real_local_verification.json`；没有新增页面或问答模型评分。

上一 PDF 私有快照登记完整隔离回归 **338/338**（292 + 服务 27 + HTTP 19）、进程退出 0，见 `reports/pdf_publication_regression.txt`。服务专项最终 27/27、HTTP 最终 19/19；初始 25/25 与 15/15 分别保留，增加锁等待期间过期及读取/提交后权限变化边界后再完整验证。覆盖 CAS 竞争、不同文档、request/owner/attempt 重用、事务回滚、v2 后 v1 字节保持、DB/文件篡改拒绝、实际会话/账户变化、readonly 无访问与旧表不写。原库计数未变、临时测试库/账户/产物已清理；副本不复制模型设置或私有快照行，未调用外部模型。见 `reports/pdf_publication_verification.json`，实现见 §18.31 和 `docs/PDF_SNAPSHOT_REGISTRY.md`。没有新增操作页面，该阶段使用真实 HTTP/数据库/文件测试。

上一准备阶段完整隔离回归 **292/292**（261 + 31）、进程退出 0，见 `reports/pdf_prepare_regression.txt`。专项最终 31/31、实际 CLI 8/8；不同文件/同 attempt/共享源的独立进程竞争、版本 ID、残缺/篡改、FIFO、加密、旋转/裁剪及真实 inline/SMask/Mask 超限均验证。首次 24 项中两个锁创建竞争错误和诊断失败保留，第二轮 24/24 与补齐边界后的最终 31/31 分列，不视作识别准确率。原库计数未变、临时测试库和 CLI 产物已清理、无新增外部模型调用；见 `reports/pdf_prepare_verification.json`、`reports/pdf_prepare_cli_verification.json`、`reports/pdf_prepare_review.md`。实现见 §18.30 和 `docs/PDF_PREPARATION.md`。

上一阶段 API 0.14.0 完整隔离回归 **261/261**（212 + 49），进程退出码 0；专项为队列 20、RAG 15、HTTP/SSE 11、上传 3。前端受控竞态检查 **5/5**；浏览器验证尺寸登记/独立批准/撤销/历史回放，混合 DXF/JSON 登记和重复点击幂等，以及两文件问答的检索/实际引用覆盖。见 `reports/next_stage_regression.txt`、`reports/next_stage_browser_verification.json`、`reports/multifile_ui_races.json`。两个一次性预览副本均清理，原数据库前后计数一致；该轮未调用外部模型。实现见技术说明书 §18.29 和 `docs/MULTIFILE_CONCURRENCY.md`。下列数字保留先前阶段原始分母。

- API 0.13.0 认证尺寸服务：`reports/cad_dimension_service_regression.txt` 保存上一阶段完整隔离回归 212/212；工作流见 `data/cad_demo/DIMENSION_SERVICE_WORKFLOW.md`，技术说明书 §18.28。上一阶段完成服务 API，本阶段补操作页面。

- `reports/cad_dimension_regression.txt`：完整隔离回归 **195/195**（179 + 16 项尺寸契约/独立声明/时间/版本/单位/事件链/回放测试），专项首轮 **16/16**。Docker 起初未运行，原失败日志保留在 `cad_dimension_regression_docker_initial.txt`；恢复既有服务后在副本验收。
- `reports/cad_dimension_contract_initial.json`：13 文件冻结、实际 DXF/测量源字节核对；合成 20.04 mm 与标注 19.9–20.1 mm 比较。身份/批准均作者声明，authentication_verified=false，不是生产准确率。
- `reports/cad_dimension_lifecycle.json`：未批准/批准/自己批准拒绝/撤销/撤销前历史时刻五个合成状态；`cad_dimension_freeze_verification.json`：代码与测量源改动拒绝、首轮报告拒绝覆盖、13 份运行快照。
- `data/cad_demo/DIMENSION_BINDING_CONTRACT.md`、`dimension_binding_v1/`、`data/evaluation/frozen/cad_dimension_v1/`：契约/Schema/材料/复现；旧 CAD 契约和六图兼容复现单列。技术说明书 §18.27。

- `reports/cad_reviews_regression.txt`：上一认证复核阶段隔离回归 **179/179**（167 + 12 项认证关联/历史/换版/目标/单位几何/完整性/权限/幂等并发测试）；专项首轮 12/12，无模型调用。
- `reports/cad_reviews_browser_verification.json`：合成页面存疑→模拟指定圆 38 确认→v2 未继承→v1 历史只读；原文件/预览/实体哈希不变，v2 后 v1 特征与复核状态响应不变。三张 `cad_reviews_*_browser.jpg` 为页面证据。
- `reports/cad_reviews_preview_cleanup.json`：副本/临时素材/凭据已清理，原库计数一致；`cad_reviews_contract_compatibility.json` 与 `cad_reviews_dxf_compatibility.json` 保存冻结范围核查与六图复现。工作流见 `ASSOCIATION_REVIEW_WORKFLOW.md`，技术说明书 §18.26。

- `reports/cad_assets_regression.txt`：上一资产阶段隔离回归 **167/167**（153 + 14 项资产/版本/权限/幂等/并发/源与快照完整性测试）。
- `reports/cad_assets_browser_verification.json`：页面登记 v1→v2→旧版回放；v1 原文件、SVG、特征响应 SHA256 均保持不变。`cad_assets_v2_browser.jpg`、`cad_assets_v1_replay_browser.jpg` 为页面证据。
- `reports/cad_assets_preview_cleanup.json`：预览副本、临时源/凭据清理及原库计数不变。`cad_dxf_assets_compatibility.json` 复现既有六份冻结图，不是新增准确率。工作流见 `data/cad_demo/ASSET_WORKFLOW.md`，技术说明书 §18.25。

- `reports/cad_dxf_regression.txt`：上一读取阶段完整隔离组件/集成回归 **153/153**（138 + 15 项原生读取/渲染/边界测试）。
- `reports/cad_dxf_acceptance_initial.json`：冻结后 6 新合成图首轮；标题 **15/15**，已知尺寸字段 **16/16**，原生实体/句柄/坐标 **35/35**，对象关联状态/使用门禁各 **5/5**，未知值保留 **4/4**，不支持 ARC 拒绝 **1/1**；不是任意 CAD 泛化准确率。
- `data/cad_demo/DXF_PROFILE.md`、`dxf_reader_v1_freeze.json`、`data/evaluation/frozen/cad_dxf_v1/`：受控 profile 边界、8 文件冻结哈希与源码快照；`reports/cad_dxf_browser.jpg` 验证原文/源 SHA/明确关联的双方高亮。初次默认字体校验失败日志保留。详见技术说明书 §18.24。

- `reports/cad_contract_regression.txt`：本轮完整隔离组件/集成回归 **138/138**（上一轮 126 + 新增 12 项 CAD 契约/门禁测试）；不是图纸识别准确率。
- `data/cad_demo/CONTRACT.md`、`manifest_v1.json`：冻结 `cad-contract-v1`；`reports/cad_contract_initial.json` 验证 11 个冻结文件、7 实体/1 作者确认直径、明确业务绑定及 19.9–20.1 mm 标注范围；源改动拒绝、首轮报告拒绝覆盖。DXF 和输出 JSON 均人工编写，未执行读取器，详见技术说明书 §18.23。

- `reports/source_authority_regression.txt`：上一权威阶段完整组件/集成回归 **126/126**；新增 14 项权限、独立启用、有效期/依据/版本、重叠、撤销、幂等/并发与历史回放检查。
- `reports/source_authority_verification.json`：页面登记→独立启用→新查询裁决→撤销恢复未消解；批准前后运行和冲突快照不变；原库未修改。截图为 `reports/source_authority_result_browser.png`、`reports/source_authority_revoked_browser.png`，技术说明书 §18.22 记录边界。

- `reports/identity_regression.txt`：上一身份/审批阶段完整组件/集成回归 **112/112**，新增 13 项身份、角色、CSRF、会话、独立审批、版本/幂等/并发与失效门禁检查；不是模型准确率。
- `reports/identity_approval_verification.json`、`reports/identity_approval_browser.png`：隔离副本中复核员确认→另一审批员批准→查看历史；批准后 B001 冲突仍未消解，原值/声明保留；副本与临时凭据已清理。技术说明书 §18.21 记录边界。

- `reports/industrial_forms_regression.txt`：上一紧凑格式阶段完整组件/集成回归 **99/99**；新增 10 项格式、绑定、坐标、复核与篡改检查。旧 89/89 报告保留；不是模型准确率分母。
- `reports/industrial_forms_acceptance_v2_initial.json`：冻结代码后新样本首轮 **104/104 字段、8/8 完整事实**；原生/扫描各 **52/52、4/4**，字段必须同时满足文本、定位、实体绑定和单位校验。
- `reports/industrial_forms_development_v2.json`：单独开发材料 **52/52、4/4**，不与新样本成绩混合。
- `reports/industrial_forms_v2_verification.json`：代码/评测/PDF 哈希、首轮历史不变、代码版本、数据库隔离与页面检查；技术说明书 §18.20 说明范围和限制。
- `reports/field_review_verification.json`：API 0.7.0、服务健康、源字段/审批不变、旧运行回放与冻结解析器哈希核查。
- `reports/industrial_forms_initial.json`：首轮 **28/56、2/4**，保留所有缺失和失败。紧凑模板原文块能被读取，但 v1 格式解析未接受；诊断不计字段通过。
- `reports/industrial_nl2sql_initial.json` 与 `reports/industrial_nl2sql.json`：同一批真实模型输出的初始评分与数值类型复算；不拼接重试成绩。
- `reports/field_review_current.png`、`reports/field_review_browser.png`：最终页面及隔离副本提交验证。
- [V1.2 历史说明书](技术文档/DataMind_Agent_技术设计说明书_V1.2.md) §18.12–18.33：阶段历史、代码路径、错误、边界和下一步；[V1.3 §18](技术文档/DataMind_Agent_技术设计说明书_V1.3.md#18-实施状态历史成绩与修订记录) 集中汇总当前状态与全部分母。

## 下一阶段顺序与完成条件

按「技术实现优先」排序；交付类工作排在最后，等前置技术任务完成后再启动。

| 顺序 | 工作（技术文档出处） | 当前状态 | 完成条件 |
|---|---|---|---|
| T1 | V-CAR 接入 HTTP 与 `task_budget`（§6.1/§6.4） | **已完成**（10 项新测试；见 `reports/vcar_bounded_verification.json`） | 节点在独立进程组内执行、占用跨进程资源槽、取消可传播；HTTP 目标由服务端白名单决定 |
| T2 | V-CAR 机制消融与误修复/错误消解统计（§6.4） | **已完成**（机制层；18 项测试，见 `reports/vcar_ablation_verification.json`） | 四条机制臂已对照并报告误修复率/错误消解/未消解；**真实模型与视觉载荷、模型规模对比仍待做** |
| T3 | 通用 Parser Adapter 与统一 Locator/Relation 契约（§5.2/§5.4） | **契约、映射、解析出口、media type 路由与定位存储已完成**（42 项测试）；仅注册 PDF 适配器 | Office/CAD 适配器、关系存储与检索消费 |
| T4 | Office（DOCX/PPTX/复杂 XLSX）与图纸 PDF/DWG 适配（§5.1.1/§5.8） | **Office 基础适配与原生 DXF 契约接入已完成**（22 项测试）；图纸 PDF 区域路径与 DWG 转换未实现 | 逐格式原生对象解析、定位与关系；每格式各有样本与独立分母 |
| T5 | 统一 CPU/内存配额与跨主机租约（§13） | **CPU 限额与租约已完成**；内存限额在本机 macOS 不可执行（失败关闭） | 资源硬隔离（需 Linux cgroup）、租约接入执行路径 |
| T6 | 授权真实输入验收（§9.1） | 缺外部材料 | 真实模板/图纸/扫描件独立样本，标签先冻结，分列原生/扫描分母 |
| D1 | 赛题交付包、答辩材料与官方专项核对 | **延后**（见「现阶段优先级」） | 前置技术任务完成后启动；不复用合成材料成绩宣称验收 |

已完成且已验收的前序项：紧凑工业格式 v2（开发 52/52、新样本 104/104）、本地认证/权限与独立字段审批、来源权威登记机制、受控 CAD 契约/原生 DXF/资产/关联复核/尺寸服务、原生 PDF 准备与私有快照/发布/检索/回答、向量修复与孤立回收、统一任务预算与 OCR/公式接入、V-CAR 编排原语。这些是组件与契约验收，**不等于真实工厂盲测或模型准确率**。

仍有明确缺口的旧项：复杂工业 SQL、通用跨源动态计划（T1/T2 完成后才成立）、通用 CAD/图纸 API、国产小模型与视觉级联、完整 P1/M6。`ingestion/ingest.py` 仍是串行离线工具，不能同时启动多个旧摄取进程或在并发任务中执行全局 reset；受控队列登记不自动把图纸与测量配对，也不替人工复核/批准。

## 启动与报告复现

**本机免登录（默认）**：服务只绑定 `127.0.0.1`，来自本机的请求自动以已登记的 `local-admin`（admin）身份进入，各页面与接口不再要求密码；CSRF、同源检查与角色门禁代码保留，设 `DATAMIND_LOCAL_NO_LOGIN=0` 即恢复强制登录，`=1` 可对非本机客户端也启用。需要区分身份（如“另一账户审批”）时，按下面步骤创建真实账户并关闭该开关。

```bash
bash scripts/start_local.sh --background
.venv/bin/python scripts/test_isolated.py
.venv/bin/python scripts/verify_cad_contract.py
# DXF 需要可选依赖：.venv/bin/python -m pip install -r requirements-cad.txt
.venv/bin/python scripts/parse_cad.py data/cad_demo/development/dev-mm.dxf --document-id doc-dev-mm --provenance synthetic_demo --output-dir tmp/cad-new-preview
# 账户由本地管理员交互创建；没有默认密码，复核/审批应使用不同账户
.venv/bin/python scripts/manage_users.py create reviewer1 --name 复核员 --role reviewer
.venv/bin/python scripts/manage_users.py create approver1 --name 审批员 --role approver
.venv/bin/python scripts/manage_users.py create admin1 --name 管理员 --role admin
# 受控多文件登记队列必须另启 worker；与 HTTP 服务使用相同写入 DSN
.venv/bin/python scripts/file_job_worker.py --watch --workers 2
```

v1 首轮复现需使用 `data/evaluation/frozen/industrial_native_v1/manifest.json` 记录的 Git 基线；当前 v2 代码不能冒充旧冻结解析器。v2 新样本验收拒绝代码/评测器哈希变化，`--current-parser` 只允许开发套件。本次审批功能改变了冻结范围内的字段读取代码；历史 v2 复现须在独立目录按 `data/evaluation/frozen/industrial_labelled_v2/manifest.json` 的 `runtime_snapshots` 恢复冻结文件，输出到新报告路径。当前检出直接运行旧验收会因哈希变化停止，不能把本次代码算作旧首轮。升级原服务前需在目标检出中完成合并、重新提取工业文档并重启；旧解析版本的复核决定不会自动继承。

页面入口：`http://127.0.0.1:8000/#fields`；图纸入口为 `/#cad`，所有图纸读取需登录，素材列表与登记仅 admin，关联复核仅 reviewer/admin，历史图纸只读。尺寸入口 `/#dimensions`：admin 登记/绑定与保存快照，独立 approver/admin 批准/撤销，所有已登录角色可读取当前状态与历史。多文件入口 `/#files`：管理员上传受控 DXF/单测量 JSON，提交逐项任务并查看结果；后台必须另启 `--watch` worker。问答页可选最多 8 份已索引公开文档，显示相关片段与实际引用覆盖，不能视作全文比较。图纸和尺寸保持独立资产，不自动进入 RAG 或生产事实。升级后登录本地配置账户；模型接入只允许管理员，复核只允许 reviewer/admin，独立审批只允许 approver/admin 且不能审批自己。来源权威入口为 `/#authority`：管理员登记，另一 approver/admin 账户启用，规则仅绑定具体测量及原始版本。公共问数/文档仍为本机演示范围，不是完整多租户访问控制。启动保留既有数据并应用增量迁移；OCR/公式权重按 README 的设置脚本安装，虚拟环境、权重/密钥、本地日志、临时渲染和可再生预览不随仓库发布。实时数据库、模型凭据和配额不属于 Git 内容；新机器需要独立初始化和配置。模型评测会调用上游，以上历史组件回归不新增真实模型调用。
