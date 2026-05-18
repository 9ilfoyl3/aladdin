# 设计：Agent 节点模型配置

## Overview

允许用户为 Agent 各执行节点（Router、Rewriter、Reflector）独立指定 LLM 模型，同时在模型管理中增加"是否在对话中可选"开关，使小模型可以注册到系统中仅供内部节点使用，不暴露给终端用户的对话模型列表。

核心目标：
- 降低 Agent 内部节点的推理成本和延迟（小模型处理简单任务）
- 保持对话模型列表的整洁（用户只看到适合对话的模型）
- 节点模型配置完全由用户控制，灵活可调

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Frontend - Models Page                                  │
│                                                          │
│  ┌──────────────────┐   ┌────────────────────────────┐  │
│  │  模型列表 (CRUD)  │   │  Agent 节点配置表单         │  │
│  │  + chat_visible   │   │  Router    → [模型下拉]    │  │
│  │    开关           │   │  Rewriter  → [模型下拉]    │  │
│  └──────────────────┘   │  Reflector → [模型下拉]    │  │
│                          └────────────────────────────┘  │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  Backend                                                 │
│                                                          │
│  LLMConfig 表            AgentNodeConfig 表              │
│  ┌────────────────┐      ┌─────────────────────┐       │
│  │ id             │      │ node_name (PK)      │       │
│  │ name           │      │ model_config_id (FK)│       │
│  │ provider       │      └─────────────────────┘       │
│  │ base_url       │                                     │
│  │ model          │                                     │
│  │ chat_visible ← │  ← 新增字段                         │
│  │ ...            │                                     │
│  └────────────────┘                                     │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  Agent Orchestrator                                      │
│                                                          │
│  run() {                                                 │
│    router_llm   = load_node_model("router")  || default │
│    rewriter_llm = load_node_model("rewriter")|| default │
│    reflector_llm= load_node_model("reflector")|| default│
│  }                                                       │
└─────────────────────────────────────────────────────────┘
```

数据流：
1. 用户在 Models 页面配置模型，设置 `chat_visible`
2. 用户在 Agent 节点配置区域为各节点选择模型
3. Chat API 接收对话请求时：
   - 对话生成：使用用户选择的对话模型（`model_config_id`）
   - Agent 节点：从 `AgentNodeConfig` 表读取节点绑定的模型，未配置则 fallback 到对话模型
4. 前端对话模型选择器只请求 `chat_visible=true` 的模型列表

## Components and Interfaces

### 后端 API 接口

#### LLMConfig API 变更

`LLMConfigCreate` / `LLMConfigUpdate` 增加字段：

```python
chat_visible: bool = True
```

`LLMConfigResponse` 增加字段：

```python
chat_visible: bool
```

列表接口支持过滤：

```python
@router.get("")
async def list_llm_configs(chat_visible: Optional[bool] = None, db=Depends(get_db)):
    query = select(LLMConfig).order_by(LLMConfig.created_at.desc())
    if chat_visible is not None:
        query = query.where(LLMConfig.chat_visible == chat_visible)
    ...
```

#### 新增 Agent 节点配置 API

```
GET    /api/agent-node-configs          → 获取所有节点配置
PUT    /api/agent-node-configs          → 批量更新节点配置
```

请求体（PUT）：

```python
class AgentNodeConfigUpdate(BaseModel):
    router_model_id: Optional[str] = None
    rewriter_model_id: Optional[str] = None
    reflector_model_id: Optional[str] = None
```

响应体（GET）：

```python
class AgentNodeConfigResponse(BaseModel):
    router_model_id: Optional[str] = None
    router_model_name: Optional[str] = None
    rewriter_model_id: Optional[str] = None
    rewriter_model_name: Optional[str] = None
    reflector_model_id: Optional[str] = None
    reflector_model_name: Optional[str] = None
```

#### Chat API 变更

`_retrieve_chunks` 中构建 AgentOrchestrator 时为各节点加载独立 LLM：

```python
async def _get_node_llm(node_name: str, fallback_llm: LLMProvider) -> LLMProvider:
    """获取指定节点的 LLM，未配置则 fallback"""
    async with async_session() as session:
        result = await session.execute(
            select(AgentNodeConfig).where(AgentNodeConfig.node_name == node_name)
        )
        node_config = result.scalar_one_or_none()
        if node_config and node_config.model_config_id:
            llm_result = await session.execute(
                select(LLMConfig).where(LLMConfig.id == node_config.model_config_id)
            )
            llm_config = llm_result.scalar_one_or_none()
            if llm_config:
                try:
                    return _create_llm_from_config(llm_config)
                except Exception:
                    pass  # fallback
    return fallback_llm
```

Orchestrator 构建变更：

```python
router_llm = await _get_node_llm("router", llm)
rewriter_llm = await _get_node_llm("rewriter", llm)
reflector_llm = await _get_node_llm("reflector", llm)

