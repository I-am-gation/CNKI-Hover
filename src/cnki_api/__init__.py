"""CNKI 机构登录 / 会话管理（A2 产出）。

纯 HTTP 复现知网「校外访问(CARSI / Shibboleth)」机构登录，
拿到可用登录态并加密持久化。下游 B2/B3 依赖本模块导出的契约。
"""
from __future__ import annotations

from .auth import (
    AuthResult,
    LoginError,
    get_authenticated_client,
    is_session_valid,
    login,
    session_summary,
)

__all__ = [
    "LoginError",
    "AuthResult",
    "login",
    "get_authenticated_client",
    "is_session_valid",
    "session_summary",
]
