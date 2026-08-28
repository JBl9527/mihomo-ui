import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates


app = FastAPI(title="Mihomo Web Manager V3")
templates = Jinja2Templates(directory="templates")

MIHOMO_DIR = Path(os.environ.get("MIHOMO_DIR", "/etc/mihomo"))
MIHOMO_CONF = MIHOMO_DIR / "config.yaml"
PROFILES_DIR = MIHOMO_DIR / "profiles"
PROFILE_META = PROFILES_DIR / "profiles.json"
ACTIVE_PROFILE = PROFILES_DIR / "active"
MIHOMO_BIN = os.environ.get("MIHOMO_BIN", "/usr/local/bin/mihomo")
MIHOMO_SERVICE = os.environ.get("MIHOMO_SERVICE", "mihomo")
DEFAULT_SECRET = os.environ.get("MIHOMO_SECRET", "123456")
PROFILE_ID_RE = re.compile(r"^[a-f0-9]{12}$")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_metadata(metadata: dict) -> None:
    atomic_write(PROFILE_META, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")


def profile_path(profile_id: str) -> Path:
    if not PROFILE_ID_RE.fullmatch(profile_id):
        raise HTTPException(status_code=404, detail="配置不存在")
    return PROFILES_DIR / f"{profile_id}.yaml"


def ensure_store() -> dict:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    metadata = read_json(PROFILE_META, {})
    if not isinstance(metadata, dict):
        metadata = {}

    metadata = {
        key: value
        for key, value in metadata.items()
        if PROFILE_ID_RE.fullmatch(key)
        and isinstance(value, dict)
        and profile_path(key).is_file()
    }

    if not metadata and MIHOMO_CONF.is_file():
        profile_id = uuid.uuid4().hex[:12]
        shutil.copy2(MIHOMO_CONF, profile_path(profile_id))
        now = int(time.time())
        metadata[profile_id] = {
            "name": "现有配置",
            "source": "yaml",
            "created_at": now,
            "updated_at": now,
        }
        atomic_write(ACTIVE_PROFILE, profile_id + "\n")
        write_metadata(metadata)
    elif metadata and not PROFILE_META.exists():
        write_metadata(metadata)

    return metadata


def current_profile_id() -> str | None:
    try:
        profile_id = ACTIVE_PROFILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return profile_id if PROFILE_ID_RE.fullmatch(profile_id) else None


def require_profile(profile_id: str) -> tuple[dict, dict, Path]:
    metadata = ensure_store()
    path = profile_path(profile_id)
    if profile_id not in metadata or not path.is_file():
        raise HTTPException(status_code=404, detail="配置不存在")
    return metadata, metadata[profile_id], path


def clean_name(value: object) -> str:
    name = str(value or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="配置名称不能为空")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="配置名称不能超过 80 个字符")
    return name


def parse_yaml(raw_yaml: str) -> dict:
    if not raw_yaml.strip():
        raise HTTPException(status_code=400, detail="YAML 内容不能为空")
    try:
        config = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"YAML 语法错误: {exc}") from exc
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="YAML 顶层必须是键值对象")
    return config


def controller_port(config: dict) -> int:
    controller = str(config.get("external-controller", "0.0.0.0:9090"))
    try:
        return int(controller.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 9090


def prepare_config(raw_yaml: str) -> str:
    config = parse_yaml(raw_yaml)
    port = controller_port(config)
    config["external-controller"] = f"0.0.0.0:{port}"
    config["secret"] = str(config.get("secret") or DEFAULT_SECRET)
    config["external-ui"] = "ui"

    rules = config.get("rules", [])
    if any(
        isinstance(rule, str) and rule.upper().startswith(("GEOIP,", "GEOSITE,"))
        for rule in rules
    ):
        config["geodata-mode"] = True

    return yaml.safe_dump(
        config,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )


def generate_base_yaml(sub_url: str) -> str:
    if not sub_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="订阅链接必须以 http:// 或 https:// 开头")

    config = {
        "port": 7890,
        "socks-port": 7891,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "external-controller": "0.0.0.0:9090",
        "secret": DEFAULT_SECRET,
        "external-ui": "ui",
        "geodata-mode": True,
        "geodata-loader": "memconservative",
        "tun": {
            "enable": True,
            "stack": "gvisor",
            "auto-route": True,
            "auto-redirect": True,
            "auto-detect-interface": True,
        },
        "dns": {
            "enable": True,
            "listen": "0.0.0.0:53",
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "nameserver": ["223.5.5.5", "119.29.29.29"],
        },
        "proxy-providers": {
            "机场节点": {
                "type": "http",
                "url": sub_url,
                "interval": 86400,
                "path": "./providers/airport.yaml",
                "health-check": {
                    "enable": True,
                    "url": "https://www.gstatic.com/generate_204",
                    "interval": 300,
                },
            }
        },
        "proxies": [{"name": "直连", "type": "direct"}],
        "proxy-groups": [
            {
                "name": "节点选择",
                "type": "select",
                "proxies": ["自动选择", "直连"],
                "use": ["机场节点"],
            },
            {
                "name": "自动选择",
                "type": "url-test",
                "use": ["机场节点"],
                "tolerance": 50,
                "interval": 300,
            },
        ],
        "rules": ["GEOSITE,cn,直连", "GEOIP,CN,直连", "MATCH,节点选择"],
    }
    return yaml.safe_dump(config, allow_unicode=True, sort_keys=False, width=120)


