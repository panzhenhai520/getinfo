#!/usr/bin/env bash
# 打包脚本：在“构建机”上构建镜像并导出为离线 tar 包，用于分发到目标服务器。
# 用法：
#   ./build-and-package.sh                          # 完整构建（含验收测试，慢但稳妥）
#   SKIP_ACCEPTANCE=true ./build-and-package.sh     # 跳过验收测试，快速打包
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${IMAGE:-collectinfo-web:latest}"
TARBALL="${TARBALL:-collectinfo-web.tar}"
SKIP_ACCEPTANCE="${SKIP_ACCEPTANCE:-false}"

echo "=== [1/3] 构建镜像 ${IMAGE} (DEV_SKIP_ACCEPTANCE=${SKIP_ACCEPTANCE}) ==="
docker build \
  --build-arg "DEV_SKIP_ACCEPTANCE=${SKIP_ACCEPTANCE}" \
  -t "${IMAGE}" .

echo "=== [2/3] 导出离线镜像 ${TARBALL} ==="
docker save -o "${TARBALL}" "${IMAGE}"

echo "=== [3/3] 完成 ==="
ls -lh "${TARBALL}"
echo "分发到目标服务器的文件："
echo "  - ${TARBALL}"
echo "  - docker-compose.prod.yml"
echo "  - deploy.sh"
echo "  - .env （含 VPN 地址/密钥，注意安全传输）"
