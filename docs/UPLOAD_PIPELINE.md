# 上传 → 解析 → 入库 → 查询 管道接口契约（冻结）

**前端版本说明（2026-10-10）**：旧版 `web/multifile.js` 已退役删除，本文相关页面实现与验收属于历史记录；上传 API 与 worker 保留，新版文件管理入口待接入。页面迁移计划见 [前端评估](FRONTEND_NEXT_STEPS_20261010.md)，旧源码可从 Git 历史查阅。

本文件是 `/v1/uploads` 系列接口的**唯一规范来源**，供前端与后端并行实现时对齐。
实现位于 `ingestion/upload_pipeline.py`（管道与队列）、`ingestion/upload_worker.py`
（独立 worker：领取、心跳、波次与并发上限）、`datamind/file_uploads.py`
（暂存）、`datamind/api.py`（路由与权限）、`ingestion/contract_store.py`（统一契约存储）；
领取代次的表结构见 `db/migrations/018_upload_claim_generation.sql`。

约定：所有响应均为 JSON；除 `/v1/documents*`、`/v1/runs*` 之外，`/v1/uploads*` 全部
位于**默认拒绝**中间件之内（`datamind/api.py: _required_permission` 把 `/v1/uploads`
按 `files_path` 同类处理）。

---

## 0. 权限、CSRF 与同源

| 方法 | 路径 | 身份 | 权限 | CSRF | 同源 |
| --- | --- | --- | --- | --- | --- |
| POST | `/v1/uploads` | 必须已登录 | `admin` | 必须 | 必须 |
| GET | `/v1/uploads` | 必须已登录 | `admin` | — | — |
| GET | `/v1/uploads/{upload_id}` | 必须已登录 | `admin` | — | — |
| POST | `/v1/uploads/{upload_id}/approve` | 必须已登录 | `admin` | 必须 | 必须 |

- 未登录 → `401 {"detail":{"error_code":"LOGIN_REQUIRED"}}`；会话失效 → 401
  `SESSION_INVALID`（均沿用既有中间件 `datamind/auth.py`）。
- 已登录但非 admin → `403 {"detail":{"error_code":"PERMISSION_DENIED"}}`。
- 变异请求缺少 CSRF 头 → `403 CSRF_REQUIRED`；跨源 → `403 ORIGIN_NOT_ALLOWED`。
- 响应头：`Cache-Control: no-store`（`/v1/uploads` 已加入 `_NO_STORE_PREFIXES`）。

---

## 1. `POST /v1/uploads` —— 一次调用完成「暂存 + 入队」

```
POST /v1/uploads?filename=<name>&provenance=<synthetic_demo|authorized_external>
Content-Type: application/octet-stream
<原始字节>
```

- `filename`：查询参数，1–200 字符，必须是**纯文件名**（无路径分隔符、无 `\`、无控制字符）。
- `provenance`：`synthetic_demo` 或 `authorized_external`；决定落盘目录
  （`development` / `authorized`）。非这两个值 → FastAPI 422 校验错误。
- 请求体：原始字节，**不做 multipart**，不做内容嗅探前的解码。

### 接受类型与上限

| 后缀 | media_type | 上限 |
| --- | --- | --- |
| `.dxf` | `image/vnd.dxf` | 8 MiB |
| `.json` | `application/json` | 1 MiB |
| `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | 16 MiB |
| `.pptx` | `application/vnd.openxmlformats-officedocument.presentationml.presentation` | 16 MiB |
| `.xlsx` | `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` | 16 MiB |
| `.pdf` | `application/pdf` | 32 MiB |

`.dwg` 属于**已知但不可处理**类型：本仓库没有 DWG 转换器。

### 成功：201

```json
{
  "upload_id": "upl-<40 hex>",
  "job_id": "job-<40 hex>",
  "filename": "dev-mm.dxf",
  "media_type": "image/vnd.dxf",
  "status": "queued",
  "review_required": true
}
```

- `upload_id` / `job_id` / `document_id` 都是**内容寻址**：由暂存字节的 SHA-256 派生。
  同一份字节重复上传是**幂等**的——返回同一 `upload_id`，复核门重新打开
  （`needs_review=true`、`status='parsing'`）。不同文件名/来源的同一份字节仍映射到同一条记录。
- `status` 是**该 job 当前的真实队列状态**（不总是 `queued`）：见下方「重传规则」。
- `review_required` 恒为 `true`：解析成功也**默认不可检索**，必须显式 approve；且 approve
  只是必要条件——是否可检索由 §2 的 `searchable` 派生判定给出。

### 重传规则（R04，本版明确的一条可断言规则）

同一份字节的再次上传**不会**无条件把任务重置为 `queued`：

| 上传时 job 状态 | 行为 | 201 响应里的 `status` |
| --- | --- | --- |
| `running`（已有 worker 领取并正在解析） | **不重置、不开新代次**：`status`/`claim_token`/`attempt`/时间戳都不变，复用当前代次 | `"running"` |
| `queued` | 保持 `queued`（刷新 `created_at`），代次未开启，`claim_token` 保持 NULL | `"queued"` |
| 终态（`succeeded`/`failed`/`partial_failure`/`cancelled`） | 重新入队为 `queued`、清空 `claim_token`；**下一次领取**开启新代次（`attempt+1` + 新 token） | `"queued"` |

理由：`running` 被重传重置会让**正在执行的领取失去归属**——旧的完成回调与新的领取同时
认为自己在跑同一个任务，结果可能是假成功、也可能覆盖新结果（审查 R04 的原始缺陷）。
守护测试：`tests/test_upload_claim_generation.py`（含两个管理员并发上传同内容）。

### 失败：稳定错误码

