# 模型权重与大文件清单（MODEL_WEIGHTS）

> **本仓库不包含任何模型权重文件。** 本文件只是**清单**：逐项记录本机实测的路径、体积、SHA256、用途与获取方式，用于在权重与大体积外部数据不随仓库分发时，列明合法的获取、缓存与网络依赖（对应「模型权重文件（若适用）」的说明口径，以及「系统效率设计与伸缩性证明」中的外部依赖声明）。

核实方式（全部为只读/实测）：

- 仓库内**无**权重：`git ls-files | grep -iE '\.(onnx|bin|safetensors|gguf|pt|pth|h5|ckpt|weights)$'` 无命中；`.gitignore:11-15` 忽略 `.venv/`、`.venv-formula/`、`.cache/`。
- 体积与 SHA256：`shasum -a 256 <file>` 与 `stat -f%z` / `du -sh`，原始测量时间 2026-10-08；2026-10-10 已清理两份未采用的试验权重，历史哈希保留在下表后的清理记录中。
- 用途与获取方式：读代码（`ingestion/formula.py`、`ingestion/formula_worker.py`、`ingestion/tables.py`、`ingestion/ocr.py`、`datamind/knowledge.py`、`datamind/pdf_vectors.py`、`scripts/setup_*.py`）与只读 `grep`。

---

## 1. 项目显式管理的权重（项目根 `.cache/`，不随仓库）

| 路径 | 体积（字节） | SHA256 | 用途 | 获取方式 | 需要它的可复现路径 |
|---|---|---|---|---|---|
| `.cache/formula_models/pp_formulanet_plus_m.onnx` | 593,915,961（566.4 MiB） | `71b6d389cf7b857e45252a4b98cfced1a3ffca7bf24d9497d02d052a41d9493b` | 公式识别（PP-FormulaNet_plus-M，ONNX） | `scripts/setup_formula.py`：从 ModelScope `RapidAI/RapidDoc` v1.0.0 下载，脚本内置 SHA256 校验；另建 `.venv-formula`（Python 3.10–3.12）装 `rapid-doc==0.9.10` | 公式适配器（`ingestion/formula.py` + `ingestion/formula_worker.py`）；`start_local.sh` 缺失时只提示不阻断；隔离回归中的公式契约/预算测试 |
| `.cache/ocr_models/ch_PP-OCRv5_rec_server.onnx` | 84,577,022（80.7 MiB） | `e09385400eaaaef34ceff54aeb7c4f0f1fe014c27fa8b9905d4709b65746562a` | 扫描页深色表头识别（表头恢复） | `scripts/setup_ocr_models.py`：ModelScope `RapidAI/RapidOCR` v3.9.2 `onnx/PP-OCRv5/rec/`，SHA 同时硬编码于 `ingestion/tables.py:14` | `ingestion/tables.py`（表头恢复）；缺失时表头保留为解析缺口、正文 OCR 继续 |
| `.cache/fastembed/models--Qdrant--bge-small-zh-v1.5/blobs/1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38`（快照内为 `model_optimized.onnx`） | 94,781,076（90.4 MiB） | `1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38` | 中文文本嵌入 `BAAI/bge-small-zh-v1.5`（fastembed 的 ONNX 量化版） | FastEmbed **运行时自动下载**（fastembed 内部使用 Hugging Face 仓库 `Qdrant/bge-small-zh-v1.5`）；缓存目录由 `datamind/knowledge.py:101`、`ingestion/ingest.py:44`、`datamind/pdf_vectors.py:183` 指定为 `.cache/fastembed`（可用 `DATAMIND_PDF_EMBED_CACHE` 覆盖）。**仓库内没有 SHA 锁定**；但 HF `trees/*.json` 记录的 `lfs_sha256` 与实测 SHA256 完全一致（独立交叉核对） | RAG 检索、`ingestion/ingest.py` 向量摄取、`datamind/pdf_vectors.py` 私有 PDF 嵌入、`scripts/verify.py`；缺缓存时向量路径降级/失败并给出稳定错误码 |

同一 fastembed 快照的辅助文件（跟随 `model_optimized.onnx` 一同下载，均很小）：

