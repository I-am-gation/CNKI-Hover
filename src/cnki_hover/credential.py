"""本地凭证解析（只读 secrets/account.txt）。

文件格式：逐行 `key=value`，key 支持 机构名称/机构/账号/用户名/密码。
注释行（不以已知 key 开头，或无 `=`）一律跳过——不会被「填写」等字样误判。
值若含占位符标记则抛 CredentialError；缺字段亦抛 CredentialError。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .paths import ACCOUNT_FILE

# 占位符判定标记（值中出现任一即视为未填写）
PLACEHOLDER_MARKERS = ("（填", "(填", "填：", "占位", "TODO", "<", ">")

# key 别名 -> 标准字段名
_KEY_ALIASES = {
    "机构名称": "institution",
    "机构": "institution",
    "账号": "username",
    "用户名": "username",
    "密码": "password",
}

_REQUIRED = ("institution", "username", "password")


class CredentialError(Exception):
    """凭证缺失或仍为占位符时抛出。"""


@dataclass
class Credentials:
    institution: str
    username: str
    password: str


def load_credentials(path: Optional[Path] = None) -> Credentials:
    p = Path(path) if path else ACCOUNT_FILE
    found: dict[str, str] = {}

    with open(p, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                # 非 key=value 行（如说明注释）跳过，不解析、不判占位符
                continue
            key, _, val = line.partition("=")
            alias = _KEY_ALIASES.get(key.strip())
            if alias is None:
                # 未知 key（含注释行、多余行）跳过
                continue
            value = val.strip()
            if any(m in value for m in PLACEHOLDER_MARKERS):
                raise CredentialError(
                    f"账号或密码仍是占位符，请填写真实凭证（字段：{alias}）"
                )
            found[alias] = value

    missing = [k for k in _REQUIRED if k not in found]
    if missing:
        raise CredentialError(f"凭证文件缺少必需字段：{', '.join(missing)}")

    return Credentials(
        institution=found["institution"],
        username=found["username"],
        password=found["password"],
    )


def has_valid_credentials(path: Optional[Path] = None) -> bool:
    try:
        load_credentials(path)
        return True
    except (CredentialError, OSError):
        return False
