# 前端六视图接口契约与诚实边界（Vue 迁移）

本文件是 `frontend/src/views/` 下**六个治理视图**（模型接入、字段复核、来源权威规则、
图纸与实体、尺寸核验、我的文件）的规范来源：每个页面用哪些接口、每个方法要什么角色、
哪些话在数据面前**不许说**。实现与后端路由对齐时以本文为准。

**这些页面是 Vue 应用里的真实路由**，不再是「未迁移」占位页。侧边栏入口与路由定义见
`frontend/src/router/index.ts`；视图文件名与路由的对应关系见下表。请求内核
（`fetch` / CSRF / 错误信封解析）集中在 `frontend/src/api/client.ts`，本文只描述契约，
不改写该文件。

约定的三层边界：

* **鉴权面**由默认拒绝中间件决定（`datamind/api.py: identity_permissions` 与
  `_required_permission`），不在前端。
* **请求面**由 `client.ts` 统一实现：错误信封、422、CSRF、相对路径同源。
* **诚实面**由各视图自己承担：接口返回什么、以及**它证明不了什么**，必须一起渲染。

---

## 0. 路由 / 视图 / 接口 / 角色总表

`/#/...` 是 hash 路由，构建产物由后端挂在 `127.0.0.1:8000`。`admin` 通过**所有**角色门，
所以「admin」表示该方法的门槛就是超管；「任意已登录」表示 `_required_permission` 返回
`None`，只要求身份、不做角色门。

| 路由 | 视图 | 用到的接口 | 各方法所需角色 | CSRF |
| --- | --- | --- | --- | --- |
| `/#/model` | `ModelPage.vue` | `GET /v1/model/config`；`GET/POST /v1/model/profiles`；`PUT/DELETE /v1/model/profiles/{id}`；`POST /v1/model/profiles/{id}/activate`；`GET /v1/model/profiles/export`；`POST /v1/model/profiles/import`；`POST /v1/model/config/test`；`POST /v1/model/config/models`；`DELETE /v1/model/config`；`GET /v1/auth/me` | **全部 `admin`**（含全部 GET） | 非 GET 必须；GET 还要**同源**（敏感读前缀） |
| `/#/fields` | `FieldsPage.vue` | `GET /v1/documents`；`GET /v1/documents/{id}/facts`；`GET …/facts/{fact_id}/reviews`；`GET …/facts/{fact_id}/approvals`；`POST …/facts/{fact_id}/reviews`；`POST …/facts/{fact_id}/approvals`；页面图 `GET …/pages/{n}`；`GET /v1/auth/me` | GET：**匿名可读**（`/v1/documents` 在公开读前缀里）；`POST …/reviews` → `reviewer`；`POST …/approvals` → `approver` | 非 GET 必须 |
| `/#/authority` | `AuthorityPage.vue` | `GET /v1/authority/candidates`；`GET /v1/authority/rules`；`GET /v1/authority/rules/{id}/events`；`POST /v1/authority/rules`；`POST /v1/authority/rules/{id}/events`；`GET /v1/auth/me` | candidates GET → `admin`；rules / events 的 GET → 任意已登录；登记 `POST /rules` → `admin`；`POST /rules/{id}/events` → `approver` | 非 GET 必须 |
| `/#/cad` | `CadPage.vue` | `GET /v1/drawings`；`GET /v1/drawings/sources`；`GET /v1/drawings/{id}`；`GET …/versions`；`GET …/entities`；`GET …/features`；`GET …/features/{fid}/reviews`；`GET …/file`；`GET …/preview`；`GET …/viewer`；`POST /v1/drawings`；`POST …/features/{fid}/reviews`；`GET /v1/auth/me` | 明细类 GET → 任意已登录；`GET /v1/drawings/sources` → `admin`；登记 `POST /v1/drawings` → `admin`；`POST …/reviews` → `reviewer` | 非 GET 必须 |
| `/#/dimensions` | `DimensionsPage.vue` | `GET /v1/dimensions/sources`；`GET /v1/dimensions/measurements`；`GET …/measurements/{id}`；`GET /v1/dimensions/bindings`；`GET …/bindings/{id}`；`GET …/bindings/{id}/comparisons`；`GET /v1/dimensions/comparisons/{id}`；`POST /v1/dimensions/measurements`；`POST /v1/dimensions/bindings`；`POST …/bindings/{id}/approvals`；`POST …/bindings/{id}/comparisons`；`GET /v1/auth/me` | `GET /v1/dimensions/sources` → `admin`；明细类 GET → 任意已登录；measurements / bindings / comparisons 的 POST → `admin`（对照按 `admin` 处理，见 §6）；`POST …/approvals` → `approver` | 非 GET 必须 |
| `/#/files` | `FilesPage.vue` | `POST /v1/uploads?filename=&provenance=`；`GET /v1/uploads`；`GET /v1/uploads/{id}`；`POST /v1/uploads/{id}/approve`；`GET /v1/auth/me` | **全部 `admin`** | 非 GET 必须 |

