#!/usr/bin/env python3
"""Mihomo Web 面板入口。

只依赖 Python 标准库 + PyYAML，可以直接 `python3 main.py` 启动，也可以被
systemd / OpenRC / 容器拉起。用法：

    python3 main.py                      # 按配置文件里的地址端口启动
    python3 main.py --port 9621          # 临时换端口
    python3 main.py --set-password xxx   # 只改面板密码然后退出
    python3 main.py --show-config        # 打印当前生效的设置
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # 换工作目录也能 import panel

try:
    import yaml  # noqa: F401  仅做依赖自检
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "缺少 PyYAML，请先安装：\n"
        "  Debian/Ubuntu: apt install -y python3-yaml\n"
        "  Alpine:        apk add py3-yaml\n"
        "  其它:          pip3 install pyyaml\n"
    )
    raise SystemExit(1)

from panel import config as config_mod  # noqa: E402
from panel import httpd, routes, util  # noqa: E402

log = logging.getLogger("panel")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mihomo-panel", description="Mihomo Web 管理面板")
    parser.add_argument("--host", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, help="监听端口，默认 9621")
    parser.add_argument("--config", help="面板配置文件路径（默认 /etc/mihomo-panel/panel.json）")
    parser.add_argument("--set-password", metavar="PASSWORD", help="设置面板登录密码后退出")
    parser.add_argument("--disable-auth", action="store_true", help="关闭登录验证（仅限内网可信环境）")
    parser.add_argument("--show-config", action="store_true", help="打印当前设置后退出")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument("--version", action="version", version="mihomo-panel %s" % config_mod.VERSION)
    return parser.parse_args(argv)


def banner(settings, api) -> None:
    shown = settings.host if settings.host not in ("0.0.0.0", "::", "") else util.local_ip()
    line = "=" * 52
    print(line)
    print(" Mihomo 面板 %s 已启动" % config_mod.VERSION)
    print(" 访问地址   http://%s:%d" % (shown, settings.port))
    print(" 内核目录   %s" % settings.mihomo_dir)
    print(" 进程管理   %s" % api.supervisor.label)
    print(" 配置文件   %s" % settings.path)
    if not settings.auth_enabled:
        print(" ! 登录验证已关闭，任何能访问该端口的人都能改配置")
    elif settings.initial_password:
        print(" 初始密码   %s   （也存放在 %s）" % (settings.initial_password, settings.state_dir / "initial-password.txt"))
    print(line)
    sys.stdout.flush()


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%m-%d %H:%M:%S",
    )

    settings = config_mod.load(Path(args.config).expanduser() if args.config else None)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.disable_auth:
        settings.auth_enabled = False
        settings.save()

    if args.set_password:
        if len(args.set_password) < 6:
            print("密码至少 6 位")
            return 2
        settings.set_password(args.set_password)
        print("面板密码已更新，已登录的设备需要重新登录" if settings.session_secret else "面板密码已更新")
        return 0

    if args.show_config:
        data = settings.to_dict()
        data["password_hash"] = "***" if data["password_hash"] else ""
        data["session_secret"] = "***" if data["session_secret"] else ""
        for key in sorted(data):
            print("%-18s %s" % (key, data[key]))
        return 0

    app, api = routes.build(settings)
    try:
        api.boot()
    except Exception as exc:  # noqa: BLE001 - 自检失败不该拦住面板启动
        log.warning("启动自检未完成: %s", exc)

    try:
        server = httpd.serve(app, settings.host, settings.port)
    except OSError as exc:
        if getattr(exc, "errno", None) in (98, 48):  # EADDRINUSE
            log.error("端口 %d 已被占用，换个端口：--port 9622", settings.port)
        else:
            log.error("监听 %s:%d 失败: %s", settings.host, settings.port, exc)
        return 1

    banner(settings, api)
    _install_signals(server)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    log.info("面板已停止")
    return 0


def _install_signals(server) -> None:
    """收到 SIGTERM/SIGINT 时优雅退出；shutdown() 必须在别的线程里调，否则会自锁。"""
    import signal

    def handler(signum, _frame):
        log.info("收到信号 %d，正在退出", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        value = getattr(signal, name, None)
        if value is not None:
            try:
                signal.signal(value, handler)
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
