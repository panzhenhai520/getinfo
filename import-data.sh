#!/usr/bin/env bash
# 数据导入脚本：在新服务器 deploy.sh 启动后再运行，还原 PostgreSQL + 文件数据
set -euo pipefail
cd "$(dirname "$0")"

DUMP="deploy-data/collectinfo.dump"
FILES="deploy-data/file-data.tar.gz"

# 1) 还原 PostgreSQL
if [ -f "$DUMP" ]; then
  echo "=== 还原 PostgreSQL ==="
  docker exec -i collectinfo-postgres pg_restore -U postgres -d collectinfo --clean --if-exists < "$DUMP"
else
  echo "⚠️ 未找到 $DUMP，跳过数据库还原"
fi

# 2) 还原文件数据
if [ -f "$FILES" ]; then
  echo "=== 还原文件数据 ==="
  tar -xzf "$FILES" -C .
fi

echo "✅ 数据导入完成，重启应用使数据生效："
echo "   docker compose -f docker-compose.prod.yml restart web worker intel-worker"
