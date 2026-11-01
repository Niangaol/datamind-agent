# DataMind Agent 双平台演示包

本目录是演示包入口模板。完整交付包还应包含 `images/arm64.tar.gz`、`images/amd64.tar.gz` 和 `seed/`，源码仓库中的模板本身不包含这些大文件。

## 启动

1. 在 Windows x86-64 或 macOS 安装 Docker Desktop，完成首次设置。Windows 需启用 WSL 2，并使用 Linux 容器。
2. 将完整演示包解压到可写目录。不要在压缩包内直接运行入口。
3. Mac 双击 `run.command`；Windows 双击 `run.bat`。Mac 入口会自动识别 Apple Silicon 和 Intel，只导入本机所需的镜像。入口会启动 Docker Desktop、导入离线镜像、恢复演示数据，并在就绪后打开 <http://127.0.0.1:8000/>。
4. 首次启动需要几分钟。之后启动复用数据库和上传文件，不会重置演示数据。

Mac 若提示文件没有执行权限，在解压目录打开终端执行 `chmod +x run.command stop.command`，再双击入口。若系统要求确认下载文件来源，请在系统设置的“隐私与安全性”中按系统提示允许打开。

## 模型接入：已预置，开箱即可问答

**本包已预置一个评测用的 DeepSeek 接入**（`data/model_config.env`：协议 `openai`、地址 `https://api.deepseek.com/v1`、模型 `deepseek-flash`）。启动后不需要任何配置，直接到「问数与问答」提问即可，三条链路（零售问数、文档问答、工业温度核验）都可用。

该密钥是**评测用临时密钥**：请勿外传或公开转载；如怀疑泄露，请在服务商侧吊销。它只落在本文件里，**容器镜像与归档层中不含任何密钥**。

想换成自己的模型（额度更可控，或预置密钥被吊销时）：打开网页[模型接入](http://127.0.0.1:8000/#/model)，新建配置并填写协议、接口地址、模型名和你的 API key，点「保存并启用」再点「测试连接」。**网页中保存并启用的配置优先于预置文件。**

使用宿主机上的模型服务时，容器内的地址填写 `http://host.docker.internal:端口/v1`。

### 预置密钥不可用时的回退

如果预置密钥被吊销、超额或网络不通（页面会明确显示「模型服务拒绝了该 API 密钥」「额度不足」「无法连接模型服务」等具体原因），**其余功能完全不受影响**，仍然可以演示：知识图谱三篇词条、图纸与实体（A200 / CAD-DEV-1 / R2、预览、标注、关联复核）、尺寸核验（20.04 mm 在 [19.9, 20.1] mm 内、独立批准链、对照历史回放）、字段复核与独立审批、上传原生 PDF 的离线解析。这些都不联网、不消耗任何额度。

完整步骤见同目录 `演示流程.md` 的「路线 A：零配置演示」与「路线 B：自然语言问答」。

## 其他依赖

页面、数据库、知识库、向量模型、OCR 和公式识别依赖已随镜像提供，启动不会在线安装依赖。完整断网问答需要另外提供本机生成模型；本演示包没有内置该模型权重。

不要把填写了你自己密钥的环境文件或运行后的数据库重新打包分发。Compose 中的固定 Qdrant token 只用于本机演示服务之间的访问控制，不是模型 API key。

## 停止与数据

Mac 双击 `stop.command`；Windows 双击 `stop.bat`。停止只结束演示服务，保留数据库、上传文件及模型配置。

数据保存在 Docker 的 `datamind-demo` 项目命名卷中；模型配置文件保存在 `data/model_config.env`。删除解压目录不会自动删除 Docker 数据卷。演示仅开放本机 `127.0.0.1:8000`，不开放数据库端口。

## 故障排查

启动失败时入口会显示容器状态和最近日志。优先检查 Docker Desktop 是否就绪、磁盘空间是否足够、8000 端口是否被其他程序占用，以及演示包是否完整解压。

终端查看状态：`docker compose -f compose.yaml ps -a`。

终端查看日志：`docker compose -f compose.yaml logs --tail 100`。

Mac 手动运行上述命令前，Apple Silicon 执行 `export DATAMIND_DEMO_ARCH=arm64`，Intel 执行 `export DATAMIND_DEMO_ARCH=amd64`。Windows 的启动入口已设置为 `amd64`。停止命令不会删除数据卷；不要使用带 `--volumes` 的删除命令。

## 包结构

```text
DataMind_Agent_Demo/
  compose.yaml
  run.command / stop.command
  run.bat / stop.bat / launch.ps1
  data/model_config.env.example
  images/arm64.tar.gz
  images/amd64.tar.gz
  seed/postgres/roles.sql
  seed/postgres/*.sql
  seed/qdrant/*.snapshot
```

三类镜像标签为 `datamind-demo-app:20261019-架构`、`datamind-demo-postgres:20261019-架构`、`datamind-demo-qdrant:20261019-架构`。Compose 禁止自动拉取镜像；启动入口从随包归档导入。
