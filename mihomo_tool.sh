#!/bin/bash
set -e

# 颜色定义，让菜单更好看
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 检查 root 权限
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}错误：请使用 root 用户或 sudo 执行此脚本！${NC}"
  exit 1
fi

# 安装基础依赖
install_deps() {
    echo -e "${CYAN}正在检查并安装基础依赖 (curl, unzip, python3)...${NC}"
    apt update -y > /dev/null 2>&1
    apt install -y curl wget unzip gzip python3 python3-venv python3-pip > /dev/null 2>&1
}

# 菜单循环
while true; do
    echo -e "\n${CYAN}=================================================${NC}"
    echo -e "${GREEN}        Mihomo & Web UI 综合安装管理工具         ${NC}"
    echo -e "${CYAN}=================================================${NC}"
    echo -e "  ${YELLOW}1.${NC} 安装 Mihomo 内核"
    echo -e "  ${YELLOW}2.${NC} 安装 Zashboard 面板"
    echo -e "  ${YELLOW}3.${NC} 导入静态配置文件 (直接拉取 config.yaml 直链)"
    echo -e "  ${YELLOW}4.${NC} 部署后端管理环境 (准备处理机场链接与模板)"
    echo -e "  ${YELLOW}5.${NC} 启动/重启 Mihomo 服务"
    echo -e "  ${YELLOW}6.${NC} 启动后端管理 UI (端口: 9621)"
    echo -e "  ${YELLOW}0.${NC} 退出脚本"
    echo -e "${CYAN}=================================================${NC}"
    read -p "请输入选项 [0-6]: " choice

    case $choice in
        1)
            echo -e "\n${GREEN}[1] 正在安装 Mihomo 内核...${NC}"
            install_deps
            mkdir -p /etc/mihomo
            wget -O /tmp/mihomo.gz https://github.com/MetaCubeX/mihomo/releases/download/v1.18.7/mihomo-linux-amd64-compatible-v1.18.7.gz
            gunzip -f /tmp/mihomo.gz
            mv /tmp/mihomo /usr/local/bin/mihomo
            chmod +x /usr/local/bin/mihomo
            
            # 写入 systemd 守护进程
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
            systemctl daemon-reload
            echo -e "${GREEN}Mihomo 内核安装成功！${NC}"
            ;;
        2)
            echo -e "\n${GREEN}[2] 正在安装 Zashboard 面板...${NC}"
            install_deps
            mkdir -p /etc/mihomo/ui
            wget -O /tmp/zashboard.zip https://github.com/Zephyruso/zashboard/releases/latest/download/dist-no-fonts.zip
            unzip -o /tmp/zashboard.zip -d /etc/mihomo/ui/
            rm /tmp/zashboard.zip
            echo -e "${GREEN}Zashboard 面板解压成功！存放在 /etc/mihomo/ui/${NC}"
            ;;
        3)
            echo -e "\n${GREEN}[3] 导入静态配置文件${NC}"
            read -p "请输入 config.yaml 的直链地址 (URL): " config_url
            if [ -n "$config_url" ]; then
                wget -O /etc/mihomo/config.yaml "$config_url"
                echo -e "${GREEN}配置文件已成功下载并覆盖到 /etc/mihomo/config.yaml${NC}"
            else
                echo -e "${RED}地址不能为空，已取消。${NC}"
            fi
            ;;
        4)
            echo -e "\n${GREEN}[4] 部署后端管理环境 (Python FastAPI)...${NC}"
            install_deps
            mkdir -p /opt/mihomo_manager
            cd /opt/mihomo_manager
            
            # 创建虚拟环境
            python3 -m venv venv
            
            # 临时生成 requirements.txt 并安装
            cat << 'EOF' > requirements.txt
fastapi
uvicorn
requests
pyyaml
jinja2
EOF
            ./venv/bin/pip install -r requirements.txt
            
            # 从你的 GitHub 拉取最新代码
            echo -e "${CYAN}正在从 GitHub 下载最新版本代码...${NC}"
            wget -O /opt/mihomo_manager/main.py https://raw.githubusercontent.com/JBl9527/mihomo-ui/main/main.py
            wget -O /opt/mihomo_manager/templates/index.html https://raw.githubusercontent.com/JBl9527/mihomo-ui/main/templates/index.html
            
            echo -e "${GREEN}后端与 UI 部署完毕！可以使用选项 6 启动了。${NC}"
            ;;
        5)
            echo -e "\n${GREEN}[5] 启动/重启 Mihomo 服务...${NC}"
            if [ ! -f "/etc/mihomo/config.yaml" ]; then
                echo -e "${RED}警告：未找到 /etc/mihomo/config.yaml！请先使用选项 3 或 4 导入配置。${NC}"
            else
                systemctl enable mihomo > /dev/null 2>&1
                systemctl restart mihomo
                echo -e "${GREEN}Mihomo 服务已启动！${NC}"
                echo -e "你可以通过 http://$(hostname -I | awk '{print $1}'):9090/ui/ 访问 Zashboard 面板。"
            fi
            ;;
        6)
            echo -e "\n${GREEN}[6] 启动后端管理 UI (9621端口)...${NC}"
            # 写入后端 systemd 守护进程
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
            systemctl daemon-reload
            systemctl enable mihomo-web > /dev/null 2>&1
            systemctl restart mihomo-web
            echo -e "${GREEN}后端管理 UI 已启动！${NC}"
            echo -e "${CYAN}请在浏览器访问: http://$(hostname -I | awk '{print $1}'):9621${NC}"
            ;;
        0)
            echo -e "${GREEN}退出脚本，感谢使用！${NC}"
            exit 0
            ;;
        *)
            echo -e "${RED}无效选项，请输入 0-6 之间的数字。${NC}"
            ;;
    esac
    
    # 暂停一下，让用户看清输出信息
    echo ""
    read -p "按回车键返回主菜单..."
done