| 触发 | HTTP | `detail.error_code` | 额外字段 |
| --- | --- | --- | --- |
| 文件名为非法（路径、反斜杠、控制字符、超长、空） | 422 | `FILE_TYPE_OR_NAME_INVALID` | — |
| 扩展名未支持（未知后缀、无后缀） | 415 | `UNSUPPORTED_MEDIA_TYPE` | `details.suffix` |
| `.dwg` | 415 | `CAD_DWG_CONVERTER_UNAVAILABLE` | **无**（响应体恰为 `{"detail":{"error_code":"CAD_DWG_CONVERTER_UNAVAILABLE"}}`） |
| 空 body | 422 | `UPLOAD_EMPTY` | — |
| 超过上限（`Content-Length` 或流式累计） | 413 | `UPLOAD_TOO_LARGE` | — |
| `Content-Length` 不是整数 | 422 | `UPLOAD_LENGTH_INVALID` | — |

> **旧接口同族错误码的归属（务必区分）**
>
> 这些码**只属于**旧接口 `POST /v1/files/uploads`（`datamind/file_uploads.py: stage`），
> 且其上限仍是 `.dxf` 2 MiB / `.json` 1 MiB：
>
> - `FILE_UPLOAD_TOO_LARGE_OR_EMPTY`（413）——旧接口的「超限或空」
> - `FILE_UPLOAD_INVALID_LENGTH`（422）——旧接口的 `Content-Length` 非整数
> - `FILE_TYPE_OR_NAME_INVALID`（422）——**两个接口共用**（含义相同：文件名/后缀非法）
>
> 新接口 `/v1/uploads` **不会**返回 `FILE_UPLOAD_TOO_LARGE_OR_EMPTY` 或
> `FILE_UPLOAD_INVALID_LENGTH`。前端按新接口实现时只需处理上表。

---

## 2. `GET /v1/uploads` —— 列表（**只读，不推进任何解析**）

```
GET /v1/uploads?status=&media_type=&since=&limit=
```

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `status` | string | 不过滤 | 必须属于 `status` 枚举，否则 422 `UPLOAD_FILTER_INVALID`（`details.filter="status"`） |
| `media_type` | string | 不过滤 | 精确匹配 `media_type` |
| `since` | string | 不过滤 | epoch 秒（如 `1710000000`）或 ISO-8601（如 `2026-10-08T00:00:00Z`）；非法 → 422 `UPLOAD_FILTER_INVALID`（`details.filter="since"`） |
| `limit` | int | `50` | 范围 1–200 |

- 排序：`created_at DESC`（最新在前）。
- `count` = **本页返回条数**（不是匹配总数，也不受分页偏移影响；本接口无 offset）。
- 时间格式：`created_at` 为带时区的 ISO-8601（服务器本地时区，如
  `2026-10-08T20:20:54.793442+08:00`）。做「最近 N 分钟」过滤请用 epoch 秒或带时区的
  ISO 字符串，不要用无时区的本地时间字符串。
- **列表不驱动解析**（详情也不驱动，见 §3）：只有独立 worker 调用 `work_once()`/`drain()`
  才会推进 job。列表在此之上如实报告 `worker`（见下），消费方据此决定文案。

响应：

```json
{
  "uploads": [
    {
      "upload_id": "upl-...",
      "job_id": "job-...",
      "document_id": "doc-upload-...",
      "filename": "demo-diameter.json",
      "media_type": "application/json",
      "provenance": "synthetic_demo",
      "status": "partial_failure",
      "created_at": "2026-10-08T20:20:54.793442+08:00",
      "review_state": "pending_review",
      "review_required": true,
      "searchable": false,
      "index_state": "not_approved",
      "index_note": "UPLOAD_NOT_APPROVED",
      "block_count": 0,
      "locator_count": 0,
      "unsupported_features": ["locator_unavailable"]
    }
  ],
  "count": 1,
  "worker": {"alive": true, "last_seen_at": "2026-10-08T20:20:55.123456+00:00", "alive_seconds": 30.0}
}
```

列表项字段集**不变**（`worker` 是响应顶层的新字段，不是列表项字段）。

### `worker` 字段（心跳，本版新增，审查 5.1）

```json
"worker": {"alive": false, "last_seen_at": null, "alive_seconds": 30.0}
```

| 字段 | 含义 |
| --- | --- |
| `alive` | 最近 `alive_seconds` 秒内**有**上传 worker 续过心跳。判定阈值 = `DATAMIND_UPLOAD_WORKER_ALIVE_SECONDS`（默认 **30s**），也是心跳租约的 TTL（`task_leases`，迁移 013）；worker 崩溃后租约自然过期，`alive` 自动变 false |
| `last_seen_at` | 最近一次心跳时间（UTC ISO-8601）。优雅退出会删除心跳行，此时为 `null`；worker 崩溃后心跳行在被清理前仍在，可据此提示「最后心跳 …」 |
| `alive_seconds` | 当前生效的阈值（秒），便于消费方解释判定 |
| `error`（仅异常时出现） | 心跳表不可读等非数据库故障时为 `"UPLOAD_WORKER_HEARTBEAT_UNREADABLE"`，同时 `alive=false`：探测失败**不猜测**有 worker，也不让 `/health`、上传列表因此 500 |

**诚实提示（必须遵守）**：`status='queued'` 且 `worker.alive=false` 时，不得显示「解析中」
或任何等价进度文案；应如实提示「后台处理器未运行（任务不会自行推进）」，并给出启动方式：

```
python -m ingestion.upload_worker --watch
```

`/health` 也带同一个 `worker` 子字段（并新增 `upload_worker` 能力标记），内容与列表一致
（同一函数 `ingestion.upload_pipeline.worker_heartbeat`）；`GET /v1/uploads/{upload_id}`
也带它（见 §3），便于轮询详情的前端直接判断。

### `status` 枚举（队列语义，与既有 `file_jobs` 一致）

`queued` | `running` | `succeeded` | `failed` | `partial_failure` | `cancelled`

映射规则（`parse_status` → `status`）：

