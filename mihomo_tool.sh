#!/bin/sh
# =====================================================================
#  Mihomo 控制台 · 安装与管理脚本
#  纯 POSIX sh，无 bash 依赖：
#    Debian / Ubuntu / Armbian、RHEL / CentOS / Fedora、Arch、openSUSE、
#    Alpine（OpenRC）、OpenWrt（procd）、以及没有 init 的容器都能跑。
#  用法：
#    sh mihomo_tool.sh                 交互菜单
#    sh mihomo_tool.sh install         安装 / 覆盖更新
#    sh mihomo_tool.sh status          查看状态
#    sh mihomo_tool.sh password [新密码]
#    sh mihomo_tool.sh start|stop|restart|uninstall|self-update
# =====================================================================
set -u

PANEL_DIR="${PANEL_DIR:-/opt/mihomo-panel}"
MIHOMO_DIR="${MIHOMO_DIR:-/etc/mihomo}"
STATE_DIR="${STATE_DIR:-/etc/mihomo-panel}"
MIHOMO_BIN="${MIHOMO_BIN:-/usr/local/bin/mihomo}"
PANEL_PORT="${PANEL_PORT:-9621}"
CTRL_PORT="${CTRL_PORT:-9090}"
REPO="${REPO:-JBl9527/mihomo-ui}"
BRANCH="${BRANCH:-main}"
KERNEL_FALLBACK="v1.18.7"
TMP="${TMPDIR:-/tmp}/mihomo-tool.$$"

# GitHub 直连不通时依次套这些镜像前缀（拼在完整 URL 前面）
MIRRORS="direct https://ghfast.top/ https://gh-proxy.com/ https://ghproxy.net/"

if [ -t 1 ]; then
  C_G='\033[0;32m'; C_C='\033[0;36m'; C_Y='\033[1;33m'; C_R='\033[0;31m'; C_D='\033[0;90m'; C_0='\033[0m'
else
  C_G=''; C_C=''; C_Y=''; C_R=''; C_D=''; C_0=''
fi

say()  { printf "%b\n" "$*"; }
step() { printf "%b\n" "${C_C}>>${C_0} $*"; }
ok()   { printf "%b\n" "${C_G}✓${C_0} $*"; }
warn() { printf "%b\n" "${C_Y}!${C_0} $*"; }
oops() { printf "%b\n" "${C_R}✗${C_0} $*" >&2; }
die()  { oops "$*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
ask()  { printf "%b" "$1"; read -r REPLY_RAW || REPLY_RAW=""; }

cleanup() { [ -n "${TMP:-}" ] && rm -rf "$TMP" 2>/dev/null; }
trap cleanup EXIT INT TERM

# --------------------------------------------------------------- 下载
fetch() { # fetch <url> <目标文件>
  _url=$1; _out=$2
  for _m in $MIRRORS; do
    if [ "$_m" = direct ]; then _real=$_url; else _real="$_m$_url"; fi
    if have curl; then
      curl -fsSL --connect-timeout 8 --retry 1 -o "$_out.part" "$_real" 2>/dev/null && \
        mv "$_out.part" "$_out" && return 0
    elif have wget; then
      wget -q -T 12 -O "$_out.part" "$_real" 2>/dev/null && \
        mv "$_out.part" "$_out" && return 0
    else
      die "系统里既没有 curl 也没有 wget，请先装一个"
    fi
    rm -f "$_out.part"
    [ "$_m" = direct ] && printf "%b\n" "${C_D}  直连失败，换镜像重试…${C_0}"
  done
  return 1
}

grab() { # grab <仓库内相对路径> <目标文件>
  fetch "https://raw.githubusercontent.com/$REPO/$BRANCH/$1" "$2"
}

# --------------------------------------------------- 包管理器 / 依赖
pkg_manager() {
  for _p in apt-get dnf yum pacman apk zypper opkg emerge; do
    have "$_p" && { echo "$_p"; return; }
  done
  echo none
}

pkg_install() { # pkg_install <包名...>，失败不致命
  [ $# -eq 0 ] && return 0
  case "$(pkg_manager)" in
    apt-get) DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" >/dev/null 2>&1 ;;
    dnf)     dnf install -y "$@" >/dev/null 2>&1 ;;
    yum)     yum install -y "$@" >/dev/null 2>&1 ;;
    pacman)  pacman -Sy --noconfirm --needed "$@" >/dev/null 2>&1 ;;
    apk)     apk add --no-cache "$@" >/dev/null 2>&1 ;;
    zypper)  zypper --non-interactive install -y "$@" >/dev/null 2>&1 ;;
    opkg)    opkg install "$@" >/dev/null 2>&1 ;;
    emerge)  emerge --quiet "$@" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

