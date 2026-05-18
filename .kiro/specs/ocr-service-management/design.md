# OCR 服务管理 - 技术设计文档

## Overview

参考现有大模型管理功能（LLMConfig + llm_config.py + Models.tsx）的设计模式，实现 OCR 服务的数据库持久化管理。核心改动是将 OCR 配置从环境变量迁移到数据库，通过 CRUD API 和前端管理页面进行维护。

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              Frontend (React + TanStack Query)               │
│                                                             │
│  OcrServices.tsx (卡片列表 + 对话框 CRUD + 测试)              │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP API
                           ▼
┌─────────────────────────────────────────────────────────────┐
│              Backend (FastAPI)                               │
│                                                             │
│  /api/ocr-configs       → CRUD + /test + /{id}/test         │
└──────────┬──────────────────────────────────────────────────┘
           │
     ┌─────┼─────────────┐
     ▼                    ▼
┌──────────┐    ┌──────────────────┐
│ SQLite   │    │  OCRManager      │
│ OCRConfig│◄───│  (从 DB 加载)     │
│ 表       │    │                  │
└──────────┘    └──────────────────┘
```

## Components and Interfaces

### 1. 数据库模型 - OCRConfig

```python
# backend/app/schema/db.py 新增

class OCRConfig(Base):
    """OCR 服务配置表"""
    __tablename__ = "ocr_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_type: Mapped[str] = mapped_column(String, nullable=False)  # paddleocr | external_api
    api_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    api_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    timeout: Mapped[float] = mapped_column(default=30.0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
```

### 2. 后端 API - ocr_config.py

```python
# backend/app/api/ocr_config.py

router = APIRouter(prefix="/api/ocr-configs", tags=["OCR Config"])

# Pydantic 模型
class OCRConfigCreate(BaseModel):
    name: str
    provider_type: str  # paddleocr | external_api
    api_url: str
    api_key: Optional[str] = None
    timeout: float = 30.0
    is_default: bool = False
    is_fallback: bool = False
    extra_config: Optional[dict] = None

class OCRConfigUpdate(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout: Optional[float] = None
    is_default: Optional[bool] = None
    is_fallback: Optional[bool] = None
    extra_config: Optional[dict] = None

class OCRConfigResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    name: str
    provider_type: str
    api_url: str
    api_key_set: bool
    timeout: float
    is_default: bool
    is_fallback: bool
    extra_config: Optional[dict] = None
    created_at: str
    updated_at: str

class OCRTestRequest(BaseModel):
    provider_type: str
    api_url: str
    api_key: Optional[str] = None
    timeout: float = 30.0

class OCRTestResponse(BaseModel):
    success: bool
    message: str
    elapsed_ms: Optional[float] = None

# 端点
GET    /api/ocr-configs          → list_ocr_configs()
POST   /api/ocr-configs          → create_ocr_config()
PUT    /api/ocr-configs/{id}     → update_ocr_config()
DELETE /api/ocr-configs/{id}     → delete_ocr_config()
POST   /api/ocr-configs/test     → test_ocr_connection()    # 临时配置测试
POST   /api/ocr-configs/{id}/test → test_ocr_config()       # 已保存配置测试
```

### 3. OCRManager 改造

```python
# backend/app/pipeline/ocr/manager.py 改造

class OCRManager:
    """从数据库加载 OCR 配置并管理 Provider"""

    def __init__(self, configs: list[OCRConfig]) -> None:
        self._providers: dict[str, OCRProvider] = {}
        self._default_name: str = ""
        self._fallback_name: str = ""
        self._init_from_db(configs)

    def _init_from_db(self, configs: list[OCRConfig]) -> None:
        """根据数据库配置初始化 Provider"""
        for config in configs:
            provider = self._create_provider(config)
            if provider and provider.is_available():
                self._providers[config.id] = provider
                if config.is_default:
                    self._default_name = config.id
                if config.is_fallback:
                    self._fallback_name = config.id

    def _create_provider(self, config: OCRConfig) -> OCRProvider | None:
        """根据配置创建对应 Provider 实例"""
        if config.provider_type == "external_api":
            return ExternalAPIProvider(
                api_url=config.api_url,
                api_key=config.api_key or "",
                timeout=config.timeout,
            )
        elif config.provider_type == "paddleocr":
            extra = config.extra_config or {}
            return PaddleOCRProvider(
                lang=extra.get("lang", "ch"),
                use_gpu=extra.get("use_gpu", False),
            )
        return None
```

### 4. 应用启动集成

```python
# backend/app/api/document.py 中 _run_pipeline 改造

async def _run_pipeline(file_path: str, doc_id: str, kb_id: str) -> None:
    settings = get_settings()
    manager = get_model_manager()
    milvus = _get_milvus()

    # 从数据库加载 OCR 配置
    ocr_manager = None
    if settings.ocr_enabled:
        async with async_session() as session:
            result = await session.execute(select(OCRConfig))
            configs = result.scalars().all()
        if configs:
            ocr_manager = OCRManager(configs)

    pipeline = DocumentPipeline(
        model_manager=manager,
        milvus_client=milvus,
        db_session_factory=async_session,
        ocr_manager=ocr_manager,
    )
    await pipeline.process(file_path, doc_id, kb_id)
```

### 5. 前端页面 - OcrServices.tsx

参考 Models.tsx 的卡片式管理界面：

- 页面头部：标题 + "添加服务"按钮
- 卡片网格：每张卡片展示 OCR 服务信息
  - 名称、类型标签、API 地址
  - 密钥状态（已设置/未设置）
  - 默认服务星标 / Fallback 标识
  - 操作按钮：编辑、测试、删除
- 对话框：创建/编辑 OCR 服务的表单
- 空状态：引导用户添加第一个 OCR 服务

### 6. 前端 API 客户端

```typescript
// lib/api.ts 新增
export const ocrConfigApi = {
  list: () => fetch('/api/ocr-configs').then(r => r.json()),
  create: (data) => fetch('/api/ocr-configs', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetch(`/api/ocr-configs/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id) => fetch(`/api/ocr-configs/${id}`, { method: 'DELETE' }),
  test: (data) => fetch('/api/ocr-configs/test', { method: 'POST', body: JSON.stringify(data) }),
  testSaved: (id) => fetch(`/api/ocr-configs/${id}/test`, { method: 'POST' }),
}
```

### 7. 路由与导航

- 新增前端路由：`/ocr-services` → `OcrServices` 页面
- 侧边栏新增导航项：OCR 服务（ScanText 图标）

## Data Models

### OCRConfig 表结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | String (UUID) | 主键 |
| name | String(100) | 服务名称 |
| provider_type | String | paddleocr / external_api |
| api_url | String(2048) | 服务地址 |
| api_key | String | 认证密钥（可选） |
| timeout | Float | 超时时间（秒），默认 30 |
| is_default | Boolean | 是否为默认服务 |
| is_fallback | Boolean | 是否为备用服务 |
| extra_config | JSON | 扩展配置（如 PaddleOCR 的 lang、use_gpu） |
| created_at | DateTime | 创建时间 |
| updated_at | DateTime | 更新时间 |

## Correctness Properties

### Property 1: 默认服务唯一性

**Validates: Requirement 7.1**

任何时刻数据库中 `is_default=true` 的记录数量 ≤ 1。创建或更新操作设置 `is_default=true` 时，必须在同一事务中先将其他记录的 `is_default` 置为 false。

```python
# 验证方式：对任意操作序列（创建/更新/删除），断言
count = session.query(OCRConfig).filter(OCRConfig.is_default == True).count()
assert count <= 1
```

### Property 2: Fallback 服务唯一性

**Validates: Requirement 7.2**

任何时刻数据库中 `is_fallback=true` 的记录数量 ≤ 1。逻辑与 Property 1 对称。

```python
count = session.query(OCRConfig).filter(OCRConfig.is_fallback == True).count()
assert count <= 1
```

### Property 3: 默认与 Fallback 互斥

**Validates: Requirement 7.3**

同一条 `OCRConfig` 记录不可同时将 `is_default` 和 `is_fallback` 设为 true。API 层在创建和更新时校验此约束，违反时返回 422。

```python
# 对数据库中所有记录
for config in all_configs:
    assert not (config.is_default and config.is_fallback)
```

### Property 4: 持久化一致性

**Validates: Requirement 1.1, 1.4**

数据库中的 OCR 配置在应用重启后完整保留，且 OCRManager 启动时从数据库加载的 Provider 集合与数据库记录一一对应。

### Property 5: API Key 脱敏

**Validates: Requirement 3.2**

所有 API 响应（列表、创建、更新）中不返回 `api_key` 明文，仅返回 `api_key_set: bool` 表示是否已设置密钥。

### Property 6: 部分更新语义

**Validates: Requirement 4.1, 4.2**

PUT 请求中未提供的字段保持原值不变。`api_key` 为空字符串时视为"不修改"而非"清空"。`updated_at` 在每次更新时自动刷新。

### Property 7: Fallback 自动切换

**Validates: Requirement 7.5, 7.6**

当默认 Provider 执行 OCR 抛出异常或超时时：
- 若存在 fallback Provider → 自动使用 fallback 重试一次
- 若无 fallback → 向调用方抛出原始异常

### Property 8: 输入验证完备性

**Validates: Requirement 2.2, 2.3, 2.6, 2.7**

对于所有创建/更新请求：
- `name` 为空或纯空白 → 422
- `name` 超过 100 字符 → 422
- `provider_type` 不在 `[paddleocr, external_api]` → 422
- `api_url` 为空 → 422
- `timeout` 不在 `[1, 300]` 范围 → 422

## Error Handling

| 错误场景 | HTTP 状态码 | 处理策略 |
|---------|------------|---------|
| name 为空或纯空白 | 422 | 返回 `{"detail": "名称不能为空"}` |
| name 超过 100 字符 | 422 | 返回 `{"detail": "名称过长，最大 100 字符"}` |
| provider_type 非法 | 422 | 返回 `{"detail": "类型无效，仅支持 paddleocr 或 external_api"}` |
| api_url 为空 | 422 | 返回 `{"detail": "API 地址不能为空"}` |
| timeout 超出范围 | 422 | 返回 `{"detail": "超时时间须在 1-300 秒之间"}` |
| is_default 与 is_fallback 同时为 true | 422 | 返回 `{"detail": "同一服务不能同时设为默认和备用"}` |
| config_id 不存在（更新/删除/测试） | 404 | 返回 `{"detail": "OCR 配置不存在"}` |
| 连通性测试超时 | 200 | 返回 `{"success": false, "message": "连接超时", "elapsed_ms": null}` |
| 连通性测试网络异常 | 200 | 返回 `{"success": false, "message": "连接失败: {错误描述}"}` |
| PaddleOCR 依赖未安装 | 200 | 测试返回 `{"success": false, "message": "PaddleOCR 未安装"}` |
| 默认 Provider 执行失败，有 Fallback | — | 自动切换 Fallback 重试，成功则正常返回 OCRResult |
| 默认 Provider 执行失败，无 Fallback | — | 向 Pipeline 调用方抛出原始异常 |
| 数据库中无任何 OCR 配置 | — | OCRManager 为 None，Pipeline 触发 OCR 时抛出 `ValueError("文档提取文本为空，且未配置 OCR 服务")` |

**错误传播链路：**

```
API 层验证失败 → 直接返回 422/404（不影响运行时 OCR 调用）

连通性测试异常 → 捕获后包装为 OCRTestResponse(success=false)

Pipeline OCR 调用链路：
  Pipeline → OCRManager.recognize()
    → Default Provider 异常
      → 有 Fallback → 尝试 Fallback Provider
        → 成功 → 返回 OCRResult
        → 失败 → 抛出 Default 的原始异常
      → 无 Fallback → 直接抛出原始异常
```

## Testing Strategy

### 单元测试（`backend/tests/test_ocr_config.py`）

**API CRUD 测试：**
```python
async def test_create_ocr_config_success():
    """创建配置返回 201，包含 UUID、api_key_set 脱敏"""

async def test_create_ocr_config_empty_name():
    """name 为空返回 422"""

async def test_create_ocr_config_invalid_provider():
    """provider_type 非法返回 422"""

async def test_create_ocr_config_empty_url():
    """api_url 为空返回 422"""

async def test_list_ocr_configs_ordered():
    """列表按 created_at 倒序，api_key 脱敏"""

async def test_update_partial_fields():
    """部分更新仅修改提供的字段"""

async def test_update_empty_api_key_keeps_original():
    """api_key 为空字符串时保持原值"""

async def test_update_nonexistent_returns_404():
    """更新不存在的 ID 返回 404"""

async def test_delete_config():
    """删除返回 204"""

async def test_delete_nonexistent_returns_404():
    """删除不存在的 ID 返回 404"""
```

**is_default / is_fallback 互斥测试：**
```python
async def test_set_default_clears_previous():
    """设置新默认时旧默认被取消"""

async def test_set_fallback_clears_previous():
    """设置新 fallback 时旧 fallback 被取消"""

async def test_same_config_default_and_fallback_rejected():
    """同一记录同时设 is_default 和 is_fallback 返回 422"""
```

**连通性测试：**
```python
async def test_connection_paddleocr_available():
    """PaddleOCR is_available() 返回 True → success"""

async def test_connection_external_api_success():
    """mock httpx 请求成功 → success + elapsed_ms"""

async def test_connection_external_api_timeout():
    """mock httpx 超时 → success=false"""

async def test_connection_saved_config():
    """测试已保存配置的连通性"""

async def test_connection_nonexistent_config():
    """测试不存在的 config_id 返回 404"""
```

### 集成测试（`backend/tests/test_ocr_manager_db.py`）

**OCRManager 数据库驱动：**
```python
async def test_manager_loads_from_db():
    """数据库有记录时 Manager 正确加载并注册 Provider"""

async def test_manager_empty_db():
    """数据库为空时 Manager 正常初始化但无 Provider"""

async def test_manager_default_fallback_from_db():
    """Manager 从 DB 读取 is_default/is_fallback 标记确定主备"""

async def test_manager_fallback_on_default_failure():
    """默认 Provider 失败时自动切换 fallback"""

async def test_manager_no_fallback_raises():
    """无 fallback 时默认失败直接抛出异常"""
```

### 前端测试要点

- 页面加载展示卡片列表（含默认星标、Fallback 标识）
- 空状态引导界面展示
- 创建/编辑对话框表单验证
- 删除确认对话框流程
- 测试按钮状态流转（加载中 → 成功/失败结果展示）
- API 错误时展示错误提示
