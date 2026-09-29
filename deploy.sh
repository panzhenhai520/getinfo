#!/usr/bin/env bash
# 部署脚本：在“目标服务器”上加载离线镜像并启动整套服务。
# 前提：本目录已放入 docker-compose.prod.yml、deploy.sh、.env，
#       以及（可选）collectinfo-web.tar.gz 离线镜像。
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE="docker-compose.prod.yml"
TARBALL="collectinfo-web.tar"
[ -f "$TARBALL" ] || TARBALL="collectinfo-web.tar.gz"
IMAGE="collectinfo-web:latest"
WEB_PORT="${WEB_PORT:-8003}"

# 0) 前置检查
if [ ! -f .env ]; then
  echo "❌ 缺少 .env（VPN 地址、密钥、SMTP、LLM 等），请先放到本目录" >&2
  exit 1
fi
command -v docker >/dev/null 2>&1 || { echo "❌ 未安装 docker" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "❌ docker compose 插件不可用" >&2; exit 1; }

# 1) 数据目录
mkdir -p data crawl_results auth_storage crawl_logs

# 2) 加载离线镜像（若存在）
if [ -f "$TARBALL" ]; then
  echo "=== 加载离线镜像 ${TARBALL} ==="
  docker load -i "$TARBALL"
fi

# 3) 启动：优先用已存在的镜像；否则从源码构建
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "=== 启动（使用镜像 ${IMAGE}）==="
  docker compose -f "$COMPOSE" up -d
else
  echo "=== 未找到镜像，改为从源码构建并启动 ==="
  docker compose -f "$COMPOSE" up -d --build
fi

# 4) 等待 web 健康
echo "=== 等待 web 健康检查 ==="
for i in $(seq 1 80); do
  if curl -fsS "http://127.0.0.1:${WEB_PORT}/api/system/health" >/dev/null 2>&1; then
    echo "✅ 服务健康（第 ${i} 次检查）"
    echo "访问地址：http://<服务器IP>:${WEB_PORT}"
    exit 0
  fi
  sleep 3
done
echo "⚠️ 健康检查超时，查看日志：docker compose -f ${COMPOSE} logs web" >&2
exit 1