| `parse_status` | `status` |
| --- | --- |
| `parsed` | `succeeded` |
| `partial` | `partial_failure` |
| `unsupported`（显式声明缺口，如 measurement JSON 无块级定位） | `partial_failure` |
| `failed` | `failed` |

### `review_state` 枚举（**只有两个值**）

- `pending_review`：`needs_review=true`（未复核，不可检索）。
- `approved`：`needs_review=false`（已 approve；若契约确有块，`status` 同时置 `indexed`）。

`review_required` 是布尔镜像（= `needs_review`），与 `review_state` 同时出现在列表项与
详情中。**没有** `not_applicable` / `rejected` 取值；失败的上传仍为 `pending_review`。

### 可检索性 `searchable` / `index_state` / `index_note`（R05）

「解析完成」「人工复核」「检索就绪」是**三件独立的事**。审批只要求上传成功，而检索器
（`datamind/knowledge.py`）**只读 `chunks`**。本版起，解析波次在终态提交之后调用
`ingestion/upload_index.py` 写入切片与向量（见 §4 的顺序协议），资产仍保持
`needs_review=TRUE`、`status='parsed'`；只有 `approve` 才解除复核门，并且**仅当
`chunks>0`** 才把资产置为 `indexed`。因此可检索是派生判定，不是存储的状态。

列表项、详情与 approve 响应统一新增：

```json
{
  "searchable": true,
  "index_state": "indexed",
  "index_note": null
}
```

- `searchable`（bool，**派生**）：
  `searchable == (asset.status == 'indexed') AND NOT needs_review AND chunk_count > 0`。
  消费方（问答文件选择器）必须要求 `searchable=true`；它是唯一可以据此宣称「该文件可
  用于问答检索」的字段。
- `index_state`（**恰好三个值**，`index_state=="indexed"` 当且仅当 `searchable=true`）：

  | 值 | 含义 |
  | --- | --- |
  | `indexed` | 已复核、已发布、且 `chunks` 非空——真正可检索 |
  | `no_chunks` | 已复核，但检索器要读的切片存储为空（建索引失败或无可索引文本） |
  | `not_approved` | 复核门未打开（`needs_review=true`），无论切片是否存在 |

- `index_note`（str \| null）：机器可读的**原因码**，人类可读文案由消费方映射，**不要
  直接展示原文**。取值集合：

  | `index_note` | 出现条件 |
  | --- | --- |
  | `null` | `searchable=true` |
  | `UPLOAD_NOT_APPROVED` | `index_state='not_approved'` |
  | `UPLOAD_HAS_NO_CHUNKS` | 已复核、契约有块（`block_count>0`）、但 `chunks=0` |
  | `UPLOAD_HAS_NO_CONTRACT_BLOCKS` | 已复核、契约也无块（`block_count=0`）、`chunks=0` |
  | `UPLOAD_STATUS_NOT_INDEXED` | `chunks>0` 但资产未被标记 `indexed`（只可能由人工改库产生） |

- 这三个字段在 `/v1/uploads` 列表项、`/v1/uploads/{upload_id}` 详情、approve 响应与
  `GET /v1/documents` 的每一项中**含义完全一致**：四个端点全部调用**同一个**判定函数
  `ingestion.upload_pipeline.searchability(status, needs_review, chunks, block_count=...)`，
  不各自写一份条件（守护测试：
  `test_upload_searchability.py::test_documents_and_upload_detail_agree_on_the_index_state`
  —— 同一文档在两个端点上必须得到相同的 `index_state`/`index_note`）。它们**都不改变**
  既有字段（`status`/`review_state`/`review_required`/计数）的含义。
- **`index_error_code`**（str \| null，本版新增）：建索引失败时的稳定原因码
  （`UPLOAD_INDEX_FAILED` / `UPLOAD_ASSET_MISSING`），未失败时为 `null`。它和
  `error_code`（**解析**失败）是两个字段：解析成功而索引失败时，`status='succeeded'`、
  `error_code=null`，只有 `index_error_code` 非空——失败不能被洗成解析失败，成功的解析
  也不能被洗成成功的索引。

### `GET /v1/documents` 的 `searchable` / `index_state` / `index_note`

问答文件选择器的数据源 `GET /v1/documents` 的**每一项也新增这三个字段**（同一派生定义、
同一批 code、同一函数）。该路由的发布过滤条件（`status='indexed'` + `public` + 非待复核）
**本版未改**：历史遗留的「`indexed` 但 `chunks=0`」行仍会出现，但带
`searchable=false` + `index_state="no_chunks"` + `index_note`（如
`UPLOAD_HAS_NO_CHUNKS` / `UPLOAD_HAS_NO_CONTRACT_BLOCKS`），于是选择器既能排除它，也能对
用户说明原因，而不是只给一句通用文案。消费方必须显式要求 `searchable=true`，否则会重现
R05 的症状。

---

## 3. `GET /v1/uploads/{upload_id}` —— 详情（**纯读，不推进任何解析**）

自本版起该请求**没有任何写副作用**：不领取、不解析、不改状态、不写契约。它只报告当前真实
状态，因此页面关掉也不会让任务停摆（任务由 §6 的独立 worker 推进）。

> 变更提示（审查 5.1）：旧版本这个 GET 会为该上传推进一波有界解析，于是「浏览器轮询」就是
> 队列时钟；同时 `workers=1..4` 只约束单次调用，不约束多个 HTTP 请求/多个进程的合计并发。
> 现在推进任务的唯一入口是 `python -m ingestion.upload_worker`（或显式调用
> `drain()`/`work_once()` 的宿主进程）。

- `upload_id` 必须匹配 `^upl-[0-9a-f]{40}$`，否则 404 `UPLOAD_NOT_FOUND`。
- 不存在 → 404 `UPLOAD_NOT_FOUND`。

响应 = 列表项的全部字段，另加：