> `GET /v1/auth/me` 每个状态化视图都会先读一次：既拿到当前身份（用于把 403 解释成
> 「权限不足」而不是「加载失败」），也顺带拿到 CSRF 令牌（`client.ts` 的 `csrf()` 缓存）。

---

## 1. 六视图共享规则

### 1.1 默认拒绝（default-deny）

只有下列路由可以匿名访问（`datamind/api.py` 的 `PUBLIC_PATHS` /
`PUBLIC_READ_PREFIXES` / `PUBLIC_WRITE_PATHS`）：

* 匿名路径：`/`、`/health`、`/v1/auth/login`、`/v1/auth/me`；
* 匿名只读前缀（**仅 GET/HEAD/OPTIONS**）：`/v1/documents`、`/v1/runs`、`/assets/`；
* 匿名写路径只有 `/v1/chat`、`/v1/chat/stream`，且必须先设 `DATAMIND_PUBLIC_CHAT=1`。

**其余一切路由都必须有身份。** 六视图里只有 `GET /v1/documents*` 落在匿名只读前缀内；
`/v1/model/*`、`/v1/authority/*`、`/v1/drawings*`、`/v1/dimensions/*`、`/v1/uploads*`
一律默认拒绝。

* 未登录 → `401 {"detail":{"error_code":"LOGIN_REQUIRED"}}`；会话失效 → `401 SESSION_INVALID`。
* 已登录但角色不够 → `403 {"detail":{"error_code":"PERMISSION_DENIED"}}`。
* 角色词表恰为 `viewer | reviewer | approver | admin`，且 **admin 通过所有角色门**。
  所以「任意已登录」不等于「viewer 专属」，也不要把它写成「viewer 才能访问」。

### 1.2 CSRF：令牌来自 `GET /v1/auth/me`

* 每一次**非 GET**（POST / PUT / PATCH / DELETE）且路由不在公开清单里，都必须带请求头
  `X-CSRF-Token`，值取自 `GET /v1/auth/me` 响应里的 `csrf_token`。
  `client.ts` 的 `csrf()` 会缓存它，写内核 `write()` 自动附带。
* 缺头 → `403 CSRF_REQUIRED`；跨源 → `403 ORIGIN_NOT_ALLOWED`。**同源是硬要求。**
* `/v1/model/*` 的 **GET 也要同源校验**（敏感读前缀），但不需要 CSRF。因此模型页的读取
  也必须走相对路径或同源代理——直接用 `http://127.0.0.1:8000/...` 绝对地址会被
  `ORIGIN_NOT_ALLOWED` 拒绝。
* 一个容易被忽略的后果：写请求拿不到身份时 `csrf()` 会先失败（`auth/me` 401），
  视图应把它显示成「需要登录」，而不是「网络错误」。

### 1.3 错误信封恒为 `{"detail":{"error_code":"…"}}`

* 业务错误一律是 `{"detail":{"error_code":"CODE"}}`，有时再加 `details` 子对象。
  解析入口是 `client.ts` 的 `apiError()`，它把 `error_code` 挂到 `Error.errorCode`。
* **绝不允许**把 `{ok:false,…}` 当作通用错误形状：那只是 `/v1/model/config/test`、
  `/v1/model/config/models` 这类**探针**接口的响应体（见 §2）。
* 视图必须把 `error_code` 本身显示出来，不要让用户只看到「请求失败」。

