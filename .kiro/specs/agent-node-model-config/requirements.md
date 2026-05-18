# Requirements Document

## Introduction

Agent 节点模型配置功能，允许用户为 Agent 编排流程中的各执行节点（Router、Rewriter、Reflector）独立指定 LLM 模型。同时在现有模型管理中增加"对话可见"开关，使小模型可以注册到系统中仅供内部节点使用，不暴露在对话模型选择列表中。参考现有 LLM 配置管理（llm_config.py / Models.tsx）的设计模式实现。

## Glossary

- **Agent_Node**: Agent 编排流程中的执行节点，当前包括 Router（查询路由）、Rewriter（查询改写）、Reflector（结果反思）
- **Node_Config**: Agent 节点与 LLM 模型的绑定关系记录，存储节点名称和对应的模型配置 ID
- **Chat_Visible**: LLM 模型配置上的布尔字段，控制该模型是否在对话页面的模型选择列表中显示
- **Fallback_LLM**: 当节点绑定的模型未配置或不可用时，自动回退使用的对话模型

## Requirements

### Requirement 1: 模型对话可见性控制

**User Story:** As a 系统管理员, I want 控制哪些模型在对话模型列表中可选, so that 配置的小模型不会干扰终端用户的对话体验。

#### Acceptance Criteria

1. THE LLM_Config SHALL 包含一个 `chat_visible` 布尔字段，默认值为 true
2. WHEN 管理员创建 LLM 配置时, THE System SHALL 接受 `chat_visible` 参数并持久化
3. WHEN 管理员更新 LLM 配置时, THE System SHALL 允许修改 `chat_visible` 字段
4. WHEN 前端对话页面请求模型列表时, THE System SHALL 支持 `chat_visible` 查询参数进行过滤
5. IF `chat_visible` 查询参数为 true, THEN THE System SHALL 仅返回 `chat_visible=true` 的模型
6. IF 未传 `chat_visible` 查询参数, THEN THE System SHALL 返回所有模型（向后兼容）
7. WHEN 数据库迁移执行时, THE System SHALL 将所有现有模型的 `chat_visible` 设为 true，确保行为不变

### Requirement 2: Agent 节点配置持久化

**User Story:** As a 系统管理员, I want 节点模型配置持久化到数据库, so that 服务重启后配置不丢失。

#### Acceptance Criteria

1. THE System SHALL 使用 AgentNodeConfig 表存储节点模型绑定，主键为 node_name（String）
2. THE AgentNodeConfig SHALL 包含以下字段：node_name（String(50) 主键，取值为 router/rewriter/reflector）、model_config_id（String(36)，外键关联 llm_config.id，可为空）、updated_at（DateTime，自动更新）
3. WHEN llm_config 表中被引用的模型被删除时, THE System SHALL 将对应 AgentNodeConfig 的 model_config_id 自动设为 NULL（ON DELETE SET NULL）
4. WHEN AgentNodeConfig 表中某节点记录不存在或 model_config_id 为 NULL, THE System SHALL 视为该节点未配置独立模型

### Requirement 3: Agent 节点配置查询

**User Story:** As a 系统管理员, I want 查看当前各节点绑定的模型, so that 了解 Agent 的模型分配情况。

#### Acceptance Criteria

1. WHEN 管理员请求节点配置列表, THE System SHALL 返回所有三个节点（router、rewriter、reflector）的配置状态
2. THE System SHALL 对每个节点返回 model_config_id 和对应的模型名称（model_name），未配置时两者均为 null
3. IF 节点引用的模型已被删除（model_config_id 为 NULL）, THEN THE System SHALL 返回该节点的 model_config_id 和 model_name 均为 null

### Requirement 4: Agent 节点配置更新

**User Story:** As a 系统管理员, I want 为各 Agent 节点指定或更换模型, so that 用适合的模型处理不同复杂度的任务。

#### Acceptance Criteria

1. WHEN 管理员提交节点配置更新请求, THE System SHALL 接受 router_model_id、rewriter_model_id、reflector_model_id 三个可选字段
2. WHEN 请求中某个字段有值, THE System SHALL 更新对应节点的 model_config_id（不存在则创建记录）
3. WHEN 请求中某个字段值为空字符串, THE System SHALL 将对应节点的 model_config_id 设为 NULL（清除绑定）
4. WHEN 请求中某个字段未提供（undefined）, THE System SHALL 保持对应节点配置不变
5. IF 请求中引用的 model_config_id 在 llm_config 表中不存在, THEN THE System SHALL 返回 400 错误并提示模型不存在

### Requirement 5: Agent 节点运行时模型加载

**User Story:** As a 系统, I want Agent 节点在运行时使用配置的专属模型, so that 降低推理成本和延迟。

#### Acceptance Criteria

1. WHEN Agent 模式处理请求时, THE System SHALL 为 Router、Rewriter、Reflector 各自加载节点配置的模型
2. IF 某节点未配置独立模型（node_config 不存在或 model_config_id 为 NULL）, THEN THE System SHALL 使用当前对话请求选择的模型作为 Fallback
3. IF 节点配置的模型连接失败或实例化异常, THEN THE System SHALL 捕获异常，使用对话模型作为 Fallback，并记录 warning 级别日志
4. THE System SHALL 在每次请求时从数据库读取节点配置（确保配置变更即时生效）

### Requirement 6: 前端模型管理页面变更

**User Story:** As a 系统管理员, I want 在模型管理页面配置对话可见性和节点绑定, so that 在同一个界面统一管理模型用途。

#### Acceptance Criteria

1. THE Models_Page SHALL 在模型创建/编辑表单中展示"允许在对话中选择"复选框，对应 `chat_visible` 字段
2. WHEN 模型的 `chat_visible` 为 false, THE Models_Page SHALL 在模型卡片上展示"仅内部"标签以区分
3. THE Models_Page SHALL 在模型列表下方展示"Agent 节点模型配置"区域
4. THE Agent_Config_Area SHALL 展示三个下拉选择器，分别对应 Router、Rewriter、Reflector 节点
5. THE Agent_Config_Area 的下拉列表 SHALL 展示系统中所有已配置的模型（不受 chat_visible 限制）
6. THE Agent_Config_Area SHALL 在每个下拉选择器中提供"未配置（使用对话模型）"选项作为默认值
7. WHEN 管理员点击"保存配置"按钮, THE Agent_Config_Area SHALL 调用 PUT 接口批量更新节点配置
8. WHEN 保存成功, THE Agent_Config_Area SHALL 展示成功提示

### Requirement 7: 前端对话页面变更

**User Story:** As a 终端用户, I want 对话模型列表中只看到适合对话的模型, so that 选择不被无关的小模型干扰。

#### Acceptance Criteria

1. WHEN 对话页面加载模型列表时, THE Chat_Page SHALL 请求 `chat_visible=true` 的模型列表
2. IF 过滤后模型列表为空, THEN THE Chat_Page SHALL 展示提示信息引导管理员添加对话模型