pkg_refresh() {
  case "$(pkg_manager)" in
    apt-get) DEBIAN_FRONTEND=noninteractive apt-get update -y >/dev/null 2>&1 ;;
    apk)     apk update >/dev/null 2>&1 ;;
    opkg)    opkg update >/dev/null 2>&1 ;;
    pacman)  pacman -Sy --noconfirm >/dev/null 2>&1 ;;
  esac
  return 0
}

# 名字在各发行版里不一样，逐个试
pkg_try() { # pkg_try <候选包名...>：装上任意一个就算成功
  for _c in "$@"; do
    pkg_install "$_c" && return 0
  done
  return 1
}

python_bin() {
  for _c in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python; do
    have "$_c" || continue
    "$_c" - <<'PY' >/dev/null 2>&1 || continue
import sys
sys.exit(0 if sys.version_info[:2] >= (3, 8) else 1)
PY
    echo "$_c"; return 0
  done
  return 1
}

ensure_python() {
  PY_BIN=$(python_bin) && return 0
  step "安装 Python 3…"
  pkg_refresh
  pkg_try python3 python || true
  PY_BIN=$(python_bin) || die "没能装上 Python 3.8+，请手动安装后重跑（OpenWrt: opkg install python3-light）"
  return 0
}

ensure_pyyaml() {
  "$PY_BIN" -c 'import yaml' >/dev/null 2>&1 && { ok "PyYAML 已就绪"; return 0; }
  step "安装 PyYAML（面板唯一的第三方依赖）…"
  pkg_try python3-yaml py3-yaml python3-pyyaml python-yaml >/dev/null 2>&1 || true
  "$PY_BIN" -c 'import yaml' >/dev/null 2>&1 && { ok "PyYAML 已就绪（系统包）"; return 0; }
  for _args in "--break-system-packages pyyaml" "pyyaml"; do
    # shellcheck disable=SC2086
    "$PY_BIN" -m pip install $_args >/dev/null 2>&1 || true
    "$PY_BIN" -c 'import yaml' >/dev/null 2>&1 && { ok "PyYAML 已就绪（pip）"; return 0; }
  done
  die "PyYAML 装不上：请手动执行 $PY_BIN -m pip install pyyaml（或装系统包 python3-yaml）"
}

# ----------------------------------------------------------- 架构识别
kernel_asset() {
  _m=$(uname -m 2>/dev/null || echo unknown)
  case "$_m" in
    x86_64|amd64)
      if grep -qm1 ' avx2 ' /proc/cpuinfo 2>/dev/null; then echo amd64; else echo amd64-compatible; fi ;;
    i386|i486|i586|i686) echo 386 ;;
    aarch64|arm64|armv8b) echo arm64 ;;
    armv7*|armv8l) echo armv7 ;;
    armv6*) echo armv6 ;;
    armv5*|arm) echo armv5 ;;
    mips64el|mips64le) echo mips64le ;;
    mips64) echo mips64 ;;
    mipsel|mipsle) echo mipsle-softfloat ;;
    mips)
      # 大小端靠 Python 判，OpenWrt 上 uname 常常统一报 mips
      if [ "$("$PY_BIN" -c 'import sys;print(sys.byteorder)' 2>/dev/null)" = little ]; then
        echo mipsle-softfloat
      else
        echo mips-softfloat
      fi ;;
    riscv64) echo riscv64 ;;
    loongarch64|loong64) echo loong64 ;;
    s390x) echo s390x ;;
    *) echo "" ;;
  esac
}

kernel_version() {
  _v=""
  if fetch "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest" "$TMP/rel.json"; then
    _v=$(sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$TMP/rel.json" | head -n1)
  fi
  case "$_v" in v*) echo "$_v" ;; *) echo "$KERNEL_FALLBACK" ;; esac
}

