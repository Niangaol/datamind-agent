# WP9 演示脚本：新文件问答闭环 + 多格式样本集

**前端版本补充（2026-10-10）**：下文界面主线是旧版前端的历史演示脚本；旧版已退役删除，不能按其旧深链或管理菜单操作新版。后端接口与样本集保留，当前页面范围及迁移计划见 [前端评估](FRONTEND_NEXT_STEPS_20261010.md)。

> 用途：演示主线与现场排障。所有命令都在仓库根目录执行：
> `cd "/Users/caicai/DataMind Agent"`，Python 一律用 `.venv/bin/python`。
> 本文只描述**当前版本真实能做与不能做**的事；不能做的部分在 §5 明确标注，演示时不得含糊带过。

---

## 0. 演示前 60 秒自检

| # | 命令 | 期望结果 | 失败时 |
|---|---|---|---|
| 1 | `curl -s http://127.0.0.1:8000/health` | `"status":"ok"`，`"document_count":27`，`"vector_store":"ready"` | 服务未起：不要现场重启容器，切到离线材料（§4） |
| 2 | `curl -s http://127.0.0.1:8000/v1/uploads \| head -c 200` | 含 `"worker":{"alive":true,...}` | `alive:false` ⇒ 解析不会被推进，见 §5 的「后台处理器未运行」 |
| 3 | `.venv/bin/python data/qa_samples/build_samples.py --check` | `{"ok": true, "problems": []}` | 样本被改动 ⇒ 用 §5 的 frozen 恢复口径说明，不要现场改文件 |
| 4 | `git rev-parse --short HEAD`（只读） | 与结论卡里记录的基线一致 | 版本不一致 ⇒ 一切数字以现场版本为准，重新跑 §3 |

---

## 1. 界面主线（今天就能演示的部分）

打开 <http://127.0.0.1:8000/>，左侧导航共 7 个视图；本主线的可用路径如下。

**1.1 「问数与问答」— 文档问答 + 引用定位（推荐开场）**

1. 在输入框问：`一般商品退款多久到账？`
2. 期望：答案 `一般商品退款在验收后 3 个工作日内原路退回。 [1]`，右侧出现引用卡片。
3. 点引用卡片里的 **原文件** / **页面** 链接 ⇒ 打开 `/v1/documents/{id}/file` 或 `/pages/{page}`；
   卡片上的定位框由 `citationLocators()` 依据 `pdf_region`（`page_no` + `bbox`）绘制。
4. 这一步证明的是「**已入库且有切片**的文档」链路可用（`eligible_chunks: 169`）。
5. 失败点：卡片没有定位框 ⇒ 该文档没有契约定位（历史解析），换成知识库里的原生 PDF 再试。

**1.2 「问数与问答」— 单表 NL2SQL**

1. 问：`有多少个客户注册了大区信息？`
2. 期望：答案 + `sql_result(sql/columns/rows)` 折叠区；「运行记录」里能看到事件流。

**1.3 「运行记录」**

1. 点任意一次运行 ⇒ 事件流（`run.started` → `query.parsed` → `retrieval` → `verification.passed` → `answer.completed`）。
2. 期望：`GET /v1/runs/{id}/evidence` 能列出 Document/Chunk 证据节点。
3. 这是 E3/F3「可解释」的现场取证入口。

**1.4 「字段复核 / 来源权威规则 / 图纸与实体 / 尺寸核验 / 我的文件」**

按需展示；与 WP9 主线无强依赖。**注意**：这 7 个视图里**没有上传/审批入口**，新文件闭环目前只能走 API（§2）。

---

## 2. 新文件闭环（API 演示，一步一步）

以下命令按顺序执行即可复现 `reports/qa_samples_v1.json` 的 `http` 段。

**2.1 取得本机合法管理身份（不绕过认证）**