```json
{
  "source_sha256": "<64 hex>",
  "parser_version": "cad-contract-adapter-v1 或 null",
  "page_count": null,
  "collection": "demo_kb",
  "attempt": 1,
  "analysis": {
    "parse_status": "parsed",
    "adapter": "dxf-native",
    "blocks": 8,
    "locators_written": 8,
    "relations": 1,
    "block_types": {"title": 3, "figure": 3, "dimension": 1, "paragraph": 1},
    "unsupported_features": [],
    "quality_findings": ["CAD_TEXT_BOUNDS_APPROXIMATE"],
    "contract_version": "parse-contract-v1",
    "layers": 5,
    "layer_names": ["GEOMETRY", "..."],
    "entities": 7,
    "dimensions": 1
  },
  "error_code": null,
  "vector_index": "embedded",
  "vector_index_note": "上传管道在解析波次内建立切片与向量（ingestion/upload_index.py），与知识库摄取共用同一发布顺序协议；未建立时如实报告 deferred/failed。",
  "index_error_code": null,
  "chunk_count": 3,
  "wave_deadline_seconds": 45.0,
  "wave_timeouts": 0,
  "locator_stores": {"contract_blocks": 8, "page_shaped": 0},
  "contract_url": "/v1/documents/doc-upload-.../contract",
  "approval": null,
  "started_at": "2026-10-08T20:20:54.5+08:00",
  "finished_at": "2026-10-08T20:20:54.9+08:00",
  "worker": {"alive": true, "last_seen_at": "2026-10-08T20:20:55.123456+00:00", "alive_seconds": 30.0}
}
```

- `attempt`（int，本版新增）：该内容**已经被领取过多少次**（`ingest_job_claims.attempt`，
  迁移 018）。`0` = 从未被领取（仍在 `queued` 或从未跑过）。它与 `claim_token` 一起构成
  领取代次，`attempt` 只增不减，是"重试过几次"的可断言来源。
- `worker`（本版新增）：**与列表、`/health` 完全相同的 `worker` 子字段**（同一函数）。轮询
  详情的客户端因此不需要再请求列表，就能在 `status='queued'` 且 `worker.alive=false` 时如实
  提示「后台处理器未运行」。
- 详情同样带列表项里的 `searchable` / `index_state` / `index_note`（定义见 §2，与 approve
  响应逐字段一致）。

### `analysis` 字段

- `parse_status`：**直接来自统一解析契约的 `ParseResult.parse_status`**，取值
  `parsed|partial|needs_review|failed|unsupported`（`datamind/parse_contract.py`）。
  它**不是** `status`：`status` 是队列终态，`parse_status` 是解析器对本次解析完整性的
  自述。
- `adapter`：产生该结果的适配器名。取值来源优先级：`ParseResult.manifest.adapter`，
  否则按后缀取
  `dxf-native|measurement-json|pdf-native|docx-native|pptx-native|xlsx-native`。
- `plugin`（本版新增）：声明该后缀的**插件名**，按注册表声明扫描而非启用状态查询，所以
  插件停用后已有记录仍如实归因；没有声明时为 `null`。它来自声明，`adapter` 来自解析结果，
  两者是不同字段（内置插件按适配器同名，通常取值相同）。
- `blocks`：写入统一契约存储（`document_contract_blocks`）的块数。
- `locators_written`：**已验证定位**的块数（当前等于 `blocks`，因为契约要求每个块都带
  合法 locator）。
- 其余为可用计数：DXF 给 `layers/layer_names/entities/dimensions`；PDF 给 `pages`；
  Office/measurement 只有 `block_types`。

### `locator_count` 的口径（与 `/v1/documents/{id}/contract` 的差异，必须知悉）

- 上传详情 / 列表的 `locator_count` = `document_contract_blocks` 行数 +
  `document_block_locators` 行数，并额外给出 `locator_stores` 明细。
- 既有 `GET /v1/documents/{id}/contract` 的 `locator_count` **只数分页型
  `document_block_locators`**（它是为 PDF 分页定位设计的历史口径），Office/DXF 的
  locator 存在 `blocks[].locator` 里，所以 DXF 会出现「`locator_count=0` 但
  `blocks[].locator` 齐全」。
- **为消除这个误导，`/contract` 自本版起同时返回三种口径**（`ingestion/contract_store.py`
  的 `read_result`）：`locator_count`（分页型）、`block_locator_count`（块内嵌）、
  `locator_total`（两者之和）。消费方**读 `locator_total` 或 `block_locator_count`**
  即可，不要再从单一的 `locator_count` 推断"没有定位"。
  守护测试：`tests/test_contract_store.py::LocatorCountSemanticsTests`。

### `/contract` 的可见性与分页（本版新增，必须知悉）

**可见性**（`datamind/api.py` 的 `_published_document` + `_contract_visible`，与发布路径共用同一判定）：

| 文档状态 | 匿名 | viewer | 已认证 admin |
|---|---|---|---|
| 已发布（`indexed` + `public` + 非待复核） | **可读** | 可读 | 可读 |
| 待复核上传（`origin='upload'`，未 `indexed`） | **404** | **404** | **可读**（复核流程依赖） |
| 未解析 / 取消 / 失败 | 404 | 404 | 404 |

- 非 admin 的 404 响应体与"该 id 不存在"**逐字节相同**，因此不泄露存在性；
- 待复核分支的响应带 `Cache-Control: no-store`，已发布分支不带；
- 该路由位于公开读前缀，因此**身份在路由内解析**（`current(..., required=False, provision_demo=False)`），身份解析用写 DSN、且 GET 不写库。
- 守护测试：`tests/test_contract_visibility.py`。

**分页与截断诚实性**（`read_result` 三存储统一 `LIMIT/OFFSET` + 稳定排序）：

