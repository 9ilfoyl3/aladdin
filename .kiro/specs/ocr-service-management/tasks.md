# Implementation Plan: OCR 服务管理

## Overview

将 OCR 服务配置从环境变量迁移到数据库，实现完整的 CRUD API 和前端管理页面。参考现有 LLMConfig + llm_config.py + Models.tsx 的设计模式，按照数据库模型 → 后端 API → Manager 改造 → 前端集成的顺序逐步实现。

## Tasks

- [x] 1. 数据库模型
  - [x] 1.1 在 `backend/app/schema/db.py` 中新增 OCRConfig ORM 模型
    - 定义 `ocr_configs` 表：id(String PK)、name(String(100))、provider_type(String)、api_url(String(2048))、api_key(String nullable)、timeout(Float default=30.0)、is_default(Boolean default=False)、is_fallback(Boolean default=False)、extra_config(JSON nullable)、created_at(DateTime server_default=func.now())、updated_at(DateTime server_default=func.now() onupdate=func.now())
    - 使用 SQLAlchemy Mapped 类型注解，参考现有 LLMConfig 写法
    - 在 `backend/app/main.py` 的 lifespan 中确保新表随 Base.metadata.create_all 自动创建
    - _Requirements: 1.1, 1.2, 1.3_

- [x] 2. 后端 CRUD API
  - [x] 2.1 创建 `backend/app/api/ocr_config.py`，定义 Router 和 Pydantic 模型
    - 创建 `router = APIRouter(prefix="/api/ocr-configs", tags=["OCR Config"])`
    - 定义 OCRConfigCreate(name, provider_type, api_url, api_key?, timeout=30.0, is_default=False, is_fallback=False, extra_config?)
    - 定义 OCRConfigUpdate（所有字段 Optional）
    - 定义 OCRConfigResponse（api_key 以 api_key_set: bool 返回，含 created_at/updated_at 字符串）
    - 定义 OCRTestRequest(provider_type, api_url, api_key?, timeout=30.0)
    - 定义 OCRTestResponse(success: bool, message: str, elapsed_ms: float?)
    - 在 `backend/app/main.py` 中注册此 router
    - _Requirements: 2.1, 3.2_

  - [x] 2.2 实现列表查询和创建端点
    - GET `/api/ocr-configs`：按 created_at 倒序返回所有配置，api_key 脱敏
    - POST `/api/ocr-configs`：生成 UUID 主键；校验 name 非空且≤100字符、provider_type 为 paddleocr|external_api、api_url 非空、timeout 在 1-300 范围；is_default=true 时取消其他 is_default；is_fallback=true 时取消其他 is_fallback；禁止同一配置同时 is_default 和 is_fallback（返回 422）
    - _Requirements: 2.1-2.7, 3.1-3.3, 7.1-7.3_

  - [x] 2.3 实现更新和删除端点
    - PUT `/api/ocr-configs/{config_id}`：部分更新（exclude_unset）；api_key 为空字符串时保持原值；is_default/is_fallback 互斥逻辑同创建；name/provider_type 校验同创建；config_id 不存在返回 404
    - DELETE `/api/ocr-configs/{config_id}`：删除记录返回 204；不存在返回 404；允许删除默认/备用服务
    - _Requirements: 4.1-4.7, 5.1-5.3_

  - [x] 2.4 实现连通性测试端点
    - POST `/api/ocr-configs/test`：临时配置测试，接受 provider_type、api_url、api_key、timeout；根据 provider_type 实例化 Provider 执行测试；PaddleOCR 检查 is_available()；External API 用 httpx 发送 HEAD 请求到 api_url，记录耗时；返回 OCRTestResponse
    - POST `/api/ocr-configs/{config_id}/test`：从数据库加载配置执行测试；config_id 不存在返回 404
    - 所有异常捕获后返回 success=false + 错误描述，不抛出 500
    - _Requirements: 6.1-6.5_

- [x] 3. OCRManager 改造
  - [x] 3.1 重构 `backend/app/pipeline/ocr/manager.py` 支持数据库配置初始化
    - 新增构造方式 `__init__(self, configs: list[OCRConfig])`，替代原有 `__init__(self, config: Settings)`
    - 实现 `_init_from_db(configs)` 方法：遍历配置列表，对每条记录调用 `_create_provider(config)` 创建 Provider 并注册到 `self._providers` 字典（key 为 config.id）
    - 根据 is_default 标记设置 `self._default_name`，根据 is_fallback 标记设置 `self._fallback_name`
    - 实现 `_create_provider(config: OCRConfig) -> OCRProvider | None`：provider_type=="paddleocr" 时创建 PaddleOCRProvider(lang=extra_config.get("lang","ch"), use_gpu=extra_config.get("use_gpu",False))；provider_type=="external_api" 时创建 ExternalAPIProvider(api_url=config.api_url, api_key=config.api_key, timeout=config.timeout)
    - 保持 `recognize()` 方法签名不变，确保默认服务失败时自动 fallback 到备用服务
    - _Requirements: 1.4, 1.5, 7.4, 7.5, 7.6_

  - [x] 3.2 修改 Pipeline 集成点，从数据库加载 OCR 配置
    - 修改 `backend/app/api/document.py` 中 `_run_pipeline` 方法：从数据库加载所有 OCRConfig 记录，有记录时构造 OCRManager(configs) 传入 DocumentPipeline，无记录时 ocr_manager=None 保持向后兼容
    - _Requirements: 1.4, 1.5_

