# Implementation Plan: Agent 节点模型配置

## Overview

为 Agent 各执行节点（Router、Rewriter、Reflector）实现独立的模型配置能力，同时在 LLM 配置中增加对话可见性控制。按照数据库变更 → 后端 API → Chat 集成 → 前端实现的顺序逐步实现。

## Tasks

- [x] 1. 数据库模型变更
  - [x] 1.1 在 `backend/app/schema/db.py` 中为 LLMConfig 新增 `chat_visible` 字段，并新增 AgentNodeConfig 模型
    - LLMConfig 新增 `chat_visible = Column(Boolean, default=True, nullable=False)`
    - 新增 AgentNodeConfig 类：node_name(String(50) PK)、model_config_id(String(36) FK → llm_config.id ON DELETE SET NULL, nullable)、updated_at(DateTime server_default=func.now() onupdate=func.now())
    - 确保 `backend/app/main.py` 的 lifespan 中 Base.metadata.create_all 能自动创建新表和新列
    - _Requirements: 1.1, 1.7, 2.1, 2.2, 2.3_

- [x] 2. LLM Config API 变更
  - [x] 2.1 修改 `backend/app/api/llm_config.py`，支持 `chat_visible` 字段的 CRUD 和过滤
    - LLMConfigCreate 新增 `chat_visible: bool = True`
    - LLMConfigUpdate 新增 `chat_visible: Optional[bool] = None`
    - LLMConfigResponse 新增 `chat_visible: bool`
    - list_llm_configs 端点新增可选查询参数 `chat_visible: Optional[bool] = None`，非空时添加 where 过滤条件
    - create/update/response 映射中正确处理 chat_visible 字段
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

- [x] 3. Agent 节点配置 API
  - [x] 3.1 创建 `backend/app/api/agent_node_config.py`，实现 GET/PUT 端点
    - 创建 `router = APIRouter(prefix="/api/agent-node-configs", tags=["Agent Node Config"])`
    - 定义 AgentNodeConfigUpdate(router_model_id: Optional[str], rewriter_model_id: Optional[str], reflector_model_id: Optional[str])
    - 定义 AgentNodeConfigResponse(router_model_id: Optional[str], router_model_name: Optional[str], rewriter_model_id: Optional[str], rewriter_model_name: Optional[str], reflector_model_id: Optional[str], reflector_model_name: Optional[str])
    - GET `/api/agent-node-configs`：查询三个节点的配置，join LLMConfig 获取 model name，未配置的节点返回 null
    - PUT `/api/agent-node-configs`：遍历请求字段，值为字符串则 upsert 对应节点记录（先验证 model_config_id 存在），值为空字符串则设 model_config_id=NULL，字段 undefined 则不操作；model_id 不存在返回 400
    - 在 `backend/app/main.py` 中注册此 router
    - _Requirements: 3.1, 3.2, 3.3, 4.1, 4.2, 4.3, 4.4, 4.5_

- [x] 4. Chat API 集成
  - [x] 4.1 修改 `backend/app/api/chat.py`，Agent 模式下为各节点加载独立 LLM
    - 新增 `_get_node_llm(node_name: str, fallback_llm: LLMProvider) -> LLMProvider` 异步函数：从 AgentNodeConfig 查询节点配置，有效则创建 LLM 实例，异常或未配置则返回 fallback_llm
    - 修改 `_retrieve_chunks` 中 agent 模式分支：调用 `_get_node_llm` 分别获取 router_llm、rewriter_llm、reflector_llm
    - 将获取的独立 LLM 传入 QueryRouter、QueryRewriter、Reflector 构造函数
    - 添加 try/except 确保节点模型创建失败时 fallback 到对话 LLM 并记录 warning 日志
    - _Requirements: 5.1, 5.2, 5.3, 5.4_

- [x] 5. 前端 API 客户端
  - [x] 5.1 在 `frontend/src/lib/api.ts` 中新增 agentNodeConfigApi 对象和修改 llmConfigApi
    - llmConfigApi.list 新增可选参数 `chatVisible?: boolean`，传入时附加 `?chat_visible=true` 查询参数
    - 新增 AgentNodeConfigResponse 接口
    - 新增 agentNodeConfigApi.get(): GET /api/agent-node-configs
    - 新增 agentNodeConfigApi.update(data): PUT /api/agent-node-configs
    - _Requirements: 1.4, 3.1, 4.1_

- [x] 6. 前端 Models 页面变更
  - [x] 6.1 修改 `frontend/src/pages/Models.tsx`，表单新增 chat_visible 字段，卡片展示"仅内部"标签
    - FormData 接口新增 `chat_visible: boolean`，emptyForm 默认 true
    - 创建/编辑对话框表单新增复选框："允许在对话中选择此模型"
    - openEdit 时从 item 读取 chat_visible 填充表单
    - handleSubmit 时将 chat_visible 包含在提交数据中
    - 模型卡片上：chat_visible 为 false 时在名称旁显示"仅内部" Badge（variant=secondary）
    - _Requirements: 6.1, 6.2_

  - [x] 6.2 在 `frontend/src/pages/Models.tsx` 中新增 Agent 节点配置区域
    - 在模型列表下方新增分隔区域，标题"Agent 节点模型配置"
    - 使用 useQuery 获取节点配置（queryKey: ['agent-node-configs']）
    - 使用 useQuery 获取所有模型列表（不过滤 chat_visible，用于下拉选项）
    - 展示三个 Select 下拉框：查询路由(Router)、查询改写(Rewriter)、结果反思(Reflector)
    - 每个 Select 选项包含"未配置（使用对话模型）"默认项 + 所有模型列表
    - 使用 useMutation 实现保存按钮，调用 agentNodeConfigApi.update
    - 保存成功后展示成功提示，invalidateQueries 刷新
    - _Requirements: 6.3, 6.4, 6.5, 6.6, 6.7, 6.8_

- [x] 7. 前端 Chat 页面变更
  - [x] 7.1 修改 `frontend/src/pages/Chat.tsx`，模型选择器仅加载对话可见模型
    - 修改模型列表请求调用，传入 `chatVisible: true` 参数
    - 列表为空时展示提示信息
    - _Requirements: 7.1, 7.2_

## Notes

- `chat_visible` 默认 true 确保向后兼容，现有模型行为不变
- AgentNodeConfig 表初始为空，所有节点 fallback 到对话模型，零配置即可运行
- 节点配置下拉列表展示所有模型（包括 chat_visible=false 的），因为内部节点应该能选择任何已配置模型
- 节点模型每次请求时从数据库读取，确保配置变更即时生效（无需重启）
- FK ON DELETE SET NULL 保证模型删除后节点自动解绑，不会引发外键错误

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["4.1"] },
    { "id": 3, "tasks": ["5.1"] },
    { "id": 4, "tasks": ["6.1", "6.2", "7.1"] }
  ]
}
```