```bash
curl -s http://127.0.0.1:8000/v1/auth/me
# 期望：{"user":{"user_id":"user-local-admin","username":"local-admin","roles":["admin"],...},
#         "csrf_token":"..."}
```

说明：`datamind/auth.py` 对 loopback 客户端提供**已配置的** demo 管理员（`demo_allowed`）；
这不是绕过认证——写操作仍需带 `X-CSRF-Token` 并通过 `origin_check`。
若部署设了 `DATAMIND_LOCAL_NO_LOGIN=0` 且没有会话，`user` 会是 `null`：**就此停手并记录阻塞**，
不要伪造 cookie/token。

```bash
CSRF=$(curl -s http://127.0.0.1:8000/v1/auth/me | .venv/bin/python -c "import json,sys;print(json.load(sys.stdin)['csrf_token'])")
```

**2.2 上传一份新文件**

```bash
curl -s -X POST "http://127.0.0.1:8000/v1/uploads?filename=05_service_sla_manual.docx&provenance=synthetic_demo" \
  -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/octet-stream" \
  --data-binary @data/qa_samples/05_service_sla_manual.docx
# 期望（HTTP 201）：{"upload_id":"upl-<40hex>","job_id":"job-<40hex>","status":"queued","review_required":true}
```

预期环节：**校验（后缀/大小/文件名）→ 暂存 → 建 `document_assets` 行 → 入解析队列**。

**2.3 推进解析（独立 worker 一次）**

```bash
.venv/bin/python -m ingestion.upload_worker --once
# 期望退出码 0；输出形如：
# [upload_worker] ... start mode=once workers=1 deadline=45 interval=2
# [upload_worker] ... done mode=once waves=N claimed=N outcomes=N
```

若已有 `--watch` worker 在跑（§0 第 2 步 `worker.alive=true`），本次 `--once` 可能报
`claimed=0` —— 那说明**别人已经把它解析完了**，不是失败；以 2.4 的详情为准。

**2.4 查看解析与审批状态**

```bash
curl -s http://127.0.0.1:8000/v1/uploads/<upload_id> | .venv/bin/python -m json.tool
# 期望：status=succeeded；review_state=pending_review；searchable=false；index_state=not_approved；
#       block_count=6；locator_count=6；vector_index="embedded"；chunk_count>0；
#       index_error_code=null
```

**2.5 复核（approve）**

```bash
curl -s -X POST http://127.0.0.1:8000/v1/uploads/<upload_id>/approve -H "X-CSRF-Token: $CSRF"
# 期望：review_state=approved、status=indexed、indexed=true、searchable=true、index_state="indexed"
```

**审批清的是复核门，索引在解析波次里已建立**：`vector_index` 取 `embedded`（已建立）、
`failed`（建立失败，`index_error_code` 非空）或 `deferred`（无可索引内容）。若为后两者，
approve 仍返回 200，但 `searchable=false`、`index_state="no_chunks"`——不可检索的原因必须
如实呈现，不能冒充成功。

**2.6 契约定位（解析证据确实存在，可指回原位）**

```bash
curl -s "http://127.0.0.1:8000/v1/documents/<document_id>/contract?limit=10" | .venv/bin/python -m json.tool
# 期望：block_count=6、block_locators_in_page=6、sampled locator_kind="office_object"，
#       形如 {"locator_kind":"office_object","part":"word/document.xml","paragraph":0}
```

DOCX 走 `office_object`（part/paragraph/table），XLSX 走 `sheet_range`（sheet/range），
PDF 走 `pdf_region`（page_no/bbox），DXF 走 `cad_entity`（layout/layer/model_xy）。
**契约定位 ≠ 检索切片**：它是「这句话在原文的第几段/第几页」的证据，不是可检索索引。

**2.7 提问（如实展示失败面）**

```bash
curl -s -X POST http://127.0.0.1:8000/v1/chat -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -d '{"question":"一线响应时限是多少小时？","mode":"rag"}'
# 期望：status=ABSTAINED、error_code=INSUFFICIENT_EVIDENCE
#       （检索器只读 chunks，新文件 0 切片 ⇒ 检索不到）
```