### 1.4 FastAPI 自己的 422 **不带** `error_code`

* pydantic 请求体校验、`Query(...)` 边界失败时，FastAPI 返回
  `422 {"detail":[{loc,msg,…}]}`——`detail` 是**数组**，没有 `error_code`。
* `apiError()` 显式识别这一形状并归一成 `VALIDATION_ERROR`，把 `loc.msg` 拼成人能读的
  一行。视图遇到 `VALIDATION_ERROR` 时要说明「参数未通过框架校验」，而不是编一个业务码。
* 与之相对，业务 422（如 `CAD_TITLE_REQUIRED`、`AUTHORITY_VALIDITY_INVALID`）**带**
  `error_code`。两者同名 422、含义不同，不能互相冒充。

### 1.5 `Cache-Control: no-store`

服务端对 `_NO_STORE_PREFIXES` 下的路径恒定下发 `Cache-Control: no-store`。与六视图相关的
是 `/v1/model/profiles`、`/v1/model/config`、`/v1/drawings`、`/v1/dimensions`、`/v1/uploads`，
以及根入口 `/`（`index.html` 是指向内容哈希产物的指针，缓存旧 HTML 会白屏）。

**诚实提醒**：`/v1/authority/*` 与 `/v1/documents*` **不在**该清单里。所以：

* 字段复核读的是公开文档数据，可被浏览器按常规规则缓存，页面不能宣称「服务端保证不缓存」；
* 权威规则列表/事件没有 `no-store` 头，刷新后**必须重新拉取**再判断 `payload_valid`，
  不能拿上一次渲染的结论当作当前注册表状态。

### 1.6 前端共同实现约束（本仓库反复踩过的坑）

* `style scoped` + `var(--color-*)` 设计变量；**每个视图给类名加前缀**（如 `.mpe-`、`.cad-`、
  `.dim-`、`.files-`），避免 Tailwind v4 `@layer utilities` 重名（历史事故：`.collapse`、
  `.result-caption` / `table-caption`）。
* 测试或人需要找到的元素都加 `data-testid`。
* 只用**相对 URL**（同源）；模型读取绝不使用绝对地址，否则触发 `ORIGIN_NOT_ALLOWED`。
* 状态化页面沿用 `RunsPage.vue` 的外壳（`SideNav` + `.center`、顶栏、加载/错误守卫、
  加载更多），会话式状态参考 `composables/useSessions.ts`。

---

## 2. 模型接入（`/#/model`，`ModelPage.vue`）

三件**互不蕴含**的事实分开渲染，谁也不许冒充谁：

1. **配置存在** ← `GET /v1/model/config`、`GET /v1/model/profiles`；
2. **连通性** ← `POST /v1/model/config/test`（按需发起，是唯一能证明「模型能应答」的证据）；
3. **上游模型名** ← `POST /v1/model/config/models`（只说明该密钥能看到哪些模型名）。

权限特别说明：`/v1/model/*` **全部**路由（**包括 GET**）都要求 `admin`。所以
`403 PERMISSION_DENIED` 是**正常结果**，必须渲染成权限说明，不能显示成「加载失败」或
「没有配置」。

| 方法 | 路径 | 角色 | CSRF |
| --- | --- | --- | --- |
| GET | `/v1/model/config`、`/v1/model/profiles`、`/v1/model/profiles/export` | `admin` | —（但要同源） |
| POST | `/v1/model/profiles`、`/v1/model/profiles/import`、`/v1/model/profiles/{id}/activate`、`/v1/model/config/test`、`/v1/model/config/models` | `admin` | 必须 |
| PUT | `/v1/model/profiles/{id}` | `admin` | 必须 |
| DELETE | `/v1/model/profiles/{id}`、`/v1/model/config` | `admin` | 必须 |

关键返回形状：

* `POST /v1/model/config/test`、`POST /v1/model/config/models` 是**探针**：响应体形如
  `{ok:true/false, …}`，**不是**错误信封；只有出错时才落到 §1.3 的 `detail.error_code`。
* `DELETE /v1/model/config` 返回**窄结构** `{"has_key","key_masked","source"}`（3 个键），
  刻意不复用 21 个键的配置视图。只能当「已清空」读，不能再从它读端点/模型。