install_kernel() {
  _asset=$(kernel_asset)
  [ -n "$_asset" ] || die "识别不出 CPU 架构（uname -m = $(uname -m)），请手动放置内核到 $MIHOMO_BIN"
  _ver=$(kernel_version)
  step "下载 Mihomo 内核 $_ver（$_asset）…"
  _url="https://github.com/MetaCubeX/mihomo/releases/download/$_ver/mihomo-linux-$_asset-$_ver.gz"
  fetch "$_url" "$TMP/mihomo.gz" || die "内核下载失败，检查网络或换个镜像（可 export MIRRORS=…）"
  gzip -dc "$TMP/mihomo.gz" > "$TMP/mihomo" 2>/dev/null || die "内核解压失败（需要 gzip/gunzip）"
  [ -s "$TMP/mihomo" ] || die "内核文件是空的，下载可能被劫持"
  mkdir -p "$(dirname "$MIHOMO_BIN")"
  install -m 0755 "$TMP/mihomo" "$MIHOMO_BIN" 2>/dev/null || {
    cp -f "$TMP/mihomo" "$MIHOMO_BIN" && chmod 0755 "$MIHOMO_BIN"; }
  ok "内核已装到 $MIHOMO_BIN（$("$MIHOMO_BIN" -v 2>/dev/null | head -n1)）"
}

