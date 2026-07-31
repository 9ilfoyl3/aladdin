"""OCR 服务配置管理接口"""

import logging
import shutil
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_platform
from app.pipeline.ocr import samples
from app.pipeline.ocr.errors import OCRError, OCRResponseFormatError
from app.pipeline.ocr.provider import INPUT_IMAGE, INPUT_PDF
from app.pipeline.ocr.registry import (
    PROVIDER_META,
    get_provider_class,
    is_valid_provider_type,
    provider_types,
)
import app.pipeline.ocr.providers  # noqa: F401 — 触发 Provider 注册
from app.schema.db import OCRConfig
from app.storage.database import get_db

logger = logging.getLogger(__name__)

# 超时上限：MinerU 等同步长耗时服务解析多页 PDF 可达数分钟
_TIMEOUT_MIN = 1.0
_TIMEOUT_MAX = 900.0

# 测试结果中回显的识别文本片段长度
_TEXT_PREVIEW_CHARS = 120

# 能力配置（OCR 服务）属平台底座，全平台一份，仅超级管理员维护
# （capability-config-to-platform）。OCRConfig 表本就无 tenant_id（全局单份），
# 此处仅收紧守卫为 require_platform：超管可管，租户管理员不再可见可改。
router = APIRouter(
    prefix="/api/ocr-configs",
    tags=["OCR Config"],
    dependencies=[Depends(require_platform())],
)


class OCRConfigCreate(BaseModel):
    """创建 OCR 服务配置请求"""
    name: str
    provider_type: str  # vl | paddle | mineru（取值域由 Provider 注册表派生）
    api_url: str
    api_key: Optional[str] = None
    timeout: float = 30.0
    is_default: bool = False
    is_fallback: bool = False
    extra_config: Optional[dict] = None


class OCRConfigUpdate(BaseModel):
    """更新 OCR 服务配置请求（所有字段 Optional）"""
    name: Optional[str] = None
    provider_type: Optional[str] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout: Optional[float] = None
    is_default: Optional[bool] = None
    is_fallback: Optional[bool] = None
    extra_config: Optional[dict] = None


class OCRConfigResponse(BaseModel):
    """OCR 服务配置响应（api_key 脱敏）"""
    model_config = {"from_attributes": True}

    id: str
    name: str
    provider_type: str
    # provider_type 是否在当前 Provider 注册表内。False 表示该配置已失效
    # （类型被移除），运行时会被 OCRManager 跳过，需在 UI 提示重建。
    provider_type_valid: bool
    api_url: str
    api_key_set: bool
    timeout: float
    is_default: bool
    is_fallback: bool
    extra_config: Optional[dict] = None
    created_at: str
    updated_at: str


class OCRProviderTypeMeta(BaseModel):
    """Provider 类型元数据（供前端渲染选项与提示，避免前端硬编码）"""
    provider_type: str
    label: str
    summary: str
    api_url_example: str
    accepts: list[str]
    accepts_pdf: bool
    outputs_markdown: bool
    recommended_timeout: float
    extra_config_keys: dict[str, str]


class OCRTestRequest(BaseModel):
    """临时 OCR 连通性测试请求"""
    provider_type: str
    api_url: str
    api_key: Optional[str] = None
    timeout: float = 30.0
    extra_config: Optional[dict] = None


class OCRTestCheck(BaseModel):
    """单种输入形态的验证结果"""
    input_kind: str  # image | pdf
    ok: bool
    elapsed_ms: Optional[float] = None
    text_preview: Optional[str] = None
    error: Optional[str] = None


class OCRTestResponse(BaseModel):
    """OCR 连通性测试响应（真实链路：上传样张 → 识别 → 契约适配）"""
    success: bool
    message: str
    elapsed_ms: Optional[float] = None
    checks: list[OCRTestCheck] = []


