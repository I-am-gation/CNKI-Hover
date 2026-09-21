"""本地缓存层（C4 产出）。

三级缓存：**题录(record) / 正文(text) / 页图(page)**
- 元数据走 SQLite（`<root>/index.db`）：key / kind / path / size / created_at / last_access
- 内容走文件（大对象不进库）：`<root>/app/{records,text,pages}/...`

容量与淘汰：
- 上限可配（默认取 config `cache_max_mb`，缺省 500MB）
- **LRU 淘汰**：`put_*` 后自动 `enforce_limit()`，按 `last_access` 从旧到新删，直到总量 ≤ 上限
- 统计/淘汰/清空都覆盖**整个 cache 根目录**（含 A4 落在 `<root>/reader/` 的原版 PDF 与页图），
  未登记在索引里的文件按 mtime 参与 LRU —— 避免出现「两套缓存各算各的」。

公共契约（C2 / C3 / C5 设置页 / D1 依赖）：
    cache = LocalCache() / get_cache()
    cache.put_text(key, text, meta=None) -> Path
    cache.get_text(key) -> str | None
    cache.put_json(key, obj) / cache.get_json(key)
    cache.put_page(key, page, dpi, data) / cache.get_page(key, page, dpi) / has_page(...)
    cache.stats() -> dict          # entries / total_bytes / limit_bytes / by_kind
    cache.enforce_limit() -> int   # 返回本次淘汰的字节数
    cache.purge() -> int           # 清空，返回释放字节数
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .config import load_config
from .log import get_logger, log_event
from .paths import OUTPUTS_DIR

LOG = get_logger("cache")

KIND_RECORD = "record"
KIND_TEXT = "text"
KIND_PAGE = "page"
KINDS = (KIND_RECORD, KIND_TEXT, KIND_PAGE)

_EXT = {KIND_RECORD: ".json", KIND_TEXT: ".txt", KIND_PAGE: ".png"}
_DEFAULT_MAX_MB = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    kind        TEXT NOT NULL,
    key         TEXT NOT NULL,
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    created_at  REAL NOT NULL,
    last_access REAL NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_last_access ON entries(last_access);
"""


def safe_key(key: str) -> str:
    """把任意 key（url / 查询串 / 页码组合）映射成安全文件名。"""
    import hashlib
    import re
    h = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:24]
    head = re.sub(r"[^0-9A-Za-z_\-]", "_", str(key))[:28].strip("_")
    return ("%s_%s" % (head, h)) if head else h


