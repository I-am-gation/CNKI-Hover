"""项目路径约定。

- PROJECT_ROOT 默认由本文件路径反推：src/cnki_hover/paths.py -> parents[2] 即根。
- 允许通过环境变量 CNKI_HOVER_HOME 覆盖（打包部署或便携场景）。
- 对 PyInstaller 单文件打包（sys._MEIPASS）留有余地：未做特殊处理，
  但 PROJECT_ROOT 始终可被 CNKI_HOVER_HOME 重定向到可写目录。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ENV_HOME = "CNKI_HOVER_HOME"


def _resolve_root() -> Path:
    override = os.environ.get(_ENV_HOME)
    if override:
        return Path(override).resolve()
    # 打包态（PyInstaller）：绝不能落在 _MEIPASS —— 那是临时解包目录，
    # 单文件模式退出即删、目录模式也在安装目录内（通常不可写）。
    # 配置/会话/缓存必须落到**用户可写且持久**的位置。
    if getattr(sys, "frozen", False):
        base = (os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
                or str(Path.home()))
        root = Path(base) / "CNKI-Hover"
        root.mkdir(parents=True, exist_ok=True)
        return root
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT: Path = _resolve_root()
SRC_DIR: Path = PROJECT_ROOT / "src"
CONFIG_DIR: Path = PROJECT_ROOT / "config"
LOG_DIR: Path = PROJECT_ROOT / "logs"
OUTPUTS_DIR: Path = PROJECT_ROOT / "outputs"
SECRETS_DIR: Path = PROJECT_ROOT / "secrets"
DOCS_API_DIR: Path = PROJECT_ROOT / "docs" / "api"

CONFIG_FILE: Path = CONFIG_DIR / "config.json"
SESSION_FILE: Path = CONFIG_DIR / "session.enc"
KEY_FILE: Path = CONFIG_DIR / ".keyfile"
ACCOUNT_FILE: Path = SECRETS_DIR / "account.txt"


def ensure_dirs() -> None:
    """幂等地创建运行时目录（CONFIG/LOG/OUTPUTS/DOCS_API）。"""
    for d in (CONFIG_DIR, LOG_DIR, OUTPUTS_DIR, DOCS_API_DIR):
        d.mkdir(parents=True, exist_ok=True)