# ------------------------------------------------------- Geo / 初始配置
install_geo() {
  step "准备 Geo 数据（缺失时才下载）…"
  for _pair in "GeoSite.dat:geosite.dat" "GeoIP.dat:geoip.dat" "geoip.metadb:geoip.metadb"; do
    _dst=$MIHOMO_DIR/${_pair%%:*}; _src=${_pair##*:}
    [ -s "$_dst" ] && continue
    fetch "https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/$_src" "$_dst" \
      && ok "已下载 ${_pair%%:*}" || warn "${_pair%%:*} 下载失败，内核首次启动时会自己重试"
  done
}

seed_config() {
  [ -s "$MIHOMO_DIR/config.yaml" ] && return 0
  step "写入一份最小可启动配置…"
  cat > "$MIHOMO_DIR/config.yaml" <<EOF
mixed-port: 7890
allow-lan: true
bind-address: '*'
mode: rule
log-level: info
ipv6: false
external-controller: 0.0.0.0:$CTRL_PORT
external-ui: ui
secret: "$CTRL_SECRET"
profile:
  store-selected: true
  store-fake-ip: true
proxies: []
proxy-groups: []
rules:
  - MATCH,DIRECT
EOF
  chmod 0644 "$MIHOMO_DIR/config.yaml"
}

# ----------------------------------------------------------- 面板代码
PANEL_FILES="main.py
panel/__init__.py
panel/auth.py
panel/config.py
panel/httpd.py
panel/mihomo.py
panel/profiles.py
panel/routes.py
panel/supervisor.py
panel/util.py
web/index.html
web/login.html
web/style.css
web/app.js
web/gate.js"

install_panel_code() {
  step "拉取面板代码…"
  # 本地就有完整源码时直接用，省得联网（把脚本和源码一起 clone 下来的场景）
  _here=$(cd "$(dirname "$0")" 2>/dev/null && pwd)
  if [ -n "$_here" ] && [ -f "$_here/main.py" ] && [ -d "$_here/panel" ] && [ -d "$_here/web" ]; then
    mkdir -p "$PANEL_DIR"
    ( cd "$_here" && tar cf - main.py panel web ) | ( cd "$PANEL_DIR" && tar xf - ) \
      && { ok "已从本地目录 $_here 复制"; return 0; }
  fi
  mkdir -p "$TMP/src/panel" "$TMP/src/web"
  for _f in $PANEL_FILES; do
    grab "$_f" "$TMP/src/$_f" || die "下载 $_f 失败"
  done
  mkdir -p "$PANEL_DIR"
  rm -rf "$PANEL_DIR/panel" "$PANEL_DIR/web" "$PANEL_DIR/__pycache__"
  ( cd "$TMP/src" && tar cf - main.py panel web ) | ( cd "$PANEL_DIR" && tar xf - ) \
    || die "面板文件解包失败"
  ok "面板代码已就位：$PANEL_DIR"
}

install_zashboard() {
  [ -s "$MIHOMO_DIR/ui/index.html" ] && { ok "Zashboard 已存在，跳过"; return 0; }
  have unzip || pkg_try unzip >/dev/null 2>&1 || true
  have unzip || { warn "没有 unzip，跳过 Zashboard（面板本身不依赖它）"; return 0; }
  step "部署 Zashboard 高级面板…"
  fetch "https://github.com/Zephyruso/zashboard/releases/latest/download/dist-no-fonts.zip" "$TMP/zash.zip" || {
    warn "Zashboard 下载失败，跳过（不影响面板使用）"; return 0; }
  rm -rf "$TMP/zash"; mkdir -p "$TMP/zash"
  unzip -oq "$TMP/zash.zip" -d "$TMP/zash" 2>/dev/null || { warn "Zashboard 解压失败，跳过"; return 0; }
  _root=$TMP/zash
  [ -f "$TMP/zash/dist/index.html" ] && _root=$TMP/zash/dist
  [ -f "$_root/index.html" ] || { warn "Zashboard 包结构异常，跳过"; return 0; }
  rm -rf "$MIHOMO_DIR/ui"; mkdir -p "$MIHOMO_DIR/ui"
  ( cd "$_root" && tar cf - . ) | ( cd "$MIHOMO_DIR/ui" && tar xf - ) && ok "Zashboard 已部署"
}

# --------------------------------------------------------- init 系统
init_kind() {
  # 想强制某种方式时可以 export FORCE_INIT=systemd|openrc|procd|none
  [ -n "${FORCE_INIT:-}" ] && { echo "$FORCE_INIT"; return; }
  if have systemctl && [ -d /run/systemd/system ]; then echo systemd; return; fi
  if [ -x /sbin/procd ] && [ -f /etc/rc.common ]; then echo procd; return; fi
  if have rc-update && have rc-service; then echo openrc; return; fi
  echo none
}

write_systemd() {
  cat > /etc/systemd/system/mihomo.service <<EOF
[Unit]
Description=Mihomo Kernel
After=network.target nss-lookup.target
[Service]
Type=simple
LimitNPROC=500
LimitNOFILE=1048576
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_TIME CAP_SYS_PTRACE CAP_DAC_READ_SEARCH
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW CAP_NET_BIND_SERVICE CAP_SYS_TIME CAP_SYS_PTRACE CAP_DAC_READ_SEARCH
ExecStart=$MIHOMO_BIN -d $MIHOMO_DIR
ExecReload=/bin/kill -HUP \$MAINPID
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
  cat > /etc/systemd/system/mihomo-panel.service <<EOF
[Unit]
Description=Mihomo Web Panel
After=network.target
[Service]
Type=simple
WorkingDirectory=$PANEL_DIR
Environment=MIHOMO_PANEL_CONF=$STATE_DIR/panel.json
Environment=PYTHONUNBUFFERED=1
ExecStart=$PY_BIN $PANEL_DIR/main.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload >/dev/null 2>&1
  systemctl enable mihomo mihomo-panel >/dev/null 2>&1
  ok "已注册 systemd 服务：mihomo / mihomo-panel"
}

write_openrc() {
  cat > /etc/init.d/mihomo <<EOF
#!/sbin/openrc-run
name="mihomo"
description="Mihomo Kernel"
command="$MIHOMO_BIN"
command_args="-d $MIHOMO_DIR"
command_background=true
pidfile="/run/mihomo.pid"
output_log="$STATE_DIR/mihomo.log"
error_log="$STATE_DIR/mihomo.log"
depend() { need net; }
EOF
  cat > /etc/init.d/mihomo-panel <<EOF
#!/sbin/openrc-run
name="mihomo-panel"
description="Mihomo Web Panel"
command="$PY_BIN"
command_args="$PANEL_DIR/main.py"
command_background=true
directory="$PANEL_DIR"
pidfile="/run/mihomo-panel.pid"
output_log="$STATE_DIR/panel.log"
error_log="$STATE_DIR/panel.log"
export MIHOMO_PANEL_CONF="$STATE_DIR/panel.json"
depend() { need net; }
EOF
  chmod 0755 /etc/init.d/mihomo /etc/init.d/mihomo-panel
  rc-update add mihomo default >/dev/null 2>&1
  rc-update add mihomo-panel default >/dev/null 2>&1
  ok "已注册 OpenRC 服务：mihomo / mihomo-panel"
}

write_procd() {
  cat > /etc/init.d/mihomo <<EOF
#!/bin/sh /etc/rc.common
START=95
STOP=10
USE_PROCD=1
start_service() {
  procd_open_instance
  procd_set_param command $MIHOMO_BIN -d $MIHOMO_DIR
  procd_set_param stdout 1
  procd_set_param stderr 1
  procd_set_param respawn 3600 5 0
  procd_set_param limits nofile="1048576 1048576"
  procd_close_instance
}
EOF
  cat > /etc/init.d/mihomo-panel <<EOF
#!/bin/sh /etc/rc.common
START=96
STOP=05
USE_PROCD=1
start_service() {
  procd_open_instance
  procd_set_param command $PY_BIN $PANEL_DIR/main.py
  procd_set_param env MIHOMO_PANEL_CONF=$STATE_DIR/panel.json PYTHONUNBUFFERED=1
  procd_set_param stdout 1
  procd_set_param stderr 1
  procd_set_param respawn 3600 5 0
  procd_close_instance
}
EOF
  chmod 0755 /etc/init.d/mihomo /etc/init.d/mihomo-panel
  /etc/init.d/mihomo enable >/dev/null 2>&1
  /etc/init.d/mihomo-panel enable >/dev/null 2>&1
  ok "已注册 procd 服务：mihomo / mihomo-panel"
}

# 没有任何 init 系统（比如精简容器）：写一个启动脚本，内核交给面板自己管
write_plain() {
  cat > "$STATE_DIR/start-panel.sh" <<EOF
#!/bin/sh
# 无 init 环境下的启动脚本：面板会自己把内核拉起来
export MIHOMO_PANEL_CONF="$STATE_DIR/panel.json"
cd "$PANEL_DIR" || exit 1
# 用绝对路径启动，方便 ps 里认出这个进程
exec "$PY_BIN" "$PANEL_DIR/main.py"
EOF
  chmod 0755 "$STATE_DIR/start-panel.sh"
  if have crontab; then
    _cron=$(crontab -l 2>/dev/null | grep -v 'start-panel.sh')
    printf '%s\n@reboot %s >> %s 2>&1\n' "$_cron" "$STATE_DIR/start-panel.sh" "$STATE_DIR/panel.log" \
      | crontab - >/dev/null 2>&1 && ok "已写入 crontab @reboot 自启"
  fi
  warn "没检测到 init 系统，用 $STATE_DIR/start-panel.sh 启动面板（内核由面板自管）"
}

install_services() {
  case "$(init_kind)" in
    systemd) write_systemd ;;
    openrc)  write_openrc ;;
    procd)   write_procd ;;
    *)       write_plain ;;
  esac
}