def validate_with_mihomo(content: str) -> None:
    if not os.path.isfile(MIHOMO_BIN):
        raise HTTPException(status_code=500, detail="找不到 Mihomo 内核，无法校验配置")

    fd, candidate = tempfile.mkstemp(prefix="mihomo-profile-", suffix=".yaml", dir=str(MIHOMO_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        result = subprocess.run(
            [MIHOMO_BIN, "-t", "-f", candidate, "-d", str(MIHOMO_DIR)],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=400, detail="内核校验超时，请检查 Geo 数据和网络") from exc
    finally:
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "未知错误").strip().splitlines()[-1]
        raise HTTPException(status_code=400, detail=f"Mihomo 配置校验失败: {message}")


def read_controller_settings(content: str | None = None) -> tuple[int, str]:
    try:
        raw = content if content is not None else MIHOMO_CONF.read_text(encoding="utf-8")
        config = yaml.safe_load(raw) or {}
    except (OSError, yaml.YAMLError):
        config = {}
    return controller_port(config), str(config.get("secret") or "")


def controller_request(path: str = "/version", timeout: float = 2.0) -> dict:
    port, secret = read_controller_settings()
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if secret:
        request.add_header("Authorization", f"Bearer {secret}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def service_is_active() -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", MIHOMO_SERVICE],
        capture_output=True,
    )
    return result.returncode == 0