- 旧的扁平键（`block_count`/`locator_count`/`block_locator_count`/`locator_total`/`relation_count`）**含义不变，仍是"本页数量"**；
- 新增 `totals`（库内总数）、`counts`（本页数量）、`page`（契约块的窗口：`limit/offset/total/returned/truncated/has_more`）、`store_pages`（三存储各自的窗口）；
- 查询参数 `limit`（默认 2000，1..5000）与 `offset`（0..1000000）；非法值返回 400 `CONTRACT_PAGE_LIMIT_INVALID` / `CONTRACT_PAGE_OFFSET_INVALID`，**不静默钳制**；
- 关系查询与块查询受同一显式上限约束，`store_pages.relations.truncated` 可让"关系可能指向本页之外的块"这件事**显式可查**。
- 守护测试：`tests/test_contract_store.py::ContractPaginationHonestyTests`。

### `vector_index`

索引**事实**字段（与 §2 派生的 `index_state` 是两件事），取值集合：

| 值 | 含义 |
| --- | --- |
| `embedded` | 解析波次已写入该文档的 `chunks` 行与 Qdrant 向量 |
| `failed` | 尝试建索引但失败（`index_error_code` 非空）；没有可用索引 |
| `deferred` | 没有可索引的内容（无契约块或切不出切片），未尝试建索引 |

- 建索引由 `ingestion/upload_index.py::index_document` 完成，与知识库摄取
  （`ingestion/ingest.py::publish_document`）共用同一发布顺序协议（F10）：
  ① 先置不可检索态并 commit → ② Qdrant `delete(document_id)` + `upsert` →
  ③ 写 `chunks` 元数据并 commit。任一步失败都停在不可检索态，绝不出现
  「旧文本 + 新向量」或「新文本 + 无向量」被读者看到。
- 建索引**不发布**文档：资产保持 `needs_review=TRUE`、`status='parsed'`；只有
  `approve` 才可能把它置为 `indexed`，且**仅当 `chunks>0`**。
- 索引在**领取代次栅栏**内写：`ingest_job_claims.attempt` 是只增不减的代次计数，
  被更新代次超越的旧代次不会写入任何向量或切片（`index_state='superseded'`，
  只在管道内部可见，不落库）。
- 前端不得把 `status='indexed'` 单独解读为「已向量化」；要判断「能否用于问答」只看
  §2 派生的 `searchable`。

### 失败原因码：`error_code` vs `index_error_code`

`error_code` 只描述**解析**；`index_error_code` 只描述**建索引**。两者可以独立出现：
解析成功（`status='succeeded'`、`error_code=null`）而建索引失败（`vector_index='failed'`、
`index_error_code='UPLOAD_INDEX_FAILED'`）是合法且必须如实呈现的状态。

### 失败的 `error_code`

`error_code` 只在 `status='failed'` 时非空，取解析器/管道抛出的稳定码，例如：

| 场景 | `error_code` |
| --- | --- |
| 内容嗅探不出已注册适配器（如伪装的 `.dxf`） | `PARSE_ADAPTER_UNKNOWN` |
| DWG 内容出现在 `.dxf` 文件中 | `CAD_DWG_CONVERTER_UNAVAILABLE` |
| 暂存路径越界/后缀不符 | `UPLOAD_STAGED_SOURCE_INVALID` |
| 暂存字节被改动（SHA 不符） | `UPLOAD_STAGED_SOURCE_CHANGED` |
| measurement JSON 结构非法 | `UPLOAD_MEASUREMENT_SOURCE_INVALID` 或 `DIMENSION_SOURCE_INVALID` |
| 解析抛未知异常 | `UPLOAD_PARSE_FAILED` |
| 资产行丢失 | `UPLOAD_ASSET_MISSING` |
| 连续 `MAX_WAVE_ATTEMPTS` 波都用尽预算 | `UPLOAD_WAVE_ATTEMPTS_EXHAUSTED` |

失败时 `document_assets.status='failed'`、`needs_review=true`，不会出现在
`/v1/documents`；approve 会被 409 拒绝。

---

## 4. `POST /v1/uploads/{upload_id}/approve` —— 最小复核动作

- 请求体：无（可选 JSON `{"reason": "..."}`，当前实现忽略 body）。
- 权限：admin + CSRF + 同源。
- 成功：200，响应体 = **详情对象**（§3）的完整字段，另加 `indexed`（bool），并始终带
  `searchable` / `index_state` / `index_note`。**`indexed == searchable`**，仅为 true 时
  才是真的可检索。关键字段示例（文件已建索引，审批后即可用于问答）：

```json
{
  "upload_id": "upl-b2408306ce5fae7c07a62f5a733bc986cc2f20f1",
  "status": "indexed",
  "review_state": "approved",
  "review_required": false,
  "indexed": true,
  "searchable": true,
  "index_state": "indexed",
  "index_note": null,
  "block_count": 8,
  "chunk_count": 3,
  "vector_index": "embedded",
  "index_error_code": null,
  "approval": {
    "user_id": "user-...",
    "display_name": "本机管理员",
    "roles": ["admin"],
    "approved_at": "2026-10-08T12:20:55.665966+00:00",
    "reason": null,
    "indexed": true,
    "block_count": 8,
    "chunk_count": 3
  }
}
```

若解析波次没能建索引（`vector_index='failed'`、`index_error_code='UPLOAD_INDEX_FAILED'`），
同一请求仍返回 200，但 `indexed=false`、`searchable=false`、`index_state="no_chunks"`、
`index_note="UPLOAD_HAS_NO_CHUNKS"`，`status` 保持 `parsed`——复核门已开，文件仍不可检索，
原因是索引缺失而非复核缺失，两者在响应里可区分。

行为：

1. 记录操作者到 `document_assets.quality_detail.review_approval`（含 `user_id`、
   `display_name`、`roles`、`approved_at`、`block_count`、`chunk_count`、`indexed`）。
