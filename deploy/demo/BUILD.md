# 演示镜像构建

运行包使用 Linux 容器，分别提供 `linux/arm64` 与 `linux/amd64` 镜像。
Windows、Intel Mac 使用 amd64，Apple 芯片 Mac 使用 arm64。
源码与测试随仓库提供；镜像仅收录运行所需的源码、演示样例、前端产物和模型。

在能够构建两个架构的 Docker 环境中，从项目根目录执行：

```bash
python3 deploy/demo/build_images.py --platform arm64 --target runtime
python3 deploy/demo/build_images.py --platform arm64
python3 deploy/demo/build_images.py --platform amd64 --target runtime
python3 deploy/demo/build_images.py --platform amd64
```

首次构建需要联网下载 Python 基础镜像、系统库和依赖。已导出的运行包在现场用
`docker load` 导入成品镜像，不重新构建或安装 Python 依赖。

`Dockerfile.runtime` 基于 Python 3.12，主环境安装项目根目录的 `requirements.lock`。
公式识别使用隔离的 `/opt/formula/bin/python3.12`，按本目录的 `requirements-formula.lock`
以 `--no-deps` 安装最小 worker 环境（含 `rapid-doc==0.9.10`）。该清单来自已运行的
Python 3.12 公式环境，满足根目录 `requirements-formula.txt`；这样公式识别的 NumPy 1.x
与主环境互不影响，也不引入 RapidDoc 其它 Office/LLM 后端依赖。
成品镜像中的 `/opt/runtime-manifests/main.freeze.txt` 和 `formula.freeze.txt`
记录实际安装版本。`Dockerfile` 在运行时镜像上追加当前应用与模型，并检查导入。

构建上下文在项目上一级的 `datamind-demo-build/build_context/`，由 `build_images.py` 的白名单生成，
不复制 Git 历史、本机虚拟环境、私有上传、真实 `.env`、模型 API 密钥或开发数据库。
三个模型缓存目录从项目的 `.cache/` 复制到镜像 `/opt/app/.cache/`：
`fastembed/`、`ocr_models/`、`formula_models/`。这三个目录必须先准备齐全。
如果外部构建缓存已有核验过的主依赖 wheel，并带 `wheelhouse/READY` 标记，
主环境安装会直接复用；否则从包索引下载。

模型 API 密钥由使用者在运行时自行配置；构建命令不接收或传递该密钥。
模型权重、容器镜像和演示数据库属于运行包，不能用本机 `.venv` 代替。
