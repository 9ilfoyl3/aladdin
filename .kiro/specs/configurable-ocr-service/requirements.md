# Requirements Document

## Introduction

本文档定义了可配置 OCR 服务功能的需求。该功能将现有单体 OCR 外部服务接口改造为可配置的多 OCR 服务架构，通过策略模式 + 适配器模式，支持维护多个 OCR 服务提供商列表，并统一适配不同 OCR 接口返回的数据结构差异。OCR 能力集成在文档处理管道的 load 阶段之后，用于处理扫描件或图片型文档。

## Glossary

- **OCR_Provider**：OCR 服务抽象基类，定义统一的识别接口
- **OCR_Manager**：OCR Provider 管理器，负责注册、选择和调度 Provider
- **OCR_Result**：统一的 OCR 输出数据结构，屏蔽各服务返回格式差异
- **PaddleOCR_Provider**：基于 PaddleOCR 引擎的本地 OCR 实现
- **External_API_Provider**：基于外部 HTTP API 的 OCR 实现
- **Data_Pipeline**：文档处理管道，负责文档加载、OCR、切片、富化和向量化
- **Fallback**：主 Provider 失败时自动切换到备用 Provider 的机制

## Requirements

### Requirement 1: OCR Provider 统一抽象

**User Story:** 作为系统开发者，我希望有统一的 OCR 抽象接口，以便灵活接入不同的 OCR 服务提供商而无需修改上层代码。

#### Acceptance Criteria

1. THE OCR_Provider SHALL 定义统一的 `recognize(file_path)` 异步接口，接受文件路径并返回 OCR_Result
2. THE OCR_Provider SHALL 提供 `name` 属性作为 Provider 的唯一标识
3. THE OCR_Provider SHALL 提供 `is_available()` 方法，用于检测该 Provider 的运行时依赖是否满足
4. THE OCR_Result SHALL 包含完整文本（full_text）、按页结果（pages）、平均置信度（avg_confidence）和 Provider 名称
5. WHEN 新增 OCR 服务时，开发者 SHALL 仅需实现 OCR_Provider 接口，无需修改 Manager 或 Pipeline 代码

### Requirement 2: OCR 服务注册与选择

**User Story:** 作为系统管理员，我希望能通过配置选择使用哪个 OCR 服务，并在主服务不可用时自动回退。

#### Acceptance Criteria

1. THE OCR_Manager SHALL 根据配置自动初始化并注册所有可用的 OCR Provider
2. THE OCR_Manager SHALL 支持通过 `ocr_provider` 配置项指定默认使用的 Provider
3. THE OCR_Manager SHALL 支持通过 `ocr_fallback_provider` 配置项指定备用 Provider
4. WHEN 默认 Provider 调用失败时，THE OCR_Manager SHALL 自动切换到 fallback Provider 执行识别
5. WHEN fallback Provider 也失败时，THE OCR_Manager SHALL 向上层抛出原始异常
6. THE OCR_Manager SHALL 提供 `list_providers()` 方法列出所有已注册的可用 Provider

### Requirement 3: PaddleOCR 本地识别

**User Story:** 作为系统用户，我希望能使用 PaddleOCR 进行本地 OCR 识别，无需依赖外部服务。

#### Acceptance Criteria

1. THE PaddleOCR_Provider SHALL 支持中文和英文文档的 OCR 识别
2. THE PaddleOCR_Provider SHALL 支持配置语言类型（`ocr_paddleocr_lang`）和是否使用 GPU（`ocr_paddleocr_use_gpu`）
3. THE PaddleOCR_Provider SHALL 将 PaddleOCR 原始返回格式（bbox + text + confidence）适配为统一 OCR_Result
4. THE PaddleOCR_Provider SHALL 采用懒加载方式初始化 OCR 引擎，避免未使用时占用资源
5. WHEN paddleocr 依赖未安装时，THE PaddleOCR_Provider 的 `is_available()` SHALL 返回 False

### Requirement 4: 外部 API OCR 服务

**User Story:** 作为系统管理员，我希望能对接已有的外部 OCR HTTP 服务，复用已部署的 OCR 能力。

#### Acceptance Criteria

1. THE External_API_Provider SHALL 通过 HTTP POST 方式将文件发送到配置的外部 OCR 服务地址
2. THE External_API_Provider SHALL 支持配置 API 地址（`ocr_external_api_url`）、认证密钥（`ocr_external_api_key`）和超时时间（`ocr_external_api_timeout`）
3. THE External_API_Provider SHALL 将外部 API 返回的自定义数据结构适配为统一 OCR_Result
4. WHEN 外部 API 返回非 200 状态码时，THE External_API_Provider SHALL 抛出包含状态码和错误信息的异常
5. WHEN 外部 API 调用超时时，THE External_API_Provider SHALL 抛出超时异常以触发 fallback 机制

### Requirement 5: 管道集成

**User Story:** 作为系统用户，我希望上传扫描件或图片型文档时，系统能自动调用 OCR 识别并完成后续处理。

#### Acceptance Criteria

1. WHEN 文档加载后提取的文本内容为空或长度小于 10 字符时，THE Data_Pipeline SHALL 自动调用 OCR_Manager 进行识别
2. WHEN 文档有有效文本内容时，THE Data_Pipeline SHALL 跳过 OCR 步骤直接进入切片流程
3. WHEN OCR 识别成功后，THE Data_Pipeline SHALL 将 OCR 结果的 full_text 作为文档内容继续后续处理
4. THE Data_Pipeline SHALL 在文档元数据中记录使用的 OCR Provider 名称
5. WHEN OCR 未启用（`ocr_enabled=false`）且文本为空时，THE Data_Pipeline SHALL 抛出明确的错误信息
6. WHEN OCR_Manager 未注入（向后兼容）时，THE Data_Pipeline SHALL 保持原有行为（抛出文本为空错误）

### Requirement 6: 配置管理

**User Story:** 作为系统管理员，我希望通过环境变量或配置文件管理 OCR 服务的所有参数。

#### Acceptance Criteria

1. THE Settings SHALL 新增 `ocr_enabled` 配置项控制 OCR 功能的全局开关，默认为 True
2. THE Settings SHALL 新增 `ocr_provider` 配置项指定默认 OCR Provider 名称
3. THE Settings SHALL 新增 `ocr_fallback_provider` 配置项指定备用 Provider 名称，默认为空
4. THE Settings SHALL 新增 PaddleOCR 相关配置项：`ocr_paddleocr_lang`（默认 "ch"）、`ocr_paddleocr_use_gpu`（默认 False）
5. THE Settings SHALL 新增外部 API 相关配置项：`ocr_external_api_url`、`ocr_external_api_key`、`ocr_external_api_timeout`（默认 30 秒）
6. ALL OCR 配置项 SHALL 支持通过环境变量注入（与现有 Settings 机制一致）
