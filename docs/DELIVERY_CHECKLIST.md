# 校内初赛完整交付清单

整理日期：2026-10-19。沿用 2026-10-14 校内初赛文档，提供 Windows 与 macOS 整体演示环境。

## 提交入口

主目录：[交付/校内初赛_20261014](../交付/校内初赛_20261014/)。完整压缩包为 `交付/DataMind_Agent_校内初赛_提交包_20261019.zip`。

| 内容 | 用途 |
| --- | --- |
| `01_设计报告_DataMind_Agent_20261014.docx` / `.pdf` | 设计报告原件，PDF 15 页 |
| `02_技术实现说明书_DataMind_Agent_20261014.docx` / `.pdf` | 技术实现说明书原件，PDF 24 页 |
| `03_源代码与数据_DataMind_Agent_20261019.zip` | 项目源码、前端成品、数据库定义、样例、评测集、依赖锁与构建工具 |
| `04_完整演示程序/` | 两架构离线镜像、预置数据库/向量快照、Windows/Mac 启停入口、演示流程和验收记录 |
| `00_提交说明.md` | 文件说明与实际启动步骤 |
| `SOURCE_MANIFEST.json` / `DELIVERY_MANIFEST.json` / `SHA256SUMS.txt` | 源码及完整提交目录的大小、哈希与检查记录 |

平台分别收件时，按要求上传两份 Word、两份 PDF，并将**外层完整提交包作为技术数据 ZIP**。内层 `03_源代码与数据...zip` 便于单独查阅源码，不能单独替代整体演示环境。

## 完整演示

1. Windows x86-64 或 Mac 先安装 Docker Desktop，完成首次设置。Windows 使用 Linux 容器。
2. 完整解压后进入 `04_完整演示程序/`。Windows 双击 `run.bat`；Mac 双击 `run.command`。
3. 入口导入对应架构的本地镜像、恢复初始数据并打开 `http://127.0.0.1:8000/`。后续启动复用数据。
4. 在“模型接入”填写自己的模型地址、模型名与 API Key，按 `演示流程.md` 展示问数、RAG、工业核验、Wiki、CAD、尺寸与审批。

包内提供 Python、隔离公式 worker、运行依赖、已构建前端、向量/OCR/公式权重、PostgreSQL 与两个 Qdrant；应用和模型无需现场下载安装。生成式问答仍依赖用户提供的模型服务及所需网络。Docker Desktop 由演示电脑预先准备。

**不分发模型密钥**：演示种子在独立环境新建，未复制本机模型配置；模型配置表为空；只提供空的 `model_config.env.example`。镜像环境及导出的层已检查，无现有模型密钥。不要重新分发后来输入密钥的配置文件或运行数据库。

## 验证口径

- 原件 Word/PDF 哈希保持不变；DOCX 容器/XML 完整，两份 PDF 全 39 页渲染并查看拼版。
- ARM64 与 AMD64 镜像的主/公式依赖一致；断网验证向量、普通 OCR、表头 OCR 与真实公式识别。
- 两架构均在本机 Docker 环境通过全新数据恢复、服务与页面检查、带原文引用的真实问答、问数和工业核验。AMD64 使用本机仿真，未在物理 Windows 电脑执行桌面入口。
- Mac 启停入口、导入归档与再次启动单独验证；停止保留数据。上传样例由后台 worker 实际完成解析，通用上传自动问答索引仍为 `deferred`，不作已可检索主张。
- 临时模型配置仅用于运行时验收，测试后移除；种子和镜像不包含它。

本次是演示链路与交付完整性验收，未重新执行完整后端回归或整体模型准确率评测。文档沿用其原有基线 `f013ad3`，源码与运行镜像记录打包时版本；不要将历史评测当作当前版本重新验收结果。

## 源码与保留材料

源码 ZIP 按白名单包含 `datamind/`、`ingestion/`、`frontend/`、`web/`、`db/`、`deploy/`、`delivery_template/`、`data/`、`scripts/`、`tests/`、`docs/`、`reports/`、`技术文档/` 及依赖说明；三个被评测直接引用的 `output/pdf/industrial_forms_*.pdf` 显式保留。上传、私有运行产物、虚拟环境、缓存和真实配置不混入源码 ZIP。

源码 ZIP 可用 `python3 scripts/build_delivery_zip.py --strict --write --i-know-this-writes-a-zip --out <输出路径> --json <清单路径>` 重建。双架构运行镜像重建方式见 `deploy/demo/BUILD.md`；交付目录内已有初始化种子。

`DataMind-Agent-Competition-Docs/` 为另一套 18/28 页扩展稿，保留备查，不与主提交混用。旧清单归档在 [历史清单](archive/DELIVERY_CHECKLIST_20261010.md)，原有状态仅表示当时情况。
