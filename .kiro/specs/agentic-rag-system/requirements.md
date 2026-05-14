# Requirements Document

## Introduction

本文档定义了分层渐进式 Agentic RAG 知识库系统的功能需求。该系统从传统 ES 检索升级为智能检索架构，采用 Python + FastAPI 后端、Milvus 统一向量存储（稠密+稀疏）、自研薄编排 Agent 框架，并提供 React + shadcn/ui 管理后台。系统支持三档检索模式（直检索、混合+Rerank、全 Agent），通过父子 chunk 结构实现精准检索与上下文完整性的平衡。

## Glossary

- **RAG_System**：分层渐进式 Agentic RAG 知识库系统的整体服务
- **Chat_API**：对外提供的对话接口，兼容 OpenAI 协议
- **Agent_Orchestrator**：Agent 编排层，负责 Router → Rewriter → Executor → Reflector 的流程调度
- **Retrieval_Engine**：检索工具层，包含向量检索、稀疏检索和 Rerank 能力
- **Index_Store**：索引/存储层，基于 Milvus 的统一稠密+稀疏向量存储
- **Data_Pipeline**：数据处理层，负责文档加载、切片、富化和向量化
- **Model_Provider**：模型抽象层，统一管理 LLM、Embedding、Rerank 模型的调用
- **Admin_UI**：React + shadcn/ui 构建的管理后台前端
- **Knowledge_Base**：知识库实体，包含文档集合及其检索配置
- **Document**：上传到知识库中的文件实体
- **Chunk**：文档经切片后的最小检索单元
- **Parent_Chunk**：父级 chunk，用于返回完整上下文
- **Child_Chunk**：子级 chunk，用于精准检索匹配
- **Reranker**：基于 bge-reranker-v2-m3 的重排序组件
- **Embedder**：基于 bge-m3 的本地向量化组件

## Requirements

### Requirement 1: 模型抽象层

**User Story:** 作为系统开发者，我希望有统一的模型抽象层，以便灵活切换不同的 LLM、Embedding 和 Rerank 模型提供方。

#### Acceptance Criteria

1. THE Model_Provider SHALL 提供统一接口抽象，支持 LLM、Embedding、Rerank 三类模型的调用
2. WHEN 配置指定 Ollama 作为 LLM 提供方时，THE Model_Provider SHALL 通过 Ollama 接口完成推理调用
3. WHEN 配置指定 vLLM 作为 LLM 提供方时，THE Model_Provider SHALL 通过 vLLM 接口完成推理调用
4. THE Model_Provider SHALL 使用 bge-m3 模型进行本地向量化
5. THE Model_Provider SHALL 使用 bge-reranker-v2-m3 模型进行重排序
6. WHEN 模型调用失败时，THE Model_Provider SHALL 返回明确的错误信息，包含失败原因和模型标识

### Requirement 2: 数据处理管道

**User Story:** 作为知识库管理员，我希望系统能处理多种格式的文档并自动完成切片和向量化，以便文档能被高效检索。

#### Acceptance Criteria

1. THE Data_Pipeline SHALL 支持加载 Markdown、TXT、PDF、Word、Excel、PPT 格式的文档
2. WHEN 文档上传完成后，THE Data_Pipeline SHALL 自动执行加载、切片、富化、向量化的完整处理流程
3. THE Data_Pipeline SHALL 将文档切分为父子 chunk 结构，其中 Child_Chunk 用于精准检索，Parent_Chunk 用于上下文返回
4. THE Data_Pipeline SHALL 使用 Embedder 对每个 Child_Chunk 生成稠密向量
5. THE Data_Pipeline SHALL 对每个 Child_Chunk 生成稀疏向量（BM25 表示）
6. WHEN 文档格式不受支持时，THE Data_Pipeline SHALL 拒绝处理并返回包含支持格式列表的错误信息
7. IF 文档处理过程中发生异常，THEN THE Data_Pipeline SHALL 记录错误日志并将该文档标记为处理失败状态

### Requirement 3: 统一向量存储

**User Story:** 作为系统架构师，我希望存储层统一到 Milvus，以便简化部署并支持混合检索。

#### Acceptance Criteria

