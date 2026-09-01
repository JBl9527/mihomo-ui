"""配置仓库：增删改查、切换（优先热重载）、订阅刷新，全程带回滚。"""
from __future__ import annotations

import logging
import re
import shutil
import threading
import time
import uuid
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import mihomo, util
from .httpd import HTTPError

log = logging.getLogger("panel")

ID_RE = re.compile(r"^[a-f0-9]{12}$")


def clean_name(value: object) -> str:
    name = unicodedata.normalize("NFC", str(value or "")).strip()
    if not name:
        raise HTTPError(400, "配置名称不能为空")
    if len(name) > 80:
        raise HTTPError(400, "配置名称最多 80 个字符")
    return name


class Store:
    def __init__(self, settings, supervisor) -> None:
        self.settings = settings
        self.supervisor = supervisor
        self.lock = threading.RLock()

    # ------------------------------------------------------------ 基础
    def path_of(self, profile_id: str) -> Path:
        if not ID_RE.fullmatch(profile_id or ""):
            raise HTTPError(404, "配置不存在")
        return self.settings.profiles_dir / ("%s.yaml" % profile_id)

    def cache_dir(self, profile_id: str) -> Path:
        return self.settings.providers_dir / profile_id

    def _read_meta(self) -> Dict[str, dict]:
        data = util.read_json(self.settings.meta_file, {})
        if not isinstance(data, dict):
            return {}
        return {key: value for key, value in data.items() if ID_RE.fullmatch(key) and isinstance(value, dict)}

    def _write_meta(self, meta: Dict[str, dict]) -> None:
        util.write_json(self.settings.meta_file, meta)

    def sync(self) -> Dict[str, dict]:
        """让索引和磁盘对齐：清掉孤儿条目、把散落的 yaml 收回索引、首次运行导入现有配置。"""
        with self.lock:
            self.settings.profiles_dir.mkdir(parents=True, exist_ok=True)
            meta = self._read_meta()
            changed = False
            for profile_id in list(meta):
                if not self.path_of(profile_id).is_file():
                    meta.pop(profile_id, None)
                    changed = True
            for item in sorted(self.settings.profiles_dir.glob("*.yaml")):
                profile_id = item.stem
                if not ID_RE.fullmatch(profile_id) or profile_id in meta:
                    continue
                stamp = int(item.stat().st_mtime)
                meta[profile_id] = {
                    "name": "找回的配置 %s" % profile_id[:6],
                    "source": "yaml",
                    "created_at": stamp,
                    "updated_at": stamp,
                }
                changed = True
            if not meta and self.settings.config_file.is_file():
                profile_id = uuid.uuid4().hex[:12]
                shutil.copy2(self.settings.config_file, self.path_of(profile_id))
                stamp = util.now()
                meta[profile_id] = {
                    "name": "当前运行配置",
                    "source": "yaml",
                    "created_at": stamp,
                    "updated_at": stamp,
                }
                util.atomic_write(self.settings.active_file, profile_id + "\n")
                changed = True
            if changed:
                self._write_meta(meta)
            return meta

    def active_id(self) -> Optional[str]:
        raw = util.read_text(self.settings.active_file).strip()
        return raw if ID_RE.fullmatch(raw) else None

    def require(self, profile_id: str) -> Tuple[Dict[str, dict], dict, Path]:
        meta = self.sync()
        path = self.path_of(profile_id)
        if profile_id not in meta or not path.is_file():
            raise HTTPError(404, "配置不存在")
        return meta, meta[profile_id], path

    def listing(self) -> dict:
        meta = self.sync()
        active = self.active_id()
        items = []
        for profile_id, item in meta.items():
            path = self.path_of(profile_id)
            items.append(
                {
                    "id": profile_id,
                    "name": item.get("name") or profile_id,
                    "source": item.get("source") or "yaml",
                    "sub_url": item.get("sub_url") or "",
                    "sub_info": item.get("sub_info") or {},
                    "created_at": int(item.get("created_at") or 0),
                    "updated_at": int(item.get("updated_at") or 0),
                    "size": path.stat().st_size,
                    "active": profile_id == active,
                }
            )
        items.sort(key=lambda entry: (not entry["active"], -entry["updated_at"]))
        return {"profiles": items, "active_id": active}

    def detail(self, profile_id: str) -> dict:
        _, item, path = self.require(profile_id)
        return {
            "id": profile_id,
            "name": item.get("name") or profile_id,
            "source": item.get("source") or "yaml",
            "sub_url": item.get("sub_url") or "",
            "sub_info": item.get("sub_info") or {},
            "active": profile_id == self.active_id(),
            "config": util.read_text(path),
        }

    # ------------------------------------------------------------ 写入
    def create(
        self,
        name: str,
        raw_yaml: str,
        source: str,
        sub_url: str = "",
        sub_info: Optional[dict] = None,
        skip_check: bool = False,
    ) -> Tuple[str, List[str]]:
        with self.lock:
            meta = self.sync()
            profile_id = uuid.uuid4().hex[:12]
            content, notes = mihomo.normalize(raw_yaml, profile_id, self.settings)
            if not skip_check:
                mihomo.validate(self.settings, content)
            util.atomic_write(self.path_of(profile_id), content)
            stamp = util.now()
            meta[profile_id] = {
                "name": clean_name(name),
                "source": source,
                "sub_url": sub_url,
                "sub_info": sub_info or {},
                "created_at": stamp,
                "updated_at": stamp,
            }
            self._write_meta(meta)
            return profile_id, notes

    def update(
        self,
        profile_id: str,
        name: Optional[str] = None,
        raw_yaml: Optional[str] = None,
        sub_url: Optional[str] = None,
        skip_check: bool = False,
    ) -> List[str]:
        with self.lock:
            meta, item, path = self.require(profile_id)
            content = util.read_text(path)
            notes: List[str] = []
            if raw_yaml is not None:
                content = raw_yaml
            if sub_url:
                content = self._replace_sub_url(content, sub_url)
                item["sub_url"] = sub_url
            if raw_yaml is not None or sub_url:
                content, notes = mihomo.normalize(content, profile_id, self.settings)
                if not skip_check:
                    mihomo.validate(self.settings, content)
                util.atomic_write(path, content)
            if name is not None:
                item["name"] = clean_name(name)
            item["updated_at"] = util.now()
            meta[profile_id] = item
            self._write_meta(meta)
            return notes

    @staticmethod
    def _replace_sub_url(content: str, sub_url: str) -> str:
        if not sub_url.lower().startswith(("http://", "https://")):
            raise HTTPError(400, "订阅链接必须以 http:// 或 https:// 开头")
        config = mihomo.load_yaml(content)
        block = config.get("proxy-providers")
        if not isinstance(block, dict) or not block:
            raise HTTPError(400, "这份配置里没有 proxy-providers，改订阅链接请直接编辑 YAML")
        first = next(iter(block))
        if not isinstance(block[first], dict):
            raise HTTPError(400, "proxy-providers 格式不正确")
        block[first]["url"] = sub_url
        return mihomo.dump_yaml(config)

    def duplicate(self, profile_id: str) -> Tuple[str, List[str]]:
        with self.lock:
            _, item, path = self.require(profile_id)
            name = clean_name(item.get("name"))[:70] + " 副本"
            return self.create(
                name,
                util.read_text(path),
                item.get("source") or "yaml",
                item.get("sub_url") or "",
                dict(item.get("sub_info") or {}),
                skip_check=True,
            )

    def delete(self, profile_id: str) -> str:
        with self.lock:
            meta, item, path = self.require(profile_id)
            if profile_id == self.active_id():
                raise HTTPError(400, "这是正在运行的配置，先切换到别的配置再删")
            path.unlink()
            shutil.rmtree(self.cache_dir(profile_id), ignore_errors=True)
            meta.pop(profile_id, None)
            self._write_meta(meta)
            return "已删除「%s」" % item.get("name", profile_id)

    # ------------------------------------------------------------ 切换与重启
    def _api_alive(self, controller: mihomo.Controller) -> bool:
        try:
            controller.version()
            return True
        except mihomo.ControllerError:
            return False

    def _wait_ready(self, controller: mihomo.Controller, timeout: float) -> Tuple[bool, str]:
        deadline = time.monotonic() + timeout
        reason = "控制器 API 一直没有就绪"
        managed = self.supervisor.name != "none"
        while time.monotonic() < deadline:
            if managed and not self.supervisor.is_active():
                reason = util.last_line(self.supervisor.logs(20)) or "内核进程已退出"
            else:
                try:
                    controller.version()
                    return True, ""
                except mihomo.ControllerError as exc:
                    reason = str(exc)
            time.sleep(0.6)
        return False, reason

    def _apply(self, content: str) -> Tuple[bool, str, str]:
        """先试控制器热重载，不行再重启进程。返回 (成功, 错误, 生效方式)。"""
        controller = mihomo.controller_of(mihomo.load_yaml(content), self.settings.controller_secret)
        if self.supervisor.is_active() or self._api_alive(controller):
            try:
                controller.reload(self.settings.config_file)
                ok, reason = self._wait_ready(controller, 12)
                if ok:
                    return True, "", "热重载生效，连接未中断"
                log.info("热重载后 API 未就绪(%s)，改为重启内核", reason)
            except mihomo.ControllerError as exc:
                log.info("热重载不可用(%s)，改为重启内核", exc)
        ok, reason = self.supervisor.restart()
        if not ok:
            return False, reason or "内核启动失败", ""
        ok, reason = self._wait_ready(controller, 30)
        if not ok:
            return False, reason, ""
        return True, "", "内核已重启"

    def activate(self, profile_id: str, skip_check: bool = False) -> dict:
        with self.lock:
            _, item, path = self.require(profile_id)
            content, notes = mihomo.normalize(util.read_text(path), profile_id, self.settings)
            if content != util.read_text(path):
                util.atomic_write(path, content)  # 顺手把老配置的共用缓存路径修好
            if not skip_check:
                mihomo.validate(self.settings, content)

            previous = util.read_text(self.settings.config_file) if self.settings.config_file.is_file() else None
            previous_id = self.active_id()
            if previous is not None:
                util.atomic_write(self.settings.backup_file, previous)
            util.atomic_write(self.settings.config_file, content)
            # 缓存目录先建好：部分内核版本不会自动创建多级目录，缺目录会导致订阅下载失败
            try:
                self.cache_dir(profile_id).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warning("机场缓存目录 %s 创建失败: %s", self.cache_dir(profile_id), exc)

            ok, error, mode = self._apply(content)
            if not ok:
                recovered = "新配置已保留在磁盘上，但内核没起来"
                if previous is not None:
                    util.atomic_write(self.settings.config_file, previous)
                    if self._apply(previous)[0]:
                        recovered = "已回滚到切换前的配置"
                    else:
                        recovered = "回滚后内核仍未启动，请查看运行日志"
                    if previous_id:
                        util.atomic_write(self.settings.active_file, previous_id + "\n")
                raise HTTPError(400, "切换失败：%s（%s）" % (error, recovered))

            util.atomic_write(self.settings.active_file, profile_id + "\n")
            return {"msg": "已切换到「%s」，%s" % (item.get("name", profile_id), mode), "notes": notes}

    def restart(self) -> dict:
        with self.lock:
            if not self.settings.config_file.is_file():
                raise HTTPError(400, "还没有正在运行的配置")
            content = util.read_text(self.settings.config_file)
            ok, reason = self.supervisor.restart()
            if not ok:
                raise HTTPError(400, "内核重启失败：%s" % reason)
            controller = mihomo.controller_of(mihomo.load_yaml(content), self.settings.controller_secret)
            ok, reason = self._wait_ready(controller, 30)
            if not ok:
                raise HTTPError(400, "内核已启动但控制器 API 无响应：%s" % reason)
            return {"msg": "内核已重启，控制器 API 正常"}

    def stop(self) -> dict:
        with self.lock:
            ok, reason = self.supervisor.stop()
            if not ok:
                raise HTTPError(400, "停止内核失败：%s" % reason)
            return {"msg": "内核已停止"}

    def start(self) -> dict:
        with self.lock:
            ok, reason = self.supervisor.start()
            if not ok:
                raise HTTPError(400, "启动内核失败：%s" % reason)
            return {"msg": "内核已启动"}

    # ------------------------------------------------------------ 订阅刷新
    def refresh(self, profile_id: str) -> dict:
        with self.lock:
            meta, item, path = self.require(profile_id)
            config = mihomo.load_yaml(util.read_text(path))
            providers = config.get("proxy-providers")
            providers = providers if isinstance(providers, dict) else {}
            notes: List[str] = []

            url = item.get("sub_url") or ""
            if not url:
                for entry in providers.values():
                    if isinstance(entry, dict) and entry.get("url"):
                        url = str(entry["url"])
                        break
            if url:
                try:
                    info, extra = mihomo.fetch_subscription(url)
                    item["sub_url"] = url
                    item["sub_info"] = info
                    notes.extend(extra)
                except HTTPError as exc:
                    notes.append(exc.detail)

            refreshed = 0
            if profile_id == self.active_id() and providers:
                controller = mihomo.controller_of(config, self.settings.controller_secret)
                for name in providers:
                    try:
                        controller.update_provider(str(name))
                        refreshed += 1
                    except mihomo.ControllerError as exc:
                        notes.append("机场「%s」刷新失败：%s" % (name, exc))
            elif providers:
                shutil.rmtree(self.cache_dir(profile_id), ignore_errors=True)
                try:
                    self.cache_dir(profile_id).mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    log.warning("机场缓存目录 %s 创建失败: %s", self.cache_dir(profile_id), exc)
                notes.append("已清空这份配置的机场缓存，切换过去时会重新拉取")

            item["updated_at"] = util.now()
            meta[profile_id] = item
            self._write_meta(meta)
            message = "已刷新 %d 个机场节点列表" % refreshed if refreshed else "订阅信息已更新"
            return {"msg": message, "notes": notes, "sub_info": item.get("sub_info") or {}}
