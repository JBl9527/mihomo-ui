"""内核进程管理：systemd / OpenRC / init.d(procd) / 面板自管四种后端，启动时自动挑一个可用的。

原版只会调 systemctl 和 journalctl，在 Alpine、OpenWrt、容器、精简发行版上直接不可用；
这里把「启停内核」抽象出来，任何 Linux 都能落到 direct 后端上跑起来。
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

from . import util

log = logging.getLogger("panel")

Result = Tuple[bool, str]


class Supervisor:
    name = "none"
    label = "未接管"

    def __init__(self, settings) -> None:
        self.settings = settings

    def available(self) -> bool:
        return False

    def is_active(self) -> bool:
        return False

    def start(self) -> Result:
        return False, "当前环境没有可用的进程管理方式"

    def stop(self) -> Result:
        return False, "当前环境没有可用的进程管理方式"

    def restart(self) -> Result:
        return False, "当前环境没有可用的进程管理方式"

    def logs(self, lines: int = 200) -> str:
        return util.tail(self.settings.mihomo_log, lines)

class SystemdSupervisor(Supervisor):
    name = "systemd"
    label = "systemd"

    def available(self) -> bool:
        if not util.which("systemctl") or not Path("/run/systemd/system").exists():
            return False
        code, _, _ = util.run(["systemctl", "cat", self.settings.service], 8)
        return code == 0

    def is_active(self) -> bool:
        code, _, _ = util.run(["systemctl", "is-active", "--quiet", self.settings.service], 8)
        return code == 0

    def _action(self, action: str) -> Result:
        code, out, err = util.run(["systemctl", action, self.settings.service], 40)
        if code == 0:
            return True, ""
        return False, util.last_line(err or out) or "systemctl %s 返回 %d" % (action, code)

    def start(self) -> Result:
        return self._action("start")

    def stop(self) -> Result:
        return self._action("stop")

    def restart(self) -> Result:
        return self._action("restart")

    def logs(self, lines: int = 200) -> str:
        code, out, _ = util.run(
            ["journalctl", "-u", self.settings.service, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            12,
        )
        if code == 0 and out.strip():
            return out.strip()
        return util.tail(self.settings.mihomo_log, lines)

class OpenRCSupervisor(Supervisor):
    name = "openrc"
    label = "OpenRC"

    def available(self) -> bool:
        return bool(util.which("rc-service")) and Path("/etc/init.d/%s" % self.settings.service).exists()

    def is_active(self) -> bool:
        code, _, _ = util.run(["rc-service", self.settings.service, "status"], 10)
        return code == 0

    def _action(self, action: str) -> Result:
        code, out, err = util.run(["rc-service", self.settings.service, action], 40)
        if code == 0:
            return True, ""
        return False, util.last_line(err or out) or "rc-service %s 返回 %d" % (action, code)

    def start(self) -> Result:
        return self._action("start")

    def stop(self) -> Result:
        return self._action("stop")

    def restart(self) -> Result:
        return self._action("restart")


class SysvSupervisor(Supervisor):
    """OpenWrt procd 和传统 sysvinit：都通过 /etc/init.d/<服务名> 脚本操作。

    这类脚本的 status 子命令实现得五花八门，所以运行状态一律扫 /proc 判断。
    """

    name = "sysv"
    label = "init.d 脚本"

    def script(self) -> Path:
        return Path("/etc/init.d/%s" % self.settings.service)

    def available(self) -> bool:
        return os.access(str(self.script()), os.X_OK)

    def is_active(self) -> bool:
        return util.running(self.settings.mihomo_bin) or util.running("mihomo -d")

    def _action(self, action: str) -> Result:
        code, out, err = util.run([str(self.script()), action], 40)
        if code == 0:
            return True, ""
        return False, util.last_line(err or out) or "%s %s 返回 %d" % (self.script(), action, code)

    def start(self) -> Result:
        return self._action("start")

    def stop(self) -> Result:
        return self._action("stop")

    def restart(self) -> Result:
        okay, message = self._action("restart")
        if okay:
            return okay, message
        self._action("stop")
        return self._action("start")


class DirectSupervisor(Supervisor):
    """没有 init 系统时由面板自己拉起内核：pid 文件 + 日志文件，重启面板不会带走内核。"""

    name = "direct"
    label = "面板自管"

    def available(self) -> bool:
        return os.path.isfile(self.settings.mihomo_bin)

    def _pid(self) -> Optional[int]:
        raw = util.read_text(self.settings.mihomo_pid).strip()
        if not raw.isdigit():
            return None
        pid = int(raw)
        cmdline = Path("/proc/%d/cmdline" % pid)
        if cmdline.exists():
            return pid if "mihomo" in util.read_text(cmdline).replace("\0", " ") else None
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            return None

    def is_active(self) -> bool:
        return self._pid() is not None

    def start(self) -> Result:
        if self.is_active():
            return True, ""
        if not os.path.isfile(self.settings.mihomo_bin):
            return False, "找不到内核可执行文件 %s" % self.settings.mihomo_bin
        try:
            handle = open(self.settings.mihomo_log, "ab")
        except OSError as exc:
            return False, "日志文件不可写: %s" % exc
        try:
            child = subprocess.Popen(
                [self.settings.mihomo_bin, "-d", str(self.settings.mihomo_dir)],
                stdout=handle,
                stderr=handle,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            handle.close()
            return False, "内核启动失败: %s" % exc
        handle.close()
        util.atomic_write(self.settings.mihomo_pid, "%d\n" % child.pid)
        time.sleep(0.5)
        if child.poll() is not None:
            return False, util.last_line(util.tail(self.settings.mihomo_log, 20)) or "内核启动后立即退出"
        return True, ""

    def stop(self) -> Result:
        pid = self._pid()
        if pid is None:
            return True, ""
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            return False, "停止内核失败: %s" % exc
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self._pid() is None:
                return True, ""
            time.sleep(0.2)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        return True, ""

    def restart(self) -> Result:
        self.stop()
        return self.start()


_BACKENDS = (SystemdSupervisor, OpenRCSupervisor, SysvSupervisor, DirectSupervisor)


def detect(settings) -> Supervisor:
    """settings.supervisor 为 auto 时按 systemd → OpenRC → init.d → 自管 的顺序探测。"""
    wanted = (settings.supervisor or "auto").strip().lower()
    if wanted not in ("", "auto"):
        for backend in _BACKENDS:
            if backend.name == wanted:
                chosen = backend(settings)
                if chosen.available():
                    return chosen
                log.warning("指定的进程管理方式 %s 当前不可用，回落到自动探测", wanted)
                break
    for backend in _BACKENDS:
        candidate = backend(settings)
        if candidate.available():
            log.info("内核进程管理方式: %s", candidate.label)
            return candidate
    return Supervisor(settings)
