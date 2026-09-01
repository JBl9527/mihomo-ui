# Mihomo 控制台

给 Linux 设备用的 Mihomo（Clash.Meta）内核管理面板：一个 Python 进程 + 一套零依赖前端，
用来导入订阅、切换机场配置、选节点、看实时速率和日志，顺手把 Zashboard 也装好。

只依赖 **Python 3.8+ 和 PyYAML**，其它全部用标准库。没有 FastAPI、没有 uvicorn、
没有 npm 构建、页面不加载任何 CDN 资源，所以在 x86 小主机、树莓派、路由器（OpenWrt）、
NAS、Alpine 容器上都能直接跑起来。

## 一键安装

```sh
# root 执行；脚本会自动装内核、面板、Geo 数据和 Zashboard
curl -fsSL https://raw.githubusercontent.com/JBl9527/mihomo-ui/main/mihomo_tool.sh -o mihomo_tool.sh
sh mihomo_tool.sh
```

装完终端会打印访问地址和随机生成的初始密码，例如 `http://192.168.1.10:9621`。

脚本也可以不进菜单直接用：

```sh
sh mihomo_tool.sh install        # 安装 / 覆盖更新
sh mihomo_tool.sh status         # 状态、地址、配置份数
sh mihomo_tool.sh password       # 查看初始密码
sh mihomo_tool.sh password 新密码 # 重置密码
sh mihomo_tool.sh restart|stop|start
sh mihomo_tool.sh uninstall
```

安装脚本会做这些事：识别 CPU 架构（amd64/amd64-compatible/386/arm64/armv7/armv6/armv5/
mips/mipsle/mips64/mips64le/riscv64/loong64/s390x）并拉对应内核；GitHub 直连失败时自动
换镜像；按发行版挑包管理器（apt/dnf/yum/pacman/apk/zypper/opkg/emerge）补 Python 和
PyYAML；再按环境注册服务 —— systemd 写 unit、Alpine 写 OpenRC、OpenWrt 写 procd，
三种都没有就写一个 `start-panel.sh` 并挂到 crontab `@reboot`，内核交给面板自己看管。

## 手动运行

```sh
git clone https://github.com/JBl9527/mihomo-ui.git /opt/mihomo-panel
cd /opt/mihomo-panel
python3 main.py                  # 默认监听 0.0.0.0:9621
```

常用参数与环境变量：

| 参数 / 变量 | 作用 |
| --- | --- |
| `--port 9621` / `PANEL_PORT` | 面板端口 |
| `--host 0.0.0.0` / `PANEL_HOST` | 监听地址，只想本机访问就填 `127.0.0.1` |
| `--config /etc/mihomo-panel/panel.json` / `MIHOMO_PANEL_CONF` | 面板配置文件位置 |
| `--set-password 新密码` | 改密码后退出，忘记密码时用 |
| `--disable-auth` | 关掉登录（只在完全可信的内网用） |
| `--show-config` | 打印当前生效设置 |
| `MIHOMO_DIR` / `MIHOMO_BIN` | 内核目录、内核可执行文件 |
| `MIHOMO_SUPERVISOR` | `auto` / `systemd` / `openrc` / `sysv` / `direct` |

面板配置写在 `/etc/mihomo-panel/panel.json`（权限 0600，非 root 时自动退到
`~/.config/mihomo-panel/`），内核配置、订阅和缓存都在 `/etc/mihomo/`。

## 功能

**配置管理**：粘贴 YAML 或填订阅地址导入，导入时自动跑 `mihomo -t` 校验，语法错误会指出
行号；每份配置存成独立文件，可以复制、编辑、下载、改订阅地址、一键刷新订阅。切换配置优先
走控制器热重载（连接不中断），失败才回退重启内核；启用前先做快照，新配置起不来会自动回滚。

**机场隔离**：每份配置的 provider 缓存单独放在 `providers/<配置ID>/`，不再互相覆盖 ——
这是原版「切换机场后节点还是旧的」的根因。订阅还会显示节点数、已用流量和到期时间。

**节点与监控**：策略组和节点直接点选，延迟由内核健康检查提供；`url-test`、`fallback` 这类
自动组会提示不可手选。首页的实时上下行曲线走控制器的 WebSocket，页面切到后台会自动断开。

**日志**：systemd 环境读 journal，其它环境读日志文件，支持自动刷新。

## 安全须知

面板以 root 运行，能改写内核配置、重启服务，因此：

- 默认强制登录。首次启动生成 12 位随机密码，存在 `/etc/mihomo-panel/initial-password.txt`，
  登录后建议在「设置」里改成自己的密码（改密码会顺带轮换会话密钥，旧 Cookie 立即失效）。
- 密码用 pbkdf2-sha256（12 万轮）存哈希，会话是 HMAC 签名的 Cookie（HttpOnly、SameSite=Lax），
  登录失败按 IP 限速。
- 写操作要求 `Content-Type: application/json`，挡掉表单跨站提交。
- 控制器 secret 不会出现在 `/api/status` 里，只有登录后请求 `/api/controller` 才拿得到。
- 即便如此，也请只在内网开放面板端口，或用防火墙 / 反向代理加一层限制。

## 目录结构

```
main.py             入口：参数解析、依赖自检、启动 HTTP 服务
panel/config.py     设置读写、路径推导、密码与会话密钥
panel/httpd.py      基于 http.server 的路由 / JSON / 静态资源框架
panel/auth.py       密码哈希、签名 Cookie、登录限速
panel/supervisor.py systemd / OpenRC / init.d / 面板自管 四种进程管理
panel/mihomo.py     配置规范化、校验、控制器 REST 调用
panel/profiles.py   配置仓库：增删改、激活、快照回滚、订阅刷新
panel/routes.py     所有 HTTP 接口
web/                零依赖前端（HTML + CSS + 原生 JS，无构建步骤）
mihomo_tool.sh      安装 / 更新 / 卸载 / 状态 / 改密码
```

## 常见问题

**面板打不开**：`sh mihomo_tool.sh status` 看进程和日志；也可以前台跑一次看报错
`MIHOMO_PANEL_CONF=/etc/mihomo-panel/panel.json python3 /opt/mihomo-panel/main.py`。

**内核起不来**：多半是配置里的端口被占用，或 DNS 想用 53 端口而系统已有 resolved 占着 ——
面板导入时会自动把 53 改成 1053 并提示。

**忘记密码**：`sh mihomo_tool.sh password 新密码`，或 `python3 main.py --set-password 新密码`。

**armv7 上 pip 装不动**：装系统包就行，`apt install python3-yaml`（Alpine 是 `apk add py3-yaml`，
OpenWrt 是 `opkg install python3-yaml`）。
