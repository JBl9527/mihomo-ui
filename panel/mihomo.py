"""mihomo 配置处理与控制器 API 客户端。"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import tempfile
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import yaml

from . import util
from .httpd import HTTPError

log = logging.getLogger("panel")

_SAFE_NAME = re.compile(r"[^0-9A-Za-z_.-]+")
DEFAULT_PORT = 9090


class ControllerError(Exception):
    pass


def yaml_error(exc: Exception) -> str:
    """把 PyYAML 的多行报错压成一句能看懂的提示（行号 + 原因）。"""
    problem = str(getattr(exc, "problem", "") or "").strip()
    context = str(getattr(exc, "context", "") or "").strip()
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    where = ""
    if mark is not None:
        where = "第 %d 行第 %d 列" % (int(getattr(mark, "line", 0)) + 1, int(getattr(mark, "column", 0)) + 1)
    detail = "，".join(part for part in (context, problem) if part)
    if not detail:
        # 退化情况：挑第一行有内容且不是插入符的说明
        for line in str(exc).splitlines():
            line = line.strip()
            if line and set(line) != {"^"} and not line.startswith('in "'):
                detail = line
                break
    return " ".join(part for part in (where, detail or "格式不正确") if part)


def load_yaml(raw: str) -> dict:
    if not raw.strip():
        raise HTTPError(400, "配置内容不能为空")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise HTTPError(400, "YAML 语法有误：%s" % yaml_error(exc))
    if not isinstance(data, dict):
        raise HTTPError(400, "YAML 顶层必须是键值对象")
    return data


def dump_yaml(data: dict) -> str:
    return yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=160
    )

def controller_port(config: dict) -> int:
    raw = str(config.get("external-controller") or "")
    try:
        return int(raw.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return DEFAULT_PORT


def controller_of(config: dict, fallback_secret: str = "") -> "Controller":
    return Controller(controller_port(config), str(config.get("secret") or fallback_secret))


def _safe(name: object) -> str:
    return _SAFE_NAME.sub("-", str(name)).strip("-") or "provider"


def normalize(raw: str, profile_id: str, settings) -> Tuple[str, List[str]]:
    """补齐面板必需字段，并把机场缓存按配置隔离。返回规范化文本和提示。"""
    config = load_yaml(raw)
    notes: List[str] = []

    config["external-controller"] = "0.0.0.0:%d" % controller_port(config)
    config["secret"] = str(config.get("secret") or settings.controller_secret)
    config["external-ui"] = "ui"

    rules = config.get("rules") or []
    if any(isinstance(rule, str) and rule.upper().startswith(("GEOIP,", "GEOSITE,")) for rule in rules):
        config.setdefault("geodata-mode", True)

    # 记住手动选择的节点和 fake-ip 映射，切配置或重启后不会被打回默认。
    profile = config.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    profile.setdefault("store-selected", True)
    profile.setdefault("store-fake-ip", True)
    config["profile"] = profile

    notes.extend(_isolate_providers(config, profile_id))
    notes.extend(_fix_dns_port(config))
    return dump_yaml(config), notes

def _isolate_providers(config: dict, profile_id: str) -> List[str]:
    """原版所有订阅共用 ./providers/airport.yaml，换机场后内核仍读旧缓存。

    这里给每个配置分配独立缓存目录，切换才会真的生效；规则集是公共列表，继续共用。
    """
    block = config.get("proxy-providers")
    if not isinstance(block, dict):
        return []
    changed = []
    for name, item in block.items():
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "http").lower() == "file":
            continue
        suffix = Path(str(item.get("path") or "")).suffix or ".yaml"
        item["path"] = "./providers/%s/%s%s" % (profile_id, _safe(name), suffix)
        changed.append(str(name))
    if changed:
        return ["机场缓存已隔离到 providers/%s/，切换配置立即生效" % profile_id]
    return []


def _fix_dns_port(config: dict) -> List[str]:
    dns = config.get("dns")
    if not isinstance(dns, dict) or not dns.get("enable"):
        return []
    listen = str(dns.get("listen") or "")
    if not listen.endswith(":53") or not util.udp_busy(53):
        return []
    dns["listen"] = "0.0.0.0:1053"
    return [
        "53 端口已被系统 DNS（systemd-resolved / dnsmasq）占用，DNS 监听自动改为 1053，"
        "否则内核会起不来；要接管 53 请先停掉占用方再改回"
    ]


def validate(settings, content: str) -> None:
    """调内核 -t 做真实校验；失败信息直接抛给前端。"""
    if not os.path.isfile(settings.mihomo_bin):
        raise HTTPError(503, "找不到内核可执行文件 %s，无法校验配置" % settings.mihomo_bin)
    settings.mihomo_dir.mkdir(parents=True, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(prefix=".verify-", suffix=".yaml", dir=str(settings.mihomo_dir))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        code, out, err = util.run([settings.mihomo_bin, "-t", "-f", temp_path, "-d", str(settings.mihomo_dir)], 75)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    if code == 124:
        raise HTTPError(400, "内核校验超时，通常是订阅或规则文件下载太慢；可勾选「跳过内核校验」直接保存")
    if code != 0:
        raise HTTPError(400, "内核校验未通过：%s" % (util.last_line(err or out) or "未知错误"))

class Controller:
    """mihomo 外部控制器客户端：热重载、机场刷新、策略组切换都走它。"""

    def __init__(self, port: int, secret: str = "", host: str = "127.0.0.1") -> None:
        self.port = int(port or DEFAULT_PORT)
        self.secret = secret or ""
        self.host = host

    def call(self, path: str, method: str = "GET", payload: Optional[dict] = None, timeout: float = 3.0):
        headers = {}
        if self.secret:
            headers["Authorization"] = "Bearer %s" % self.secret
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            _, _, body = util.http_request(
                "http://%s:%d%s" % (self.host, self.port, path), method, headers, data, timeout
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ControllerError(util.describe_error(exc))
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def version(self) -> dict:
        return self.call("/version")

    def proxies(self) -> dict:
        result = self.call("/proxies", timeout=6)
        return result.get("proxies", {}) if isinstance(result, dict) else {}

    def providers(self) -> dict:
        result = self.call("/providers/proxies", timeout=6)
        return result.get("providers", {}) if isinstance(result, dict) else {}

    def reload(self, path: Path, force: bool = True) -> None:
        self.call("/configs?force=%s" % ("true" if force else "false"), "PUT", {"path": str(path)}, 30)

    def update_provider(self, name: str) -> None:
        self.call("/providers/proxies/%s" % quote(name, safe=""), "PUT", None, 90)

    def select(self, group: str, node: str) -> None:
        self.call("/proxies/%s" % quote(group, safe=""), "PUT", {"name": node}, 12)

def parse_userinfo(raw: str) -> Dict[str, int]:
    """解析机场返回的 subscription-userinfo 头，拿到已用流量和到期时间。"""
    info: Dict[str, int] = {}
    for field in str(raw or "").split(";"):
        if "=" not in field:
            continue
        key, _, value = field.partition("=")
        key = key.strip().lower()
        if key in ("upload", "download", "total", "expire"):
            try:
                info[key] = int(float(value.strip() or 0))
            except ValueError:
                continue
    return info


def probe_subscription(body: bytes) -> Dict[str, Any]:
    """判断订阅内容是 Clash YAML 还是 base64 分享链接，并估算节点数。"""
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return {"format": "empty", "nodes": 0}
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict) and isinstance(data.get("proxies"), list):
            return {"format": "clash", "nodes": len(data["proxies"])}
    except yaml.YAMLError:
        pass
    for candidate in (text, text + "=" * (-len(text) % 4)):
        try:
            decoded = base64.b64decode(candidate, validate=False).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            continue
        links = [line for line in decoded.splitlines() if "://" in line]
        if links:
            return {"format": "links", "nodes": len(links)}
    if "://" in text:
        return {"format": "links", "nodes": len([line for line in text.splitlines() if "://" in line])}
    return {"format": "unknown", "nodes": 0}


def fetch_subscription(url: str) -> Tuple[Dict[str, Any], List[str]]:
    """拉一次订阅做连通性检查并取流量信息；内核之后会自己再拉一次做缓存。"""
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPError(400, "订阅链接必须以 http:// 或 https:// 开头")
    try:
        _, headers, body = util.http_request(url, timeout=25)
    except Exception as exc:  # noqa: BLE001 - 统一转成人话
        raise HTTPError(400, "订阅拉取失败：%s" % util.describe_error(exc))
    info = parse_userinfo(headers.get("subscription-userinfo", ""))
    detail = probe_subscription(body)
    notes: List[str] = []
    if detail["format"] == "unknown":
        notes.append("订阅返回的内容既不是 Clash 配置也不是分享链接，请确认链接是否正确")
    info.update({"nodes": detail["nodes"], "format": detail["format"], "checked_at": util.now()})
    return info, notes

def base_config(sub_url: str, settings, options: Optional[dict] = None) -> str:
    """按订阅链接生成一份保守的可跑配置：默认关掉需要新内核/新内核特性的开关。"""
    options = options or {}
    group = str(options.get("provider_name") or "机场节点").strip() or "机场节点"
    interval = int(options.get("interval") or 86400)
    config: Dict[str, Any] = {
        "mixed-port": int(options.get("port") or 7890),
        "allow-lan": True,
        "bind-address": "*",
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "unified-delay": True,
        "tcp-concurrent": True,
        "external-controller": "0.0.0.0:%d" % DEFAULT_PORT,
        "secret": settings.controller_secret,
        "external-ui": "ui",
        "geodata-mode": True,
        "geodata-loader": "memconservative",
        "profile": {"store-selected": True, "store-fake-ip": True},
    }
    if options.get("tun", True):
        config["tun"] = {
            "enable": True,
            "stack": "gvisor",  # gvisor 不依赖内核模块，兼容性最好
            "auto-route": True,
            "auto-redirect": False,  # 需要 nftables + 较新内核，默认关掉免得起不来
            "auto-detect-interface": True,
            "dns-hijack": ["any:53"],
        }
    config["dns"] = {
        "enable": True,
        "listen": "0.0.0.0:53",
        "ipv6": False,
        "enhanced-mode": "fake-ip",
        "fake-ip-range": "198.18.0.1/16",
        "fake-ip-filter": ["*.lan", "*.local", "*.localdomain", "+.internal", "+.pool.ntp.org", "time.*.com"],
        "default-nameserver": ["223.5.5.5", "119.29.29.29"],
        "nameserver": ["223.5.5.5", "119.29.29.29"],
    }
    config["proxy-providers"] = {
        group: {
            "type": "http",
            "url": sub_url,
            "interval": interval,
            "health-check": {"enable": True, "url": "https://www.gstatic.com/generate_204", "interval": 300},
        }
    }
    config["proxy-groups"] = [
        {"name": "节点选择", "type": "select", "proxies": ["自动选择", "DIRECT"], "use": [group]},
        {"name": "自动选择", "type": "url-test", "use": [group], "tolerance": 50, "interval": 300},
    ]
    config["rules"] = [
        "GEOSITE,private,DIRECT",
        "GEOIP,private,DIRECT,no-resolve",
        "GEOSITE,cn,DIRECT",
        "GEOIP,CN,DIRECT",
        "MATCH,节点选择",
    ]
    return dump_yaml(config)
