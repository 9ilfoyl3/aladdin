# 可配置 OCR 服务 - 技术设计文档

## Overview

将当前单体 OCR 外部服务接口改造为可配置的多 OCR 服务架构。核心问题：不同 OCR 服务返回的数据结构各异，需要设计统一的适配层来屏蔽差异。

设计采用**策略模式 + 适配器模式**组合：
- 统一抽象层 (`OCRProvider`) 定义标准接口和输出结构
- 适配逻辑内聚到每个 Provider 实现类
- `OCRManager` 管理 Provider 注册、选择和 fallback
- Pipeline 无感集成，只与 `OCRManager.recognize()` 交互

OCR 在管道中的位置处于 `load` 阶段之后、`chunk` 之前：

```
load → [文本为空?] → 是 → OCR 识别 → 得到文本 → chunk → ...
                   → 否 → 直接进入 chunk → ...
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  DocumentPipeline                        │
│                                                         │
│  load → [OCR 判断] → chunk → enrich → embed → index    │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────┐
│      OCRManager          │  ← 管理 OCR Provider 注册/选择
│  - get_provider(name)    │
│  - list_providers()      │
│  - default_provider      │
└──────────┬───────────────┘
           │
           ▼
┌──────────────────────────┐
│   OCRProvider (ABC)      │  ← 统一抽象接口
│  - recognize(input)      │
│  - name / is_available   │
└──────────┬───────────────┘
           │
     ┌─────┼──────────────┐
     ▼     ▼              ▼
┌────────┐ ┌────────┐ ┌──────────┐
│PaddleOCR│ │外部API │ │Tesseract │  ← 具体实现
│Provider │ │Provider│ │Provider  │
└────┬───┘ └────┬───┘ └─────┬────┘
     │          │            │
     ▼          ▼            ▼
┌──────────────────────────────────┐
│    OCRResult (统一数据结构)       │  ← 适配层输出
│  - full_text: str                │
│  - pages: list[PageOCRResult]    │
│  - confidence: float             │
└──────────────────────────────────┘
```

目录结构：

```
backend/app/pipeline/ocr/
├── __init__.py
├── provider.py              # 抽象基类 + 数据结构
├── manager.py               # OCRManager
├── paddleocr_provider.py    # PaddleOCR 实现
├── external_api_provider.py # 外部 API 实现
└── tesseract_provider.py    # (可选) Tesseract 实现
```

## Components and Interfaces

### OCRProvider 抽象基类

```python
# backend/app/pipeline/ocr/provider.py

from abc import ABC, abstractmethod


class OCRProvider(ABC):
    """OCR 服务抽象基类"""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider 唯一标识名"""
        ...

    @abstractmethod
    async def recognize(self, file_path: str) -> OCRResult:
        """对文件执行 OCR 识别

        Args:
            file_path: 文件路径（PDF/图片）

        Returns:
            OCRResult: 统一格式的识别结果
        """
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """检查该 Provider 是否可用（依赖是否安装等）"""
        ...
```

### OCRManager

```python
# backend/app/pipeline/ocr/manager.py

class OCRManager:
    """OCR Provider 管理器"""

    def __init__(self, config: Settings):
        self._providers: dict[str, OCRProvider] = {}
        self._default_name: str = config.ocr_provider
        self._fallback_name: str = config.ocr_fallback_provider
        self._init_providers(config)

    def _init_providers(self, config: Settings) -> None:
        """根据配置初始化所有可用 Provider"""
        ...

    def get_provider(self, name: Optional[str] = None) -> OCRProvider:
        """获取指定 Provider，默认返回配置中的默认 Provider"""
        ...

    def list_providers(self) -> list[str]:
        """列出所有已注册的可用 Provider 名称"""
        ...

    async def recognize(self, file_path: str, provider_name: Optional[str] = None) -> OCRResult:
        """执行 OCR 识别，支持自动 fallback"""
        ...
```

### PaddleOCR Provider

```python
# backend/app/pipeline/ocr/paddleocr_provider.py

class PaddleOCRProvider(OCRProvider):
    """PaddleOCR 本地识别"""

    def __init__(self, lang: str = "ch", use_gpu: bool = False): ...

    @property
    def name(self) -> str:
        return "paddleocr"

    async def recognize(self, file_path: str) -> OCRResult:
        """调用 PaddleOCR 引擎，将结果适配为 OCRResult"""
        ...

    def is_available(self) -> bool:
        """检查 paddleocr 包是否已安装"""
        ...
```

### External API Provider

```python
# backend/app/pipeline/ocr/external_api_provider.py

class ExternalAPIProvider(OCRProvider):
    """外部 HTTP API 形式的 OCR 服务"""

    def __init__(self, api_url: str, api_key: str = "", timeout: float = 30.0): ...

    @property
    def name(self) -> str:
        return "external_api"

    async def recognize(self, file_path: str) -> OCRResult:
        """通过 HTTP 调用外部 OCR API"""
        ...

    def _adapt_response(self, data: dict) -> OCRResult:
        """将外部 API 响应适配为统一结构"""
        ...

    def is_available(self) -> bool:
        return bool(self._api_url)
```

### Pipeline 集成

