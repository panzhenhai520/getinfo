#!/bin/bash
# Clash Meta (mihomo) 一键安装脚本 - Ubuntu服务器

echo "=========================================="
echo "  Clash Meta 一键安装脚本"
echo "=========================================="
echo ""

# 检查是否为root
if [ "$EUID" -ne 0 ]; then 
    echo "请使用sudo运行此脚本"
    echo "使用方法: sudo bash install_clash.sh"
    exit 1
fi

# 获取当前用户（真实用户，不是root）
REAL_USER=${SUDO_USER:-$(whoami)}
USER_HOME=$(eval echo ~$REAL_USER)

echo "当前用户: $REAL_USER"
echo "用户目录: $USER_HOME"
echo ""

# 1. 下载Clash
echo "步骤1: 获取Clash Meta..."
echo "----------------------------------------"

# 保存当前目录
CURRENT_DIR=$(pwd)

# 在多个位置查找文件（支持多种文件名）
# 优先级：当前目录 > 用户目录 > /tmp（避免找到旧文件）
UPLOADED_FILE=""
SEARCH_DIRS=("$CURRENT_DIR" "$USER_HOME" "/tmp")

echo "正在查找已上传的文件..."
for search_dir in "${SEARCH_DIRS[@]}"; do
    if [ -d "$search_dir" ]; then
        # 优先查找 mihomo 开头的文件（更新版本）
        found_file=$(find "$search_dir" -maxdepth 1 -type f -name "mihomo*.gz" 2>/dev/null | head -n 1)
        if [ -z "$found_file" ]; then
            # 如果没找到 mihomo，再找 clash
            found_file=$(find "$search_dir" -maxdepth 1 -type f -name "clash*.gz" 2>/dev/null | head -n 1)
        fi
        
        if [ -n "$found_file" ] && [ -f "$found_file" ]; then
            # 检查文件大小是否合理（大于5MB）
            test_size=$(stat -c%s "$found_file" 2>/dev/null || stat -f%z "$found_file" 2>/dev/null)
            if [ -n "$test_size" ] && [ "$test_size" -gt 5000000 ]; then
                UPLOADED_FILE="$found_file"
                echo "   在 $search_dir 找到有效文件: $(basename $found_file)"
                break
            else
                echo "   在 $search_dir 找到文件但过小，跳过"
            fi
        fi
    fi
done

cd /tmp

if [ -n "$UPLOADED_FILE" ] && [ -f "$UPLOADED_FILE" ]; then
    echo "✅ 检测到已上传的文件: $(basename $UPLOADED_FILE)"
    echo "   完整路径: $UPLOADED_FILE"
    FILE_SIZE=$(stat -c%s "$UPLOADED_FILE" 2>/dev/null || stat -f%z "$UPLOADED_FILE" 2>/dev/null)
    
    if [ -z "$FILE_SIZE" ] || [ "$FILE_SIZE" -eq 0 ]; then
        echo "   ⚠️ 无法获取文件大小或文件为空"
        DOWNLOAD_SUCCESS=0
    elif [ "$FILE_SIZE" -gt 5000000 ]; then
        echo "   文件大小: $((FILE_SIZE/1024/1024))MB - 正常"
        echo "   跳过下载步骤"
        # 复制或移动到 /tmp/clash.gz 方便后续处理
        if [ "$UPLOADED_FILE" != "/tmp/clash.gz" ]; then
            echo "   复制到 /tmp/clash.gz..."
            cp "$UPLOADED_FILE" /tmp/clash.gz
        fi
        echo ""
        # 直接跳到解压步骤
        DOWNLOAD_SUCCESS=1
    else
        echo "   ⚠️ 文件过小($((FILE_SIZE/1024))KB)，将重新下载"
        DOWNLOAD_SUCCESS=0
    fi
