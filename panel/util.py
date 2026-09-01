"""通用工具：原子写入、JSON 读写、子进程调用、端口探测、HTTP 请求。

这里只用 Python 标准库，保证在没有编译环境的小型 Linux 设备上也能直接跑。
"""
from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

log = logging.getLogger("panel")

# 机场订阅普遍按 UA 下发不同格式，带 clash 关键字才能拿到 YAML。
USER_AGENT = "clash.meta/mihomo-panel"


def now() -> int:
    return int(time.time())


def atomic_write(path: Path, content: str, mode: int = 0o644) -> None:
    """先写临时文件再 rename，避免写一半掉电留下坏配置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return default


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data, mode: int = 0o644) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", mode)


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def run(cmd: Sequence[str], timeout: float = 20.0) -> Tuple[int, str, str]:
    """执行命令。命令不存在或超时都转成返回码，调用方不必到处 try。"""
    try:
        done = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout or "", done.stderr or ""
    except FileNotFoundError:
        return 127, "", "找不到命令 %s" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "命令超时 %s" % " ".join(cmd)
    except OSError as exc:
        return 126, "", str(exc)


def running(keyword: str) -> bool:
    """扫 /proc 判断某个可执行文件是否在跑，不依赖 pgrep/ps（BusyBox 上参数都不一样）。"""
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as handle:
                line = handle.read().decode("utf-8", "replace").replace("\0", " ")
        except OSError:
            continue
        if keyword and keyword in line:
            return True
    return False



def tail(path: Path, lines: int = 200, max_bytes: int = 256 * 1024) -> str:
    """读文件末尾若干行，日志很大时也不会把内存吃满。"""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-lines:])


def tcp_busy(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def udp_busy(port: int) -> bool:
    """探测 UDP 端口是否被占用，用来提前发现 53 端口和 systemd-resolved 打架。"""
    for host in ("0.0.0.0", "127.0.0.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return True
        finally:
            sock.close()
    return False


def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: float = 15.0,
    max_bytes: int = 16 * 1024 * 1024,
) -> Tuple[int, Dict[str, str], bytes]:
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("User-Agent", USER_AGENT)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("响应内容超过 %d 字节，已中止" % max_bytes)
        head = {key.lower(): value for key, value in response.headers.items()}
        return response.getcode(), head, body

def describe_error(exc: BaseException) -> str:
    """把 urllib 那堆嵌套异常压成一句人话。"""
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %s %s" % (exc.code, exc.reason)
    if isinstance(exc, urllib.error.URLError):
        return "网络不可达: %s" % (getattr(exc, "reason", "") or exc)
    if isinstance(exc, socket.timeout):
        return "请求超时"
    text = str(exc).strip()
    return text or exc.__class__.__name__


def last_line(text: str, limit: int = 400) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][-limit:] if lines else ""


def dedupe(items) -> list:
    """按原顺序去重，用于合并多个环节产生的提示文案。"""
    seen = set()
    result = []
    for item in items or []:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def local_ip() -> str:
    """取一个对外可达的本机地址，仅用于启动横幅提示。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 53))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()