orchestrator = AgentOrchestrator(
    router=QueryRouter(router_llm),
    rewriter=QueryRewriter(rewriter_llm),
    reflector=Reflector(reflector_llm),
    ...
)
```

### 前端组件

#### Models 页面变更

- 模型表单新增 `chat_visible` 复选框："允许在对话中选择此模型"
- 模型卡片上 `chat_visible = false` 时显示"仅内部"标签

#### Agent 节点配置区域

在 Models 页面新增配置区域：

```
┌─────────────────────────────────────────────┐
│  Agent 节点模型配置                           │
│                                              │
│  查询路由 (Router)     [▼ Qwen2.5-1.5B    ] │
│  查询改写 (Rewriter)   [▼ Qwen2.5-7B      ] │
│  结果反思 (Reflector)  [▼ Qwen2.5-7B      ] │
│                                              │
│  提示：未配置的节点将使用对话时选择的模型       │
│                                              │
│                          [ 保存配置 ]         │
└─────────────────────────────────────────────┘
```

下拉列表展示所有已配置的模型（不受 `chat_visible` 限制）。

#### Chat 页面变更

对话模型选择器请求时增加 `chat_visible=true` 过滤参数。

## Data Models

### LLMConfig 表变更

新增字段：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `chat_visible` | Boolean | `true` | 是否在对话模型列表中可选 |

### 新增 AgentNodeConfig 表

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `node_name` | String(50) | PK | 节点名：router / rewriter / reflector |
| `model_config_id` | String(36) | FK → llm_config.id, ON DELETE SET NULL | 绑定的模型 ID |
| `updated_at` | DateTime | auto | 更新时间 |

SQLAlchemy 模型定义：

```python
class AgentNodeConfig(Base):
    __tablename__ = "agent_node_config"

    node_name = Column(String(50), primary_key=True)
    model_config_id = Column(String(36), ForeignKey("llm_config.id", ondelete="SET NULL"), nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
```

### 迁移兼容性

- `chat_visible` 默认 `true`，现有模型行为不变
- `AgentNodeConfig` 表初始为空，所有节点默认 fallback 到对话模型
- 无破坏性变更，完全向后兼容

## Correctness Properties

### Property 1: Fallback 一致性
未配置节点模型时，Agent 行为与当前版本完全一致（使用对话模型）。
**Validates: Requirements 5.2**

### Property 2: 删除安全
模型被删除时，`AgentNodeConfig.model_config_id` 自动置 NULL（ON DELETE SET NULL），节点自动 fallback 到对话模型。
**Validates: Requirements 2.3**

### Property 3: 对话可见性与节点配置独立
`chat_visible = false` 的模型仍可被节点引用，两个字段的语义互不干扰。
**Validates: Requirements 1.1, 6.5**

### Property 4: 默认模型语义不变
`is_default` 仍表示对话默认模型，与节点配置无关。
**Validates: Requirements 1.7**

### Property 5: 并发安全
节点配置读取为只读操作，每次请求时从数据库加载，确保配置变更即时生效。
**Validates: Requirements 5.4**

## Error Handling

| 场景 | 处理策略 |
|------|----------|
| 节点配置的模型连接失败 | 捕获异常，fallback 到当前对话模型，记录 warning 日志 |
| 节点配置的 model_config_id 对应记录不存在 | 视为未配置，使用对话模型 |
| PUT 请求中 model_id 不存在 | 返回 400 错误，提示模型不存在 |
| 删除模型时该模型被节点引用 | 允许删除，FK SET NULL 自动解绑 |
| 前端请求 `chat_visible=true` 返回空列表 | 前端展示提示"请先添加对话模型" |

## Testing Strategy

### 后端测试

1. **LLMConfig API 测试**
   - 创建模型时 `chat_visible` 默认为 true
   - 列表过滤 `chat_visible=true` 只返回可见模型
   - 更新 `chat_visible` 字段生效

2. **AgentNodeConfig API 测试**
   - GET 空配置返回所有字段为 null
   - PUT 更新单个节点成功
   - PUT 引用不存在的 model_id 返回 400
   - 删除被引用的模型后 GET 返回对应节点为 null

3. **Chat API 集成测试**
   - 配置节点模型后，Agent 模式使用对应模型（mock 验证）
   - 节点模型连接失败时 fallback 到对话模型
   - 未配置节点时使用对话模型

### 前端测试

1. Models 页面表单正确提交 `chat_visible` 字段
2. 节点配置区域加载和保存正常
3. Chat 页面模型列表仅展示 `chat_visible=true` 的模型

## 文件变更清单

| 文件 | 变更 |
|------|------|
| `backend/app/schema/db.py` | LLMConfig 加 `chat_visible` 字段；新增 `AgentNodeConfig` 模型 |
| `backend/app/api/llm_config.py` | Create/Update/Response 加 `chat_visible`；列表支持过滤 |
| `backend/app/api/agent_node_config.py` | 新增 GET/PUT 接口 |
| `backend/app/main.py` | 注册新路由 |
| `backend/app/api/chat.py` | Agent 模式下为各节点加载独立 LLM |
| `frontend/src/lib/api.ts` | 新增 agent node config API 调用 |
| `frontend/src/pages/Models.tsx` | 模型表单加 `chat_visible`；新增节点配置区域 |
| `frontend/src/pages/Chat.tsx` | 模型列表请求加 `chat_visible=true` 过滤 |
