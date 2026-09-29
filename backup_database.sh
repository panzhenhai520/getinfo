#!/bin/bash
# 定期备份数据库

DB_PATH="crawler_articles.db"
BACKUP_DIR="db_backups"
DATE=$(date +%Y%m%d_%H%M%S)

# 创建备份目录
mkdir -p $BACKUP_DIR

# 备份数据库
echo "备份数据库..."
cp $DB_PATH "$BACKUP_DIR/crawler_articles_$DATE.db"

# 只保留最近7天的备份
find $BACKUP_DIR -name "crawler_articles_*.db" -mtime +7 -delete

echo "✅ 备份完成: $BACKUP_DIR/crawler_articles_$DATE.db"