- [x] 4. 前端 API 客户端与类型定义
  - [x] 4.1 在 `frontend/src/lib/api.ts` 中新增 `ocrConfigApi` 对象
    - 定义 OCRConfigItem 接口（id, name, provider_type, api_url, api_key_set, timeout, is_default, is_fallback, extra_config, created_at, updated_at）
    - 实现 list(): Promise<OCRConfigItem[]>
    - 实现 create(data): POST /api/ocr-configs
    - 实现 update(id, data): PUT /api/ocr-configs/{id}
    - 实现 delete(id): DELETE /api/ocr-configs/{id}
    - 实现 test(data): POST /api/ocr-configs/test
    - 实现 testSaved(id): POST /api/ocr-configs/{id}/test
    - 参考现有 llmConfigApi 的写法
    - _Requirements: 2.1, 3.1, 4.1, 5.1, 6.1, 6.4_

- [x] 5. 前端 OCR 服务管理页面
  - [x] 5.1 创建 `frontend/src/pages/OcrServices.tsx` 页面组件
    - 页面头部：标题 "OCR 服务管理" + 副标题 + "添加服务" 按钮
    - 使用 useQuery 获取服务列表（queryKey: ['ocr-configs']）
    - 加载状态：居中 spinner
    - 空状态：ScanText 图标 + 提示文字 + "添加服务" 按钮
    - 卡片网格（grid sm:grid-cols-2 lg:grid-cols-3）展示服务列表
    - 每张卡片：类型图标（Server/Globe）、名称 + provider_type Badge、API 地址、密钥状态、超时时间
    - 默认服务卡片右上角显示 Star 图标（黄色填充）
    - Fallback 服务卡片右上角显示 Shield 图标（蓝色）
    - 操作按钮行：测试(Zap)、编辑(Pencil)、删除(Trash2)
    - 测试结果区域：成功绿色/失败红色提示框，含耗时信息
    - _Requirements: 8.1, 8.2, 8.3, 8.7, 8.8_

  - [x] 5.2 实现创建/编辑对话框和删除确认
    - Dialog 组件：标题根据编辑/创建切换
    - 表单字段：name(Input required)、provider_type(Select: PaddleOCR本地/外部API)、api_url(Input required)、api_key(Input password)、timeout(Input number)、is_default(Checkbox)、is_fallback(Checkbox)
    - provider_type 为 paddleocr 时额外显示 lang 和 use_gpu 字段（写入 extra_config）
    - 编辑时预填充当前配置值，api_key 留空表示不修改
    - 对话框内"测试连接"按钮（调用临时测试接口）
    - 提交后 invalidateQueries 刷新列表
    - 删除：点击删除按钮弹出确认 Dialog，确认后调用 delete API 并刷新列表
    - 错误处理：API 返回错误时展示 toast 或内联错误提示
    - _Requirements: 8.4, 8.5, 8.6, 8.9_

- [x] 6. 路由与导航集成
  - [x] 6.1 在前端路由和侧边栏中集成 OCR 服务页面
    - 在 `frontend/src/App.tsx` 中添加 `/ocr-services` 路由，导入 OcrServices 组件
    - 在 `frontend/src/components/Layout.tsx` 侧边栏中添加 "OCR 服务" 导航项，使用 ScanText 图标，放置在模型管理之后
    - _Requirements: 8.1_

## Notes

- 参考 llm_config.py 的完整 CRUD 模式，保持 API 风格一致
- OCRManager 改造需保持 `recognize()` 方法签名和 fallback 逻辑不变
- 前端参考 Models.tsx 的卡片式管理界面风格，使用相同的 shadcn/ui 组件
- api_key 安全：所有接口仅返回 api_key_set 布尔值，不暴露明文
- 数据库无 OCR 配置时 Pipeline 正常运行，ocr_manager 为 None
- extra_config 约定：paddleocr 类型存储 {lang: "ch", use_gpu: false}

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4"] },
    { "id": 3, "tasks": ["3.1", "3.2"] },
    { "id": 4, "tasks": ["4.1"] },
    { "id": 5, "tasks": ["5.1", "5.2", "6.1"] }
  ]
}
```
