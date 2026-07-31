"""Provider 注册表与 UI 元数据

`provider_type` 的取值域、API 层校验、前端下拉选项全部由本注册表派生，
新增一种 OCR 服务只需新增一个 Provider 文件并挂上 ``@register_provider``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, TypeVar

if TYPE_CHECKING:
    from .provider import OCRProvider

PROVIDER_REGISTRY: dict[str, type["OCRProvider"]] = {}

_T = TypeVar("_T", bound=type)


def register_provider(provider_type: str) -> Callable[[_T], _T]:
    """把 Provider 类注册到 ``provider_type`` 标识下"""

    def _decorator(cls: _T) -> _T:
        PROVIDER_REGISTRY[provider_type] = cls  # type: ignore[assignment]
        return cls

    return _decorator


@dataclass(frozen=True)
class ProviderMeta:
    """Provider 的 UI 展示元数据（能力部分从 Provider 类的 capability 派生）

    Attributes:
        label: 下拉框显示名。
        summary: 一句话说明该服务的定位。
        api_url_example: 端点地址示例（帮助用户填对路径，而不是只填主机）。
        extra_config_keys: 该 Provider 支持的 ``extra_config`` 键说明。
    """

    label: str
    summary: str
    api_url_example: str
    extra_config_keys: dict[str, str]


PROVIDER_META: dict[str, ProviderMeta] = {
    "vl": ProviderMeta(
        label="VL 文件解析服务",
        summary="多模态大模型解析，直接接受 PDF，输出带表格结构的 Markdown，质量最高、耗时较长。",
        api_url_example="http://10.30.1.3:8909/parse",
        extra_config_keys={},
    ),
    "paddle": ProviderMeta(
        label="PaddleOCR",
        summary="纯文字识别，速度快；只接受图片，PDF 由系统按页渲染成整页图片后逐页识别。",
        api_url_example="http://10.30.1.3:8989/ocr",
        extra_config_keys={},
    ),
    "mineru": ProviderMeta(
        label="MinerU",
        summary="版面还原能力强，接受 PDF 与图片，输出 Markdown（含 LaTeX 公式与 HTML 表格）。",
        api_url_example="http://10.30.1.3:8000/file_parse",
        extra_config_keys={
            "backend": "解析后端，默认 pipeline（CPU 部署必须用 pipeline）",
            "lang_list": "识别语言，默认 ch",
        },
    ),
}


def provider_types() -> list[str]:
    """当前支持的 provider_type 列表（供 API 校验与前端选项使用）"""
    return list(PROVIDER_REGISTRY.keys())


def is_valid_provider_type(provider_type: str) -> bool:
    """给定 provider_type 是否在注册表内"""
    return provider_type in PROVIDER_REGISTRY


def get_provider_class(provider_type: str) -> type["OCRProvider"] | None:
    """按 provider_type 取 Provider 类，未注册返回 None"""
    return PROVIDER_REGISTRY.get(provider_type)