```python
# pipeline.py 改动

class DocumentPipeline:
    def __init__(self, model_manager, milvus_client, db_session_factory, ocr_manager=None):
        # ...现有初始化...
        self.ocr_manager = ocr_manager  # 可选注入

    async def process(self, file_path, doc_id, kb_id):
        # ... load ...
        load_result = loader.load(file_path)

        # OCR 判断：文本为空时尝试 OCR
        content = load_result.content.strip()
        if (not content or len(content) < 10) and self.ocr_manager:
            ocr_result = await self.ocr_manager.recognize(file_path)
            load_result = LoadResult(
                content=ocr_result.full_text,
                metadata={**load_result.metadata, "ocr_provider": ocr_result.provider_name},
            )
        elif not content or len(content) < 10:
            raise ValueError("文档提取文本为空，且未配置 OCR 服务")

        # ... chunk → enrich → embed → index ...
```

## Data Models

### OCR 输出数据结构

```python
from dataclasses import dataclass, field


@dataclass
class OCRBlock:
    """单个识别区块"""
    text: str
    confidence: float
    bbox: tuple[float, float, float, float] | None = None  # (x1, y1, x2, y2)


@dataclass
class PageOCRResult:
    """单页 OCR 结果"""
    page_num: int
    blocks: list[OCRBlock]
    full_text: str  # 该页完整文本（blocks 拼接）


@dataclass
class OCRResult:
    """统一 OCR 输出结构"""
    full_text: str                  # 全文本拼接
    pages: list[PageOCRResult]      # 按页结果
    avg_confidence: float           # 平均置信度
    provider_name: str              # 使用的 Provider 名称
    metadata: dict = field(default_factory=dict)
```

### 配置模型

在 `Settings` 中新增字段：

```python
# OCR
ocr_enabled: bool = True
ocr_provider: str = "paddleocr"
ocr_fallback_provider: str = ""

# PaddleOCR
ocr_paddleocr_lang: str = "ch"
ocr_paddleocr_use_gpu: bool = False

# External API
ocr_external_api_url: str = ""
ocr_external_api_key: str = ""
ocr_external_api_timeout: float = 30.0
```

对应 `.env` 配置示例：

```
OCR_ENABLED=true
OCR_PROVIDER=paddleocr
OCR_FALLBACK_PROVIDER=external_api

OCR_PADDLEOCR_LANG=ch
OCR_PADDLEOCR_USE_GPU=true

OCR_EXTERNAL_API_URL=http://ocr-service:8080/recognize
OCR_EXTERNAL_API_KEY=xxx
OCR_EXTERNAL_API_TIMEOUT=30
```

## Correctness Properties

### Property 1: 统一输出保证

无论使用哪个 Provider，`OCRResult` 的 `full_text` 字段始终为非空字符串（识别成功时）。

### Property 2: Provider 隔离

任何单个 Provider 的异常不影响其他 Provider 的正常使用。

### Property 3: Fallback 一致性

Fallback 执行后，返回结果与直接调用 fallback provider 结果一致。

### Property 4: 幂等性

同一文件多次调用 `recognize()` 应返回相同结果（外部 API 服务端保证）。

### Property 5: 配置验证

`ocr_provider` 指定的 Provider 如果不可用，应用启动时即报错而非运行时才发现。

### Property 6: 向后兼容

`ocr_manager=None` 时 Pipeline 行为与当前完全一致（抛出原有异常）。

## Error Handling

| 错误场景 | 处理策略 |
|---------|---------|
| 默认 Provider 不可用 | 自动切换到 fallback，fallback 也不可用则抛出 `ValueError` |
| OCR 调用超时 | 外部 API 按配置的 `timeout` 超时，抛出 `httpx.TimeoutException`，触发 fallback |
| OCR 返回空结果 | `full_text` 为空时记录 warning 日志，仍然返回 `OCRResult`（由上层 Pipeline 判断是否可用） |
| 文件格式不支持 | Provider 内部抛出 `ValueError`，附带明确错误信息 |
| 外部 API 返回非 200 | 抛出 `httpx.HTTPStatusError`，触发 fallback |
| Provider 依赖未安装 | `is_available()` 返回 False，Manager 不注册该 Provider |
| 配置错误（如 URL 为空） | `is_available()` 返回 False，跳过注册 |

错误传播链路：

```
Provider 异常 → Manager 捕获 → 尝试 Fallback → Fallback 也失败 → 向上抛出原始异常
                                             → Fallback 成功 → 返回 OCRResult
```

## Testing Strategy

### 单元测试

- **Provider 适配逻辑**：mock OCR 引擎/HTTP 响应，验证各种原始格式正确转为 `OCRResult`
- **OCRManager**：mock providers，验证注册、选择、fallback 逻辑
- **Pipeline 集成点**：mock `ocr_manager`，验证文本为空时触发 OCR、非空时跳过

### 集成测试

- 使用真实的 PaddleOCR 引擎（需要依赖安装）处理测试 PDF/图片，验证端到端结果
- 使用 `httpx` mock server 模拟外部 API，验证 HTTP 调用和适配流程

### 测试用例

```python
# 核心测试场景
def test_paddleocr_provider_returns_unified_result(): ...
def test_external_api_adapts_different_formats(): ...
def test_manager_fallback_on_primary_failure(): ...
def test_manager_raises_when_no_fallback(): ...
def test_pipeline_triggers_ocr_on_empty_content(): ...
def test_pipeline_skips_ocr_on_valid_content(): ...
def test_provider_is_available_check(): ...
```