2. `needs_review := false`（复核门打开；这是 approve 唯一无条件做的事）。
3. **当且仅当**检索器真正读取的切片存储非空（`chunks` 行数 > 0）时 `status := 'indexed'`；
   否则 `status` **保持原值**（解析成功即 `parsed`），并返回 `indexed=false` +
   `searchable=false` + `index_state="no_chunks"` + `index_note`（见 §2 的取值表）。**契约块数
   > 0 不再等于 `indexed`**——块是证据，不是索引。因此只有 `chunks>0` 的上传才会出现在
   `GET /v1/documents`（它按 `status='indexed'` 过滤）与问答文件选择器里。
4. **幂等**：重复 approve 只是重新记录一次操作者与时间，仍返回 200，`needs_review` 保持
   false，不产生第二次状态变更、不新建 job，也不会产生第二份 `chunks`/向量。已 `indexed`
   的资产不会被后续慢 worker 降级（`_finish` 带 `status<>'indexed'` 保护）。若首次审批时索引
   尚未落地而之后补齐，第二次 approve 会如实变为 `indexed=true`（判定是派生的，不是缓存的）。

失败：

| 触发 | HTTP | `error_code` |
| --- | --- | --- |
| 非 admin / 缺 CSRF / 跨源 | 403 | 中间件既有码 |
| `status` 不是 `succeeded`/`partial_failure`（仍在队列或已失败） | 409 | `UPLOAD_NOT_REVIEWABLE`（`details.job_status`） |
| 未知 upload_id | 404 | `UPLOAD_NOT_FOUND` |

---

## 5. 复核门（可见性规则，fail-closed）

`document_assets` 行的可见性门是**两把锁同时打开**：`status='indexed'`
**且** `needs_review=false`，且 `'public'=ANY(acl)`、`collection` 属于
`{demo_kb, ohrbench_kb, industrial_demo}`。**但可见 ≠ 可检索**：`status='indexed'`
只说明它通过了发布门，检索器仍要求 `chunks`（见 §2 的 `searchable` 定义）。

- 未 approve 时：`needs_review=true` 且 `status` ∈ {`parsing`（队列中）, `parsed`
  （解析成功）, `failed`}。因此
  - `GET /v1/documents` **不列出**它；
  - `GET /v1/documents/{id}` 返回 404 `DOCUMENT_NOT_FOUND`；
  - 向量检索（RAG）选不中它（发布门未开；即使解析波次已写入 `chunks` 也选不中）。
- approve 后：`needs_review=false`；`status='indexed'` **仅当** `chunks>0`。解析波次会为能切出
  切片的文档写入 `chunks` 与 Qdrant 向量，所以审批后的正常结果是 **`searchable=true`**、
  出现在 `GET /v1/documents`，并被检索器选中。
- 若建索引失败（`vector_index='failed'`）或无可索引内容（`vector_index='deferred'`），审批后
  仍是 `index_state='no_chunks'`、**不可检索**——失败被如实呈现，不会被复核动作掩盖。
- 无论哪种情况，复核门都先于切片：未审批的文档即使已有 `chunks`，检索器也选不中它。
- **唯一例外**：`GET /v1/documents/{id}/contract` 对 `origin='upload'` 的行在
  approve **之前**也可读（契约是证据浏览，不是检索；上传详情的 `contract_url` 指向
  它）。其它 `/v1/documents*` 路由的门保持不变。
- 守护测试：`tests/test_upload_searchability.py`（①未审批 ②已审批无切片 ③有切片反向对照
  ④`/v1/documents` 每项带 `searchable` ⑤幂等 ⑥失败解析不可审批）。

---

## 6. 异步、独立 worker 与「重试 ≠ 断点恢复」（有界执行）

**job 只在 worker 领取时推进；没有任何 HTTP 路由会推进它。**

1. `POST /v1/uploads` 只做：校验 → 暂存 → 写 `document_assets`（`status='parsing'`,
   `needs_review=true`）→ 写 `ingest_jobs`（`job_type='upload_parse'`, `status='queued'`）。
   **不解析**。
2. `GET /v1/uploads/{upload_id}` 与 `GET /v1/uploads` 都是**纯读**（§2/§3），不领取、不解析。
3. **推进任务的唯一入口**是独立 worker：`python -m ingestion.upload_worker`。
   **没有 worker 时任务会永远停在 `queued`**——这是部署事实，不是解析失败；客户端必须
   依据 `worker.alive` 如实提示「后台处理器未运行」（§2），**不得**显示成「解析中」。

### worker CLI（`ingestion/upload_worker.py`）

```bash
python -m ingestion.upload_worker --watch                  # 常驻；start_local.sh 用这个
python -m ingestion.upload_worker --once                   # 排空后退出（cron / 测试）
python -m ingestion.upload_worker --watch --interval 5 --workers 2 --deadline 60
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--once` | — | 在**一个跨进程解析槽**内跑一次 `drain()`：推进所有可领取任务后退出（退出码 0，即使队列为空）。测试用的确定性路径 |
| `--watch` | — | 常驻循环：每轮 `work_once()` 跑一波有界解析，无可领取任务时睡 `--interval` 秒 |
| `--interval` | `2` | `--watch` 的空闲轮询间隔（秒，≥0.1） |
| `--workers` | `1` | 单次波次**领取**几个任务（1–4）。同一波次内的任务**顺序执行**，所以它不等于并发解析数 |
| `--deadline` | `wave_deadline()` | 单波次硬时限；默认 **45s**，可用 `DATAMIND_UPLOAD_WAVE_DEADLINE_SECONDS` 覆盖（非法值回退默认，**不会**变成无界） |