# ------------------------------------------------------------ 服务操作
plain_pid() { # 在无 init 环境里找面板进程
  { ps ax 2>/dev/null || ps -ef 2>/dev/null; } | grep -F "$PANEL_DIR/main.py" | grep -v grep \
    | awk '{print $1}' | head -n1
}

svc() { # svc <mihomo|mihomo-panel> <start|stop|restart>
  case "$(init_kind)" in
    systemd) systemctl "$2" "$1" >/dev/null 2>&1 ;;
    openrc)  rc-service "$1" "$2" >/dev/null 2>&1 ;;
    procd)   /etc/init.d/"$1" "$2" >/dev/null 2>&1 ;;
    *)
      [ "$1" = mihomo ] && return 0   # 无 init 时内核由面板自己管
      _pid=$(plain_pid)
      case "$2" in
        stop|restart) [ -n "$_pid" ] && kill "$_pid" 2>/dev/null; sleep 1 ;;
      esac
      case "$2" in
        start|restart)
          [ -n "$(plain_pid)" ] && return 0
          nohup "$STATE_DIR/start-panel.sh" >> "$STATE_DIR/panel.log" 2>&1 &
          sleep 1 ;;
      esac ;;
  esac
  return 0
}

svc_state() { # svc_state <服务名> → 运行中 / 已停止
  case "$(init_kind)" in
    systemd) systemctl is-active --quiet "$1" && { echo 运行中; return; } ;;
    openrc)  rc-service "$1" status >/dev/null 2>&1 && { echo 运行中; return; } ;;
    procd)
      _key=$MIHOMO_BIN; [ "$1" = mihomo-panel ] && _key=$PANEL_DIR/main.py
      { ps ax 2>/dev/null || ps -ef 2>/dev/null; } | grep -F "$_key" | grep -qv grep \
        && { echo 运行中; return; } ;;
    *)
      _key=$MIHOMO_BIN; [ "$1" = mihomo-panel ] && _key=$PANEL_DIR/main.py
      { ps ax 2>/dev/null || ps -ef 2>/dev/null; } | grep -F "$_key" | grep -qv grep \
        && { echo 运行中; return; } ;;
  esac
  echo 已停止
}

