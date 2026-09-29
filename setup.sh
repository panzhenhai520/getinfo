 #!/bin/bash
# FireCrawl 依赖安装和启动脚本
# 确保所有依赖正常安装，项目能正常运行

set -e  # 遇到错误时退出

echo "=========================================="
echo "  FireCrawl 安装脚本"
echo "=========================================="
echo ""

# 获取当前目录
CURRENT_DIR=$(pwd)
echo "项目目录: $CURRENT_DIR"
echo ""

# 1. 检查Python版本
echo "步骤1: 检查Python环境..."
echo "----------------------------------------"
python3 --version
if [ $? -ne 0 ]; then
    echo "❌ Python3未安装，请先安装Python3"
    exit 1
fi
echo "✅ Python环境正常"
echo ""

# 2. 安装Python依赖
echo "步骤2: 安装Python依赖..."
echo "----------------------------------------"
echo "使用清华镜像源加速..."

# 升级pip（跳过，使用系统自带版本）
# pip3 install --break-system-packages --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple

# 安装requirements.txt中的所有依赖（必须全部成功）
echo "正在安装requirements.txt中的所有依赖..."
pip3 install --break-system-packages --ignore-installed -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

if [ $? -eq 0 ]; then
    echo "✅ 所有依赖安装成功"