- **合计并发上限**：每波持有一个跨进程 advisory slot（`datamind.task_budget.resource_slot`，
  槽名 `upload-parse`，槽数 = `DATAMIND_TASK_SLOTS`，默认 2）。因此**多个 worker 进程 / 多个
  `drain()` 调用者的合计并发波次数 ≤ `DATAMIND_TASK_SLOTS`**，不再由 HTTP 请求数决定。
  槽被占满时立即返回 `TASK_BUDGET_BUSY`（不是无界排队）：`--once` 以退出码 **3** 结束、
  什么都不领取；`--watch` 打印一行提示后按 `--interval` 重试。
- **信号**：SIGINT/SIGTERM 只置停止标志，**不打断已领取的解析**（子进程在独立会话中，Ctrl-C
  不会杀掉它）；当前波次结束后退出，退出码 0，并释放心跳租约。`--watch` 在**波次边界**退出；
  `--once` 会把本次 drain 的剩余可领取任务做完再退出（不会丢弃已领取任务，也不会半途改写状态）。
- **退出码**：`0` 正常完成（含空队列与信号停止）｜`1` 异常（含心跳注册失败，拒绝在无心跳下
  启动，否则客户端会误报"处理器未运行"）｜`2` 参数错误｜`3` 解析槽全被占用、本轮未领取。
- **进度**：stdout 每行一条 `[upload_worker] <UTC 时间戳> ...`（心跳、每一波、每个任务的
  `outcome=... job_id=... attempt=... error_code=...`、停止）。
- **心跳**：worker 在既有 `task_leases`（迁移 013）里注册一行
  `upload_worker:<host>:<pid>:<nonce>` 并周期续租（间隔
  `DATAMIND_UPLOAD_WORKER_HEARTBEAT_SECONDS`，默认 5s；租约 TTL = 阈值）。优雅退出时删除该行；
  崩溃时租约到期后 `alive` 自动变为 false。判定阈值见 §2 的 `worker` 字段。

### 波次超时与「重试 ≠ 断点恢复」（审查 5.1 明确区分）

- 一波用尽预算：子进程被杀，job **保持 `running`**（不写成 failed、不假装成功），
  `wave_timeouts` 计数 +1；`stale_running_seconds()`（= max(120s, 波次时限+30s)）之后可被
  重新领取。波次超时**最多 `MAX_WAVE_ATTEMPTS = 3` 次**，第 3 次仍超时即如实失败为
  `UPLOAD_WAVE_ATTEMPTS_EXHAUSTED`。
- **重新领取 = 重试，不是断点恢复**：新代次会**从头解析整个文件**，不复用上一次的任何中间
  产物，也不存在持久化检查点。因此：
  - 大文件要么分段保存进度，要么明确给出"可完成的时间预算与大小上限"；
  - UI/文档**不得**把"重试中"说成"从上次继续"；`attempt` 只表示"被领取过几次"。
  - 与"解析已完成但提交前中断"不同：那种情况事务整体回滚（§7），**同一代次**重跑后结果一致，
    不产生半套结果，也不需要新的代次。

### 并发安全：领取代次（R04）

- `_claim` 用 `SELECT ... FOR UPDATE SKIP LOCKED` 抢占，并在同一事务里写入
  `ingest_job_claims`：`claim_token`（`secrets.token_urlsafe(32)`，不可复用）+ `attempt+1`
  （只增不减）。`running` 行仍带 `started_at`，陈旧行按 `stale_running_seconds()` 可重抢。
- **所有依赖该次领取的写入都校验同一 token**（`SELECT ... FOR UPDATE`）：契约结果、
  `quality_detail`、终态状态、波次超时计数、取消/重抢。校验失败即**完全无副作用**（不改状态、
  不改块/定位/质量明细）。详见 §7。

### 一次波次的耗时上界

- 上界 ≈ `--deadline`（默认 45s）+ 子进程启动/回收开销。历史实测（旧版由 GET 驱动，同一
  波次机制）：DXF/DOCX/PDF/JSON 单波 **0.23–0.57 s** 直接到终态；强制 400 页 PDF 在
  0.5s 预算下超时，调用方 **0.556 s** 拿回 `status='running'`。
- `GET /v1/uploads/{upload_id}` 现在是纯读：响应时间与解析量**无关**（只有数据库读取）。
- PDF 分支在**子进程内**可能触发 OCR 与公式识别（`ingestion/ingest.py: parse_pdf`），它们没有
  各自的子预算，但整段都在波次时限内，超时即整波被杀。
- 一个永久超时上传的最坏总时长（默认参数）≈ `MAX_WAVE_ATTEMPTS × (wave_deadline +
  stale_running)` ≈ 3 × (45s + 120s) ≈ 8 分钟，之后稳定失败为
  `UPLOAD_WAVE_ATTEMPTS_EXHAUSTED`。

### 启动方式（照文档启动一次，任务就会自己推进）

- 前台：`bash scripts/start_local.sh` —— 同一脚本启动 API 与 worker，`Ctrl-C`/退出时一并
  停止 worker（日志 `.cache/api/upload_worker.log`）。
- 后台：`bash scripts/start_local.sh --background` —— `scripts/start_api_background.py` 分离
  启动 API 与 worker（`start_new_session`），PID 记在 `.cache/api/upload_worker.pid`；
  该模式没有停止脚本，停止请 `kill -TERM "$(cat .cache/api/upload_worker.pid)"`。
- `--sql-only`：不启动 worker（该部署没有上传管道）。
- 手动：`.venv/bin/python -m ingestion.upload_worker --watch`。若任务停在 `queued` 且
  `worker.alive=false`，先启动

- 解析事务**提交之后**，`run_parse` 调用 `ingestion/upload_index.py::index_document` 建索引
  （切片 + 向量）。它**不在**解析事务内：Qdrant 无法与 PG 同事务，且嵌入调用不能持有该 job 的
  行锁。
- 建索引发生在**同一波次子进程**内，因此受同一个 `--deadline` 约束；实测嵌入模型加载
  0.34s、吞吐约 784 texts/s，最大的真实样本（1307 块 DXF）约 1.7s，远在 45s 预算内。
