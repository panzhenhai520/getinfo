#!/bin/bash

echo "=========================================="
echo "安装 curl-cffi (Cloudflare 绕过工具)"
echo "=========================================="
echo ""

# 检查 Python 版本
echo "检查 Python 版本..."
python3 --version

# 安装 curl-cffi
echo ""
echo "安装 curl-cffi..."
pip3 install curl-cffi --upgrade

# 验证安装
echo ""
echo "验证安装..."
python3 -c "from curl_cffi import requests; print('✅ curl-cffi 安装成功!')" 2>&1

echo ""
echo "=========================================="
echo "安装完成！"
echo "=========================================="
echo ""
echo "说明："
echo "- curl-cffi 可以模拟浏览器 TLS 指纹绕过 Cloudflare"
echo "- 不需要新的 GLIBC 版本（兼容 GLIBC 2.17）"
echo "- 比 Playwright 更轻量，更适合老系统"
echo ""

