"""极简 HTTP 框架：路由、JSON、静态文件、Cookie，全部基于标准库。

不用 FastAPI/uvicorn 是有原因的：pydantic 在 armv7、riscv64 这类平台上常常没有
预编译 wheel，pip 现场编译会失败。标准库 http.server 足够撑起一个单机管理面板。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import socket
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger("panel")

MAX_BODY = 16 * 1024 * 1024
_PARAM = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class HTTPError(Exception):
    """业务异常，会被统一转成 JSON 错误响应。"""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Request:
    def __init__(
        self,
        method: str,
        path: str,
        query: Dict[str, List[str]],
        headers: Dict[str, str],
        body: bytes,
        client: str,
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.client = client

    def json(self) -> dict:
        if not self.body:
            return {}
        # 强制 JSON 请求头，顺带挡掉跨站表单伪造（表单发不出这个 Content-Type）。
        if "application/json" not in self.headers.get("content-type", ""):
            raise HTTPError(415, "请求头需要 Content-Type: application/json")
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "请求体不是合法的 JSON")
        if not isinstance(data, dict):
            raise HTTPError(400, "请求体必须是 JSON 对象")
        return data

    def arg(self, name: str, default: str = "") -> str:
        values = self.query.get(name) or []
        return values[0] if values else default

    def int_arg(self, name: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(self.arg(name, str(default)))))
        except ValueError:
            return default

    def cookie(self, name: str) -> str:
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("cookie", ""))
        except Exception:
            return ""
        item = jar.get(name)
        return item.value if item else ""


class Response:
    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        content_type: str = "application/json; charset=utf-8",
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = {"Content-Type": content_type}
        self.headers.update(headers or {})
        self.cookies: List[str] = []

    def set_cookie(self, name: str, value: str, max_age: Optional[int] = None) -> "Response":
        parts = ["%s=%s" % (name, value), "Path=/", "HttpOnly", "SameSite=Lax"]
        if max_age is not None:
            parts.append("Max-Age=%d" % max_age)
        self.cookies.append("; ".join(parts))
        return self

    @classmethod
    def of_json(cls, data, status: int = 200) -> "Response":
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        return cls(status, raw, headers={"Cache-Control": "no-store"})

    @classmethod
    def of_text(cls, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> "Response":
        return cls(status, text.encode("utf-8"), content_type)

    @classmethod
    def redirect(cls, location: str) -> "Response":
        return cls(302, b"", "text/plain", {"Location": location})


class Router:
    def __init__(self) -> None:
        self.routes: List[Tuple[str, "re.Pattern", Callable, bool]] = []

    def add(self, method: str, pattern: str, handler: Callable, public: bool = False) -> None:
        regex = re.compile("^" + _PARAM.sub(r"(?P<\1>[^/]+)", pattern) + "$")
        self.routes.append((method.upper(), regex, handler, public))

    def route(self, method: str, pattern: str, public: bool = False) -> Callable:
        def wrapper(handler: Callable) -> Callable:
            self.add(method, pattern, handler, public)
            return handler

        return wrapper

    def match(self, method: str, path: str):
        allowed = False
        for route_method, regex, handler, public in self.routes:
            found = regex.match(path)
            if not found:
                continue
            if route_method != method.upper():
                allowed = True
                continue
            # 路径参数要解码：策略组名常含中文和空格，不解码会二次编码导致内核 404。
            params = {key: unquote(value or "") for key, value in found.groupdict().items()}
            return handler, params, public
        raise HTTPError(405 if allowed else 404, "接口不存在" if not allowed else "请求方法不被支持")

def file_response(path: Path, request: Optional[Request] = None) -> Response:
    try:
        stat = path.stat()
        raw = path.read_bytes()
    except OSError:
        raise HTTPError(404, "文件不存在: %s" % path.name)
    etag = '"%x-%x"' % (int(stat.st_mtime), stat.st_size)
    if request is not None and request.headers.get("if-none-match") == etag:
        return Response(304, b"", "text/plain", {"ETag": etag})
    guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if guessed.startswith("text/") or guessed in ("application/javascript", "application/json"):
        guessed += "; charset=utf-8"
    return Response(200, raw, guessed, {"ETag": etag, "Cache-Control": "no-cache"})


class App:
    def __init__(
        self,
        router: Router,
        static_dir: Optional[Path] = None,
        static_prefix: str = "/assets/",
        before: Optional[Callable] = None,
    ) -> None:
        self.router = router
        self.static_dir = static_dir
        self.static_prefix = static_prefix
        self.before = before

    def handle(self, request: Request) -> Response:
        try:
            if self.static_dir and request.method in ("GET", "HEAD") and request.path.startswith(self.static_prefix):
                return self._static(request)
            handler, params, public = self.router.match(request.method, request.path)
            if self.before is not None:
                early = self.before(request, public)
                if early is not None:
                    return early
            result = handler(request, **params)
            if isinstance(result, Response):
                return result
            return Response.of_json({"status": "success"} if result is None else result)
        except HTTPError as exc:
            return Response.of_json({"detail": exc.detail}, exc.status)
        except Exception as exc:  # noqa: BLE001 - 兜底，别让线程直接崩
            log.exception("%s %s 处理失败", request.method, request.path)
            return Response.of_json({"detail": "面板内部错误：%s" % exc}, 500)

    def _static(self, request: Request) -> Response:
        assert self.static_dir is not None
        root = self.static_dir.resolve()
        target = (root / request.path[len(self.static_prefix):]).resolve()
        try:
            if os.path.commonpath([str(root), str(target)]) != str(root):
                raise HTTPError(403, "非法路径")
        except ValueError:
            raise HTTPError(403, "非法路径")
        return file_response(target, request)


class _Handler(BaseHTTPRequestHandler):
    server_version = "mihomo-panel"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: App = None  # 由 serve() 注入

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY:
            raise HTTPError(413, "请求体过大")
        return self.rfile.read(length)

    def _run(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_body()
        except HTTPError as exc:
            response = Response.of_json({"detail": exc.detail}, exc.status)
        else:
            request = Request(
                method,
                parsed.path or "/",
                parse_qs(parsed.query),
                {key.lower(): value for key, value in self.headers.items()},
                body,
                self.client_address[0] if self.client_address else "-",
            )
            response = self.app.handle(request)
        self._send(response, include_body=method != "HEAD")

    def _send(self, response: Response, include_body: bool = True) -> None:
        try:
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            for cookie in response.cookies:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if include_body and response.body:
                self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):  # noqa: N802
        self._run("GET")

    def do_HEAD(self):  # noqa: N802
        self._run("HEAD")

    def do_POST(self):  # noqa: N802
        self._run("POST")

    def do_PUT(self):  # noqa: N802
        self._run("PUT")

    def do_DELETE(self):  # noqa: N802
        self._run("DELETE")

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    def version_string(self) -> str:
        return self.server_version


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(app: App, host: str, port: int) -> _Server:
    handler = type("PanelHandler", (_Handler,), {"app": app})
    if ":" in host:
        _Server.address_family = socket.AF_INET6
    server = _Server((host, port), handler)
    return server
