#!/bin/bash
set -e

# 颜色定义
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# 检查 root 权限
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}错误：请使用 root 用户或 sudo 执行此脚本！${NC}"
  exit 1
fi

# 定义 GitHub Raw 基础路径 (指向你的仓库)
GITHUB_RAW_URL="https://raw.githubusercontent.com/JBl9527/mihomo-ui/main"

while true; do
    echo -e "\n${CYAN}=================================================${NC}"
    echo -e "${GREEN}        Mihomo & Web UI 综合安装管理工具         ${NC}"
    echo -e "${CYAN}=================================================${NC}"
    echo -e "  ${YELLOW}1.${NC} 一键安装/覆盖更新 Mihomo-UI 完整环境"
    echo -e "  ${YELLOW}2.${NC} 更新本管理脚本 (自动拉取最新 mihomo_tool.sh)"
    echo -e "  ${YELLOW}0.${NC} 退出脚本"
    echo -e "${CYAN}=================================================${NC}"
    read -p "请输入选项 [0-2]: " choice

    case $choice in
        1)
            echo -e "\n${GREEN}[1] 开始一键安装/部署完整环境...${NC}"
            
            echo -e "${CYAN}>> 1. 安装基础依赖...${NC}"
            apt update -y > /dev/null 2>&1
            apt install -y curl wget unzip gzip python3 python3-venv python3-pip > /dev/null 2>&1

            echo -e "${CYAN}>> 2. 部署 Mihomo 内核...${NC}"
            mkdir -p /etc/mihomo/ui
            wget -O /tmp/mihomo.gz https://github.com/MetaCubeX/mihomo/releases/download/v1.18.7/mihomo-linux-amd64-compatible-v1.18.7.gz
            gunzip -f /tmp/mihomo.gz
            mv /tmp/mihomo /usr/local/bin/mihomo
            chmod +x /usr/local/bin/mihomo
            
            # 生成防崩溃初始配置 (如果不存在)
            if [ ! -f "/etc/mihomo/config.yaml" ]; then
                echo -e "port: 7890\nallow-lan: true\nexternal-controller: 0.0.0.0:9090\nsecret: \"123456\"\nexternal-ui: ui" > /etc/mihomo/config.yaml
            fi
            
            cat << 'EOF' > /etc/systemd/system/mihomo.service
[Unit]
Description=Mihomo Daemon
After=network.target

[Service]
Type=simple
LimitNPROC=500
LimitNOFILE=1048576
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_TIME CAP_SYS_PTRACE CAP_DAC_READ_SEARCH
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_TIME CAP_SYS_PTRACE CAP_DAC_READ_SEARCH
Restart=always
ExecStart=/usr/local/bin/mihomo -d /etc/mihomo
ExecReload=/bin/kill -HUP $MAINPID

[Install]
WantedBy=multi-user.target
EOF

            echo -e "${CYAN}>> 3. 部署 Zashboard 面板...${NC}"
            wget -O /tmp/zashboard.zip https://github.com/Zephyruso/zashboard/releases/latest/download/dist-no-fonts.zip
            unzip -o /tmp/zashboard.zip -d /etc/mihomo/ui/ > /dev/null 2>&1
            rm /tmp/zashboard.zip

            echo -e "${CYAN}>> 4. 部署 Python 后端控制台...${NC}"
            mkdir -p /opt/mihomo_manager/templates
            cd /opt/mihomo_manager
            
            # 如果没有 venv 环境则创建
            if [ ! -d "venv" ]; then
                python3 -m venv venv
            fi
            
            cat << 'EOF' > requirements.txt
fastapi
uvicorn
requests
pyyaml
jinja2
EOF
            ./venv/bin/pip install -r requirements.txt > /dev/null 2>&1
            
            echo -e "${CYAN}>> 5. 从 GitHub 拉取最新后端与 UI 代码...${NC}"
            wget -O /opt/mihomo_manager/main.py "${GITHUB_RAW_URL}/main.py"
            wget -O /opt/mihomo_manager/templates/index.html "${GITHUB_RAW_URL}/templates/index.html"
            
            cat << 'EOF' > /etc/systemd/system/mihomo-web.service
[Unit]
Description=Mihomo Web Manager Backend
After=network.target mihomo.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/mihomo_manager
ExecStart=/opt/mihomo_manager/venv/bin/python main.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

            echo -e "${CYAN}>> 6. 启动并注册服务...${NC}"
            systemctl daemon-reload
            systemctl enable --now mihomo > /dev/null 2>&1
            systemctl enable --now mihomo-web > /dev/null 2>&1
            systemctl restart mihomo-web
            
            echo -e "${GREEN}=========================================${NC}"
            echo -e "${GREEN}🎉 部署与更新完成！${NC}"
            echo -e "Web 控制台: ${YELLOW}http://$(hostname -I | awk '{print $1}'):9621${NC}"
            echo -e "${GREEN}=========================================${NC}"
            ;;
        2)
            echo -e "\n${GREEN}[2] 正在从 GitHub 拉取最新脚本...${NC}"
            # 自动获取当前执行的脚本路径
            SCRIPT_PATH=$(readlink -f "$0")
            
            # 覆盖下载
            wget -O "$SCRIPT_PATH" "${GITHUB_RAW_URL}/mihomo_tool.sh"
            chmod +x "$SCRIPT_PATH"
            
            echo -e "${GREEN}✅ 脚本更新完毕！正在自动重启脚本...${NC}"
            sleep 1
            # 使用 exec 完美重启脚本当前进程
            exec bash "$SCRIPT_PATH"
            ;;
        0)
            echo -e "${GREEN}退出脚本，感谢使用！${NC}"
            exit 0
            ;;
        *)
            echo -e "${RED}无效选项，请输入 0-2 之间的数字。${NC}"
            ;;
    esac
    
    echo ""
    read -p "按回车键返回主菜单..."
done