* 密钥清空哨兵有两条不同通道：
  * `api_key` 传 `"clear"` / `"none"` / `"-"`（大小写不敏感）才是清空；
  * `__clear__`（`settings_store.CLEAR_SENTINEL`）**只**对**敏感请求头**与**代理**有效。
* 跨源探针：如果 `base_url` 指向与已存配置不同的主机、又不自带密钥，后端返回
  `400 MODEL_KEY_NOT_INHERITED`——这是刻意的「不给已存密钥外泄」，不是配置损坏。

### 本页拒绝夸大的地方

* **`has_key` 不是可用性。** `has_key=true` 只等于「密钥字段填过值」，它不证明密钥有效、
  更不证明模型能应答。这三件事必须在界面上分开，任何一处把 `has_key` 渲染成「可用/已就绪」
  都是过度声称。
* **`key_masked` 绝不回传。** 掩码只准进 `placeholder`。后端**没有**任何「这是掩码」的
  服务端防呆：把 `key_masked` 当 `api_key` 提交，就会用掩码字面量**永久覆盖**真实密钥。
  本页因此不提供任何「回填/复制密钥」动作。
* **`__clear__` 不是 `api_key` 的哨兵。** 把它填进 `api_key` 会被当作普通值存成字面量
  `"__clear__"`；它只用于删除敏感请求头与代理。留空 = **保持不变**，不是清空。
* **`DELETE /v1/model/config` 的返回体是窄结构。** 只有 `has_key` / `key_masked` / `source`
  三个键；不能拿它当完整配置视图继续渲染端点或模型名。
* **每个 `/v1/model/*` 路由都要求 `admin`，读取也不例外。** 403 不是异常，是权限事实；
  非管理员看到的是「权限不足」，而不是「未配置」。
* **跨源探针的 400 是保护，不是故障。** `MODEL_KEY_NOT_INHERITED` 说明服务端拒绝把已存
  密钥带到另一个主机，页面要如实说明，不能提示「密钥错误」。

---

## 3. 字段复核（`/#/fields`，`FieldsPage.vue`）

数据链：文档列表 → 文档事实 → 单条事实的抽取复核链 / 审批链。读取走公开读前缀
（匿名可读），写入必须登录并满足角色。

| 方法 | 路径 | 角色 | CSRF |
| --- | --- | --- | --- |
| GET | `/v1/documents`、`/v1/documents/{id}/facts`、`…/reviews`、`…/approvals`、`…/pages/{n}` | 匿名可读（公开读前缀） | — |
| POST | `/v1/documents/{id}/facts/{fact_id}/reviews` | `reviewer` | 必须 |
| POST | `/v1/documents/{id}/facts/{fact_id}/approvals` | `approver` | 必须 |

关键字段语义：

* 列表用 `doc_version` / `sha256`，事实接口用 `document_version` / `source_sha256`，
  这是同一行的**同一个值**，命名陷阱，不要当成两份数据。
* 列表附带 `searchable` / `index_state` / `index_note` 三个派生字段：**收录 ≠ 可检索**。
* 审批必须绑定一条 `decision='confirmed'` 的人工复核，且复核人 ≠ 审批人。

### 本页拒绝夸大的地方

* **文档列表不按 `searchable` 过滤。** 工业演示文档有 **0 个 chunk**（`searchable=false`）
  却承载 **17 条事实**；如果按 `searchable` 过滤，用户会看不到这些事实，等于用「检索得到」
  冒充「事实存在」。`searchable` / `index_state` / `index_note` 只作为**信息**显示，并配一句
  说明：审批 ≠ 可检索。
* **旋转或坐标系不受支持的定位器只出标签，绝不按猜测位置绘制。** 本屏只接受
  `coordinate_system === 'pdf_points'` 且 bbox / page_size / page_no 全部合法的引用；
  其余（旋转页、别的坐标系、缺页码、0 面积框）一律渲染成「无法绘制：<原因>」标签。
  画错一个框比不画更糟——用户会把猜出来的区域当成证据来源。
