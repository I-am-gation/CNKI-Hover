"""CNKI HTTP 客户端。

硬要求：
1. 代理规避：构造时清除进程级代理环境变量，并设置 NO_PROXY；
   同时 session.trust_env=False，彻底不走系统/环境代理。
2. 节流：同实例两次 request 之间至少 min_interval 秒（默认 2.0），
   用单调时钟 time.monotonic() 计时，不足则 sleep。提供 self.last_request_ts
   与 _throttle() 返回的实际等待秒数，便于 verify 断言。
3. 默认带真实浏览器 UA。
4. 日志只记 URL（敏感 query 已脱敏）、状态码、耗时、字节数；绝不含凭证/登录态。
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from .log import get_logger, log_event
from .session_store import SessionStore, get_store

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 日志中对 URL 脱敏时移除的敏感 query 参数名（小写）
_SENSITIVE_QUERY_KEYS = {
    "password", "pwd", "passwd", "token", "ticket", "code", "vcode",
    "captcha", "sid", "sessionid", "cookie", "auth", "sign", "signature",
}

_PROXY_ENV_KEYS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
)


class CnkiHttpClient:
    def __init__(
        self,
        session_data: Optional[dict] = None,
        min_interval: float = 2.0,
        timeout: float = 20.0,
        logger: Any = None,
    ):
        # 1) 代理规避：清除环境代理 + 设置 NO_PROXY + trust_env=False
        for k in _PROXY_ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")

        self._session = requests.Session()
        self._session.trust_env = False
        self._session.headers["User-Agent"] = DEFAULT_UA

        self.min_interval = float(min_interval)
        self.timeout = float(timeout)
        self.logger = logger or get_logger("cnki_http")

        self.last_request_ts: float = 0.0  # 单调时钟，上次请求时刻
        self.last_cost_ms: float = 0.0     # 最近一次请求的纯网络耗时（不含节流等待）
        self.last_waited_ms: float = 0.0   # 最近一次请求为满足节流而实际等待的毫秒数

        if session_data:
            self.apply_session(session_data)

    @property
    def session(self) -> requests.Session:
        return self._session

    def apply_session(self, session_data: dict) -> None:
        """从 {"cookies": {...}, "headers": {...}} 恢复登录态。"""
        cookies = session_data.get("cookies") or {}
        for name, value in cookies.items():
            if value is None:
                continue
            self._session.cookies.set(str(name), str(value))
        headers = session_data.get("headers") or {}
        for name, value in headers.items():
            if value is None:
                continue
            self._session.headers[str(name)] = str(value)

    def dump_session(self) -> dict:
        """导出当前登录态，供落盘加密存储。"""
        return {
            "cookies": {k: v for k, v in self._session.cookies.items()},
            "headers": {k: v for k, v in self._session.headers.items()},
        }

    def _throttle(self) -> float:
        """确保距上次请求至少 min_interval 秒，返回实际等待秒数。"""
        now = time.monotonic()
        if self.last_request_ts > 0:
            elapsed = now - self.last_request_ts
            if elapsed < self.min_interval:
                wait = self.min_interval - elapsed
                time.sleep(wait)
                return wait
        return 0.0

    def request(self, method: str, url: str, *, throttle: bool = True, **kwargs):
        waited = 0.0
        if throttle:
            waited = self._throttle()
        self.last_request_ts = time.monotonic()

        safe_url = self._sanitize_url(url)
        t0 = time.monotonic()
        resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
        dt = time.monotonic() - t0
        self.last_cost_ms = dt * 1000.0
        self.last_waited_ms = waited * 1000.0

        # 4) 日志脱敏：仅记 URL（去敏感 query）、状态码、耗时、字节数
        log_event(
            self.logger,
            "http_request",
            method=method.upper(),
            url=safe_url,
            status=resp.status_code,
            cost_ms=round(dt * 1000, 1),
            bytes=len(resp.content or b""),
            waited_ms=round(waited * 1000, 1),
        )
        return resp

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def save_to_store(self) -> None:
        get_store().save(self.dump_session())

    @classmethod
    def from_store(cls, **kwargs) -> "CnkiHttpClient":
        data = get_store().load() or {}
        return cls(session_data=data, **kwargs)

    @staticmethod
    def _sanitize_url(url: str) -> str:
        """移除 query 中的敏感参数用于日志，不改动真实请求。"""
        try:
            parts = urlsplit(url)
            q = parse_qsl(parts.query, keep_blank_values=True)
            q = [(k, "***") for (k, v) in q if k.lower() in _SENSITIVE_QUERY_KEYS]
            # 仅保留非敏感参数，敏感参数脱敏为 ***
            kept = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
                    if k.lower() not in _SENSITIVE_QUERY_KEYS]
            new_query = urlencode(kept)
            return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
        except Exception:
            return url
