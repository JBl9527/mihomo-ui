"""HTTP 接口层：会话、状态、配置管理、策略组、日志。"""
from __future__ import annotations

import logging
import os
import re
import secrets
from typing import List, Optional
from urllib.parse import quote

from . import auth
from . import config as config_mod
from . import mihomo, profiles
from . import supervisor as supervisor_mod
from . import util
from .httpd import App, HTTPError, Request, Response, Router, file_response

log = logging.getLogger("panel")

_BUILTIN = ("DIRECT", "REJECT", "REJECT-DROP", "PASS", "GLOBAL", "COMPATIBLE")
_GROUP_TYPES = ("selector", "urltest", "fallback", "loadbalance", "relay")


class Api:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.supervisor = supervisor_mod.detect(settings)
        self.store = profiles.Store(settings, self.supervisor)
        self.guard = auth.LoginGuard()

    # ------------------------------------------------------------ 鉴权
    def before(self, request: Request, public: bool) -> Optional[Response]:
        if public or not self.settings.auth_enabled:
            return None
        token = request.cookie(auth.COOKIE_NAME)
        if token and auth.check_token(token, self.settings.session_secret):
            return None
        if request.path.startswith("/api/"):
            return Response.of_json({"detail": "会话已失效，请重新登录"}, 401)
        return Response.redirect("/login")

    def page_index(self, request: Request) -> Response:
        return file_response(config_mod.WEB_DIR / "index.html", request)

    def page_login(self, request: Request) -> Response:
        return file_response(config_mod.WEB_DIR / "login.html", request)

    def session(self, request: Request) -> dict:
        token = request.cookie(auth.COOKIE_NAME)
        return {
            "auth_enabled": self.settings.auth_enabled,
            "logged_in": not self.settings.auth_enabled
            or bool(token and auth.check_token(token, self.settings.session_secret)),
        }

    def login(self, request: Request) -> Response:
        data = request.json()
        if not self.settings.auth_enabled:
            return Response.of_json({"status": "success", "msg": "面板未启用密码"})
        waiting = self.guard.locked_for(request.client)
        if waiting:
            raise HTTPError(429, "失败次数太多，请 %d 秒后再试" % waiting)
        password = str(data.get("password") or "")
        if not password or not auth.verify_password(password, self.settings.password_hash):
            self.guard.record_failure(request.client)
            raise HTTPError(401, "密码不正确")
        self.guard.reset(request.client)
        token = auth.issue_token(self.settings.session_secret, self.settings.session_ttl)
        response = Response.of_json({"status": "success", "msg": "登录成功"})
        return response.set_cookie(auth.COOKIE_NAME, token, self.settings.session_ttl)

    def logout(self, request: Request) -> Response:
        response = Response.of_json({"status": "success", "msg": "已退出登录"})
        return response.set_cookie(auth.COOKIE_NAME, "", 0)

    def change_password(self, request: Request) -> Response:
        data = request.json()
        old = str(data.get("old") or "")
        new = str(data.get("new") or "")
        if len(new) < 6:
            raise HTTPError(400, "新密码至少 6 位")
        if self.settings.password_hash and not auth.verify_password(old, self.settings.password_hash):
            raise HTTPError(401, "当前密码不正确")
        self.settings.session_secret = secrets.token_hex(32)  # 让其他设备的会话立刻失效
        self.settings.set_password(new)
        token = auth.issue_token(self.settings.session_secret, self.settings.session_ttl)
        response = Response.of_json({"status": "success", "msg": "密码已更新，其他设备需要重新登录"})
        return response.set_cookie(auth.COOKIE_NAME, token, self.settings.session_ttl)

    # ------------------------------------------------------------ 运行状态
    def _running_config(self) -> dict:
        content = util.read_text(self.settings.config_file)
        if not content.strip():
            return {}
        try:
            return mihomo.load_yaml(content)
        except HTTPError:
            return {}

    def _controller(self, config: Optional[dict] = None) -> mihomo.Controller:
        config = self._running_config() if config is None else config
        return mihomo.controller_of(config, self.settings.controller_secret)

    def status(self, request: Request) -> dict:
        config = self._running_config()
        controller = self._controller(config)
        nodes = groups = 0
        version = ""
        error = ""
        api_ready = False
        try:
            version = str((controller.version() or {}).get("version") or "")
            payload = controller.proxies()
            api_ready = True
            for name, item in payload.items():
                if not isinstance(item, dict):
                    continue
                if str(item.get("type") or "").lower() in _GROUP_TYPES:
                    groups += 1
                elif name not in _BUILTIN:
                    nodes += 1
        except mihomo.ControllerError as exc:
            error = str(exc)
        return {
            "panel_version": config_mod.VERSION,
            "supervisor": self.supervisor.name,
            "supervisor_label": self.supervisor.label,
            "service_active": self.supervisor.is_active(),
            "api_ready": api_ready,
            "ui_ready": (self.settings.ui_dir / "index.html").is_file(),
            "kernel_installed": os.path.isfile(self.settings.mihomo_bin),
            "controller_port": mihomo.controller_port(config),
            "kernel_version": version,
            "nodes": nodes,
            "groups": groups,
            "mode": str(config.get("mode") or ""),
            "tun": bool((config.get("tun") or {}).get("enable")) if isinstance(config.get("tun"), dict) else False,
            "mixed_port": config.get("mixed-port") or config.get("port") or 0,
            "mihomo_dir": str(self.settings.mihomo_dir),
            "auth_enabled": self.settings.auth_enabled,
            "error": error,
        }

    def controller_info(self, request: Request) -> dict:
        """给前端拿控制器地址：实时流量曲线和 Zashboard 跳转都要用。"""
        config = self._running_config()
        return {
            "port": mihomo.controller_port(config),
            "secret": str(config.get("secret") or self.settings.controller_secret),
            "ui_ready": (self.settings.ui_dir / "index.html").is_file(),
        }

    def logs(self, request: Request) -> dict:
        lines = request.int_arg("lines", 200, 20, 1000)
        return {"text": self.supervisor.logs(lines), "source": self.supervisor.label}

    # ------------------------------------------------------------ 策略组
    def proxy_groups(self, request: Request) -> dict:
        try:
            payload = self._controller().proxies()
        except mihomo.ControllerError as exc:
            raise HTTPError(503, "控制器 API 无响应：%s" % exc)
        delays = {}
        for name, item in payload.items():
            if not isinstance(item, dict):
                continue
            history = item.get("history") or []
            if history and isinstance(history[-1], dict):
                delays[name] = int(history[-1].get("delay") or 0)
        groups = []
        for name, item in payload.items():
            if not isinstance(item, dict) or str(item.get("type") or "").lower() not in _GROUP_TYPES:
                continue
            options = [str(opt) for opt in (item.get("all") or [])][:400]
            groups.append(
                {
                    "name": name,
                    "type": str(item.get("type") or ""),
                    "now": str(item.get("now") or ""),
                    "options": [{"name": opt, "delay": delays.get(opt, 0)} for opt in options],
                }
            )
        groups.sort(key=lambda entry: (entry["type"] != "Selector", entry["name"]))
        return {"groups": groups}

    def select_node(self, request: Request, group: str) -> dict:
        node = str(request.json().get("name") or "").strip()
        if not node:
            raise HTTPError(400, "请提供要切换到的节点名")
        try:
            self._controller().select(group, node)
        except mihomo.ControllerError as exc:
            raise HTTPError(400, "切换节点失败：%s" % exc)
        return {"status": "success", "msg": "「%s」已切到 %s" % (group, node)}

    # ------------------------------------------------------------ 配置管理
    def list_profiles(self, request: Request) -> dict:
        return self.store.listing()

    def get_profile(self, request: Request, pid: str) -> dict:
        return self.store.detail(pid)

    def _finish(self, profile_id: str, data: dict, notes: List[str], message: str) -> dict:
        if not bool(data.get("activate", True)):
            return {"status": "success", "msg": message, "id": profile_id, "notes": util.dedupe(notes)}
        outcome = self.store.activate(profile_id, skip_check=True)
        return {
            "status": "success",
            "msg": outcome["msg"],
            "id": profile_id,
            "notes": util.dedupe(notes + outcome["notes"]),
        }

    def import_subscription(self, request: Request) -> dict:
        data = request.json()
        url = str(data.get("sub_url") or "").strip()
        info, notes = mihomo.fetch_subscription(url)
        content = mihomo.base_config(url, self.settings, data.get("options") or {})
        profile_id, extra = self.store.create(
            data.get("name"), content, "subscription", url, info, skip_check=bool(data.get("skip_check"))
        )
        return self._finish(profile_id, data, notes + extra, "订阅已导入")

    def import_yaml(self, request: Request) -> dict:
        data = request.json()
        activate = bool(data.get("activate", True))
        profile_id, notes = self.store.create(
            data.get("name"),
            str(data.get("raw_yaml") or ""),
            "yaml",
            skip_check=bool(data.get("skip_check")) or activate,
        )
        return self._finish(profile_id, data, notes, "YAML 配置已保存")

    def activate_profile(self, request: Request, pid: str) -> dict:
        data = request.json() if request.body else {}
        return self.store.activate(pid, skip_check=bool(data.get("skip_check")))

    def refresh_profile(self, request: Request, pid: str) -> dict:
        return self.store.refresh(pid)

    def duplicate_profile(self, request: Request, pid: str) -> dict:
        profile_id, notes = self.store.duplicate(pid)
        return {"status": "success", "msg": "已复制配置", "id": profile_id, "notes": notes}

    def delete_profile(self, request: Request, pid: str) -> dict:
        return {"status": "success", "msg": self.store.delete(pid)}

    def update_profile(self, request: Request, pid: str) -> dict:
        data = request.json()
        raw_yaml = data.get("raw_yaml")
        sub_url = str(data.get("sub_url") or "").strip() or None
        notes = self.store.update(
            pid,
            name=data.get("name"),
            raw_yaml=None if raw_yaml is None else str(raw_yaml),
            sub_url=sub_url,
            skip_check=bool(data.get("skip_check")),
        )
        content_changed = raw_yaml is not None or bool(sub_url)
        if data.get("activate") or (content_changed and pid == self.store.active_id()):
            outcome = self.store.activate(pid, skip_check=True)
            return {"status": "success", "msg": outcome["msg"], "notes": util.dedupe(notes + outcome["notes"])}
        return {"status": "success", "msg": "配置已保存", "notes": util.dedupe(notes)}

    def download_profile(self, request: Request, pid: str) -> Response:
        _, item, path = self.store.require(pid)
        name = str(item.get("name") or pid)
        ascii_name = re.sub(r"[^0-9A-Za-z_.-]+", "_", name).strip("_")
        if not ascii_name or ascii_name == "_":
            ascii_name = "profile-%s" % pid  # 纯中文名会被过滤成空，退回 id 免得下载成 _.yaml
        disposition = "attachment; filename=\"%s.yaml\"; filename*=UTF-8''%s.yaml" % (
            ascii_name,
            quote(name, safe=""),
        )
        return Response(
            200,
            util.read_text(path).encode("utf-8"),
            "application/x-yaml; charset=utf-8",
            {"Content-Disposition": disposition},
        )

    # ------------------------------------------------------------ 内核控制
    def service(self, request: Request) -> dict:
        action = str((request.json() if request.body else {}).get("action") or "restart").lower()
        if action == "restart":
            return self.store.restart()
        if action == "stop":
            return self.store.stop()
        if action == "start":
            return self.store.start()
        raise HTTPError(400, "不支持的操作：%s" % action)

    def restart(self, request: Request) -> dict:
        return self.store.restart()

    def boot(self) -> None:
        """面板启动时的自检：索引自愈；自管模式下内核没跑就顺手拉起来。"""
        self.store.sync()
        if self.supervisor.name != "direct" or self.supervisor.is_active():
            return
        if not self.settings.config_file.is_file():
            return
        ok, reason = self.supervisor.start()
        log.info("自管模式启动内核: %s", "成功" if ok else reason)