* **`reviewer_user_id` 为 null 的复核是「历史未认证」，永远不能获批。** 页面对这种记录
  只给说明，不给审批动作；后端也会以
  `CONFIRMED_AUTHENTICATED_REVIEW_REQUIRED` 拒绝。
* **`formal_approval_status='stale'` 表示审批已失效。** 审批有效的前提是它绑定的那条复核
  仍是当前链尾；复核链一旦变化，旧审批作废。stale / rejected 必须用警示色，不能和
  「已批准」长得一样。

---

## 4. 来源权威规则（`/#/authority`，`AuthorityPage.vue`）

这一页只为**一个已存在的数值冲突**登记「以哪一侧为准」，再由**另一个**账户启用。
来源名字（「质检报告」）不构成任何权威，只有显式登记 + 独立启用才算数。

| 方法 | 路径 | 角色 | CSRF |
| --- | --- | --- | --- |
| GET | `/v1/authority/candidates` | `admin` | — |
| GET | `/v1/authority/rules`、`/v1/authority/rules/{id}/events` | 任意已登录 | — |
| POST | `/v1/authority/rules` | `admin` | 必须 |
| POST | `/v1/authority/rules/{id}/events` | `approver` | 必须 |

关键语义：

* `payload_valid`：登记载荷与 `payload_sha256`（或契约字段集合）是否对得上。
* `eligible`：报告侧事实是否已拿到「已认证的确认复核 + 独立审批」，决定能否登记。
* 有效期是**半开区间**：`valid_from ≤ produced_at < valid_to`，且时间串**必须带显式时区
  偏移**（如 `+08:00`）；naive 串（`2026-10-01T08:00`）被后端 `422 AUTHORITY_VALIDITY_INVALID`。
  时区是规则语义的一部分，前端不许替用户悄悄补。

### 本页拒绝夸大的地方

* **`payload_valid=false` 只渲染「登记完整性校验失败」并拒绝一切审批动作。** 那一行不展示
  载荷细节、不给启用按钮；`payload_valid` 缺失时同样「未取得结论」，不得当作有效依据。
* **`eligible=false` 直接封住登记动作。** 它表示报告侧事实尚未独立审批；此时不能登记规则，
  页面要给出门槛说明，不能靠禁用按钮「静默失败」。
* **有效期是半开的，且必须带时区。** 起点包含、终点不包含；没有时区的时间不是「差一点」，
  而是会被服务端 422 拒绝。页面显示的必须是**原样时间串**，不做本地化改写。
* **两条适用规则同时命中时失败即关闭（fail closed），没有优先级。** 运行时返回
  `AUTHORITY_RULE_OVERLAP`；本页不得暗示「有冲突会按先后/具体度选一条」。
* **注册表里任何一条 `payload_valid=false`，整体裁决就失败即关闭。** 运行时返回
  `AUTHORITY_REGISTRY_INVALID`；一条坏规则会拖住全部，而不是被跳过。页面必须在
  注册表级给出这条警示。
* **登记 ≠ 生效 ≠ 当前适用。** 规则要另一账户启用，运行时还要再核验版本、审批、依据与重叠。

---

## 5. 图纸与实体（`/#/cad`，`CadPage.vue`）

两类数据必须分开显示，不能合并成一句「已识别」：

* `features` = **冻结的机器抽取**（原始抽取 / 对象一致性）；
* `review_gate` 与 `association_reviews` = **人的关联决定**（哪条标注对应哪个对象）。

| 方法 | 路径 | 角色 | CSRF |
| --- | --- | --- | --- |
| GET | `/v1/drawings`、`/{id}`、`/{id}/versions`、`/{id}/entities`、`/{id}/features`、`…/features/{fid}/reviews`、`/{id}/file`、`/{id}/preview`、`/{id}/viewer` | 任意已登录 | — |
| GET | `/v1/drawings/sources` | `admin` | — |
| POST | `/v1/drawings`（登记版本） | `admin` | 必须 |
| POST | `/v1/drawings/{id}/features/{fid}/reviews` | `reviewer` | 必须 |

`association_reviews` 里的 `latest_review` 为 `null`，代表该要素**从未**被人工确认过。
`confirmed` 决策必须带属于 `target_candidates` 的 `selected_target`。

### 本页拒绝夸大的地方

