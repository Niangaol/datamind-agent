#!/bin/bash
set -uo pipefail
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
export COMPOSE_PROJECT_NAME=datamind-demo
case "$(uname -m)" in arm64) export DATAMIND_DEMO_ARCH=arm64 ;; *) export DATAMIND_DEMO_ARCH=amd64 ;; esac
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker 当前未运行，演示数据仍保留。"
  exit 0
fi
if docker compose -f compose.yaml stop; then
  echo "演示已停止，数据库、上传文件和模型配置均已保留。"
else
  echo "停止失败，请查看上方错误。"
  docker compose -f compose.yaml ps -a
  if [ -t 0 ]; then read -r -p "按回车关闭…" _reply || true; fi
  exit 1
fi
