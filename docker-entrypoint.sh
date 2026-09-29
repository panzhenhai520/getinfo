#!/usr/bin/env sh
set -eu

: "${DATABASE_PATH:=/app/data/crawler_articles.db}"
: "${CRAWL_RESULTS_DIR:=/app/crawl_results}"
: "${AUTH_STORAGE_DIR:=/app/auth_storage}"
: "${LOG_FILE:=/app/crawl_logs/app.log}"

export DATABASE_PATH CRAWL_RESULTS_DIR AUTH_STORAGE_DIR LOG_FILE

mkdir -p "$(dirname "$DATABASE_PATH")" "$CRAWL_RESULTS_DIR" "$AUTH_STORAGE_DIR" "$(dirname "$LOG_FILE")"

if [ ! -f "$DATABASE_PATH" ]; then
  echo "SQLite database not found at $DATABASE_PATH, initializing..."
  python init_sqlite_database.py init
fi

exec "$@"