lan_ip() {
  _ip=$("$PY_BIN" - <<'PY' 2>/dev/null
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("223.5.5.5", 53)); print(s.getsockname()[0])
except OSError:
    print("127.0.0.1")
finally:
    s.close()
PY
)
  [ -n "$_ip" ] && echo "$_ip" || echo 127.0.0.1
}

# ------------------------------------------------------ 面板状态目录
rand_hex() {
  _n=${1:-16}
  if [ -r /dev/urandom ]; then
    _v=$(tr -dc 'a-f0-9' < /dev/urandom 2>/dev/null | head -c "$_n")
    [ -n "$_v" ] && { printf '%s' "$_v"; return 0; }
  fi
  "$PY_BIN" -c "import secrets;print(secrets.token_hex($_n // 2), end='')"
}

seed_state() {
  mkdir -p "$STATE_DIR" "$MIHOMO_DIR/profiles" "$MIHOMO_DIR/providers" "$MIHOMO_DIR/ui"
  chmod 0700 "$STATE_DIR" 2>/dev/null
  if [ -s "$STATE_DIR/panel.json" ]; then
    CTRL_SECRET=$("$PY_BIN" - "$STATE_DIR/panel.json" <<'PY' 2>/dev/null
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("controller_secret") or "")
except Exception:
    print("")
PY
)
    [ -n "$CTRL_SECRET" ] || CTRL_SECRET=$(rand_hex 16)
    ok "沿用已有面板配置 $STATE_DIR/panel.json（密码不变）"
    return 0
  fi
  CTRL_SECRET=$(rand_hex 16)
  cat > "$STATE_DIR/panel.json" <<EOF
{
  "host": "0.0.0.0",
  "port": $PANEL_PORT,
  "mihomo_dir": "$MIHOMO_DIR",
  "mihomo_bin": "$MIHOMO_BIN",
  "service": "mihomo",
  "supervisor": "auto",
  "auth_enabled": true,
  "controller_secret": "$CTRL_SECRET"
}
EOF
  chmod 0600 "$STATE_DIR/panel.json"
  ok "已生成面板配置（首次启动时会随机生成登录密码）"
}

show_password() {
  if [ -s "$STATE_DIR/initial-password.txt" ]; then
    say "${C_G}面板登录密码：${C_0}${C_Y}$(cat "$STATE_DIR/initial-password.txt")${C_0}"
    say "${C_D}（这是首次安装生成的初始密码，改过密码后此文件即失效）${C_0}"
  else
    warn "没有保存的初始密码，说明你已经改过密码；忘了就用菜单里的「重置面板密码」"
  fi
}

reset_password() {
  [ -f "$PANEL_DIR/main.py" ] || die "面板还没安装，先执行安装"
  ensure_python
  _pw=${1:-}
  if [ -z "$_pw" ]; then
    ask "请输入新密码（至少 6 位，直接回车则随机生成）："
    _pw=$REPLY_RAW
  fi
  if [ -z "$_pw" ]; then
    _pw=$(rand_hex 12)
    say "随机密码：${C_Y}$_pw${C_0}"
  fi
  MIHOMO_PANEL_CONF="$STATE_DIR/panel.json" "$PY_BIN" "$PANEL_DIR/main.py" --set-password "$_pw" \
    || die "密码设置失败"
  svc mihomo-panel restart
  ok "密码已更新，面板已重启"
}

kernel_version_text() {
  [ -x "$MIHOMO_BIN" ] || { echo 未安装; return; }
  _v=$("$MIHOMO_BIN" -v 2>/dev/null | head -n1)
  [ -n "$_v" ] && echo "$_v" || echo "已安装（版本未知）"
}