def _validate_ocr_config(name: str, provider_type: str, api_url: str, timeout: float,
                          is_default: bool, is_fallback: bool) -> None:
    """校验 OCR 配置字段，不通过时抛出 HTTPException 422"""
    if not name or not name.strip():
        raise HTTPException(status_code=422, detail="名称不能为空")
    if len(name) > 100:
        raise HTTPException(status_code=422, detail="名称过长，最大 100 字符")
    _validate_provider_type(provider_type)
    if not api_url or not api_url.strip():
        raise HTTPException(status_code=422, detail="API 地址不能为空")
    _validate_timeout(timeout)
    if is_default and is_fallback:
        raise HTTPException(status_code=422, detail="同一服务不能同时设为默认和备用")


def _validate_provider_type(provider_type: str) -> None:
    """provider_type 取值域由 Provider 注册表派生"""
    if not is_valid_provider_type(provider_type):
        raise HTTPException(
            status_code=422,
            detail=f"类型无效，仅支持 {' / '.join(provider_types())}",
        )


def _validate_timeout(timeout: float) -> None:
    """超时范围校验"""
    if timeout < _TIMEOUT_MIN or timeout > _TIMEOUT_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"超时时间须在 {int(_TIMEOUT_MIN)}-{int(_TIMEOUT_MAX)} 秒之间",
        )


def _config_to_response(config: OCRConfig) -> OCRConfigResponse:
    """将 ORM 对象转换为响应模型"""
    return OCRConfigResponse(
        id=config.id,
        name=config.name,
        provider_type=config.provider_type,
        provider_type_valid=is_valid_provider_type(config.provider_type),
        api_url=config.api_url,
        api_key_set=bool(config.api_key),
        timeout=config.timeout,
        is_default=config.is_default,
        is_fallback=config.is_fallback,
        extra_config=config.extra_config,
        created_at=config.created_at.isoformat() if config.created_at else "",
        updated_at=config.updated_at.isoformat() if config.updated_at else "",
    )


@router.get("/provider-types", response_model=list[OCRProviderTypeMeta])
async def list_provider_types():
    """列出支持的 OCR 服务类型及其能力元数据

    前端据此渲染类型下拉、能力说明、地址示例与推荐超时，
    不在前端硬编码类型列表。
    """
    items: list[OCRProviderTypeMeta] = []
    for provider_type in provider_types():
        provider_cls = get_provider_class(provider_type)
        meta = PROVIDER_META.get(provider_type)
        if provider_cls is None or meta is None:
            continue
        cap = provider_cls.capability
        items.append(OCRProviderTypeMeta(
            provider_type=provider_type,
            label=meta.label,
            summary=meta.summary,
            api_url_example=meta.api_url_example,
            accepts=sorted(cap.accepts),
            accepts_pdf=cap.accepts_kind(INPUT_PDF),
            outputs_markdown=cap.outputs_markdown,
            recommended_timeout=cap.recommended_timeout,
            extra_config_keys=meta.extra_config_keys,
        ))
    return items


@router.get("", response_model=list[OCRConfigResponse])
async def list_ocr_configs(db: AsyncSession = Depends(get_db)):
    """获取所有 OCR 服务配置，按创建时间倒序排列"""
    result = await db.execute(select(OCRConfig).order_by(OCRConfig.created_at.desc()))
    configs = result.scalars().all()
    return [_config_to_response(c) for c in configs]


@router.post("", response_model=OCRConfigResponse, status_code=201)
async def create_ocr_config(body: OCRConfigCreate, db: AsyncSession = Depends(get_db)):
    """创建 OCR 服务配置"""
    _validate_ocr_config(
        name=body.name,
        provider_type=body.provider_type,
        api_url=body.api_url,
        timeout=body.timeout,
        is_default=body.is_default,
        is_fallback=body.is_fallback,
    )

    # 如果设为默认，取消其他默认
    if body.is_default:
        result = await db.execute(select(OCRConfig).where(OCRConfig.is_default == True))
        for c in result.scalars().all():
            c.is_default = False

    # 如果设为备用，取消其他备用
    if body.is_fallback:
        result = await db.execute(select(OCRConfig).where(OCRConfig.is_fallback == True))
        for c in result.scalars().all():
            c.is_fallback = False

    config = OCRConfig(
        id=str(uuid.uuid4()),
        name=body.name.strip(),
        provider_type=body.provider_type,
        api_url=body.api_url.strip(),
        api_key=body.api_key or None,
        timeout=body.timeout,
        is_default=body.is_default,
        is_fallback=body.is_fallback,
        extra_config=body.extra_config,
    )
    db.add(config)
    await db.flush()
    await db.refresh(config)

    return _config_to_response(config)

