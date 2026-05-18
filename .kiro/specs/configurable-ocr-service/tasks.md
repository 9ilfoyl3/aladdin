# Implementation Plan:

## Overview

本实现计划将可配置 OCR 服务功能分为 3 个阶段 7 个任务组，从基础数据结构到 Provider 实现再到管道集成，逐步完成整个功能的开发。

## Tasks

### Phase 1: 基础设施 - 数据结构与抽象层

- [x] 1. OCR 模块目录与数据结构
  - [x] 1.1 创建 `backend/app/pipeline/ocr/` 目录和 `__init__.py`
  - [x] 1.2 实现 OCR 数据结构（`backend/app/pipeline/ocr/provider.py`）：定义 `OCRBlock`、`PageOCRResult`、`OCRResult` dataclass
  - [x] 1.3 实现 OCR Provider 抽象基类（同 `provider.py`）：定义 `name` 属性、`recognize()` 异步方法、`is_available()` 方法

- [x] 2. 配置扩展
  - [x] 2.1 在 `backend/app/config.py` 的 Settings 中新增 OCR 相关配置字段：`ocr_enabled`、`ocr_provider`、`ocr_fallback_provider`、`ocr_paddleocr_lang`、`ocr_paddleocr_use_gpu`、`ocr_external_api_url`、`ocr_external_api_key`、`ocr_external_api_timeout`
  - [x] 2.2 在 `backend/.env.example` 中补充 OCR 配置示例

### Phase 2: Provider 实现

- [x] 3. PaddleOCR Provider
  - [x] 3.1 实现 `PaddleOCRProvider`（`backend/app/pipeline/ocr/paddleocr_provider.py`）：懒加载引擎初始化、调用 PaddleOCR 识别、将原始结果（bbox + text + confidence）适配为 `OCRResult`
  - [x] 3.2 实现 `is_available()` 方法：检测 paddleocr 包是否已安装

- [x] 4. External API Provider
  - [x] 4.1 实现 `ExternalAPIProvider`（`backend/app/pipeline/ocr/external_api_provider.py`）：通过 httpx 发送文件到外部 OCR 服务、处理响应状态码和超时
  - [x] 4.2 实现 `_adapt_response()` 方法：将外部 API 返回的 JSON 数据适配为统一 `OCRResult` 结构
  - [x] 4.3 实现 `is_available()` 方法：检查 api_url 是否已配置

### Phase 3: Manager 与管道集成

- [x] 5. OCR Manager
  - [x] 5.1 实现 `OCRManager`（`backend/app/pipeline/ocr/manager.py`）：根据 Settings 初始化并注册可用 Provider
  - [x] 5.2 实现 `get_provider(name)` 和 `list_providers()` 方法
  - [x] 5.3 实现 `recognize()` 方法：调用 Provider 执行识别，失败时自动 fallback

- [x] 6. Pipeline 集成
  - [x] 6.1 修改 `DocumentPipeline.__init__`（`backend/app/pipeline/pipeline.py`）：新增可选参数 `ocr_manager`
  - [x] 6.2 修改 `DocumentPipeline.process` 方法：在 load 后判断文本是否为空，为空时调用 `ocr_manager.recognize()` 获取文本
  - [x] 6.3 在文档元数据中记录 OCR Provider 名称
  - [x] 6.4 保持向后兼容：`ocr_manager=None` 时行为不变

- [x] 7. 应用启动集成
  - [x] 7.1 在应用启动时根据配置初始化 `OCRManager` 实例并注入 `DocumentPipeline`
  - [x] 7.2 在 `requirements.txt` 中添加可选依赖说明（paddleocr、httpx）

### Phase 4: API 与前端集成

- [x] 8. 后端 API 扩展
  - [x] 8.1 在 `SystemConfigResponse` 中新增 OCR 相关字段：`ocr_enabled`、`ocr_provider`、`ocr_fallback_provider`、`ocr_paddleocr_lang`、`ocr_paddleocr_use_gpu`、`ocr_external_api_url`、`ocr_external_api_key`（脱敏）、`ocr_external_api_timeout`
  - [x] 8.2 在 `SystemConfigUpdate` 中新增对应的可选更新字段
  - [x] 8.3 在 GET/PUT `/api/system/config` 中处理 OCR 配置的读取和更新

- [x] 9. 前端 Settings 页面
  - [x] 9.1 在 `Settings.tsx` 的 `configGroups` 中新增 "OCR 配置" 卡片组
  - [x] 9.2 包含字段：启用开关（Switch）、默认 Provider（Select）、Fallback Provider（Select）、PaddleOCR 语言（Input）、PaddleOCR GPU 开关（Switch）、外部 API 地址（Input）、外部 API 密钥（Input password）、超时时间（Input number）

## Task Dependency Graph

```json
{
  "waves": [
    {
      "name": "Wave 1: 基础设施",
      "tasks": [1, 2],
      "description": "数据结构、抽象层和配置扩展，无外部依赖"
    },
    {
      "name": "Wave 2: Provider 实现",
      "tasks": [3, 4],
      "dependsOn": [1, 2],
      "description": "各 OCR Provider 实现，依赖 Wave 1 的抽象层和配置"
    },
    {
      "name": "Wave 3: Manager 与管道集成",
      "tasks": [5, 6, 7],
      "dependsOn": [3, 4],
      "description": "OCR Manager、Pipeline 集成和应用启动注入"
    },
    {
      "name": "Wave 4: API 与前端",
      "tasks": [8, 9],
      "dependsOn": [2, 7],
      "description": "后端 API 扩展和前端 Settings 页面集成"
    }
  ]
}
```

## Notes

- PaddleOCR 为可选依赖，未安装时 Provider 不注册，不影响系统运行
- httpx 已在项目中使用（或作为新增依赖），用于外部 API 调用
- 所有 Provider 实现采用懒加载，避免启动时加载未使用的重量级模型
- 向后兼容：`ocr_manager=None` 时 Pipeline 行为与当前版本一致
