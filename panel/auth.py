"""登录鉴权：pbkdf2 口令散列、HMAC 签名会话 cookie、按 IP 的失败限速。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

ITERATIONS = 120000
COOKIE_NAME = "mihomo_panel"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (ITERATIONS, salt.hex(), digest.hex())


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, rounds, salt_hex, digest_hex = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, body: str) -> str:
    return _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())

def issue_token(secret: str, ttl: int) -> str:
    """无状态会话：签名里带过期时间，面板重启后已登录的浏览器不会被踢。"""
    payload = {"exp": int(time.time()) + max(300, ttl), "nonce": secrets.token_hex(8)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return body + "." + _sign(secret, body)


def check_token(token: str, secret: str) -> bool:
    try:
        body, signature = token.split(".", 1)
        if not hmac.compare_digest(signature, _sign(secret, body)):
            return False
        payload = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return False
    return int(payload.get("exp", 0)) > time.time()


class LoginGuard:
    """暴力破解防护：同一 IP 连续失败若干次后短暂锁定。"""

    def __init__(self, limit: int = 6, window: int = 300, lock_for: int = 180) -> None:
        self.limit = limit
        self.window = window
        self.lock_for = lock_for
        self._lock = threading.Lock()
        self._hits: Dict[str, Tuple[int, float]] = {}

    def locked_for(self, ip: str) -> int:
        with self._lock:
            count, stamp = self._hits.get(ip, (0, 0.0))
            if count < self.limit:
                return 0
            remain = int(stamp + self.lock_for - time.time())
            if remain <= 0:
                self._hits.pop(ip, None)
                return 0
            return remain

    def record_failure(self, ip: str) -> None:
        with self._lock:
            count, stamp = self._hits.get(ip, (0, 0.0))
            now = time.time()
            count = count + 1 if now - stamp < self.window else 1
            self._hits[ip] = (count, now)

    def reset(self, ip: str) -> None:
        with self._lock:
            self._hits.pop(ip, None)