async def _perform_ocr_test(
    provider_type: str,
    api_url: str,
    api_key: Optional[str],
    timeout: float,
    extra_config: Optional[dict] = None,
) -> OCRTestResponse:
    """真实链路连通性测试：上传内置样张 → 走完整 recognize + 契约适配 → 校验文本

    对 Provider 声明可接受的每种输入形态各测一次（图片、以及支持 PDF 时的单页 PDF）。
    只有"HTTP 通 + 响应契约匹配 + 识别文本非空"三者全满足才算成功——
    这样配错端点、选错服务类型、服务不支持该输入类型都能在保存前暴露。
    """
    provider_cls = get_provider_class(provider_type)
    if provider_cls is None:
        return OCRTestResponse(
            success=False,
            message=f"不支持的服务类型 {provider_type}，仅支持 {' / '.join(provider_types())}",
        )

    if not api_url or not api_url.strip():
        return OCRTestResponse(success=False, message="API 地址不能为空")

    provider = provider_cls(
        api_url=api_url.strip(),
        api_key=api_key or "",
        timeout=timeout,
        extra_config=extra_config or {},
    )
    capability = provider_cls.capability

    sample_dir = samples.make_sample_dir()
    checks: list[OCRTestCheck] = []
    total_start = time.time()
    try:
        # 图片形态：所有 Provider 都应支持
        if capability.accepts_kind(INPUT_IMAGE):
            checks.append(
                await _check_one(provider, INPUT_IMAGE, samples.build_test_image(sample_dir))
            )
        # PDF 形态：仅验证声明支持 PDF 的 Provider（只吃图片的由 pipeline 渲染，不在此验证）
        if capability.accepts_kind(INPUT_PDF):
            checks.append(
                await _check_one(provider, INPUT_PDF, samples.build_test_pdf(sample_dir))
            )
    finally:
        shutil.rmtree(sample_dir, ignore_errors=True)

    elapsed_ms = (time.time() - total_start) * 1000

    if not checks:
        return OCRTestResponse(
            success=False, message="该服务类型未声明任何可接受的输入形态", elapsed_ms=elapsed_ms
        )

    failed = [c for c in checks if not c.ok]
    if failed:
        first = failed[0]
        return OCRTestResponse(
            success=False,
            message=f"{first.input_kind} 形态验证失败：{first.error}",
            elapsed_ms=elapsed_ms,
            checks=checks,
        )

    kinds = "、".join(c.input_kind for c in checks)
    return OCRTestResponse(
        success=True,
        message=f"识别正常（已验证 {kinds}），返回文本与样张一致性请核对下方片段",
        elapsed_ms=elapsed_ms,
        checks=checks,
    )


async def _check_one(provider, input_kind: str, sample_path: str) -> OCRTestCheck:
    """对单个样张走完整识别链路，把失败原因归类为可操作的提示"""
    start = time.time()
    try:
        result = await provider.recognize(sample_path)
    except OCRResponseFormatError as e:
        return OCRTestCheck(
            input_kind=input_kind,
            ok=False,
            elapsed_ms=(time.time() - start) * 1000,
            error=f"响应格式与所选服务类型不符 — {e}",
        )
    except OCRError as e:
        return OCRTestCheck(
            input_kind=input_kind,
            ok=False,
            elapsed_ms=(time.time() - start) * 1000,
            error=str(e),
        )
    except Exception as e:  # 连接失败 / 超时 / 非 2xx
        return OCRTestCheck(
            input_kind=input_kind,
            ok=False,
            elapsed_ms=(time.time() - start) * 1000,
            error=f"请求失败: {type(e).__name__}: {e}",
        )

    elapsed_ms = (time.time() - start) * 1000
    text = result.full_text.strip()
    if not text:
        return OCRTestCheck(
            input_kind=input_kind,
            ok=False,
            elapsed_ms=elapsed_ms,
            error=(
                "服务返回成功但未识别出任何文本"
                f"（样张内容为 {samples.TEST_TEXT}），"
                "请确认该端点支持此输入类型"
            ),
        )

    return OCRTestCheck(
        input_kind=input_kind,
        ok=True,
        elapsed_ms=elapsed_ms,
        text_preview=text[:_TEXT_PREVIEW_CHARS],
    )