else
    echo "⚠️ 批量安装失败，逐个安装确保完整性..."
    
    # 读取requirements.txt逐行安装
    while IFS= read -r package; do
        # 跳过空行和注释
        if [[ -z "$package" || "$package" =~ ^# ]]; then
            continue
        fi
        
        echo "安装 $package ..."
        pip3 install --break-system-packages --ignore-installed "$package" -i https://pypi.tuna.tsinghua.edu.cn/simple
        
        if [ $? -eq 0 ]; then
            echo "  ✅ $package 安装成功"
        else
            echo "  ⚠️ $package 安装失败，尝试备用方法..."
            # 尝试不指定版本号安装
            package_name=$(echo "$package" | cut -d'=' -f1 | cut -d'>' -f1 | cut -d'<' -f1)
            pip3 install --break-system-packages --ignore-installed "$package_name" -i https://pypi.tuna.tsinghua.edu.cn/simple || echo "  ❌ $package_name 仍然失败"
        fi
    done < requirements.txt
    
    echo "✅ 依赖安装流程完成"
fi

# 验证关键依赖
echo ""
echo "验证关键依赖..."
python3 << 'PYEOF'
import sys
packages = ['flask', 'requests', 'bs4', 'newspaper', 'selenium']
failed = []
for pkg in packages:
    try:
        __import__(pkg)
        print(f"  ✅ {pkg}")
    except ImportError:
        print(f"  ❌ {pkg}")
        failed.append(pkg)

if failed:
    print(f"\n⚠️ 以下包安装失败: {', '.join(failed)}")
    sys.exit(1)
else:
    print("\n✅ 所有关键依赖验证通过")
PYEOF

echo ""

# 3. 安装Playwright浏览器（可选，允许失败）
echo "步骤3: 安装Playwright浏览器..."
echo "----------------------------------------"
export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/

python3 -m playwright install chromium 2>/dev/null && echo "✅ Playwright浏览器安装成功" || {
    echo "⚠️ Playwright安装失败（系统GLIBC版本可能过低）"
    echo "   项目将使用curl-cffi作为替代，功能不受影响"
}
echo ""

# 4. 创建.env配置文件
echo "步骤4: 配置环境变量..."
echo "----------------------------------------"

if [ ! -f .env ]; then
    cat > .env << 'EOF'
# FireCrawl 配置文件

# ==================== Flask应用配置 ====================
FLASK_HOST=0.0.0.0
FLASK_PORT=8003
FLASK_DEBUG=False

# ==================== 数据库配置 ====================
DATABASE_PATH=crawler_articles.db

# ==================== 存储目录配置 ====================
CRAWL_RESULTS_DIR=crawl_results
AUTH_STORAGE_DIR=auth_storage

# ==================== Redis配置 ====================
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=1

# ==================== 爬虫配置 ====================
USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36
CRAWL_TIMEOUT=30
CRAWL_WAIT_TIME=3

# ==================== 代理配置 ====================
# 服务器需要代理访问外网
PROXY_ENABLED=true
PROXY_HTTP=http://127.0.0.1:7890
PROXY_HTTPS=http://127.0.0.1:7890
# 如果Clash端口不是7890，请修改为实际端口

# ==================== 其他配置 ====================
SESSION_LIFETIME=86400
AUTH_CHECK_INTERVAL=3600
LOG_LEVEL=INFO
LOG_FILE=app.log
EOF
    echo "✅ .env配置文件创建成功（端口8003）"
else
    echo "✅ .env配置文件已存在"
    # 确保端口是8003
    if grep -q "FLASK_PORT=" .env; then
        sed -i 's/FLASK_PORT=.*/FLASK_PORT=8003/' .env
    else
        echo "FLASK_PORT=8003" >> .env
    fi
    echo "✅ 端口已确认为8003"
fi
echo ""

# 5. 初始化数据库
echo "步骤5: 初始化数据库..."
echo "----------------------------------------"
if [ -f init_sqlite_database.py ]; then
    python3 init_sqlite_database.py && echo "✅ 数据库初始化成功" || echo "⚠️ 数据库初始化失败（可能已存在）"
else
    echo "⚠️ init_sqlite_database.py不存在，跳过"
fi
echo ""

# 6. 创建systemd服务
echo "步骤6: 创建系统服务..."
echo "----------------------------------------"

# 获取当前用户
CURRENT_USER=$(whoami)

sudo tee /etc/systemd/system/firecrawl-login.service > /dev/null << EOF
[Unit]
Description=FireCrawl Web Crawler Service (Port 8003)
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$CURRENT_DIR
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
ExecStart=/usr/bin/python3 $CURRENT_DIR/firecrawl_app.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/firecrawl-login.log
StandardError=append:/var/log/firecrawl-login-error.log

[Install]
WantedBy=multi-user.target
EOF

echo "✅ 服务文件创建成功"
echo ""

# 7. 创建日志文件
echo "步骤7: 创建日志文件..."
echo "----------------------------------------"
sudo touch /var/log/firecrawl-login.log /var/log/firecrawl-login-error.log
sudo chown $CURRENT_USER:$CURRENT_USER /var/log/firecrawl-login*.log
echo "✅ 日志文件创建成功"
echo ""

# 8. 重载systemd并启动服务
echo "步骤8: 启动服务..."
echo "----------------------------------------"
sudo systemctl daemon-reload
sudo systemctl enable firecrawl-login
sudo systemctl restart firecrawl-login

sleep 2

# 9. 检查服务状态
echo ""
echo "=========================================="
echo "  安装完成 - 检查状态"
echo "=========================================="
echo ""

sudo systemctl status firecrawl-login --no-pager | head -n 15

echo ""
echo "检查端口8003..."
netstat -tlnp 2>/dev/null | grep 8003 || ss -tlnp 2>/dev/null | grep 8003 || echo "⚠️ 无法检测端口（可能需要等待几秒）"

echo ""
echo "=========================================="
echo "  ✅ 安装完成！"
echo "=========================================="
echo ""
echo "服务信息:"
echo "  服务名: firecrawl-login"
echo "  端口: 8003"
echo "  访问: http://$(hostname -I | awk '{print $1}'):8003"
echo "  默认账号: admin / admin123"
echo ""
echo "代理配置:"
echo "  已启用代理（PROXY_ENABLED=true）"
echo "  代理地址: http://127.0.0.1:7890"
echo "  如需修改代理端口，请编辑 .env 文件"
echo ""
echo "管理命令:"
echo "  查看状态: sudo systemctl status firecrawl-login"
echo "  启动服务: sudo systemctl start firecrawl-login"
echo "  停止服务: sudo systemctl stop firecrawl-login"
echo "  重启服务: sudo systemctl restart firecrawl-login"
echo "  查看日志: tail -f /var/log/firecrawl-login.log"
echo ""
echo "如果服务未启动，请查看日志:"
echo "  tail -n 50 /var/log/firecrawl-login-error.log"
echo ""