class LocalCache:
    def __init__(self, root: Optional[Path] = None, max_mb: Optional[int] = None,
                 db_path: Optional[Path] = None):
        self.root = Path(root) if root else (Path(OUTPUTS_DIR) / "cache")
        self.app_dir = self.root / "app"
        for k in KINDS:
            (self.app_dir / (k + "s")).mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path else (self.root / "index.db")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        if max_mb is None:
            try:
                max_mb = int(load_config().get("cache_max_mb", _DEFAULT_MAX_MB))
            except Exception:  # noqa: BLE001
                max_mb = _DEFAULT_MAX_MB
        self.max_bytes = max(1, int(max_mb)) * 1024 * 1024

    # ---------------------------------------------------------------- 内部
    def _dir(self, kind: str) -> Path:
        return self.app_dir / (kind + "s")

    def _row(self, kind: str, key: str):
        cur = self._conn.execute("SELECT path,size,last_access FROM entries WHERE kind=? AND key=?",
                                 (kind, str(key)))
        return cur.fetchone()

    def has(self, kind: str, key: str) -> bool:
        with self._lock:
            row = self._row(kind, key)
        if not row:
            return False
        return Path(row[0]).exists()

    def _touch(self, kind: str, key: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE entries SET last_access=? WHERE kind=? AND key=?",
                               (time.time(), kind, str(key)))
            self._conn.commit()

    def put(self, kind: str, key: str, data: bytes, ext: Optional[str] = None,
            meta: Optional[dict] = None) -> Optional[Path]:
        """写入一条缓存（自动 LRU 淘汰）。"""
        try:
            p = self._dir(kind) / (safe_key(key) + (ext or _EXT.get(kind, ".bin")))
            p.write_bytes(data)
            now = time.time()
            with self._lock:
                self._conn.execute(
                    "INSERT INTO entries(kind,key,path,size,created_at,last_access) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(kind,key) DO UPDATE SET path=excluded.path, size=excluded.size, "
                    "last_access=excluded.last_access",
                    (kind, str(key), str(p), len(data), now, now))
                self._conn.commit()
            if meta:
                self.put_json("%s::meta" % key, meta)
            self.enforce_limit()
            return p
        except Exception as e:  # noqa: BLE001
            log_event(LOG, "put_failed", kind=kind, error=str(e)[:120])
            return None

    def get(self, kind: str, key: str) -> Optional[bytes]:
        """读一条缓存（命中即刷新 last_access）。"""
        with self._lock:
            row = self._row(kind, key)
        if not row:
            return None
        p = Path(row[0])
        if not p.exists():
            return None
        try:
            data = p.read_bytes()
        except Exception:  # noqa: BLE001
            return None
        self._touch(kind, key)
        return data

    # ---------------------------------------------------------------- 便捷层
    def put_text(self, key: str, text: str, meta: Optional[dict] = None) -> Optional[Path]:
        return self.put(KIND_TEXT, key, text.encode("utf-8"), ".txt", meta)

    def get_text(self, key: str) -> Optional[str]:
        d = self.get(KIND_TEXT, key)
        return d.decode("utf-8", errors="replace") if d is not None else None

    def put_json(self, key: str, obj) -> Optional[Path]:
        return self.put(KIND_RECORD, key,
                        json.dumps(obj, ensure_ascii=False).encode("utf-8"), ".json")

    def get_json(self, key: str):
        d = self.get(KIND_RECORD, key)
        if d is None:
            return None
        try:
            return json.loads(d.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def page_key(key: str, page: int, dpi: int) -> str:
        return "%s#p%d@%ddpi" % (key, int(page), int(dpi))

    def put_page(self, key: str, page: int, dpi: int, data: bytes) -> Optional[Path]:
        return self.put(KIND_PAGE, self.page_key(key, page, dpi), data, ".png")

    def get_page(self, key: str, page: int, dpi: int) -> Optional[bytes]:
        return self.get(KIND_PAGE, self.page_key(key, page, dpi))

    def has_page(self, key: str, page: int, dpi: int) -> bool:
        return self.has(KIND_PAGE, self.page_key(key, page, dpi))

    # ---------------------------------------------------------------- 统计 / 淘汰
    def _all_files(self) -> list:
        out = []
        for p in self.root.rglob("*"):
            if not p.is_file() or p.name == self.db_path.name:
                continue
            try:
                out.append((p, p.stat().st_size, p.stat().st_mtime))
            except OSError:
                continue
        return out

    def stats(self) -> dict:
        files = self._all_files()
        total = sum(s for _p, s, _m in files)
        with self._lock:
            cur = self._conn.execute("SELECT kind, COUNT(*), COALESCE(SUM(size),0) FROM entries GROUP BY kind")
            by_kind = {k: {"entries": c, "bytes": b} for k, c, b in cur.fetchall()}
        return {
            "root": str(self.root),
            "entries": len(files),
            "tracked_entries": sum(v["entries"] for v in by_kind.values()),
            "total_bytes": total,
            "limit_bytes": self.max_bytes,
            "usage_ratio": round(total / self.max_bytes, 4) if self.max_bytes else 0,
            "by_kind": by_kind,
        }

    def enforce_limit(self) -> int:
        """按 LRU（last_access，未登记者用 mtime）淘汰，直到总量 ≤ 上限。返回淘汰字节数。"""
        total = sum(s for _p, s, _m in self._all_files())
        if total <= self.max_bytes:
            return 0

        with self._lock:
            cur = self._conn.execute(
                "SELECT kind,key,path,last_access FROM entries ORDER BY last_access ASC")
            tracked = cur.fetchall()

        # 先淘汰「有索引的」按 last_access 从旧到新；索引耗尽后再按 mtime 淘汰余下文件
        evicted = 0
        for kind, key, path, _la in tracked:
            if total - evicted <= self.max_bytes:
                break
            p = Path(path)
            try:
                sz = p.stat().st_size if p.exists() else 0
                if p.exists():
                    p.unlink()
                with self._lock:
                    self._conn.execute("DELETE FROM entries WHERE kind=? AND key=?", (kind, key))
                    self._conn.commit()
                evicted += sz
            except OSError:
                continue

        if total - evicted > self.max_bytes:
            rest = sorted(self._all_files(), key=lambda x: x[2])  # 旧 → 新
            for p, sz, _m in rest:
                if total - evicted <= self.max_bytes:
                    break
                try:
                    p.unlink()
                    evicted += sz
                except OSError:
                    continue
        if evicted:
            log_event(LOG, "lru_evicted", bytes=evicted, total_after=total - evicted,
                      limit=self.max_bytes)
        return evicted

    def purge(self, kinds=None) -> int:
        """清空缓存（默认清**整个 cache 根目录**，含 A4 的 reader 子目录）。返回释放字节数。"""
        freed = 0
        if kinds:
            for k in kinds:
                for p in self._dir(k).glob("*"):
                    try:
                        freed += p.stat().st_size
                        p.unlink()
                    except OSError:
                        continue
            with self._lock:
                self._conn.execute("DELETE FROM entries WHERE kind IN (%s)"
                                   % ",".join("?" * len(kinds)), tuple(kinds))
                self._conn.commit()
        else:
            for p, sz, _m in self._all_files():
                try:
                    p.unlink()
                    freed += sz
                except OSError:
                    continue
            with self._lock:
                self._conn.execute("DELETE FROM entries")
                self._conn.commit()
        log_event(LOG, "purged", bytes=freed, kinds=list(kinds) if kinds else "all")
        return freed

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass


_CACHE: Optional[LocalCache] = None
_CACHE_LOCK = threading.Lock()


def get_cache() -> LocalCache:
    """进程内共享缓存实例。"""
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            _CACHE = LocalCache()
        return _CACHE