@router.post("/test", response_model=OCRTestResponse)
async def test_ocr_connection(body: OCRTestRequest):
    """临时配置连通性测试（无需先保存）"""
    return await _perform_ocr_test(
        provider_type=body.provider_type,
        api_url=body.api_url,
        api_key=body.api_key,
        timeout=body.timeout,
        extra_config=body.extra_config,
    )


@router.post("/{config_id}/test", response_model=OCRTestResponse)
async def test_saved_ocr_config(config_id: str, db: AsyncSession = Depends(get_db)):
    """测试已保存的 OCR 服务配置连通性"""
    result = await db.execute(select(OCRConfig).where(OCRConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="OCR 配置不存在")

    return await _perform_ocr_test(
        provider_type=config.provider_type,
        api_url=config.api_url,
        api_key=config.api_key,
        timeout=config.timeout,
        extra_config=config.extra_config,
    )


@router.put("/{config_id}", response_model=OCRConfigResponse)
async def update_ocr_config(config_id: str, body: OCRConfigUpdate, db: AsyncSession = Depends(get_db)):
    """更新 OCR 服务配置（部分更新）"""
    result = await db.execute(select(OCRConfig).where(OCRConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="OCR 配置不存在")

    update_data = body.model_dump(exclude_unset=True)

    # api_key 为空字符串时保持原值
    if "api_key" in update_data and update_data["api_key"] == "":
        del update_data["api_key"]

    # 字段校验（仅校验提供的字段）
    if "name" in update_data:
        name = update_data["name"]
        if not name or not name.strip():
            raise HTTPException(status_code=422, detail="名称不能为空")
        if len(name) > 100:
            raise HTTPException(status_code=422, detail="名称过长，最大 100 字符")
        update_data["name"] = name.strip()

    if "provider_type" in update_data:
        _validate_provider_type(update_data["provider_type"])

    if "api_url" in update_data:
        if not update_data["api_url"] or not update_data["api_url"].strip():
            raise HTTPException(status_code=422, detail="API 地址不能为空")

    if "timeout" in update_data:
        _validate_timeout(update_data["timeout"])

    # is_default / is_fallback 互斥校验
    new_is_default = update_data.get("is_default", config.is_default)
    new_is_fallback = update_data.get("is_fallback", config.is_fallback)
    if new_is_default and new_is_fallback:
        raise HTTPException(status_code=422, detail="同一服务不能同时设为默认和备用")

    # 如果设为默认，取消其他默认
    if update_data.get("is_default"):
        others = await db.execute(
            select(OCRConfig).where(OCRConfig.is_default == True, OCRConfig.id != config_id)
        )
        for c in others.scalars().all():
            c.is_default = False

    # 如果设为备用，取消其他备用
    if update_data.get("is_fallback"):
        others = await db.execute(
            select(OCRConfig).where(OCRConfig.is_fallback == True, OCRConfig.id != config_id)
        )
        for c in others.scalars().all():
            c.is_fallback = False

    for field, value in update_data.items():
        setattr(config, field, value)

    await db.flush()
    await db.refresh(config)

    return _config_to_response(config)


@router.delete("/{config_id}", status_code=204)
async def delete_ocr_config(config_id: str, db: AsyncSession = Depends(get_db)):
    """删除 OCR 服务配置"""
    result = await db.execute(select(OCRConfig).where(OCRConfig.id == config_id))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="OCR 配置不存在")
    await db.delete(config)
    await db.flush()