* **`features` 是冻结的机器抽取，复核永不改写它。** 人工决定落在 `review_gate` 与
  `association_reviews`；页面不能因为「有人复核过」就声称 `features` 被修正了。
* **登记不是批准。** `POST /v1/drawings` 只是版本登记，返回体语义明确写着
  `not formal approval or production binding`；页面不得把它渲染成「已批准/可生产」。
* **viewer iframe 保持 `sandbox allow-scripts`，不含 `allow-same-origin`。** 这是服务端
  CSP 的刻意隔离，客户端**不许补** `allow-same-origin`；补上等于把图纸脚本放回同源上下文。
* **本读取器要求源文件带 handle，缺 handle 的图纸会 422。** 位置式（positional）身份与
  由此产生的 `partial` 结果属于**上传页**的处理范围，本页不猜测对象身份；`CAD_TITLE_REQUIRED`
  这类业务 422 与 §1.4 的框架 422 要分别显示。
* **关联复核不构成正式批准或生产绑定。** `review_gate` 只说明人已确认「标注 ↔ 对象」的
  对应关系，不是图纸的正式审批结论。

---

## 6. 尺寸核验（`/#/dimensions`，`DimensionsPage.vue`）

记录链四步、顺序不能糊在一起：**注册**（append-only 版本链，admin）→ **绑定**（admin，
必须引用一条已 `confirmed` 的人工关联复核）→ **独立使用审批**（approver）→ **对照**
（落快照，之后可回放）。

| 方法 | 路径 | 角色 | CSRF |
| --- | --- | --- | --- |
| GET | `/v1/dimensions/sources` | `admin` | — |
| GET | `/v1/dimensions/measurements`、`…/measurements/{id}`、`/v1/dimensions/bindings`、`…/bindings/{id}`、`…/bindings/{id}/comparisons`、`/v1/dimensions/comparisons/{id}` | 任意已登录 | — |
| POST | `/v1/dimensions/measurements`、`/v1/dimensions/bindings`、`…/bindings/{id}/comparisons` | `admin` | 必须 |
| POST | `/v1/dimensions/bindings/{id}/approvals` | `approver` | 必须 |

关键语义：

* 绑定必须先有 `confirmed` 关联复核，否则 `409 DIMENSION_CONFIRMED_ASSOCIATION_REQUIRED`；
  上游版本变了是 `DIMENSION_INPUT_VERSION_CHANGED`。
* `POST …/bindings` 返回体**只有**新登记的绑定，不含审批链与当前结论——那两样要再 GET 详情。
* 列表路由**没有** `version` 查询参数（只有单条详情有）；返回体的 `truncated` /
  `current_version` 才是唯一可信信息，`version` 不能当已生效的过滤条件。
* 对照按 `admin` 处理（`POST …/comparisons`；见上表）。

### 本页拒绝夸大的地方

* **两个都叫 `authentication` 的事实不能合并。**
  * 结果**自身**的 `authentication_verified=false`：这份输入是**合成的、作者自述的**，
    不是质量结论；它说的是「这份数据能不能当依据」；
  * 信封的 `authentication.verified=true`：**本地服务**确实用了已认证的记录。
  两者是不同层面的两件事，同屏出现时必须分别标注，绝不能用外层的 `true` 去掩盖内层的
  `false`，也不能反过来。
* **登记人与复核人永远不能审批。** 审批人必须独立于登记人与该复核的复核人；页面在提交前
  就要说明这道独立性要求。
* **撤销（revoke）需要链尾存在生效中的审批。** 链尾结论不是 `approved` 时撤销是非法请求，
  后端 `409`；页面不能用「撤销」按钮假装随时可撤回。
* **回放的对照是当时落库的快照，逐字节相同，绝不在前端或服务端重算。** `replay=true`
  表示返回的是已存记录；「历史仍可复现」靠的就是这份快照，页面不得把回放描述成新一次计算。
* **对照结果不是制造授权，也不是产品质量判定。** 它是限定规则下的单条比较。

---

## 7. 我的文件（`/#/files`，`FilesPage.vue`）

唯一上传入口是 `POST /v1/uploads?filename=&provenance=`，请求体是**原始字节**
（`Content-Type: application/octet-stream`），**不是** multipart。所有 `/v1/uploads*`
路由都要求 `admin`。

