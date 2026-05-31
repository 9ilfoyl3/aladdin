"""用户名与口令的输入校验（tenant-auth）。

集中定义规则，供 DTO 复用（Controller 不散落校验）。校验失败抛
ValidationInputError -> 全局异常处理器映射为 400。

规则要点：
- 用户名：3–32 字符，仅 [A-Za-z0-9_.-]，必须以字母/数字开头，大小写不敏感语义由唯一约束保证。
- 口令：8–64 字符且 ≤72 字节（bcrypt 上限），至少包含字母与数字两类，禁止纯空白/控制字符。
  不做"必须含特殊字符"的强约束（可用性与安全的平衡），但拒绝弱长度与单一字符集。
"""

from __future__ import annotations

import re

from app.api.errors import ValidationInputError

# 用户名：以字母/数字开头，整体 3–32，允许字母数字与 _ . -
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")

USERNAME_MIN = 3
USERNAME_MAX = 32
PASSWORD_MIN = 8
PASSWORD_MAX = 64
_PASSWORD_MAX_BYTES = 72  # bcrypt 硬上限


def validate_username(username: str) -> str:
    """校验并返回规整后的用户名（去首尾空白）。非法 -> 400。"""
    if username is None:
        raise ValidationInputError("用户名不能为空")
    name = username.strip()
    if len(name) < USERNAME_MIN or len(name) > USERNAME_MAX:
        raise ValidationInputError(f"用户名长度需为 {USERNAME_MIN}–{USERNAME_MAX} 个字符")
    if not _USERNAME_RE.match(name):
        raise ValidationInputError(
            "用户名只能包含字母、数字、下划线、点、连字符，且以字母或数字开头"
        )
    return name


def validate_password(password: str) -> str:
    """校验口令强度。非法 -> 400。返回原值（不做规整，口令保留原样）。"""
    if password is None:
        raise ValidationInputError("口令不能为空")
    if len(password) < PASSWORD_MIN or len(password) > PASSWORD_MAX:
        raise ValidationInputError(f"口令长度需为 {PASSWORD_MIN}–{PASSWORD_MAX} 个字符")
    if len(password.encode("utf-8")) > _PASSWORD_MAX_BYTES:
        raise ValidationInputError(f"口令过长：编码后不得超过 {_PASSWORD_MAX_BYTES} 字节")
    # 禁止包含控制字符（含换行、制表等）
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in password):
        raise ValidationInputError("口令不能包含控制字符")
    has_alpha = any(ch.isalpha() for ch in password)
    has_digit = any(ch.isdigit() for ch in password)
    if not (has_alpha and has_digit):
        raise ValidationInputError("口令需至少同时包含字母与数字")
    return password


def validate_tenant_name(name: str) -> str:
    """校验租户名（1–64，去首尾空白，非空）。非法 -> 400。"""
    if name is None:
        raise ValidationInputError("租户名不能为空")
    n = name.strip()
    if len(n) < 1 or len(n) > 64:
        raise ValidationInputError("租户名长度需为 1–64 个字符")
    return n


def validate_role_name(name: str) -> str:
    """校验角色名（1–32，去首尾空白）。非法 -> 400。"""
    if name is None:
        raise ValidationInputError("角色名不能为空")
    n = name.strip()
    if len(n) < 1 or len(n) > 32:
        raise ValidationInputError("角色名长度需为 1–32 个字符")
    return n