1. THE Index_Store SHALL 使用 Milvus 同时存储稠密向量和稀疏向量（BM25）
2. THE Index_Store SHALL 为每个 Knowledge_Base 创建独立的 Milvus Collection
3. THE Index_Store SHALL 存储 Chunk 的元数据，包含文档来源、位置信息和父子关系
4. WHEN 执行向量检索时，THE Index_Store SHALL 支持稠密向量相似度搜索
5. WHEN 执行稀疏检索时，THE Index_Store SHALL 通过 Milvus 内置 BM25 稀疏向量完成关键词匹配
6. THE Index_Store SHALL 使用 SQLite 或 PostgreSQL 存储文档元数据和知识库配置信息

### Requirement 4: 检索工具层

**User Story:** 作为系统用户，我希望系统提供多种检索策略，以便在不同场景下获得最佳检索效果。

#### Acceptance Criteria

1. THE Retrieval_Engine SHALL 支持三档检索模式：直检索模式、混合+Rerank 模式、全 Agent 模式
2. WHEN 使用直检索模式时，THE Retrieval_Engine SHALL 仅执行单路向量检索并直接返回结果
3. WHEN 使用混合+Rerank 模式时，THE Retrieval_Engine SHALL 同时执行稠密向量检索和稀疏向量检索，并使用 Reranker 对合并结果进行重排序
4. WHEN 使用全 Agent 模式时，THE Retrieval_Engine SHALL 将检索请求交由 Agent_Orchestrator 进行智能编排
5. WHEN 检索命中 Child_Chunk 时，THE Retrieval_Engine SHALL 返回对应的 Parent_Chunk 作为完整上下文
6. THE Retrieval_Engine SHALL 在检索结果中包含相关性分数和来源文档信息

### Requirement 5: Agent 编排层

**User Story:** 作为系统用户，我希望在全 Agent 模式下获得智能化的检索体验，系统能自动改写查询、反思结果质量并迭代优化。

#### Acceptance Criteria

1. THE Agent_Orchestrator SHALL 按照 Router → Rewriter → Executor → Reflector 的流程执行编排
2. WHEN 接收到用户查询时，THE Agent_Orchestrator 的 Router 组件 SHALL 判断查询意图并选择合适的处理路径
3. THE Agent_Orchestrator 的 Rewriter 组件 SHALL 对用户原始查询进行改写优化以提升检索效果
4. THE Agent_Orchestrator 的 Executor 组件 SHALL 调用 Retrieval_Engine 执行实际检索操作
5. THE Agent_Orchestrator 的 Reflector 组件 SHALL 评估检索结果质量，并在质量不足时触发重新检索
6. THE Agent_Orchestrator SHALL 设置 max_iterations 限制，防止无限循环
7. THE Agent_Orchestrator SHALL 设置超时兜底机制，在超时后返回当前最佳结果
8. IF Agent 编排过程中发生异常，THEN THE Agent_Orchestrator SHALL 降级到混合+Rerank 模式返回结果
9. WHILE 处于全 Agent 模式时，THE Agent_Orchestrator SHALL 在 3 至 8 秒内完成整个编排流程

### Requirement 6: Chat API 接入层

**User Story:** 作为 API 调用方，我希望系统提供兼容 OpenAI 协议的接口，以便无缝集成到现有系统中。

#### Acceptance Criteria

1. THE Chat_API SHALL 提供兼容 OpenAI Chat Completions 协议的接口
2. THE Chat_API SHALL 支持流式输出（Server-Sent Events）
3. THE Chat_API SHALL 支持非流式同步响应
4. THE Chat_API SHALL 使用无状态 API Key 进行身份认证
5. WHEN API Key 无效或缺失时，THE Chat_API SHALL 返回 401 状态码和明确的错误信息
6. WHEN 请求参数不合法时，THE Chat_API SHALL 返回 400 状态码和参数校验错误详情
7. THE Chat_API SHALL 在响应中包含 token 使用量统计信息

### Requirement 7: 知识库管理

**User Story:** 作为知识库管理员，我希望通过管理后台对知识库进行完整的生命周期管理。

#### Acceptance Criteria