else
    echo "未检测到上传的文件"
    echo ""
    echo "💡 提示: 如果服务器网络不好，可以先在本地下载后上传"
    echo "   1. 下载地址(兼容版本，支持老旧CPU):"
    echo "      - https://github.com/MetaCubeX/mihomo/releases/download/v1.18.10/mihomo-linux-amd64-compatible-v1.18.10.gz"
    echo "   2. 上传到 /tmp/ 目录，文件名保持不变或改为 clash.gz 都可以"
    echo "   3. 重新运行此脚本"
    echo ""
    echo "现在将自动从网络下载..."
    echo ""
    sleep 2
    DOWNLOAD_SUCCESS=0
fi

# 如果没有本地文件，则进行网络下载
if [ $DOWNLOAD_SUCCESS -eq 0 ]; then
    # 清理旧文件
    rm -f clash.gz clash 2>/dev/null
fi

# 只有在没有本地文件时才下载
if [ $DOWNLOAD_SUCCESS -eq 0 ]; then
    # 定义多个下载源 (使用兼容版本，支持老旧CPU)
    declare -a DOWNLOAD_URLS=(
        "https://github.com/MetaCubeX/mihomo/releases/download/v1.18.10/mihomo-linux-amd64-compatible-v1.18.10.gz"
        "https://ghproxy.com/https://github.com/MetaCubeX/mihomo/releases/download/v1.18.10/mihomo-linux-amd64-compatible-v1.18.10.gz"
        "https://mirror.ghproxy.com/https://github.com/MetaCubeX/mihomo/releases/download/v1.18.10/mihomo-linux-amd64-compatible-v1.18.10.gz"
        "https://ghps.cc/https://github.com/MetaCubeX/mihomo/releases/download/v1.18.10/mihomo-linux-amd64-compatible-v1.18.10.gz"
        "https://gh.api.99988866.xyz/https://github.com/MetaCubeX/mihomo/releases/download/v1.18.10/mihomo-linux-amd64-compatible-v1.18.10.gz"
    )

    # 尝试每个下载源
    for i in "${!DOWNLOAD_URLS[@]}"; do
        URL="${DOWNLOAD_URLS[$i]}"
        echo "尝试下载源 $((i+1))/${#DOWNLOAD_URLS[@]}..."
        echo "URL: $URL"
        
        # 尝试3次
        for retry in {1..3}; do
            echo "  第 $retry 次尝试..."
            
            # 使用wget下载，显示进度，超时30秒
            wget --timeout=30 --tries=1 --no-check-certificate -O clash.gz "$URL" 2>&1 | grep -E "saved|failed|error" || true
            
            # 检查文件是否下载成功且大小合理（至少5MB）
            if [ -f clash.gz ]; then
                FILE_SIZE=$(stat -c%s clash.gz 2>/dev/null || stat -f%z clash.gz 2>/dev/null)
                if [ "$FILE_SIZE" -gt 5000000 ]; then
                    echo "  ✅ 下载成功！文件大小: $((FILE_SIZE/1024/1024))MB"
                    DOWNLOAD_SUCCESS=1
                    break 2
                else
                    echo "  ⚠️ 文件过小($FILE_SIZE bytes)，可能下载不完整"
                    rm -f clash.gz
                fi
            else
                echo "  ⚠️ 下载失败"
            fi
            
            sleep 2
        done
        
        if [ $DOWNLOAD_SUCCESS -eq 1 ]; then
            break
        fi
        
        echo ""
    done

    # 检查是否下载成功
    if [ $DOWNLOAD_SUCCESS -eq 0 ]; then
        echo ""
        echo "❌ 所有下载源都失败了！"
        echo ""
        echo "建议手动下载："
        echo "1. 在本地电脑下载: https://github.com/MetaCubeX/mihomo/releases/download/v1.18.10/mihomo-linux-amd64-compatible-v1.18.10.gz"
        echo "2. 上传到服务器: scp mihomo-linux-amd64-compatible-v1.18.10.gz 用户名@服务器IP:/tmp/clash.gz"
        echo "3. 重新运行此脚本"
        echo ""
        exit 1
    fi