show_status() {
  ensure_python >/dev/null 2>&1 || PY_BIN=python3
  _ip=$(lan_ip)
  say ""
  say "${C_C}────────── 运行状态 ──────────${C_0}"
  printf "  %-10s %s\n" "init 系统" "$(init_kind)"
  printf "  %-10s %s\n" "内核进程" "$(svc_state mihomo)"
  printf "  %-10s %s\n" "面板进程" "$(svc_state mihomo-panel)"
  printf "  %-10s %s\n" "内核版本" "$(kernel_version_text)"
  printf "  %-10s %s\n" "内核目录" "$MIHOMO_DIR"
  printf "  %-10s %s\n" "面板目录" "$PANEL_DIR"
  printf "  %-10s %s\n" "面板配置" "$STATE_DIR/panel.json"
  printf "  %-10s %s\n" "配置份数" "$(ls -1 "$MIHOMO_DIR/profiles"/*.yaml 2>/dev/null | wc -l | tr -d ' ')"
  say "  ${C_G}控制台：${C_0}http://$_ip:$PANEL_PORT"
  say "${C_C}──────────────────────────────${C_0}"
  if [ "$(svc_state mihomo-panel)" = 已停止 ]; then
    warn "面板没在跑，最近日志："
    tail -n 12 "$STATE_DIR/panel.log" 2>/dev/null || say "${C_D}（没有 panel.log）${C_0}"
  fi
}

# ------------------------------------------------------------- 安装主流程
do_install() {
  [ "$(id -u)" = 0 ] || die "请用 root 执行（sudo sh mihomo_tool.sh）"
  mkdir -p "$TMP" || die "临时目录不可写：$TMP"
  say ""
  say "${C_C}=== 安装 / 更新 Mihomo 控制台 ===${C_0}"
  step "检查基础工具…"
  have curl || have wget || { pkg_refresh; pkg_try curl wget >/dev/null 2>&1; }
  have gzip || pkg_try gzip busybox >/dev/null 2>&1 || true
  have tar  || pkg_try tar >/dev/null 2>&1 || true
  have tar  || die "缺少 tar，请先安装（Debian: apt install tar）"
  ensure_python
  ok "Python: $("$PY_BIN" -V 2>&1)"
  ensure_pyyaml
  mkdir -p "$MIHOMO_DIR"
  if [ -x "$MIHOMO_BIN" ]; then ok "已有内核：$(kernel_version_text)"; else install_kernel; fi
  install_geo
  seed_state
  seed_config
  install_panel_code
  install_zashboard
  install_services

  step "启动服务…"
  svc mihomo restart
  svc mihomo-panel restart
  sleep 2
  _ip=$(lan_ip)
  say ""
  say "${C_G}================ 部署完成 ================${C_0}"
  say "  控制台地址： ${C_Y}http://$_ip:$PANEL_PORT${C_0}"
  say "  内核状态：   $(svc_state mihomo)"
  say "  面板状态：   $(svc_state mihomo-panel)"
  show_password
  say "  ${C_D}面板以 root 运行、可改写内核配置并重启服务，${C_0}"
  say "  ${C_D}请只在内网开放，或用防火墙限制 $PANEL_PORT 端口。${C_0}"
  say "${C_G}=========================================${C_0}"
  if [ "$(svc_state mihomo-panel)" = 已停止 ]; then
    warn "面板没起来，日志如下："
    tail -n 15 "$STATE_DIR/panel.log" 2>/dev/null
    say "${C_D}也可以手动前台跑一次看报错：MIHOMO_PANEL_CONF=$STATE_DIR/panel.json $PY_BIN $PANEL_DIR/main.py${C_0}"
  fi
}