1. THE RAG_System SHALL 提供知识库的创建、查询、更新、删除操作接口
2. WHEN 创建知识库时，THE RAG_System SHALL 要求指定知识库名称和描述信息
3. WHEN 删除知识库时，THE RAG_System SHALL 同时清理该知识库关联的所有文档、Chunk 和向量数据
4. THE RAG_System SHALL 支持为每个知识库独立配置检索模式和参数
5. THE RAG_System SHALL 提供知识库的文档数量和存储状态统计信息

### Requirement 8: 文档管理

**User Story:** 作为知识库管理员，我希望能上传和管理知识库中的文档。

#### Acceptance Criteria

1. THE RAG_System SHALL 支持向指定知识库上传文档文件
2. THE RAG_System SHALL 展示文档的处理状态（待处理、处理中、已完成、失败）
3. WHEN 删除文档时，THE RAG_System SHALL 同时删除该文档关联的所有 Chunk 和向量数据
4. THE RAG_System SHALL 支持查看文档的切片结果和元数据信息
5. WHEN 文档上传成功后，THE Data_Pipeline SHALL 自动触发文档处理流程

### Requirement 9: 管理后台前端

**User Story:** 作为知识库管理员，我希望有一个直观的管理界面来操作系统的各项功能。

#### Acceptance Criteria

1. THE Admin_UI SHALL 使用 React 和 shadcn/ui 组件库构建
2. THE Admin_UI SHALL 提供知识库管理界面，支持知识库的创建、编辑、删除和列表展示
3. THE Admin_UI SHALL 提供文档上传与管理界面，支持文件拖拽上传和处理状态展示
4. THE Admin_UI SHALL 提供对话界面，支持流式输出的实时展示
5. THE Admin_UI SHALL 提供检索测试工具界面，支持输入查询并展示检索结果和评分
6. THE Admin_UI SHALL 提供系统配置界面，支持模型参数和检索参数的调整
7. THE Admin_UI SHALL 提供 API Key 管理界面，支持 Key 的创建、查看和撤销

### Requirement 10: API Key 认证

**User Story:** 作为系统管理员，我希望通过 API Key 机制控制接口访问权限。

#### Acceptance Criteria

1. THE RAG_System SHALL 支持创建、查看和撤销 API Key
2. THE RAG_System SHALL 采用无状态方式验证 API Key 的有效性
3. WHEN 请求未携带有效 API Key 时，THE Chat_API SHALL 拒绝访问并返回认证失败响应
4. THE RAG_System SHALL 记录每个 API Key 的调用次数和最后使用时间
5. WHEN API Key 被撤销后，THE RAG_System SHALL 立即使该 Key 失效

### Requirement 11: 系统部署

**User Story:** 作为运维人员，我希望系统能通过 Docker 容器化部署，以便简化环境搭建和服务管理。

#### Acceptance Criteria

1. THE RAG_System SHALL 提供 Docker 容器化部署方案，作为独立服务运行
2. THE RAG_System SHALL 提供 docker-compose 配置，包含所有依赖服务（Milvus、模型服务等）
3. THE RAG_System SHALL 通过环境变量或配置文件管理所有可配置参数
4. WHEN 服务启动时，THE RAG_System SHALL 自动检查依赖服务的连接状态并报告健康状况
5. THE RAG_System SHALL 提供健康检查接口供容器编排工具使用

### Requirement 12: 防御性设计与容错

**User Story:** 作为系统用户，我希望系统在异常情况下仍能提供降级服务，而非完全不可用。

#### Acceptance Criteria

1. WHEN Agent 编排超时时，THE Agent_Orchestrator SHALL 返回当前已获取的最佳结果
2. WHEN 模型服务不可用时，THE RAG_System SHALL 降级到纯检索模式返回原始检索结果
3. IF Reranker 服务异常，THEN THE Retrieval_Engine SHALL 跳过重排序步骤，直接返回初始检索结果
4. THE Agent_Orchestrator SHALL 将最大迭代次数限制为可配置参数，默认值明确设定
5. THE RAG_System SHALL 对所有外部服务调用设置超时时间
6. WHEN 发生降级时，THE RAG_System SHALL 在响应中标注当前使用的降级模式
