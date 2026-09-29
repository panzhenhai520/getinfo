#!/usr/bin/env bash
# Deploy/redeploy the isolated CollectInfo pipeline on Ubuntu.
# This project only touches container 'collectinfo-pipeline'; it never touches
# voice-project, Ollama, CosyVoice or RAGFlow containers.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "[deploy] creating .env from .env.example"
    python3 init_config.py --directory .
fi

echo "[deploy] validating compose file"
docker compose config --quiet

echo "[deploy] rebuilding and restarting collectinfo-pipeline"
docker compose up -d --build

echo "[deploy] waiting for health"
# compose 端口绑定在 PIPELINE_BIND_IP（通常 10.88.0.1）而非 127.0.0.1：
# 先按 .env 的绑定 IP 探测，回退 127.0.0.1。
BIND_IP=$(grep -E '^PIPELINE_BIND_IP=' .env 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')
BIND_IP="${BIND_IP:-10.88.0.1}"
for _ in $(seq 1 30); do
    if curl -fsS "http://${BIND_IP}:11236/v1/health" >/dev/null 2>&1 \
       || curl -fsS "http://127.0.0.1:11236/v1/health" >/dev/null 2>&1; then
        echo "[deploy] healthy"
        exit 0
    fi
    sleep 2
done

echo "[deploy] health check timed out" >&2
docker compose logs --tail=80
exit 1
