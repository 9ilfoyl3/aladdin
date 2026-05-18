# Requirements Document

## Introduction

OCR 服务管理功能，参考现有大模型管理页面（Models.tsx / llm_config.py）的设计模式，提供一个完整的 OCR 服务 CRUD 管理界面。用户可通过前端页面维护多个 OCR 服务配置（包含 PaddleOCR 本地服务和外部 API 服务），设置默认服务和 Fallback 服务，配置数据持久化到数据库，替代当前基于环境变量的静态 OCR 配置方式。

## Glossary

- **OCR_Service_Manager**: OCR 服务管理后端模块，负责 OCR 服务配置的 CRUD 操作和持久化
- **OCR_Config**: 单个 OCR 服务的配置记录，包含名称、类型、API 地址、密钥、超时等字段
- **OCR_Management_Page**: OCR 服务管理前端页面，提供可视化的服务列表展示与表单编辑
- **Default_Service**: 被标记为默认的 OCR 服务，系统执行 OCR 时优先使用
- **Fallback_Service**: 被标记为备用的 OCR 服务，当默认服务失败时自动切换使用
- **Provider_Type**: OCR 服务的类型标识，当前支持 paddleocr（本地）和 external_api（外部 HTTP API）

## Requirements

### Requirement 1: OCR 服务配置数据持久化

**User Story:** As a 系统管理员, I want OCR 服务配置持久化到数据库, so that 服务重启后配置不会丢失。

#### Acceptance Criteria

1. THE OCR_Service_Manager SHALL 使用 SQLAlchemy ORM 模型将 OCR_Config 存储到 SQLite 数据库
2. WHEN 一条 OCR_Config 被创建, THE OCR_Service_Manager SHALL 为其生成唯一的 UUID 主键
3. THE OCR_Config SHALL 包含以下字段：id（String 主键）、name（String，最大长度 100 字符）、provider_type（String，仅允许 paddleocr 或 external_api）、api_url（String，最大长度 2048 字符）、api_key（String，可为空）、timeout（Float，单位为秒，默认值 30，有效范围 1-300）、is_default（Boolean，默认 false）、is_fallback（Boolean，默认 false）、extra_config（JSON 类型，可为空，存储键值对形式的扩展参数）、created_at（DateTime，记录创建时自动生成）、updated_at（DateTime，记录创建时自动生成，每次更新时自动刷新为当前时间）
4. WHEN 应用启动时, THE OCR_Service_Manager SHALL 从数据库加载所有 OCR_Config 以初始化可用 Provider
5. IF 应用启动时数据库中无任何 OCR_Config 记录, THEN THE OCR_Service_Manager SHALL 正常启动且不注册任何 Provider，直到管理员通过管理界面添加配置

### Requirement 2: OCR 服务配置创建

**User Story:** As a 系统管理员, I want 通过管理界面添加新的 OCR 服务配置, so that 系统可以接入更多 OCR 服务。

#### Acceptance Criteria

1. WHEN 管理员提交包含 name、provider_type、api_url 的创建请求, THE OCR_Service_Manager SHALL 创建一条新的 OCR_Config，返回 201 状态码及该记录的所有字段（其中 api_key 以 api_key_set 布尔值代替明文返回）
2. IF 创建请求中 name 为空字符串或仅包含空白字符, THEN THE OCR_Service_Manager SHALL 返回 422 错误并提示名称不能为空
3. IF 创建请求中 provider_type 不是 paddleocr 或 external_api, THEN THE OCR_Service_Manager SHALL 返回 422 错误并提示类型无效
4. WHEN 创建请求中 is_default 为 true, THE OCR_Service_Manager SHALL 将其他 OCR_Config 的 is_default 设为 false
5. WHEN 创建请求中 is_fallback 为 true, THE OCR_Service_Manager SHALL 将其他 OCR_Config 的 is_fallback 设为 false
6. IF 创建请求中 api_url 为空字符串, THEN THE OCR_Service_Manager SHALL 返回 422 错误并提示 API 地址不能为空
7. IF 创建请求中 name 长度超过 100 个字符, THEN THE OCR_Service_Manager SHALL 返回 422 错误并提示名称过长

### Requirement 3: OCR 服务配置查询

**User Story:** As a 系统管理员, I want 查看所有已配置的 OCR 服务列表, so that 了解当前系统的 OCR 服务状态。

#### Acceptance Criteria

1. WHEN 管理员请求 OCR 服务列表, THE OCR_Service_Manager SHALL 返回按创建时间倒序排列的所有 OCR_Config，每条记录包含 id、name、provider_type、api_url、timeout、is_default、is_fallback、extra_config、created_at、updated_at 字段
2. THE OCR_Service_Manager SHALL 在响应中隐藏 api_key 明文，仅返回 api_key_set 布尔值标识是否已设置密钥
3. IF 系统中无任何 OCR_Config 记录, THEN THE OCR_Service_Manager SHALL 返回空列表

### Requirement 4: OCR 服务配置更新

**User Story:** As a 系统管理员, I want 修改已有的 OCR 服务配置, so that 适应服务地址或参数变更。

#### Acceptance Criteria