| 文件（`…/snapshots/46fbe35f…/`） | 体积（字节） | SHA256 |
|---|---|---|
| `tokenizer.json` | 439,125 | `48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26` |
| `config.json` | 739 | `9088751d39abbf86ec3d19ffca92ad62ad19075f7e59712e6c71217fa125d1d3` |
| `tokenizer_config.json` | 367 | `e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a` |
| `special_tokens_map.json` | 125 | `b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3` |

> 注：HF `trees` 元数据还列出 `vocab.txt`（109,540 字节），但本机快照目录中**不存在**该文件（未被下载）；不影响当前 ONNX 推理路径。fastembed 缓存目录总量实测 `du -sh .cache/fastembed` = **91M**。
> 清理后的分目录总量（2026-10-10）：`.cache/formula_models` **566M**、`.cache/ocr_models` **81M**、`.cache/fastembed` **91M**。

已清理的历史试验权重（2026-10-10，共 295.7 MiB）：

| 文件 | 历史字节数 / SHA256 | 清理依据与保留证据 |
|---|---|---|
| `pp_formulanet_plus_s.onnx` | 233,421,946 / `30998d10c94ccff1ad8981df0c71048cb1f3eec7b1e515b809767f1f72aebe3b` | 运行代码和 setup 脚本均未使用；公式功能继续使用 M 模型，历史公式报告保留 |
| `PP-OCRv6_rec_medium.onnx` | 76,629,984 / `eef444829dbbe18d7fea59a3f6eb75647518d2b3a9568d27c92e42940204894b` | 早期版本记录为未采用；失败结果保留在 `reports/header_medium_trial.json`，正文与表头的运行模型保留 |

---

## 2. 随依赖包安装的权重（`site-packages`，不在 `.cache/`、不随仓库）

这些权重由 pip 包自带/自动下载，落在虚拟环境中。列出它们是因为它们同样参与本项目的解析能力，分发说明应声明「由依赖包提供」而不是当作仓库内容。

| 路径 | 体积（字节） | SHA256 | 用途 |
|---|---|---|---|
| `.venv/lib/python3.14/site-packages/rapidocr/models/PP-OCRv6_det_small.onnx` | 9,929,594 | `090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f` | 通用扫描页文本检测（`ingestion/ocr.py::engine()` 用 RapidOCR 默认 `ocr_version: PP-OCRv6`） |
| `.venv/lib/python3.14/site-packages/rapidocr/models/PP-OCRv6_rec_small.onnx` | 21,234,383 | `6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884` | 通用扫描页文本识别（同上） |
| `.venv/lib/python3.14/site-packages/rapidocr/models/ch_ppocr_mobile_v2.0_cls_mobile.onnx` | 585,532 | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` | 文本方向分类（同上） |
| `.venv-formula/lib/python3.12/site-packages/rapid_latex_ocr/models/encoder.onnx` | 89,008,136 | `01bf5dc25539ca0cd5b1bd29296ea495977a6ba5f629dc4178277809d26e5e7d` | 公式 worker 依赖包 `rapid-doc` 自带资产；**本仓库代码未显式加载**，其调用链未逐一核实 |
| `.venv-formula/lib/python3.12/site-packages/rapid_latex_ocr/models/decoder.onnx` | 50,952,726 | `bd695497bf1b22279b7626f5916c79226e1e244c84355f8da7edfd2d921d0072` | 同上 |
| `.venv-formula/lib/python3.12/site-packages/rapid_latex_ocr/models/image_resizer.onnx` | 38,967,751 | `e0b075c39700f64d50400f39c8fc186bbb3b5d84d31864008313f376603aca9d` | 同上 |
| `.venv-formula/lib/python3.12/site-packages/rapid_doc/resources/rapid_orientation.onnx` | 6,783,084 | `2f62c9bfb830a0b417241269fde7ef2d0ad5446c0ed2b8af33b1f6543545e8e2` | 依赖包自带资源；未被本仓库显式引用 |
| `.venv-formula/lib/python3.12/site-packages/rapid_doc/resources/pp-ocrv4_mobile_seal_det.onnx` | 4,826,518 | `e6109a1022b5ebf0822fc00646ef2398a7ef387390ca5c978de79352b1314204` | 同上 |
| `.venv-formula/lib/python3.12/site-packages/rapid_doc/resources/slanet-1m.onnx` | 7,671,903 | `53099b464f7e62e60f45cd7889ef8997247ebab1cd567a00b74a059e08cc605d` | 同上 |
| `.venv-formula/lib/python3.12/site-packages/rapid_doc/model/magika/models/standard_v3_3/model.onnx` | 3,163,737 | `fe2d2eb49c5f88a9e0a6c048e15d6ffdf86235519c2afc535044de433169ec8c` | 依赖包的文件类型识别权重；未被本仓库显式引用 |

目录总量：`…/rapidocr/models` **30M**、`…/rapid_latex_ocr/models` **171M**、`…/rapid_doc/resources` **79M**。这些文件属于 pip 包，`requirements.lock` 固定的是**包版本**（`rapidocr==3.9.2`、`onnxruntime==1.30.0` 等），不是这些文件的 SHA。

---

## 3. 与已移除本地网关的关系：没有因此变成孤儿的权重

- `gateway/`（vendored New API，997 个跟踪文件）已于 API 0.37.0 移除（V1.3 §18.24）。用只读 git 实测其删除前的内容：`git ls-tree -r 4c246b3^ -- gateway` 中**没有任何** `.onnx/.bin/.safetensors/.gguf/.pt/.pth/.h5/.ckpt` 文件（文件类型为 933 个 `.go`、27 个 `.json`、11 个 `.js` 等）。即该网关是纯 HTTP 透传服务，**不提供本地推理权重**，移除后没有遗留权重文件需要额外清理；`.cache/` 中也没有网关专属产物。
- 现模型接入为**直连上游** `https://api.deepseek.com/v1`（`datamind/settings_store.py:23` 的 `DEFAULT_BASE_URL`），使用远端模型（如 `deepseek-flash`）：**没有本地权重文件、无法提供权重哈希**，只能登记为远端别名。
- `app_settings` 中仍留有一行历史 `model.gateway` 死配置（指向已删除的 `localhost:4000`）。它是**配置行**而非权重文件；本清单不记录其任何密钥内容（真实密钥值一律不写入文档）。