带显式文件范围时（`scope.document_refs` 指向这份未发布的上传）：

```bash
# 期望：status=FAILED_WITH_EVIDENCE、error_code=DOCUMENT_SELECTION_UNAVAILABLE
```

**对照组（证明链路本身没坏）**：同一个回答入口问已入库内容：

```bash
curl -s -X POST http://127.0.0.1:8000/v1/chat -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -d '{"question":"一般商品退款多久到账？","mode":"rag"}'
# 期望：status=COMPLETED，citations[0].title=04_售后服务与退款管理制度，
#       locator_kinds=["pdf_region"]，locators 带 page_no/bbox
```

**演示口径**：新文件这条链**到 2.6 为止**（解析 + 复核 + 契约定位）；
2.7 的拒答是**系统如实表达「没有索引」**，不是模型答错。要闭环必须接入自动切片/索引（未做，见结论卡）。

---

## 3. 一键复现（把上面的手工步骤跑成报告）

```bash
cd "/Users/caicai/DataMind Agent"
.venv/bin/python scripts/eval_qa_samples.py --all      # 期望退出码 0
.venv/bin/python -c "import json;d=json.load(open('reports/qa_samples_v1.json'));print(d['stored']['consistency']);print(d['index_wiring'])"
.venv/bin/python scripts/test_isolated.py --pattern test_qa_samples.py   # 期望 Ran 16 tests / OK
```

- `--stored`：匿名（不带会话 cookie）只读 `GET /v1/documents` 与 `/contract`，用
  `ingestion.upload_pipeline.searchability` **重算**可检索性三元组并逐条比对。
- `--http`：§2 的自动化版本，每一步的 HTTP 状态/耗时/响应摘要都写进报告。
- `--control`（默认开）：跑对照组问题。

---

## 4. 离线样本集（不依赖服务与模型）

`data/qa_samples/` 共 **11 份、6 种格式、23 条可判定问题**，`manifest.json` 冻结了
文件/格式/大小/sha256/期望可回答问题（含答案断言）/期望引用定位类型：

```bash
.venv/bin/python data/qa_samples/build_samples.py --check     # 冻结校验，期望 ok=true
.venv/bin/python - <<'PY'
import importlib.util, json
from pathlib import Path
spec=importlib.util.spec_from_file_location('b','data/qa_samples/build_samples.py')
b=importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
manifest=json.loads(Path('data/qa_samples/manifest.json').read_text(encoding='utf-8'))
for entry in manifest['samples']:
    path=Path('data/qa_samples')/entry['file']
    for q in entry['questions']:
        r=b.check_assertion(path, entry, q)
        print(entry['file'], q['id'], r['ok'], r.get('computed', ''), r.get('observed','')[:40])
PY
```

期望：11 份 × 2–3 条断言全为 `True`（数值由脚本从文件本身重算，不是人工填的）。

| 文件 | 格式 | 定位类型 | 问题数 |
|---|---|---|---|
| 01_data_service_policy_native.pdf | 原生 PDF | pdf_region | 2 |
| 02_regional_sales_native.pdf | 原生 PDF | pdf_region | 2 |
| 03_equipment_inspection_scanned.pdf | 扫描 PDF（无文本层） | pdf_region | 2 |
| 04_quality_notice_scanned.pdf | 扫描 PDF（无文本层，2 页） | pdf_region | 2 |
| 05_service_sla_manual.docx | DOCX | office_object | 2 |
| 06_downtime_incident.docx | DOCX | office_object | 2 |
| 07_sales_ledger.xlsx | XLSX（2 表） | sheet_range | 3 |
| 08_inventory_levels.xlsx | XLSX | sheet_range | 2 |
| 09_product_review.pptx | PPTX（4 页） | office_object | 2 |
| 10_safety_training.pptx | PPTX（3 页） | office_object | 2 |
| 11_bracket_spec_layers.dxf | DXF（工业特色） | cad_entity | 2 |

