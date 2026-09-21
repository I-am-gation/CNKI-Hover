"""应用配置（JSON 文件，原子写，支持变更回调）。

约定：
- 默认配置见 DEFAULT_CONFIG。
- 写入使用「临时文件 + os.replace」保证原子性，避免半写文件。
- on_change 回调在每次 save() 后触发，供 C5 等模块做热更新。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

from .paths import CONFIG_FILE, ensure_dirs

DEFAULT_CONFIG: dict = {
    "hotkey": "Alt+Space",
    "default_field": "SU",          # SU=主题
    "match_mode": "fuzzy",          # fuzzy | exact
    "theme": "dark",
    "cache_max_mb": 500,
    "autostart": False,
    "institution": "",             # 使用者自行配置（配合 institutions.json）
    "min_request_interval": 2.0,
    "prefetch_enabled": True,
    "prefetch_delay_ms": 1000,
}


class Config:
    def __init__(self, path: Optional[Path] = None):
        self._path = Path(path) if path else CONFIG_FILE
        self._data: dict = dict(DEFAULT_CONFIG)
        self._callbacks: list[Callable[["Config"], None]] = []

    def load(self) -> "Config":
        if self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data.update(loaded)
            except (json.JSONDecodeError, OSError):
                # 损坏文件静默回退到默认，不抛异常
                pass
        return self

    def save(self) -> None:
        ensure_dirs()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._path.parent), suffix=".tmp", prefix=".cfg_"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
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
        self._notify()

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    def update(self, mapping: dict) -> None:
        self._data.update(mapping)
        self.save()

    def as_dict(self) -> dict:
        return dict(self._data)

    def reset(self) -> None:
        self._data = dict(DEFAULT_CONFIG)
        self.save()

    def on_change(self, callback: Callable[["Config"], None]) -> None:
        """注册变更回调，save() 时统一触发（供 C5 热更新）。"""
        self._callbacks.append(callback)

    def _notify(self) -> None:
        for cb in self._callbacks:
            try:
                cb(self)
            except Exception:
                # 回调异常不应影响保存结果
                pass


def load_config() -> Config:
    return Config().load()