| 方法 | 路径 | 角色 | CSRF |
| --- | --- | --- | --- |
| POST | `/v1/uploads?filename=&provenance=` | `admin` | 必须 |
| GET | `/v1/uploads`、`/v1/uploads/{id}` | `admin` | — |
| POST | `/v1/uploads/{id}/approve` | `admin` | 必须 |

本页**不使用**退役的 `/v1/files/uploads` + `/v1/files/jobs` 两步家族。

### 本页拒绝夸大的地方

* **只用 `/v1/uploads`，上传就是原始字节。** 不做 multipart；把请求包装成表单会直接失败。
  退役的 file_jobs 契约不接。
* **审批是检索的前提，但不是充分条件。** `approve` 只解开复核闸门；能否检索还取决于解析
  波次是否成功写入 `chunks`。正常路径下审批后 `searchable=true`、文件进入问答文件选择器；
  建索引失败时 `index_error_code` 非空、`index_state='no_chunks'`，页面必须如实显示，不能在
  索引未建立时说「已可检索」。
* **索引状态如实呈现。** `vector_index` 取值 `embedded` / `failed` / `deferred`；页面
  不得在 `failed` 或 `deferred` 时暗示已建立向量索引或语义检索。
* **`worker.alive=false` 时排队中的任务不会自己前进。** 解析只由独立 worker 进程推进，
  没有任何 HTTP 路由会推进它；此时 `queued` 必须说「后台处理器未运行」，不能渲染成「解析中」。
* **列表数量就是本页大小（page size），不是总数。** `GET /v1/uploads` 不返回 total；
  页面不能把当前页条数说成「共 N 条」。
* **重试会从头重新解析；而本页根本无法重试。** 服务端没有重试路由，这个 UI 也没有任何
  重试入口——所以要如实说明「重试即全量重解析」以及「此处不提供重试」，不能放一个假的
  重试按钮。

---

## 8. 如何验证

### 8.1 类型检查与构建（在 `frontend/` 下执行）

```bash
cd frontend
npx tsc --noEmit          # 类型检查，不产出文件
npm run deploy            # vite build --outDir ../web/vue --emptyOutDir，产出后端使用的页面
```

`tsc` 必须零错误；`npm run deploy` 成功后由后端从根路径提供页面（产物在 `web/vue/`）。

### 8.2 后端契约测试（在仓库根目录执行）

`scripts/test_isolated.py` 用 `--pattern`（`unittest discover` 的 glob）选择用例，例如：

```bash
# 前端服务/卫生相关
.venv/bin/python scripts/test_isolated.py --pattern 'test_*frontend*.py'

# 六视图对应的后端契约（按需各跑一条）
.venv/bin/python scripts/test_isolated.py --pattern 'test_model_config_api.py'
.venv/bin/python scripts/test_isolated.py --pattern 'test_document_fields.py'
.venv/bin/python scripts/test_isolated.py --pattern 'test_source_authority.py'
.venv/bin/python scripts/test_isolated.py --pattern 'test_cad_reviews.py'
.venv/bin/python scripts/test_isolated.py --pattern 'test_cad_dimensions.py'
.venv/bin/python scripts/test_isolated.py --pattern 'test_upload_pipeline.py'
```

注意：`scripts/test_isolated.py` 每次运行都会覆写 `reports/latest_regression.json`，
带 `--pattern` 的**专项**运行不能当作全量回归结果；发布成绩必须用最后一次无参数全量运行
的记录。

### 8.3 打开六个路由

启动服务后（`bash scripts/start_local.sh --background`），在浏览器中逐页核对：

```
http://127.0.0.1:8000/#/model
http://127.0.0.1:8000/#/fields
http://127.0.0.1:8000/#/authority
http://127.0.0.1:8000/#/cad
http://127.0.0.1:8000/#/dimensions
http://127.0.0.1:8000/#/files
```

验收时至少确认两件事：**每个页面在 403 / 401 / 422（含无 `error_code` 的框架 422）下都给出
可读解释**，以及**每页 §「本页拒绝夸大的地方」的每一条都能在界面上找到对应的限定语**。