样本由 `build_samples.py` **确定性生成**（ZIP 时间戳、OOXML core 属性、PDF metadata、
DXF 时间戳/GUID 全部固定），因此「冻结 sha256」是可复现的，而不是一次性的。

---

## 5. 可能的失败点与应对（现场话术）

| 现象 | 根因（当前版本事实） | 现场应对 | 不要这样说 |
|---|---|---|---|
| `/v1/auth/me` 的 `user` 为 `null` | 部署要求真实登录（`DATAMIND_LOCAL_NO_LOGIN=0`），本机免登录被关掉 | 记阻塞，改演 §1 与 §4；或用合法账号走 `/v1/auth/login` | 不要说「认证无关紧要」 |
| `GET /v1/uploads` 的 `worker.alive=false` | 独立 worker 没在跑，`queued` 会一直排队 | 前台起 worker：`.venv/bin/python -m ingestion.upload_worker --once` | 不要把 `queued` 说成「解析中」 |
| approve 后仍 `searchable=false` | **设计如此**：有契约块但 0 切片 | 直接讲「无切片不可检索」，并给出 `index_state=no_chunks` | 不要说「已索引、可以直接问」 |
| 提问拒答 / `INSUFFICIENT_EVIDENCE` | 新文件没有 chunks，检索器读不到 | 用 §2.7 对照组证明链路可用 | 不要归因于「模型不会答」 |
| `DOCUMENT_SELECTION_UNAVAILABLE` | 未发布（非 `indexed`）文档不能作为问答范围 | 与 2.7 一起解释为版本/状态门 | 不要说「文件坏了」 |
| `block_count>0` 但 `locator_count=0` | Office/DXF 的定位在 `document_contract_blocks`，PDF 才在 `document_block_locators` | 看合同返回的 `total.block_locators` 与 `blocks[].locator` | 不要说「没有定位」 |
| 扫描件问不出内容 | 两份扫描样本**故意没有文本层**，走 OCR 才有字 | 说明 OCR 路径与离线模型依赖（见 `docs/MODEL_WEIGHTS.md`） | 不要现场跑全量摄取重建索引 |
| `--check` 报 sha256 不一致 | 有人改了冻结样本 | 用 `data/qa_samples/manifest.json` 的 `sha256` 复核；不要现场改写 manifest | 不要说「样本不重要」 |
| `test_isolated.py` 跑完 `reports/latest_regression.json` 变了 | 该命令**按设计**会重写这份记录 | 演示前备份、跑完按原字节恢复（否则状态同步测试会红） | 不要把它的 16 项当全量回归 |

---

## 6. 演示主线建议（8 分钟版）

1. **30 秒**：`/health` + 文档数 27 + worker alive。
2. **2 分钟**：§1.1 文档问答 → 引用卡片 → 点击原页定位（E3/F3、E2）。
3. **1 分钟**：§1.2 单表 NL2SQL → SQL 与结果（E1）。
4. **3 分钟**：§2.2–2.6 新文件上传 → 解析 → 复核 → **契约定位**（`office_object`），
   明确说出「索引未接入 ⇒ 服务端如实报 `no_chunks`」。
5. **1 分钟**：§2.7 对照组（已入库文档答得出且带 `pdf_region` 定位）⇒ 证明失败来自缺索引，不是缺能力。
6. **30 秒**：`reports/qa_samples_v1.json` 的 `stored.consistency`（27/27 一致）与
   `test_qa_samples.py` 的 16/16。

**一句话口径**：WP9 交付的是「多格式样本集 + 可检索性判定一致性 + 新文件的解析/复核/契约定位证据」；
**自动切片与索引仍未接入**，因此新文件的问答与引用点击闭环标记为 `not_done`。