# --------------------------------------------------------------- 卸载
do_uninstall() {
  [ "$(id -u)" = 0 ] || die "请用 root 执行"
  ask "${C_R}确认卸载面板与内核？(y/N) ${C_0}"
  case "$REPLY_RAW" in y|Y|yes|YES) ;; *) warn "已取消"; return 0 ;; esac
  step "停止服务…"
  svc mihomo-panel stop
  svc mihomo stop
  case "$(init_kind)" in
    systemd)
      systemctl disable mihomo mihomo-panel >/dev/null 2>&1
      rm -f /etc/systemd/system/mihomo.service /etc/systemd/system/mihomo-panel.service
      systemctl daemon-reload >/dev/null 2>&1 ;;
    openrc)
      rc-update del mihomo default >/dev/null 2>&1
      rc-update del mihomo-panel default >/dev/null 2>&1
      rm -f /etc/init.d/mihomo /etc/init.d/mihomo-panel ;;
    procd)
      /etc/init.d/mihomo disable >/dev/null 2>&1
      /etc/init.d/mihomo-panel disable >/dev/null 2>&1
      rm -f /etc/init.d/mihomo /etc/init.d/mihomo-panel ;;
    *)
      have crontab && crontab -l 2>/dev/null | grep -v 'start-panel.sh' | crontab - >/dev/null 2>&1 ;;
  esac
  rm -rf "$PANEL_DIR"
  rm -f "$MIHOMO_BIN"
  ask "同时删除配置和订阅（$MIHOMO_DIR、$STATE_DIR）？(y/N) "
  case "$REPLY_RAW" in
    y|Y|yes|YES) rm -rf "$MIHOMO_DIR" "$STATE_DIR"; ok "配置已一并删除" ;;
    *) warn "已保留 $MIHOMO_DIR 与 $STATE_DIR，重装后配置还在" ;;
  esac
  ok "卸载完成"
}

self_update() {
  _self=$0
  case "$_self" in /*) ;; *) _self=$(pwd)/$_self ;; esac
  mkdir -p "$TMP"
  step "拉取最新管理脚本…"
  grab "mihomo_tool.sh" "$TMP/tool.sh" || die "脚本下载失败"
  head -n1 "$TMP/tool.sh" | grep -q '^#!' || die "下载到的内容不像脚本，可能被网关拦了"
  cat "$TMP/tool.sh" > "$_self" && chmod +x "$_self" || die "写入 $_self 失败"
  ok "脚本已更新，重新执行中…"
  exec sh "$_self"
}

# --------------------------------------------------------------- 菜单
menu() {
  while :; do
    say ""
    say "${C_C}=============================================${C_0}"
    say "${C_G}        Mihomo 控制台 · 安装管理工具${C_0}"
    say "${C_C}=============================================${C_0}"
    say "  ${C_Y}1.${C_0} 安装 / 覆盖更新（内核 + 面板 + Zashboard）"
    say "  ${C_Y}2.${C_0} 查看运行状态与访问地址"
    say "  ${C_Y}3.${C_0} 查看面板初始密码"
    say "  ${C_Y}4.${C_0} 重置面板密码"
    say "  ${C_Y}5.${C_0} 重启内核 + 面板"
    say "  ${C_Y}6.${C_0} 停止内核 + 面板"
    say "  ${C_Y}7.${C_0} 只更新面板代码（保留配置）"
    say "  ${C_Y}8.${C_0} 更新本脚本"
    say "  ${C_Y}9.${C_0} 卸载"
    say "  ${C_Y}0.${C_0} 退出"
    say "${C_C}=============================================${C_0}"
    ask "请选择 [0-9]: "
    case "$REPLY_RAW" in
      1) do_install ;;
      2) show_status ;;
      3) show_password ;;
      4) reset_password ;;
      5) ensure_python; svc mihomo restart; svc mihomo-panel restart; ok "已重启"; show_status ;;
      6) svc mihomo-panel stop; svc mihomo stop; ok "已停止" ;;
      7) [ "$(id -u)" = 0 ] || die "请用 root 执行"
         mkdir -p "$TMP"; ensure_python; install_panel_code; svc mihomo-panel restart; ok "面板已更新并重启" ;;
      8) self_update ;;
      9) do_uninstall ;;
      0) say "再见"; exit 0 ;;
      *) warn "无效选项" ;;
    esac
    ask "${C_D}按回车返回菜单…${C_0}"
  done
}

case "${1:-menu}" in
  install|update)  do_install ;;
  status)          show_status ;;
  password)        [ -n "${2:-}" ] && reset_password "$2" || { show_password; } ;;
  set-password)    reset_password "${2:-}" ;;
  start)           ensure_python; svc mihomo start; svc mihomo-panel start; show_status ;;
  stop)            svc mihomo-panel stop; svc mihomo stop; ok "已停止" ;;
  restart)         ensure_python; svc mihomo restart; svc mihomo-panel restart; show_status ;;
  uninstall)       do_uninstall ;;
  self-update)     self_update ;;
  menu)            menu ;;
  -h|--help|help)  sed -n '2,14p' "$0" ;;
  *) die "未知参数：$1（试试 install / status / password / restart / uninstall）" ;;
esac
