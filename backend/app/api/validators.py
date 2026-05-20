"""通用名称校验工具

对文件名和文件夹名进行轻量级校验，确保：
1. 不为空、不超长
2. 不包含路径分隔符（防止路径穿越）
3. 不包含控制字符
4. 不使用系统保留名称
"""

import re

# 最大名称长度
MAX_NAME_LENGTH = 200

# 禁止的字符（路径分隔符 + 部分文件系统危险字符）
_FORBIDDEN_CHARS = re.compile(r'[/\\<>:"|?*\x00-\x1f]')

# Windows 保留名称
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


class NameValidationError(Exception):
    """名称校验失败"""

    def __init__(self, message: str, field: str = "name"):
        self.message = message
        self.field = field
        super().__init__(message)


def validate_name(name: str, field_label: str = "名称") -> str:
    """校验并清理文件/文件夹名称

    Args:
        name: 原始名称
        field_label: 字段标签，用于错误提示（如 "文件名"、"文件夹名"）

    Returns:
        清理后的名称（首尾空格已去除）

    Raises:
        NameValidationError: 校验失败时抛出
    """
    # 去除首尾空格
    cleaned = name.strip()

    # 空值检查
    if not cleaned:
        raise NameValidationError(f"{field_label}不能为空")

    # 长度检查
    if len(cleaned) > MAX_NAME_LENGTH:
        raise NameValidationError(
            f"{field_label}不能超过 {MAX_NAME_LENGTH} 个字符（当前 {len(cleaned)} 个）"
        )

    # 禁止字符检查
    match = _FORBIDDEN_CHARS.search(cleaned)
    if match:
        char = match.group()
        if ord(char) < 32:
            char_desc = f"控制字符(0x{ord(char):02x})"
        else:
            char_desc = f"'{char}'"
        raise NameValidationError(
            f"{field_label}包含不允许的字符: {char_desc}。"
            f"不能包含以下字符: / \\ < > : \" | ? *"
        )

    # 不能以点或空格结尾（Windows 文件系统限制）
    if cleaned.endswith(".") or cleaned.endswith(" "):
        raise NameValidationError(f"{field_label}不能以点号或空格结尾")

    # 保留名称检查（不区分大小写，去掉扩展名后比较）
    base_name = cleaned.split(".")[0].upper()
    if base_name in _RESERVED_NAMES:
        raise NameValidationError(
            f"{field_label} '{cleaned}' 是系统保留名称，请使用其他名称"
        )

    return cleaned


def validate_filename(filename: str) -> str:
    """校验上传文件名

    Args:
        filename: 原始文件名

    Returns:
        清理后的文件名

    Raises:
        NameValidationError: 校验失败时抛出
    """
    return validate_name(filename, field_label="文件名")


def validate_folder_name(name: str) -> str:
    """校验文件夹名称

    Args:
        name: 原始文件夹名

    Returns:
        清理后的文件夹名

    Raises:
        NameValidationError: 校验失败时抛出
    """
    return validate_name(name, field_label="文件夹名")