fi

echo ""
echo "解压文件..."

# 解压
gunzip -f clash.gz 2>/dev/null

if [ $? -ne 0 ]; then
    echo "❌ 解压失败！文件可能损坏"
    echo "正在清理并重新尝试..."
    rm -f clash.gz clash mihomo-linux-amd64-*
    
    # 使用curl再试一次
    echo "使用curl工具重试..."
    curl -L --max-time 60 --retry 3 --retry-delay 2 -o clash.gz "https://github.com/MetaCubeX/mihomo/releases/download/v1.18.10/mihomo-linux-amd64-compatible-v1.18.10.gz"
    
    if [ -f clash.gz ]; then
        gunzip -f clash.gz 2>/dev/null
        if [ $? -ne 0 ]; then
            echo "❌ 再次解压失败"
            exit 1
        fi
    else
        echo "❌ curl下载也失败"
        exit 1
    fi
fi

# 检查解压后的文件（可能是clash或mihomo开头）
EXTRACTED_FILE=""
if [ -f clash ]; then
    EXTRACTED_FILE="clash"
elif [ -f mihomo-linux-amd64-v1.18.10 ]; then
    EXTRACTED_FILE="mihomo-linux-amd64-v1.18.10"
elif [ -f mihomo ]; then
    EXTRACTED_FILE="mihomo"
else
    # 尝试查找任何解压出来的文件
    EXTRACTED_FILE=$(ls | grep -E "clash|mihomo" | head -n 1)
fi

if [ -z "$EXTRACTED_FILE" ] || [ ! -f "$EXTRACTED_FILE" ]; then
    echo "❌ 解压后找不到可执行文件"
    ls -la
    exit 1
fi

echo "找到文件: $EXTRACTED_FILE"

# 检查文件大小
CLASH_SIZE=$(stat -c%s "$EXTRACTED_FILE" 2>/dev/null || stat -f%z "$EXTRACTED_FILE" 2>/dev/null)
if [ "$CLASH_SIZE" -lt 5000000 ]; then
    echo "❌ 文件过小($CLASH_SIZE bytes)，可能不完整"
    exit 1
fi

# 移动并重命名为clash
mv "$EXTRACTED_FILE" /usr/local/bin/clash
chmod +x /usr/local/bin/clash

# 验证安装
if [ ! -x /usr/local/bin/clash ]; then
    echo "❌ Clash安装失败"
    exit 1
fi

echo "✅ Clash下载并安装完成！"
echo "   文件位置: /usr/local/bin/clash"
echo "   文件大小: $((CLASH_SIZE/1024/1024))MB"
echo ""

# 2. 创建配置目录
echo "步骤2: 创建配置目录..."
echo "----------------------------------------"

mkdir -p /etc/clash
chown $REAL_USER:$REAL_USER /etc/clash

echo "✅ 配置目录创建完成: /etc/clash"
echo ""

# 3. 配置文件
echo "步骤3: 配置Clash..."
echo "----------------------------------------"
echo ""
echo "请选择配置方式："
echo "  1) 使用订阅链接"
echo "  2) 稍后手动上传配置文件"
echo ""
read -p "请选择 (1/2): " -n 1 -r CONFIG_METHOD
echo ""

if [ "$CONFIG_METHOD" == "1" ]; then
    echo ""
    read -p "请输入订阅链接: " SUBSCRIBE_URL
    
    if [ ! -z "$SUBSCRIBE_URL" ]; then
        echo "正在下载配置..."
        wget -O /etc/clash/config.yaml "$SUBSCRIBE_URL"
        
        if [ $? -eq 0 ]; then
            chown $REAL_USER:$REAL_USER /etc/clash/config.yaml
            echo "✅ 配置文件下载成功"
        else
            echo "❌ 下载失败"
            CONFIG_METHOD="2"
        fi
    fi
