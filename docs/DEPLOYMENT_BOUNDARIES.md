# 部署边界与不安全配置

**前端版本补充（2026-10-10）**：旧版前端已退役删除，本文页面提示与文件选择器描述属于旧版实现；后端权限、可检索门禁和 worker 接口保留，新版相应入口待接入，见 [前端评估](FRONTEND_NEXT_STEPS_20261010.md)。

日期：2026-10-09。本文件只记录**部署时必须知道的事实**：哪些默认值是安全的、哪些配置会**静默**削弱本仓库的安全前提。每条都在当前提交上实测过，未经验证的说法不写。

> 定位：本仓库是**本机单用户演示**（限定场景的可信问数 + 文档证据检索 + 文件解析/复核）。它**不是**多租户系统，也没有经过安全加固的外部部署验收。

## 1. `DATAMIND_LOCAL_NO_LOGIN`：三态，其中一态是全局旁路

`datamind/auth.py::demo_allowed()` 的实际行为（实测）：

| 取值 | 效果 | 适用 |
|---|---|---|
| **未设置**（默认） | **仅回环**对端（`127.0.0.1`/`::1`/`localhost`）自动以 `local-admin`(admin) 进入 | 本机演示 |
| `0` / `false` / `no` / `off` / 空串 | **不旁路**：所有客户端都要登录（会话/角色/CSRF 照常） | 需要区分身份时（如"另一账户审批"） |
| **任何其它值**（如 `1`） | **对所有对端**（含非回环）返回 demo admin | ⚠️ **仅供隔离测试；生产/联网环境绝不要设** |

**风险**：第三种取值是一条**全局认证旁路**——任何能连到 8000 端口的客户端都会成为管理员。它不会打印任何警告。

**怎么验证（实测过的确切行为，注意别用错端点）**：

| 端点 | 回环 | 非回环（默认） | 非回环（`=1`） |
|---|---|---|---|
| `GET /v1/auth/me`（**公开路径**） | 200 + demo admin + csrf | **200 但 `{"user":null,"csrf_token":null}`** | **200 + demo admin + csrf** |
| `POST /v1/chat`（变更型） | 403 `CSRF_REQUIRED`（缺令牌，符合设计） | **401 `LOGIN_REQUIRED`** | **200**（旁路成立） |

`/v1/auth/me` 是公开路径，所以它对非回环也返回 200——那只是"没有身份"的诚实视图，**不代表被放行**；判断是否被旁路要看**变更型路由**。伪装来源头（`X-Forwarded-For`）不改变上述任一结果，因为 `socket_host()` 只读 socket 对端。

## 2. `--no-proxy-headers`：必须由启动脚本传入

两个启动入口都已带上它（`scripts/start_local.sh:119`、`scripts/start_api_background.py:68`）。

**原因**：uvicorn 默认信任来自回环对端的 `X-Forwarded-For` 等转发头。若不关闭，`scope["client"]` 会被请求头改写，于是"是否来自本机"这个判断可被伪装——第 1 节的安全前提随之失效。

**风险**：**手工**启动 `uvicorn datamind.api:app` 而漏掉该参数时，代码侧无法自证（它只能读 uvicorn 给出的对端地址）。请始终用仓库脚本启动。

## 3. 监听范围：只绑回环

- 应用：`--host 127.0.0.1`（两个脚本都写死）。
- 容器：`datamind-postgres` 与 `datamind-qdrant` 只发布到 `127.0.0.1`（实测 `docker ps` 端口列）。

**风险**：把任一端口改成 `0.0.0.0` 会让第 1、2 节的前提全部失效（未认证的数据库/向量库暴露）。

## 4. 数据库角色：`readonly_user` 读不到敏感表

实测 `has_table_privilege`：

| 表 | `readonly_user` 可读 |
|---|---|
| `auth_users` | **否** |
| `auth_sessions` | **否** |
| `app_settings`（模型密钥所在） | **否** |
| `ingest_job_claims`（迁移 018） | **否** |

这正是某些读取路径仍走写 DSN 的原因（例如 `/v1/auth/me` 需要读会话表）：**只读角色确实没有权限**，不是没做。相关取舍记录在审查 F06 的处置里（保留原状并说明理由）。

## 5. 迁移：`db/migrate.py` 改不了 init 建的表

`db/migrate.py` 以 `datamind_app` 连接，而 `ingest_jobs`、`document_assets`、`chunks`、`runs`、`document_blocks`、`evaluation_cases` 等表由 `deploy/init/*.sql` **以 `postgres` 属主**创建。

**实测后果**：`ALTER TABLE ingest_jobs ADD COLUMN ...` 会以 `must be owner of table ingest_jobs` **中止整个迁移流程**（不是跳过这一条）。

**因此**：给这些表加列的迁移**不能**放在 `db/migrations/` 里直接 `ALTER`。可选做法：① 改 `deploy/init/*.sql`（只对新库生效）+ 另配存量库升级步骤；② 新建应用自己的表（迁移 018 的 `ingest_job_claims` 就是这么做的）。

## 6. 上传任务需要独立 worker 才会推进

`GET /v1/uploads/{upload_id}` 是**纯读**：它不再执行解析。任务由 `python -m ingestion.upload_worker`（`--once` / `--watch`）推进，两个启动脚本会自动拉起它。

- `worker.alive`（列表/详情/`/health` 同源）由心跳租约判定，阈值 30 秒（`DATAMIND_UPLOAD_WORKER_ALIVE_SECONDS` 可配）。
- **无 worker 时任务会停在 `queued`**；旧版前端曾据此提示"后台处理器未运行：任务不会自行推进"。新版文件管理入口尚未接入，目前应从上述 API 检查 worker 状态。
- 优雅退出会释放心跳；崩溃后租约自然过期。

## 7. 上传的文档**复核通过后可问答**，但索引失败时不可问答

解析波次会在契约写入提交后为文档建立切片与向量（`ingestion/upload_index.py`），资产仍保持
未复核态，因此**复核门先于检索**：

- 复核通过**不等于**可问答；判据是 `searchable == (status=='indexed' AND NOT needs_review AND chunks>0)`。
- 正常路径：上传 → 解析并建索引（`vector_index: "embedded"`）→ approve → `searchable=true`，
  文档出现在 `GET /v1/documents` 与问答文件选择器里。
- 建索引失败（`vector_index: "failed"`、`index_error_code` 非空）或无可索引内容
  （`"deferred"`）时：approve 仍会成功，但 `index_state='no_chunks'`、`searchable=false`；
  该文档不出现在 `GET /v1/documents`（按 `status='indexed'` 过滤）。失败原因在
  `/v1/uploads/{id}` 的 `index_error_code` 中如实报告，不会被复核动作掩盖。
- 若把不可检索的文档作为选中文档提交，**服务端会拒绝**（`DOCUMENT_SELECTION_NOT_SEARCHABLE`），
  而不是返回一个没有证据的答案。

## 8. 适用范围

- 单用户本机演示：无多租户隔离、无外部实名核验、无 SSO/MFA。
- 审批与权威规则是**本地登记与审批机制**，不自动证明业务或法律权威。
- 数据库所有者仍可管理数据，**不是防篡改存储**。
- 公式/OCR/V-CAR 的范围与失败边界见各自文档；组件回归通过率**不代表**模型准确率或工业泛化能力。