1. WHEN 管理员提交更新请求且仅包含部分字段, THE OCR_Service_Manager SHALL 仅更新提供的字段，保持其他字段不变，更新 updated_at 时间戳，并返回更新后的完整 OCR_Config
2. WHEN 更新请求中 api_key 为空字符串, THE OCR_Service_Manager SHALL 保持原有 api_key 不变
3. WHEN 更新请求中 is_default 为 true, THE OCR_Service_Manager SHALL 将其他 OCR_Config 的 is_default 设为 false
4. WHEN 更新请求中 is_fallback 为 true, THE OCR_Service_Manager SHALL 将其他 OCR_Config 的 is_fallback 设为 false
5. IF 指定的 config_id 不存在, THEN THE OCR_Service_Manager SHALL 返回 404 错误
6. IF 更新请求中 name 字段为空字符串, THEN THE OCR_Service_Manager SHALL 返回 422 错误并提示名称不能为空
7. IF 更新请求中 provider_type 不是 paddleocr 或 external_api, THEN THE OCR_Service_Manager SHALL 返回 422 错误并提示类型无效

### Requirement 5: OCR 服务配置删除

**User Story:** As a 系统管理员, I want 删除不再使用的 OCR 服务配置, so that 保持服务列表整洁。

#### Acceptance Criteria

1. WHEN 管理员请求删除指定 OCR_Config, THE OCR_Service_Manager SHALL 从数据库移除该记录并返回 204 状态码
2. IF 指定的 config_id 不存在, THEN THE OCR_Service_Manager SHALL 返回 404 错误
3. WHEN 被删除的 OCR_Config 当前标记为 is_default 或 is_fallback, THE OCR_Service_Manager SHALL 允许删除，删除后系统无默认或备用服务直至管理员重新指定

### Requirement 6: OCR 服务连通性测试

**User Story:** As a 系统管理员, I want 测试 OCR 服务是否可用, so that 在使用前确认配置正确。

#### Acceptance Criteria

1. WHEN 管理员对已保存的 OCR_Config 发起测试请求, THE OCR_Service_Manager SHALL 根据 provider_type 实例化对应 Provider，使用该配置的 timeout 值（默认 30 秒）执行连通性检查
2. WHEN 连通性检查在 timeout 时间内收到有效响应, THE OCR_Service_Manager SHALL 返回 success 为 true 和响应耗时信息
3. IF 连通性检查超时或收到错误响应, THEN THE OCR_Service_Manager SHALL 返回 success 为 false 和错误原因描述
4. WHEN 管理员发起临时配置测试请求, THE OCR_Service_Manager SHALL 接受 provider_type、api_url、api_key、timeout 参数并执行连通性检查（无需先保存）
5. IF 测试已保存配置时指定的 config_id 不存在, THEN THE OCR_Service_Manager SHALL 返回 404 错误

### Requirement 7: 默认服务与 Fallback 服务选择

**User Story:** As a 系统管理员, I want 指定默认 OCR 服务和 Fallback 服务, so that 系统自动使用正确的服务并在失败时切换。

#### Acceptance Criteria

1. THE OCR_Service_Manager SHALL 确保同一时刻最多只有一个 OCR_Config 被标记为 is_default
2. THE OCR_Service_Manager SHALL 确保同一时刻最多只有一个 OCR_Config 被标记为 is_fallback
3. THE OCR_Service_Manager SHALL 禁止将同一个 OCR_Config 同时标记为 is_default 和 is_fallback
4. WHEN OCR Manager 初始化时, THE OCR_Service_Manager SHALL 从数据库中读取 is_default 和 is_fallback 标记来确定主备关系
5. WHEN 默认服务执行 OCR 时抛出异常或请求超时, THE OCR_Service_Manager SHALL 自动使用 Fallback 服务重试一次
6. IF 默认服务执行失败且未配置 Fallback 服务, THEN THE OCR_Service_Manager SHALL 向调用方抛出错误并附带原始失败原因

### Requirement 8: 前端管理界面

**User Story:** As a 系统管理员, I want 一个类似大模型管理页面的 OCR 服务管理界面, so that 直观地管理所有 OCR 服务配置。

#### Acceptance Criteria

1. THE OCR_Management_Page SHALL 以卡片网格形式展示所有 OCR 服务配置，每张卡片包含名称、类型、API 地址、密钥状态（显示"已设置"或"未设置"）
2. THE OCR_Management_Page SHALL 在默认服务卡片上展示星标图标标识
3. THE OCR_Management_Page SHALL 在 Fallback 服务卡片上展示备用标识
4. WHEN 管理员点击"添加服务"按钮, THE OCR_Management_Page SHALL 弹出包含所有配置字段的创建对话框
5. WHEN 管理员点击卡片上的"编辑"按钮, THE OCR_Management_Page SHALL 弹出预填充当前配置的编辑对话框
6. WHEN 管理员点击卡片上的"删除"按钮, THE OCR_Management_Page SHALL 弹出确认对话框，确认后删除该配置并刷新列表
7. WHEN 管理员点击卡片上的"测试"按钮, THE OCR_Management_Page SHALL 展示加载状态，完成后在卡片下方展示测试结果（成功或失败原因）
8. WHEN 服务列表为空, THE OCR_Management_Page SHALL 展示空状态引导界面，包含提示文字和"添加服务"入口按钮
9. IF 页面加载或操作时后端接口返回错误, THEN THE OCR_Management_Page SHALL 展示错误提示信息