- 建索引失败**不改写**解析结果（`status`/`error_code` 保持成功态），只写
  `quality_detail.index` 并在接口上以 `vector_index='failed'` / `index_error_code` 如实呈现；
  检索可用性仍由 §2 派生，继续报 `no_chunks`。
- 不在 API 进程内解析：worker 的每一波默认走子进程（`datamind.task_budget.run_bounded`）；
  `in_process=True` 是**显式**选项（测试与已自带一次性进程的宿主），默认不可达。子进程的
  stdout 只承载那一个 JSON 结果，`index_document` 会把任何打印重定向到 stderr。


---

## 7. 落库与契约（结果与终态同一事务 + 代次校验）

`run_parse` **先解析（事务之外）**，解析结束后在**同一个数据库事务**内按顺序做：

0. **校验领取代次**：`SELECT ... FROM ingest_jobs j JOIN ingest_job_claims c ON
   c.job_id=j.job_id WHERE j.job_id=? AND j.status='running' AND c.claim_token=?
   FOR UPDATE OF j, c`。不匹配 → **不写任何东西**（连终态都不写），返回 `claim_lost`；
   `_execute` 把它记成 `outcome=claim_lost`、`error_code=UPLOAD_CLAIM_LOST`。
   仅"在终态处加判断"是不够的——如果结果先独立提交，旧代次仍会污染块/定位/质量明细
   （审查 R04 明确指出），所以校验必须在**写入之前**、且与该次写入在同一事务内。
1. `ingestion.contract_store.persist_result(cur, document_id=..., payload=..., dsn=...)`
   写入 `document_contract_blocks` / `document_block_relations`（先删后插，幂等替换）。
   只有通过 `datamind/parse_contract.py` 校验的结果才会落库；校验失败即抛稳定错误码，
   不写半份文档。
2. 更新 `document_assets.quality_detail`，形状为
   `{"upload": {...}, "parse_contract": {...}, "analysis": {...}, "review_approval": {...}}`。
   其中 `parse_contract` 是 `/v1/documents/{id}/contract` 会回显的摘要，含
   `unsupported_features`（显式缺口如实声明，如 measurement JSON 的
   `locator_unavailable`）。
3. 写终态：`ingest_jobs.status`（`succeeded`/`partial_failure`/`failed`）+
   `finished_at`，清空 `ingest_job_claims.claim_token`，并按 `ASSET_TERMINAL`
   更新 `document_assets.status`（`indexed` 永不降级）。

因此：

- **要么全落、要么全不落**：事务中途被中断（进程被杀、连接断开）→ 契约行、`quality_detail`
  与终态一起回滚，job 仍是同一代次的 `running`，可被安全重跑；
- 取消（`cancel`）会清空 `claim_token`：正在解析的 worker 之后**无法**写入结果或终态；
- 一个被新代次取代的旧 worker 晚到时，`ingest_jobs` / 契约表 / 定位表 / `quality_detail`
  **逐行不变**（守护测试：`tests/test_upload_claim_generation.py`）。

代次存储：`ingest_job_claims`（迁移 `db/migrations/018_upload_claim_generation.sql`，
`job_id` 主键 + `claim_token` + `attempt` + `claimed_at`，`ON DELETE CASCADE`）。
**为什么不加在 `ingest_jobs` 上**：该表由 `deploy/init/02_metadata.sql` 以 `postgres` 身份创建、
属主是 `postgres`，而 `db/migrate.py` 以 `datamind_app` 连接（只有 DML 权限、没有属主权限），
`ALTER TABLE ingest_jobs ADD COLUMN ...` 会以 `must be owner of table ingest_jobs` 中止整个
迁移流程。迁移 018 因此只新建自己的表（并 REVOKE 给 `readonly_user`）。

`quality_detail` 中**不写任何明文凭据**；上传记录只含 `user_id`/`display_name`/`roles`
与文件身份，不含 session 哈希。

---

## 8. 与既有接口的关系

| 既有接口 | 与本管道的关系 |
| --- | --- |
| `POST /v1/files/uploads` | 旧暂存路径，上限 `.dxf` 2 MiB / `.json` 1 MiB，错误码见 §1 注记。本管道**未改动**它。 |
| `POST /v1/files/jobs` + `scripts/file_job_worker.py` | DXF/measurement 的**登记**队列（`cad_versions` / `dimension_measurement_versions`）。上传队列不写这两张表（原因见 `ingestion/upload_pipeline.py` 模块文档：`file_job_items.kind` 有 CHECK 约束，且两个 target 都不写统一契约与 `document_assets`）。 |
| `GET /v1/documents/{id}/contract` | 上传的契约读取入口；`contract_url` 指向它。 |
| `GET /v1/documents` | 问答文件选择器的数据源；只有 `indexed` 且 `needs_review=false` 才列出，且每项带 `searchable` / `index_state` / `index_note`（与上传端点同一判定函数）。选择器必须要求 `searchable=true`。 |
| `ingestion/upload_worker.py` | 上传队列的**唯一常驻推进者**（`--watch`/`--once`）。与 `scripts/file_job_worker.py` 面向不同队列：后者处理 `file_jobs` 登记队列。 |
| `datamind/task_budget.py` 的 `task_leases` | worker 心跳与跨进程解析槽都复用它（迁移 013），没有新增常驻进程/依赖。 |
| 前端 `web/multifile.js` | **本版未改**：它按 `status` 轮询详情渲染，`queued` 显示为「排队中」，但**没有消费 `worker.alive`**，所以后台无 worker 时会一直轮询到上限并提示「仍在处理中」（`poll_exhausted`）——本版已在**详情/列表/`/health`** 三处提供该字段，UI 只需读取它即可改成「后台处理器未运行，任务不会自行推进」。这是本版**已知的跨任务待办**。 |
