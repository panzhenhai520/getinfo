#!/bin/bash
# 检查磁盘空间

echo "=========================================="
echo "磁盘空间检查"
echo "=========================================="

# 1. 查看所有分区
echo -e "\n【所有磁盘分区】"
df -h

# 2. 查看当前目录所在分区
echo -e "\n【当前目录所在分区】"
df -h .

# 3. 检查使用率
USAGE=$(df -h . | awk 'NR==2 {print $5}' | sed 's/%//')
echo -e "\n【磁盘使用率】: ${USAGE}%"

if [ $USAGE -lt 80 ]; then
    echo "✅ 磁盘空间充足"
elif [ $USAGE -lt 90 ]; then
    echo "⚠️  磁盘空间紧张，建议清理"
else
    echo "❌ 磁盘空间严重不足，必须立即清理！"
fi

# 4. 查看当前目录大小
echo -e "\n【当前目录大小】"
du -sh .

# 5. 查看数据库文件
echo -e "\n【数据库文件】"
ls -lh *.db* 2>/dev/null || echo "未找到数据库文件"

# 6. 查看日志文件
echo -e "\n【日志文件】"
find . -name "*.log" -exec ls -lh {} \; 2>/dev/null | head -10

# 7. 查看大文件（>100MB）
echo -e "\n【大文件 (>100MB)】"
find . -type f -size +100M -exec ls -lh {} \; 2>/dev/null | head -10

echo -e "\n=========================================="
echo "检查完成"
echo "=========================================="