fi

if [ "$CONFIG_METHOD" == "2" ]; then
    echo ""
    echo "请手动上传配置文件到: /etc/clash/config.yaml"
    echo ""
    echo "在本地执行："
    echo "  scp config.yaml zx@192.168.1.233:/tmp/"
    echo "  ssh zx@192.168.1.233 'sudo mv /tmp/config.yaml /etc/clash/'"
    echo ""
    
    # 创建一个示例配置
    cat > /etc/clash/config.yaml << 'EOF'
# Clash 配置文件示例
# 请替换为你自己的配置

port: 7890
socks-port: 7891
allow-lan: true
mode: rule
log-level: info
external-controller: 127.0.0.1:9090

proxies: []

proxy-groups: []

rules:
  - MATCH,DIRECT
EOF
    
    chown $REAL_USER:$REAL_USER /etc/clash/config.yaml
    
    echo "⚠️ 已创建示例配置文件，请替换为你自己的配置"
    echo ""
    read -p "配置文件准备好后，按回车继续..."
fi

echo ""

# 4. 创建systemd服务
echo "步骤4: 创建系统服务..."
echo "----------------------------------------"

cat > /etc/systemd/system/clash.service << EOF
[Unit]
Description=Clash Proxy Service
After=network.target

[Service]
Type=simple
User=$REAL_USER
ExecStart=/usr/local/bin/clash -d /etc/clash
Restart=always
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

echo "✅ 系统服务创建完成"
echo ""

# 5. 启动Clash
echo "步骤5: 启动Clash..."
echo "----------------------------------------"

systemctl start clash
systemctl enable clash

sleep 2

# 检查状态
if systemctl is-active --quiet clash; then
    echo "✅ Clash 启动成功！"
    
    # 获取配置的端口
    HTTP_PORT=$(grep "^port:" /etc/clash/config.yaml 2>/dev/null | awk '{print $2}')
    SOCKS_PORT=$(grep "^socks-port:" /etc/clash/config.yaml 2>/dev/null | awk '{print $2}')
    
    HTTP_PORT=${HTTP_PORT:-7890}
    SOCKS_PORT=${SOCKS_PORT:-7891}
    
    echo ""
    echo "=========================================="
    echo "  ✅ Clash 安装完成！"
    echo "=========================================="
    echo ""
    echo "代理信息:"
    echo "  HTTP代理: http://127.0.0.1:$HTTP_PORT"
    echo "  SOCKS5代理: socks5://127.0.0.1:$SOCKS_PORT"
    echo ""
    echo "管理命令:"
    echo "  查看状态: systemctl status clash"
    echo "  启动: systemctl start clash"
    echo "  停止: systemctl stop clash"
    echo "  重启: systemctl restart clash"
    echo "  查看日志: journalctl -u clash -f"
    echo ""
    echo "验证代理:"
    echo "  curl -x http://127.0.0.1:$HTTP_PORT https://www.google.com"
    echo ""
    echo "配置文件位置:"
    echo "  /etc/clash/config.yaml"
    echo ""
    echo "=========================================="
    echo "  下一步: 部署FireCrawl"
    echo "=========================================="
    echo ""
    echo "1. 上传 firecrawl.tar.gz 到 /tmp/"
    echo "2. 解压并进入目录"
    echo "3. 运行: sudo bash setup.sh"
    echo ""
    echo "setup.sh 会自动配置使用Clash代理（端口$HTTP_PORT）"
    echo ""
    
else
    echo "❌ Clash 启动失败"
    echo ""
    echo "查看错误日志:"
    echo "  journalctl -u clash -n 50"
    echo ""
    echo "可能的原因:"
    echo "  1. 配置文件格式错误"
    echo "  2. 配置文件不存在"
    echo "  3. 端口被占用"
    echo ""
fi

