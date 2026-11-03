# DataMind Agent

**多模态数据驱动的可解释问数与问答智能体。** 将自然语言问题转成只读 SQL、文档检索或限定工业核验，并保留执行过程、来源版本和证据，帮助用户核查答案。

本项目面向企业文档与业务库，提供可解释的问数与问答能力：自然语言问题落到只读 SQL、文档检索或限定工业核验，执行过程、来源版本与证据一并保留。当前使用 **Vue 3 + TypeScript + Vite** 前端、**FastAPI** 服务、**PostgreSQL** 元数据与业务库、**Qdrant** 文档向量索引；生成模型直连上游，支持 OpenAI 兼容接口与 Anthropic Messages。

[快速开始](#快速开始) · [新版前端](#新版前端) · [开发与验证](#开发与验证) · [文档导航](#文档导航)

**项目状态（2026-11-01）**：本仓库是该项目当前的工作状态。保留可运行的源码、演示数据、测试与设计文档；阶段性过程材料与打包产物已从工作区移除，如需追溯可查阅 Git 历史。Windows / macOS 一键演示包不再随仓库提供，本机运行方式见下文。

## 能力与边界

| 能力 | 已有实现 | 使用边界 |
| --- | --- | --- |
| 结构化问数 | Schema Linking、SQL 生成与校验、只读查询、结果验证、受限修复 | 演示范围包括零售、Chinook 和工业库；复杂查询与真实业务泛化需单独验收 |
| 文档问答 | 混合检索、引用与定位、证据不足时拒答、显式多文件检索 | 多文件最多选择 8 份；相关片段覆盖不等于全文审阅 |
| 工业跨源核验 | 温度规则、单位与版本核对、来源绑定、冲突证据与显式权威规则 | 受控演示材料与限定规则；不构成真实工厂的质量判定 |
| 文件解析与复核 | PDF、扫描 OCR、公式、Office、DXF 适配；统一解析契约、定位、复核与审批 | 格式与资源上限见专题文档；上传后解析或审批成功不代表已建立问答索引 |
| CAD 与尺寸 | 受控二维 DXF、资产版本、实体预览、关联复核、单条尺寸比较 | 不覆盖通用 DWG、复杂三维图纸或整批制造验收 |
| 可解释执行 | HTTP/SSE、运行与证据 API、V-CAR 依赖计划、局部修复、预算与恢复 | 通用编排原语已有实现；真实视觉载荷与广泛来源裁决仍待扩展 |

后端能力与新版页面的接入进度分别说明，详细范围见 [项目进度](PROGRESS.md) 和 [技术设计说明书 V1.3](技术文档/DataMind_Agent_技术设计说明书_V1.3.md)。

## 快速开始

所有命令均在仓库根目录执行。

### 环境准备

- **Docker Desktop / Docker Compose**：启动 PostgreSQL 16 与 Qdrant 1.12.4。
- **Python 3**：当前记录的应用验收环境为 Python 3.14；公式识别使用独立的 Python 3.10–3.12 环境。
- **Node.js 与 npm**：当前前端依赖锁建议使用 Node.js 22.20+ 或 24.12+ 的对应版本系列。
- 首次安装需联网获取依赖与嵌入模型；生成式 SQL/文档问答还需要可用的上游模型凭据。权重与缓存说明见 [MODEL_WEIGHTS](docs/MODEL_WEIGHTS.md)。

获取源码：

```bash
git clone https://github.com/cyc120/datamind-agent.git
cd datamind-agent
```

### 配置模型

新版模型设置页面尚未接入。生成式问答需要在**启动 API 前**配置模型。首次运行可设置以下环境变量，替换模型 ID 与密钥：

```bash
export NEW_API_BASE_URL="https://api.deepseek.com/v1"
export NEW_API_MODEL="<可用的模型 ID>"
export NEW_API_KEY="<你的 API Key>"
```

PowerShell 对应写法：

```powershell
$env:NEW_API_BASE_URL = "https://api.deepseek.com/v1"
$env:NEW_API_MODEL = "<可用的模型 ID>"
$env:NEW_API_KEY = "<你的 API Key>"
```

默认协议为 OpenAI 兼容接口；使用 Anthropic Messages 时设置 `NEW_API_PROTOCOL=anthropic`。也可先启动基础服务，再通过受认证保护的 `/v1/model/config`、`/v1/model/profiles` 管理配置，通过 `/v1/model/config/test` 测试连接。**带密钥的启用存储配置优先于环境变量**；真实密钥不要提交到仓库。

同一认证面下还提供 `/v1/sessions` 会话管理：会话是对既有 run 的命名分组（只存标题与归属，transcript 由 run 推导），仅元数据、不向模型提供历史，契约见 [会话接口契约](docs/SESSIONS.md)。

### macOS / Linux

先启动 Docker，再构建前端并启动服务：

```bash
npm --prefix frontend ci
npm --prefix frontend run deploy
bash scripts/start_local.sh --background
```

启动脚本会创建 `.venv`、安装运行依赖、启动数据库、执行迁移、补齐空库的演示数据，并摄取文档、初始化工业底座及启动 API / 上传 worker。它保留已有数据，但会执行初始化与摄取操作。只初始化 SQL 演示时，可使用 `bash scripts/start_local.sh --sql-only --background`。

### Windows PowerShell

```powershell
npm --prefix frontend ci
npm --prefix frontend run deploy
powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1 -Background
```

Windows 下 Python 命令使用 `.venv\Scripts\python.exe`；下文 `.venv/bin/python` 示例适用于 macOS / Linux。

### 访问

| 入口 | 地址 |
| --- | --- |
| 项目首页 | [http://127.0.0.1:8000/#/](http://127.0.0.1:8000/#/) |
| 问数工作台 | [http://127.0.0.1:8000/#/app](http://127.0.0.1:8000/#/app) |
| API 文档 | [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) |
| 服务健康 | [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) |

六个治理页面（模型接入、字段复核、来源权威规则、图纸与实体、尺寸核验、我的文件）已由 Vue 应用提供，入口分别为 `/#/model`、`/#/fields`、`/#/authority`、`/#/cad`、`/#/dimensions`、`/#/files`；接口契约与各页的诚实边界见 [前端六视图契约](docs/FRONTEND_VIEWS.md)。

`/health` 成功只说明服务及基础设施状态，不能证明上游模型可调用。后台日志位于 `.cache/api/server.log` 与 `.cache/api/upload_worker.log`。脚本检测到已有 API 时会复用进程；更新后端代码或环境变量后，应先停止原 API 再启动。

## 新版前端

当前页面源码位于 `frontend/`，构建产物位于 `web/vue/`。后端从根路径提供首页，静态资源使用 `/assets/vue/` 前缀；启动脚本不会自动构建 Vue，缺少构建入口时首页返回 `503 FRONTEND_NOT_BUILT`。

工作台已接入自然语言提问、流式执行事件、SQL 结果表、引用标题与可点击证据星图。可直接点击以下快捷示例，也可选择相应范围后手动输入：

| 范围 | 示例问题 |
| --- | --- |
| 零售问数 | 2025 年销售额最高的前三个产品是什么？ |
| 文档问答 | 七天无理由退款的条件是什么？ |
| 工业温度核验 | 2026 年 9 月 A102 工序 30 哪些批次温度超限？生产记录和质检报告是否一致？ |

**仍待完善**：引用原文与页码显示、工业核验明细、窄屏证据入口及主题/导航问题；运行记录、模型接入、文件管理、字段复核与审批等新版页面待迁移。界面虽然显示多条消息，目前提问请求尚未携带历史上下文；后端多轮能力见 [多轮上下文](docs/MULTI_TURN.md)。

旧版 HTML / JS / CSS 已退役，旧页面深链不再适用。按优先级列出的修复与功能计划见 [新版前端评估与后续建议](docs/FRONTEND_NEXT_STEPS_20261010.md)。

## 开发与验证

### 前端开发

先完成上述本地初始化。使用 Vite 热更新时，停止占用 8000 端口的原 API，在终端一启动允许开发来源的后端：

```bash
DATAMIND_ALLOWED_ORIGINS="http://127.0.0.1:5173,http://localhost:5173" \
  .venv/bin/uvicorn datamind.api:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

终端二启动前端：

```bash
npm --prefix frontend run dev
```

Vite 将 `/v1` 与 `/health` 代理到本机 8000。打开终端显示的开发地址；构建配置的路径前缀为 `/assets/vue/`。未允许 5173 来源时，浏览器写请求会返回 `ORIGIN_NOT_ALLOWED`。源码更新后，执行 `npm --prefix frontend run deploy` 重新生成后端使用的页面产物。

### 可选解析依赖

```bash
# Office / DXF 适配器
.venv/bin/python -m pip install -r requirements-office.txt
.venv/bin/python -m pip install -r requirements-cad.txt

# 扫描表头模型与独立公式 worker
.venv/bin/python scripts/setup_ocr_models.py
.venv/bin/python scripts/setup_formula.py --python /path/to/python3.12
```

SQL、RAG 与扫描 OCR 的核心依赖由启动脚本安装。严格复现已记录的应用依赖时使用 `requirements.lock`；额外材料生成工具见 `requirements.txt`。解析范围、缺口与模型缓存分别见 [解析契约](docs/PARSE_CONTRACT.md)、[模型权重清单](docs/MODEL_WEIGHTS.md)。

通用上传管道由 `ingestion.upload_worker` 推进；DXF / 测量 JSON 登记批次使用独立的 `scripts/file_job_worker.py`，两者不能互相替代。队列与并发说明见 [上传管道](docs/UPLOAD_PIPELINE.md) 和 [多文件登记](docs/MULTIFILE_CONCURRENCY.md)。

### 验证与评测

完整回归包含 Office / CAD 测试，运行前安装固定版本依赖；快速启动安装的核心依赖不足以覆盖全部测试。

```bash
# 准备完整回归环境（包含 Office / CAD）
.venv/bin/python -m pip install -r requirements.lock

# 基础设施、只读权限与数据库/知识库不变量
.venv/bin/python scripts/verify.py

# 前端布局与可点性（无头 Chrome + 真实鼠标事件；没有 Chrome 时明确跳过）
.venv/bin/python scripts/verify_frontend_layout.py

# 在临时元数据库副本运行完整回归
.venv/bin/python scripts/test_isolated.py

# 根据机读回归记录同步文档；--check 仅检查漂移
.venv/bin/python scripts/status_sync.py
.venv/bin/python scripts/status_sync.py --check
```

`scripts/test_isolated.py` 会覆写机读回归记录 `reports/latest_regression.json`（`reports/` 为运行产物目录，不随仓库保留），包括使用 `--pattern` 的专项运行。发布完整回归结果时，应以最后一次**无参数全量运行**的记录为准，同时检查 `failures`、`errors` 和验证提交；执行数的 `N/N` 写法不代表通过率，也不是模型准确率。

| 记录项 | 值 |
| --- | --- |
| 代码 API | **0.39.0** |
| 完整隔离回归 | **1658/1658**（最近存档的全量执行口径，结果以机读记录为准） |
| 验证基线 | 提交 `dab1233` 由 `scripts/test_isolated.py` 在克隆元数据库上运行 |

此回归记录对应**旧前端退役前的提交**，不能作为当前 Vue 页面迁移完成或当前 HEAD 已全量验收的证明。阶段评测、真实模型结果与合成样本边界见 [PROGRESS](PROGRESS.md)；不要跨阶段拼接成绩。

## 目录结构

```text
datamind/       Agent、SQL/RAG、核验、认证、证据图与 HTTP/SSE API
frontend/       Vue 3 + TypeScript 页面源码与 npm 依赖锁
web/vue/        前端构建产物，由 `npm run deploy` 生成，不入库
ingestion/      解析适配器、摄取、索引、上传管道与 worker
db/             元数据库迁移、业务演示数据与 Catalog
deploy/         PostgreSQL / Qdrant 的 Compose 配置及初始化 SQL
data/           演示材料、知识库 manifest、冻结评测集与契约
scripts/        启动、验证、评测与模型准备工具
tests/          组件、契约、API 与隔离回归测试
docs/           功能规范、部署边界与前端计划
技术文档/       技术设计与历史实施说明
```

Chinook SQL 随仓库提供，演示知识库见 [manifest](data/knowledge_base/manifest.json)。OHR-Bench 原始 Parquet 为可选外部数据，不影响已有抽样材料的演示；重新抽样时放置于 `OHR-Bench/OHR-Bench_v2.parquet`，使用 [抽样脚本](ingestion/sample_ohrbench.py)。模型权重、虚拟环境、密钥和开发缓存不随源码交付。

## 文档导航

| 主题 | 入口 |
| --- | --- |
| 产品定位与路线 | [产品说明](docs/PRODUCT.md) · [路线图](docs/ROADMAP.md) |
| 当前实现与下一阶段 | [项目进度](PROGRESS.md) · [新版前端计划](docs/FRONTEND_NEXT_STEPS_20261010.md) |
| 技术设计与历史记录 | [V1.3 整合版](技术文档/DataMind_Agent_技术设计说明书_V1.3.md)（已整合并取代早期 V1.2；历史阶段记录见其 §18） |
| 部署与依赖 | [部署边界](docs/DEPLOYMENT_BOUNDARIES.md) · [模型权重与缓存](docs/MODEL_WEIGHTS.md) |
| 文件解析与上传 | [统一解析契约](docs/PARSE_CONTRACT.md) · [上传管道](docs/UPLOAD_PIPELINE.md) · [多文件并发](docs/MULTIFILE_CONCURRENCY.md) |
| 私有 PDF | [离线准备](docs/PDF_PREPARATION.md) · [快照登记](docs/PDF_SNAPSHOT_REGISTRY.md) · [知识发布](docs/PDF_KNOWLEDGE_PUBLICATION.md) · [私有检索与回答](docs/PDF_PRIVATE_SEARCH.md) |
| CAD 与尺寸 | [图纸资产流程](data/cad_demo/ASSET_WORKFLOW.md) · [关联复核](data/cad_demo/ASSOCIATION_REVIEW_WORKFLOW.md) · [尺寸核验](data/cad_demo/DIMENSION_SERVICE_WORKFLOW.md) |
| 编排与性能 | [V-CAR](docs/VCAR.md) · [任务预算](docs/TASK_BUDGET.md) · [多轮上下文](docs/MULTI_TURN.md) · [性能](docs/PERFORMANCE.md) |

项目默认面向本机演示，API 与容器端口绑定回环地址。跨机或生产部署前，请按 [部署边界](docs/DEPLOYMENT_BOUNDARIES.md) 配置认证、代理、数据库角色与密钥；历史文档中的页面操作和阶段成绩按其对应版本理解。