def recent_mihomo_error() -> str:
    result = subprocess.run(
        ["journalctl", "-u", MIHOMO_SERVICE, "-n", "30", "--no-pager", "-o", "cat"],
        capture_output=True,
        text=True,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[-1][-500:] if lines else "服务未能启动"


def restart_and_wait(timeout: float = 20.0) -> tuple[bool, str]:
    result = subprocess.run(
        ["systemctl", "restart", MIHOMO_SERVICE],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip() or "systemctl restart 失败"

    deadline = time.monotonic() + timeout
    last_error = "控制器 API 尚未就绪"
    while time.monotonic() < deadline:
        if not service_is_active():
            last_error = recent_mihomo_error()
        else:
            try:
                controller_request(timeout=1.5)
                return True, ""
            except (OSError, ValueError, urllib.error.URLError) as exc:
                last_error = str(exc)
        time.sleep(0.75)
    return False, last_error


def save_profile(name: str, content: str, source: str) -> str:
    metadata = ensure_store()
    profile_id = uuid.uuid4().hex[:12]
    now = int(time.time())
    atomic_write(profile_path(profile_id), content)
    metadata[profile_id] = {
        "name": name,
        "source": source,
        "created_at": now,
        "updated_at": now,
    }
    write_metadata(metadata)
    return profile_id


def activate_profile(profile_id: str) -> dict:
    _, profile, path = require_profile(profile_id)
    content = path.read_text(encoding="utf-8")
    validate_with_mihomo(content)

    previous = MIHOMO_CONF.read_text(encoding="utf-8") if MIHOMO_CONF.exists() else None
    previous_id = current_profile_id()
    atomic_write(MIHOMO_CONF, content)
    ok, error = restart_and_wait()
    if not ok:
        if previous is not None:
            atomic_write(MIHOMO_CONF, previous)
            restart_and_wait(timeout=10)
        if previous_id:
            atomic_write(ACTIVE_PROFILE, previous_id + "\n")
        raise HTTPException(status_code=400, detail=f"新配置启动失败，已恢复原配置: {error}")

    atomic_write(ACTIVE_PROFILE, profile_id + "\n")
    return {"status": "success", "msg": f"已切换到“{profile['name']}”，Mihomo 和控制器 API 均正常"}


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/profiles")
def list_profiles():
    metadata = ensure_store()
    active_id = current_profile_id()
    profiles = []
    for profile_id, item in metadata.items():
        path = profile_path(profile_id)
        profiles.append(
            {
                "id": profile_id,
                "name": item.get("name", profile_id),
                "source": item.get("source", "yaml"),
                "created_at": item.get("created_at", 0),
                "updated_at": item.get("updated_at", 0),
                "size": path.stat().st_size,
                "active": profile_id == active_id,
            }
        )
    profiles.sort(key=lambda item: (not item["active"], -item["updated_at"]))
    return {"profiles": profiles, "active_id": active_id}


@app.get("/api/profiles/{profile_id}")
def get_profile(profile_id: str):
    _, profile, path = require_profile(profile_id)
    return {
        "id": profile_id,
        "name": profile["name"],
        "source": profile.get("source", "yaml"),
        "active": profile_id == current_profile_id(),
        "config": path.read_text(encoding="utf-8"),
    }


@app.post("/api/profiles/subscription")
async def import_subscription(request: Request):
    data = await request.json()
    name = clean_name(data.get("name"))
    sub_url = str(data.get("sub_url") or "").strip()
    content = generate_base_yaml(sub_url)
    validate_with_mihomo(content)
    profile_id = save_profile(name, content, "subscription")
    if data.get("activate", True):
        return {**activate_profile(profile_id), "id": profile_id}
    return {"status": "success", "msg": "订阅配置已导入", "id": profile_id}


@app.post("/api/profiles/yaml")
async def import_yaml(request: Request):
    data = await request.json()
    name = clean_name(data.get("name"))
    content = prepare_config(str(data.get("raw_yaml") or ""))
    validate_with_mihomo(content)
    profile_id = save_profile(name, content, "yaml")
    if data.get("activate", True):
        return {**activate_profile(profile_id), "id": profile_id}
    return {"status": "success", "msg": "YAML 配置已导入", "id": profile_id}


@app.put("/api/profiles/{profile_id}")
async def update_profile(profile_id: str, request: Request):
    metadata, profile, path = require_profile(profile_id)
    data = await request.json()
    previous_content = path.read_text(encoding="utf-8")
    previous_profile = dict(profile)
    name = clean_name(data.get("name", profile["name"]))
    content = previous_content
    if "raw_yaml" in data:
        content = prepare_config(str(data.get("raw_yaml") or ""))
        validate_with_mihomo(content)

    atomic_write(path, content)
    profile["name"] = name
    profile["updated_at"] = int(time.time())
    metadata[profile_id] = profile
    write_metadata(metadata)

    if profile_id == current_profile_id() or data.get("activate"):
        try:
            return activate_profile(profile_id)
        except HTTPException:
            atomic_write(path, previous_content)
            metadata[profile_id] = previous_profile
            write_metadata(metadata)
            raise
    return {"status": "success", "msg": "配置已保存"}


@app.post("/api/profiles/{profile_id}/activate")
def activate_profile_api(profile_id: str):
    return activate_profile(profile_id)


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str):
    metadata, profile, path = require_profile(profile_id)
    if profile_id == current_profile_id():
        raise HTTPException(status_code=400, detail="当前运行配置不能删除，请先切换到其他配置")
    path.unlink()
    metadata.pop(profile_id, None)
    write_metadata(metadata)
    return {"status": "success", "msg": f"已删除“{profile['name']}”"}


@app.get("/api/status")
def get_status():
    port, secret = read_controller_settings()
    ui_ready = (MIHOMO_DIR / "ui" / "index.html").is_file()
    api_ready = False
    version = ""
    proxies = 0
    error = ""
    try:
        version_data = controller_request()
        proxy_data = controller_request("/proxies")
        api_ready = True
        version = str(version_data.get("version", ""))
        proxies = len(proxy_data.get("proxies", {}))
    except Exception as exc:
        error = str(exc)

    return {
        "service_active": service_is_active(),
        "api_ready": api_ready,
        "ui_ready": ui_ready,
        "controller_port": port,
        "secret": secret,
        "version": version,
        "proxies": proxies,
        "error": error,
    }


@app.post("/api/restart")
def restart_mihomo():
    if not MIHOMO_CONF.is_file():
        raise HTTPException(status_code=400, detail="当前没有运行配置")
    content = MIHOMO_CONF.read_text(encoding="utf-8")
    validate_with_mihomo(content)
    ok, error = restart_and_wait()
    if not ok:
        raise HTTPException(status_code=400, detail=f"Mihomo 启动失败: {error}")
    return {"status": "success", "msg": "Mihomo 已重启，控制器 API 正常"}


# Keep the original endpoints available for older browser caches.
@app.get("/api/config")
def get_config():
    if MIHOMO_CONF.exists():
        return {"config": MIHOMO_CONF.read_text(encoding="utf-8")}
    return {"config": ""}


@app.post("/api/update_sub")
async def legacy_update_sub(request: Request):
    data = await request.json()
    data.setdefault("name", f"订阅 {time.strftime('%Y-%m-%d %H:%M')}")
    content = generate_base_yaml(str(data.get("sub_url") or "").strip())
    validate_with_mihomo(content)
    profile_id = save_profile(clean_name(data["name"]), content, "subscription")
    return {**activate_profile(profile_id), "id": profile_id}


@app.post("/api/save_raw")
async def legacy_save_raw(request: Request):
    data = await request.json()
    active_id = current_profile_id()
    if active_id:
        metadata, profile, path = require_profile(active_id)
        content = prepare_config(str(data.get("raw_yaml") or ""))
        validate_with_mihomo(content)
        atomic_write(path, content)
        profile["updated_at"] = int(time.time())
        metadata[active_id] = profile
        write_metadata(metadata)
        return activate_profile(active_id)
    content = prepare_config(str(data.get("raw_yaml") or ""))
    validate_with_mihomo(content)
    profile_id = save_profile("导入配置", content, "yaml")
    return activate_profile(profile_id)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9621)
