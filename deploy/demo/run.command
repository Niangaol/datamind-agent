#!/bin/bash
# macOS 3.2-compatible launcher. All application dependencies are in the image.
set -Eeuo pipefail
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export COMPOSE_PROJECT_NAME=datamind-demo
case "$(uname -m)" in
  arm64) export DATAMIND_DEMO_ARCH=arm64 ;;
  x86_64) export DATAMIND_DEMO_ARCH=amd64 ;;
  *) echo "不支持当前处理器：$(uname -m)"; exit 1 ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
fi
fail() {
  trap - ERR
  echo
  echo "启动未完成：$1"
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker compose -f compose.yaml ps -a || true
    docker compose -f compose.yaml logs --no-color --tail 80 || true
  fi
  echo "数据已保留。处理错误后可重新双击 run.command。"
  if [ -t 0 ]; then read -r -p "按回车关闭…" _reply || true; fi
  exit 1
}
trap 'fail "第 $LINENO 行执行失败。"' ERR
command -v docker >/dev/null 2>&1 || fail "请先安装并完成 Docker Desktop 的首次设置。"

if ! docker info >/dev/null 2>&1; then
  echo "正在启动 Docker Desktop…"
  open -a Docker || fail "无法打开 Docker Desktop。"
  ready=0
  for ((n=0; n<120; n++)); do
    if docker info >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
  done
  [ "$ready" -eq 1 ] || fail "Docker Desktop 尚未就绪，请检查它的窗口。"
fi
[ "$(docker info --format '{{.OSType}}')" = linux ] || fail "Docker 必须使用 Linux 容器。"
docker compose version >/dev/null 2>&1 || fail "Docker Compose 不可用，请检查 Docker Desktop 安装。"
mkdir -p data
if [ ! -f data/model_config.env ]; then
  cp data/model_config.env.example data/model_config.env
  chmod 600 data/model_config.env
fi

archive="images/$DATAMIND_DEMO_ARCH.tar.gz"
marker="data/.image-import-$DATAMIND_DEMO_ARCH.sha256"
images_ready=1
for name in app postgres qdrant; do
  if ! docker image inspect "datamind-demo-$name:20261019-$DATAMIND_DEMO_ARCH" >/dev/null 2>&1; then
    images_ready=0
  fi
done
if [ -f "$archive" ]; then
  digest="$(shasum -a 256 "$archive" | cut -d ' ' -f 1)"
  previous=""
  if [ -f "$marker" ]; then previous="$(cat "$marker")"; fi
  if [ "$images_ready" -ne 1 ] || [ "$digest" != "$previous" ]; then
    echo "正在导入本机架构的离线镜像（首次启动需要几分钟）…"
    docker load --input "$archive"
    printf '%s\n' "$digest" > "$marker"
  fi
elif [ "$images_ready" -ne 1 ]; then
  fail "缺少 ${archive}，请完整解压演示包。"
else
  echo "使用本机已导入的演示镜像。"
fi
for name in app postgres qdrant; do
  actual="$(docker image inspect --format '{{.Architecture}}' "datamind-demo-$name:20261019-$DATAMIND_DEMO_ARCH")"
  [ "$actual" = "$DATAMIND_DEMO_ARCH" ] || fail "$name 镜像的处理器架构不匹配。"
done
[ -f seed/postgres/roles.sql ] || fail "缺少 seed/postgres/roles.sql，请完整解压演示包。"
echo "正在启动数据库、知识库和演示页面…"
docker compose -f compose.yaml up -d
ready=0
for ((n=0; n<150; n++)); do
  state="$(docker compose -f compose.yaml ps -q app 2>/dev/null || true)"
  if [ -n "$state" ]; then
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$state" 2>/dev/null || true)"
    if [ "$status" = healthy ] && curl --noproxy '*' --silent --fail --max-time 5 http://127.0.0.1:8000/ >/dev/null; then
      ready=1
      break
    fi
  fi
  sleep 2
done
[ "$ready" -eq 1 ] || fail "演示服务在 5 分钟内没有就绪，具体原因见上方日志。"
echo "演示已就绪：http://127.0.0.1:8000/"
echo "停止时双击 stop.command；再次启动会保留本次数据。"
open http://127.0.0.1:8000/ || echo "请在浏览器打开上面的地址。"
