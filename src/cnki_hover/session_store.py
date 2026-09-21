"""登录态存储（Fernet 对称加密落盘）。

- 密钥存于 KEY_FILE，首次生成后写入并尽量收紧权限（0o600）。
- 密文绝不等于明文（Fernet 输出为 base64 令牌）。
- load 在文件缺失/损坏时返回 None，不抛异常。
- 加密载荷内附带 saved_at（UTC ISO8601）与 max_age（秒），供过期判断。
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .paths import CONFIG_DIR, KEY_FILE, SESSION_FILE, ensure_dirs

DEFAULT_MAX_AGE = 60 * 60 * 24 * 7  # 7 天


def _parse_iso(ts: str) -> Optional[float]:
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


class SessionStore:
    def __init__(
        self,
        path: Optional[Path] = None,
        key_path: Optional[Path] = None,
    ):
        self._path = Path(path) if path else SESSION_FILE
        self._key_path = Path(key_path) if key_path else KEY_FILE
        self._max_age = DEFAULT_MAX_AGE

    def _load_key(self) -> bytes:
        ensure_dirs()
        if self._key_path.exists():
            with open(self._key_path, "rb") as f:
                key = f.read().strip()
            if not key:
                raise RuntimeError("密钥文件为空，请删除后重试以重新生成")
            return key
        key = Fernet.generate_key()
        with open(self._key_path, "wb") as f:
            f.write(key)
        try:
            os.chmod(self._key_path, 0o600)
        except OSError:
            pass
        return key

    def _fernet(self) -> Fernet:
        return Fernet(self._load_key())

    def save(self, data: dict) -> None:
        ensure_dirs()
        payload = dict(data)
        payload["saved_at"] = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        payload["max_age"] = self._max_age

        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        token = self._fernet().encrypt(raw)

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent), suffix=".tmp", prefix=".sess_"
            )
            with os.fdopen(fd, "wb") as f:
                f.write(token)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
            tmp_path = None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def load(self) -> Optional[dict]:
        if not self._path.exists():
            return None
        try:
            with open(self._path, "rb") as f:
                token = f.read()
            raw = self._fernet().decrypt(token)
            return json.loads(raw.decode("utf-8"))
        except (InvalidToken, json.JSONDecodeError, OSError, Exception):
            return None

    def exists(self) -> bool:
        return self._path.exists()

    def clear(self) -> None:
        if self._path.exists():
            try:
                os.remove(self._path)
            except OSError:
                pass

    def is_expired(self) -> bool:
        data = self.load()
        if not data:
            return True
        ts = _parse_iso(data.get("saved_at", ""))
        max_age = data.get("max_age", self._max_age)
        if ts is None:
            return True
        return (datetime.datetime.now().timestamp() - ts) > max_age


def get_store() -> SessionStore:
    return SessionStore()
