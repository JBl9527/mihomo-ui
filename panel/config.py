"""面板设置：JSON 配置文件 + 环境变量覆盖，改密码后可原地落盘。"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from . import auth, util

log = logging.getLogger("panel")

VERSION = "2.1.0"
ROOT = Path(__file__).resolve().parent.parent  # 仓库/安装目录
WEB_DIR = ROOT / "web"  # 静态资源按 __file__ 定位，换工作目录也不会 404

_CONF_CANDIDATES = (
    "/etc/mihomo-panel/panel.json",
    "~/.config/mihomo-panel/panel.json",
    "/tmp/mihomo-panel/panel.json",
)
_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _flag(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False

def conf_path() -> Path:
    """按 /etc → 用户目录 → /tmp 的顺序挑一个能写的位置，非 root 也能启动。"""
    explicit = _env("MIHOMO_PANEL_CONF")
    if explicit:
        path = Path(explicit).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    for candidate in _CONF_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() or _writable(path.parent):
            return path
    return Path("/tmp/mihomo-panel/panel.json")


class Settings:
    def __init__(self, path: Path, data: Optional[dict] = None) -> None:
        data = data or {}
        self.path = path
        self.host = str(data.get("host") or "0.0.0.0")
        self.port = int(data.get("port") or 9621)
        self.mihomo_dir = Path(str(data.get("mihomo_dir") or "/etc/mihomo"))
        self.mihomo_bin = str(data.get("mihomo_bin") or "/usr/local/bin/mihomo")
        self.service = str(data.get("service") or "mihomo")
        self.supervisor = str(data.get("supervisor") or "auto")
        self.auth_enabled = bool(data.get("auth_enabled", True))
        self.password_hash = str(data.get("password_hash") or "")
        self.session_secret = str(data.get("session_secret") or "")
        self.session_ttl = int(data.get("session_ttl") or 7 * 86400)
        self.controller_secret = str(data.get("controller_secret") or "123456")
        self.initial_password = ""  # 仅首次生成时有值，不落盘

    # --- 派生路径 ---
    @property
    def state_dir(self) -> Path:
        return self.path.parent

    @property
    def config_file(self) -> Path:
        return self.mihomo_dir / "config.yaml"

    @property
    def profiles_dir(self) -> Path:
        return self.mihomo_dir / "profiles"

    @property
    def providers_dir(self) -> Path:
        return self.mihomo_dir / "providers"

    @property
    def meta_file(self) -> Path:
        return self.profiles_dir / "profiles.json"

    @property
    def active_file(self) -> Path:
        return self.profiles_dir / "active"

    @property
    def backup_file(self) -> Path:
        return self.profiles_dir / ".rollback.yaml"

    @property
    def ui_dir(self) -> Path:
        return self.mihomo_dir / "ui"

    @property
    def mihomo_log(self) -> Path:
        return self.state_dir / "mihomo.log"

    @property
    def mihomo_pid(self) -> Path:
        return self.state_dir / "mihomo.pid"

    # --- 读写 ---
    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "mihomo_dir": str(self.mihomo_dir),
            "mihomo_bin": self.mihomo_bin,
            "service": self.service,
            "supervisor": self.supervisor,
            "auth_enabled": self.auth_enabled,
            "password_hash": self.password_hash,
            "session_secret": self.session_secret,
            "session_ttl": self.session_ttl,
            "controller_secret": self.controller_secret,
        }

    def save(self) -> None:
        util.write_json(self.path, self.to_dict(), 0o600)

    def ensure_dirs(self) -> None:
        for directory in (self.state_dir, self.mihomo_dir, self.profiles_dir, self.providers_dir):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("目录 %s 创建失败: %s", directory, exc)

    def set_password(self, password: str) -> None:
        self.password_hash = auth.hash_password(password)
        self.auth_enabled = True
        self.save()

    def apply_env(self) -> None:
        self.host = str(_env("PANEL_HOST", self.host))
        self.port = int(_env("PANEL_PORT", str(self.port)))
        self.mihomo_dir = Path(str(_env("MIHOMO_DIR", str(self.mihomo_dir))))
        self.mihomo_bin = str(_env("MIHOMO_BIN", self.mihomo_bin))
        self.service = str(_env("MIHOMO_SERVICE", self.service))
        self.supervisor = str(_env("MIHOMO_SUPERVISOR", self.supervisor))
        self.controller_secret = str(_env("MIHOMO_SECRET", self.controller_secret))
        self.auth_enabled = not _flag("PANEL_NO_AUTH", not self.auth_enabled)

    def ensure_credentials(self) -> bool:
        """补齐会话密钥和登录密码；首次生成的密码会同时落到文件里方便安装脚本展示。"""
        changed = False
        if not self.session_secret:
            self.session_secret = secrets.token_hex(32)
            changed = True
        if self.auth_enabled and not self.password_hash:
            password = _env("PANEL_PASSWORD") or "".join(secrets.choice(_ALPHABET) for _ in range(12))
            self.password_hash = auth.hash_password(password)
            self.initial_password = password
            changed = True
            try:
                util.atomic_write(self.state_dir / "initial-password.txt", password + "\n", 0o600)
            except OSError as exc:
                log.warning("初始密码写入失败: %s", exc)
        return changed

def load(path: Optional[Path] = None) -> Settings:
    target = path or conf_path()
    settings = Settings(target, util.read_json(target, {}) if target.is_file() else {})
    settings.apply_env()
    settings.ensure_dirs()
    if settings.ensure_credentials() or not target.is_file():
        settings.save()
    return settings
