#!/bin/bash
# 清除 Python 缓存文件

echo "=========================================="
echo "清除 Python 缓存"
echo "=========================================="

# 删除 __pycache__ 目录
echo "正在删除 __pycache__ 目录..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
echo "✅ __pycache__ 目录已删除"

# 删除 .pyc 文件
echo "正在删除 .pyc 文件..."
find . -name "*.pyc" -delete 2>/dev/null
echo "✅ .pyc 文件已删除"

# 删除 .pyo 文件
echo "正在删除 .pyo 文件..."
find . -name "*.pyo" -delete 2>/dev/null
echo "✅ .pyo 文件已删除"

echo ""
echo "=========================================="
echo "✅ Python 缓存清除完成"
echo "=========================================="
echo ""
echo "现在可以重启服务了："
echo "  sudo systemctl restart your-service-name"
echo "或"
echo "  sudo supervisorctl restart your-service-name"