def build(settings):
    api = Api(settings)
    router = Router()
    router.add("GET", "/", api.page_index)
    router.add("GET", "/login", api.page_login, True)
    router.add("GET", "/api/session", api.session, True)
    router.add("POST", "/api/login", api.login, True)
    router.add("POST", "/api/logout", api.logout, True)
    router.add("POST", "/api/password", api.change_password)
    router.add("GET", "/api/status", api.status)
    router.add("GET", "/api/controller", api.controller_info)
    router.add("GET", "/api/logs", api.logs)
    router.add("GET", "/api/proxies", api.proxy_groups)
    router.add("PUT", "/api/proxies/{group}", api.select_node)
    router.add("GET", "/api/profiles", api.list_profiles)
    router.add("POST", "/api/profiles/subscription", api.import_subscription)
    router.add("POST", "/api/profiles/yaml", api.import_yaml)
    router.add("GET", "/api/profiles/{pid}", api.get_profile)
    router.add("PUT", "/api/profiles/{pid}", api.update_profile)
    router.add("DELETE", "/api/profiles/{pid}", api.delete_profile)
    router.add("GET", "/api/profiles/{pid}/download", api.download_profile)
    router.add("POST", "/api/profiles/{pid}/activate", api.activate_profile)
    router.add("POST", "/api/profiles/{pid}/refresh", api.refresh_profile)
    router.add("POST", "/api/profiles/{pid}/duplicate", api.duplicate_profile)
    router.add("POST", "/api/service", api.service)
    router.add("POST", "/api/restart", api.restart)
    app = App(router, config_mod.WEB_DIR, "/assets/", api.before)
    return app, api