---

## 4. 复现所需的网络与降级行为

| 依赖 | 缺失时的行为 | 获取脚本 / 出处 |
|---|---|---|
| BGE 嵌入（fastembed） | 向量检索不可用 → `knowledge.py` 有词法检索降级路径；私有 PDF 嵌入/发布失败并给出错误码 | 无专用脚本：FastEmbed 运行时下载到 `cache_dir`；离线环境请预置缓存或设 `DATAMIND_PDF_EMBED_CACHE` |
| `ch_PP-OCRv5_rec_server.onnx`（表头） | 表头保留为解析缺口，不阻断正文 OCR | `scripts/setup_ocr_models.py`（ModelScope，SHA 校验） |
| `pp_formulanet_plus_m.onnx`（公式） | 返回 `FORMULA_MODEL_UNAVAILABLE`；未识别图片保留解析缺口 | `scripts/setup_formula.py`（ModelScope，SHA 校验） |
| RapidOCR 通用检测/识别 | 扫描页 OCR 不可用（依赖包资产随 pip 安装，通常不需要额外下载） | `pip install rapidocr==3.9.2`（`requirements-ocr.txt`） |

网络事实（实测 `grep -rn HF_ENDPOINT`）：全仓库只有 `scripts/reset_demo.sh:40` 设置了 `export HF_ENDPOINT=https://hf-mirror.com`；**常规启动路径（`scripts/start_local.sh`）没有设置该变量**，因此 BGE 首次下载在受限网络下可能长时间挂起或失败。这是已核实的复现风险，不是猜测。

## 5. 未核实项（如实列出）

- 已清理的 `pp_formulanet_plus_s.onnx`、`PP-OCRv6_rec_medium.onnx` 的原始下载 URL 未核实；历史体积与哈希已留档，不将它们列为运行所需权重。
- `rapid-doc` / `rapid_latex_ocr` 自带权重在公式 worker 内的**实际调用链**：本仓库只显式加载 `.cache/formula_models/pp_formulanet_plus_m.onnx`，其余资产的调用路径未逐一核实。
- BGE 的**许可证**：未核实，本清单只记录名称与哈希。
