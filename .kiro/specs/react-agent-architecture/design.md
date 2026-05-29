# ReAct Agent 架构重构设计文档

## Overview

将 Artoo 的 Agent 层从固定管道（Planner→Executor→Reflector）重构为真正的 ReAct（Reasoning + Acting）循环架构，参考 WeKnora v0.6 的设计。LLM 作为 Agent 自主决策调用什么工具、搜几次、从什么角度搜，而不是由代码预设检索策略。

核心变更：
- 删除 orchestrator/planner/reflector/rewriter/router/executor 六个模块
- 新建 AgentEngine（ReAct 循环）+ ToolRegistry（工具注册表）+ EventBus（事件系统）
- 保留 retrieval/ 层不动，被 knowledge_search tool 内部调用
- LLM Provider 扩展 Function Calling 接口
- 前端适配新的 SSE 事件格式（thought/tool_call/tool_result/final_answer）

## Architecture

### 现有架构（删除）

```
Router → Planner → Executor → Reflector → 循环
         ↓                      ↓
    意图拆分+查询生成      LLM评估是否充分
```

问题：
- 检索策略由代码预设，LLM 只在 Planner 和 Reflector 被调用
- Reflector 是独立的 LLM 调用，增加延迟且评估标准模糊
- 无法动态决定"搜几次"、"换什么角度搜"

### 新架构（ReAct 循环）

```
┌─────────────────────────────────────────────────────────┐
│  Chat API (/v1/chat/completions)                        │
│  ├── direct 模式 → VectorRetriever → LLM 生成          │
│  ├── hybrid 模式 → HybridRetriever → LLM 生成          │
│  └── agent 模式 → AgentEngine (ReAct Loop)              │
│                     ├── EventBus (SSE 推送)              │
│                     ├── ToolRegistry                     │
│                     │   ├── knowledge_search             │
│                     │   ├── grep_chunks                  │
│                     │   ├── list_knowledge_chunks        │
│                     │   ├── thinking                     │
│                     │   ├── final_answer                 │
│                     │   └── [MCP tools / Skills]         │
│                     └── LLM (Function Calling)           │
└─────────────────────────────────────────────────────────┘
```

ReAct 循环伪代码：

```python
while iteration < max_iterations:
    response = LLM(messages, tools)     # LLM 自主决策
    
    if is_done(response):               # 自然停止 or final_answer
        break
    
    for tool_call in response.tool_calls:
        result = tool_registry.execute(tool_call)
        messages.append(tool_result)
    
    iteration++
```

### 目录结构（重构后）

```
backend/app/agent/
├── __init__.py
├── engine.py              # AgentEngine 核心引擎（ReAct 循环）
├── config.py              # AgentConfig 配置
├── events.py              # EventBus 事件系统
├── state.py               # AgentState / AgentStep 数据结构
├── tools/
│   ├── __init__.py
│   ├── base.py            # BaseTool 接口 + ToolResult
│   ├── registry.py        # ToolRegistry 注册表
│   ├── knowledge_search.py
│   ├── grep_chunks.py
│   ├── list_chunks.py
│   ├── final_answer.py
│   ├── thinking.py
│   └── web_search.py
├── prompts/
│   ├── __init__.py
│   ├── progressive_rag.py
│   └── templates.py
├── skills/
│   ├── __init__.py
│   ├── manager.py
│   └── loader.py
└── memory/
    ├── __init__.py
    └── context_manager.py
```

## Components and Interfaces

### 1. AgentEngine（核心引擎）

```python
class AgentEngine:
    """ReAct Agent 引擎 - 无状态，每轮对话由调用方传入历史"""
    
    def __init__(self, config: AgentConfig, llm: LLMProvider,
                 tool_registry: ToolRegistry, event_bus: EventBus): ...
    
    async def execute(self, session_id: str, query: str,
                      llm_context: list[Message], image_urls: list[str] | None = None) -> AgentState: ...
    
    async def _execute_loop(self, state, query, messages, tools, session_id) -> AgentState: ...
    async def _call_llm_with_retry(self, messages, tools, iteration, session_id) -> ChatResponse: ...
    def _analyze_response(self, response, iteration, session_id) -> ResponseVerdict: ...
    async def _execute_tool_calls(self, tool_calls, step, session_id): ...
    def _append_tool_results(self, messages, step) -> list[Message]: ...
    async def _synthesize_final_answer(self, query, state, session_id): ...
```

### 2. BaseTool 接口

```python
class BaseTool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...
    
    @property
    @abstractmethod
    def description(self) -> str: ...
    
    @property
    @abstractmethod
    def parameters(self) -> dict: ...  # JSON Schema
    
    @abstractmethod
    async def execute(self, args: dict) -> ToolResult: ...
```

### 3. ToolRegistry

```python
class ToolRegistry:
    def register(self, tool: BaseTool): ...
    def get_function_definitions(self) -> list[dict]: ...  # OpenAI format
    async def execute(self, name: str, args: dict) -> ToolResult: ...
    def list_tools(self) -> list[str]: ...
```

### 4. EventBus

```python
class EventBus:
    def on(self, event_type: EventType, handler: EventHandler): ...
    async def emit(self, event: AgentEvent): ...
```

### 5. LLMProvider 扩展（Function Calling）

```python
class LLMProvider(ABC):
    # 现有接口保留
    async def generate(self, messages, **kwargs) -> str: ...
    async def stream(self, messages, **kwargs) -> AsyncIterator[str]: ...
    
    # 新增 Function Calling 接口
    async def chat_with_tools(self, messages, tools, **kwargs) -> ChatResponse: ...
    async def stream_with_tools(self, messages, tools, **kwargs) -> AsyncIterator[StreamChunk]: ...
```

### 6. 内置 Tools

| Tool | 职责 | 内部调用 |
|------|------|---------|
| `knowledge_search` | 语义检索（1-5 queries） | HybridRetriever.search() |
| `grep_chunks` | BM25 关键词精确匹配 | BM25Retriever.search() |
| `list_knowledge_chunks` | 按 doc_id 读取完整 chunk | DB 查询 Chunk 表 |
| `thinking` | 自我反思/规划（不展示给用户） | 无外部调用 |
| `final_answer` | 提交最终答案 | 无外部调用 |
| `web_search` | 网页搜索（可选） | SearXNG API |

### 8. knowledge_search 内部检索流程

参考 WeKnora `knowledgebase_search.go` + `knowledge_search.go` 确认的流程：

```
knowledge_search(queries: ["q1", "q2", ...])
│
├── 1. 并发检索（按 embedding model 分组，共享 query embedding）
│   ├── Dense 向量检索（必选，threshold 过滤）
│   ├── BM25 关键词检索（必选，threshold 过滤）
│   └── Sparse 稀疏检索（可选，远程服务支持时启用）
│
├── 2. RRF 融合（Reciprocal Rank Fusion）
│   ├── WeKnora 实现：vectorWeight/(k+vectorRank) + keywordWeight/(k+keywordRank)
│   ├── 支持可配置权重（默认 vector=0.7, keyword=0.3, k=60）
│   ├── 仅 vector 结果时跳过 RRF，保留原始 embedding 分数
│   └── 仅 keyword 结果时跳过 RRF，保留原始 BM25 分数
│
├── 3. 去重（多键去重）
│   ├── chunk_id 去重（保留最高分）
│   ├── parent_chunk_id 去重
│   └── content signature 去重（Jaccard 近似）
│
├── 4. Rerank（BGE-Reranker via TEI）
│   ├── 优先用 rerank model
│   ├── fallback: LLM prompt scoring（批量 15 条/批）
│   └── fallback: 保留原始分数
│
├── 5. Composite Score（复合评分）
│   └── final = 0.6 * rerank_score + 0.3 * base_score + 0.1 * source_weight
│
├── 6. MMR 多样性选择（Maximal Marginal Relevance）
│   ├── lambda=0.7（平衡相关性和多样性）
│   ├── Jaccard 相似度计算冗余度
│   └── 迭代选择直到 top_k
│
├── 7. seen_chunks 跨调用去重
│   └── 同一 Agent 执行中已返回的 chunk 用简短标记代替
│
└── 8. XML 格式化输出
    ├── <search_results count="N">
    ├── <chunk rank="1" chunk_id="..." score="0.85" ...>
    │   ├── <match_snippet>...</match_snippet>
    │   └── <content>...</content>
    └── <retrieval_statistics>（覆盖率统计）
```

**关于 Sparse 的处理**：Sparse 路是可插拔的。当远程 embedding 服务支持 `/embed_sparse` 端点时自动启用，否则退化为 Dense + BM25 两路。RRF 融合自动适应 2 路或 3 路输入。

**关于 Graph 的处理**：图谱检索作为独立 tool（`query_knowledge_graph`）存在，不在 `knowledge_search` 内部。Phase 1 先占位不实现。

### 7. SSE 事件协议

```typescript
{ type: "thought", content: string, iteration: number, done: boolean }
{ type: "tool_call", tool_name: string, tool_call_id: string, hint: string, iteration: number }
{ type: "tool_result", tool_call_id: string, tool_name: string, success: boolean, duration_ms: number }
{ type: "final_answer", content: string, done: boolean }
{ type: "references", references: ReferenceItem[] }
{ type: "complete", total_steps: number, total_duration_ms: number }
```

## Data Models

### AgentConfig

```python
@dataclass
class AgentConfig:
    max_iterations: int = 20
    allowed_tools: list[str] = field(default_factory=lambda: [
        "knowledge_search", "grep_chunks", "list_knowledge_chunks", "final_answer"
    ])
    temperature: float = 0.7
    knowledge_base_ids: list[str] = field(default_factory=list)
    web_search_enabled: bool = False
    thinking_enabled: bool = True
    parallel_tool_calls: bool = False
    max_context_tokens: int = 200000
    llm_call_timeout: int = 120  # seconds
    max_tool_output_chars: int = 16000
    system_prompt: str = ""  # 空则使用默认 Progressive RAG prompt
```

### AgentState

```python
@dataclass
class AgentState:
    current_round: int = 0
    steps: list[AgentStep] = field(default_factory=list)
    is_complete: bool = False
    final_answer: str = ""
    knowledge_refs: list[SearchResult] = field(default_factory=list)

@dataclass
class AgentStep:
    iteration: int
    thought: str = ""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)

@dataclass
class ToolCallRecord:
    id: str
    name: str
    args: dict
    result: ToolResult | None = None
    duration_ms: int = 0
```

### ToolResult

```python
@dataclass
class ToolResult:
    success: bool
    output: str
    data: dict[str, Any] | None = None
    error: str = ""
```

### ChatResponse（LLM Function Calling 返回）

```python
@dataclass
class ChatResponse:
    content: str
    tool_calls: list[LLMToolCall]
    finish_reason: str  # "stop" | "tool_calls"
    usage: TokenUsage | None = None

@dataclass
class LLMToolCall:
    id: str
    function_name: str
    arguments: str  # JSON string

@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
```

### AgentEvent

```python
class EventType(str, Enum):
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FINAL_ANSWER = "final_answer"
    REFERENCES = "references"
    COMPLETE = "complete"
    ERROR = "error"

@dataclass
class AgentEvent:
    type: EventType
    session_id: str
    data: dict[str, Any]
    done: bool = False
```

## Correctness Properties

### Property 1: 终止保证
ReAct 循环必须在 `max_iterations` 内终止。达到上限时强制合成最终答案，不会无限循环。

### Property 2: final_answer 唯一性
每次执行只能产生一个 final_answer。检测到 final_answer tool call 后立即终止循环，不会重复输出。

### Property 3: 工具结果一致性
每个 tool_call 必须有对应的 tool_result 追加到消息历史，否则 LLM 上下文会不一致导致幻觉。

### Property 4: 事件顺序保证
EventBus 同步发射事件，保证前端收到的事件顺序与执行顺序一致。

### Property 5: 重复响应检测
连续 N 轮（默认 2）LLM 返回相同 content 且无 tool_calls 时，强制终止（stuck loop detection）。

### Property 6: 跨轮次数据新鲜度
多轮对话中，历史轮次的 KB 工具结果被 redact，强制每轮重新检索，避免使用过期数据。

## Error Handling

### LLM 调用失败

- **瞬态错误**（429/500/502/503/504/timeout）：最多重试 2 次，指数退避（1s, 2s）
- **永久错误**：如果已有工具结果，尝试从已有结果合成最终答案（graceful degradation）；否则返回错误
- **空响应**：LLM 返回空 content 且无 tool_calls 时，追加 nudge 消息重试（最多 2 次）

### 工具执行失败

- 单个工具超时（默认 60s）：返回 ToolResult(success=False, error="timeout")
- 工具内部异常：捕获异常，返回 ToolResult(success=False, error=str(e))
- 工具结果追加 `[Analyze the error above and try a different approach.]` 提示，引导 LLM 换策略

### 上下文溢出

- 每轮开始前估算当前 token 数
- 超过 `max_context_tokens * 0.8` 时触发压缩：
  - 保留 system prompt + 最近 2 轮完整消息
  - 中间轮次的 tool results 替换为摘要
- 工具输出超过 `max_tool_output_chars` 时截断（保留头尾）

### 前端断连

- SSE 连接断开时，Agent 继续执行完成
- 结果通过 Redis Stream 持久化，前端重连后可增量拉取

## Testing Strategy

### 单元测试

- **ToolRegistry**: 注册/查找/执行/first-wins 策略
- **BaseTool 实现**: 每个内置 tool 的参数校验和执行逻辑
- **AgentEngine._analyze_response()**: 各种停止条件（natural stop / final_answer / content_filter / stuck loop）
- **EventBus**: 事件发射和订阅
- **Context Manager**: token 估算和消息压缩

### 集成测试

- **ReAct 循环端到端**: Mock LLM 返回预设的 tool_calls 序列，验证完整循环
- **knowledge_search tool**: Mock HybridRetriever，验证检索→格式化→去重流程
- **Chat API**: 验证 agent 模式的 SSE 事件流格式
- **Function Calling**: 验证 Ollama/vLLM 的 tool_calls 解析

### 压力测试

- 最大迭代次数边界（max_iterations 耗尽时的 graceful degradation）
- 大量工具结果的上下文窗口管理
- 并发 Agent 执行的资源隔离

---

## 关键设计决策

### 1. 为什么删除 Reflector？

WeKnora 没有独立的 Reflector 模块。反思能力通过两种方式实现：
- **System Prompt 引导**：prompt 中明确要求"每次检索后必须反思信息是否充分"
- **thinking tool**：LLM 可以调用 thinking tool 进行显式反思

优势：减少一次 LLM 调用延迟，LLM 自己判断"够不够"比外部评估更准确。

### 2. 为什么用 final_answer tool？

WeKnora 同时支持自然停止和 final_answer tool。final_answer 的好处：
- 强制 LLM 在提交答案前"想清楚"
- 答案内容在 tool arguments 中，解析更可靠
- 减少 LLM 提前停止的概率

### 3. direct/hybrid/agent 三档共存

- `direct`：纯向量检索，最快
- `hybrid`：三路混合检索 + Rerank，不经过 Agent
- `agent`：完整 ReAct 循环，LLM 自主决策

保留前两档是因为简单查询不需要 Agent 开销，且兼容纯检索 API 调用方。

### 4. 与现有代码的映射

| 现有模块 | 新模块 | 变化 |
|---------|--------|------|
| `agent/orchestrator.py` | `agent/engine.py` | 完全重写为 ReAct 循环 |
| `agent/planner.py` | 删除 | LLM 通过 prompt 自主规划 |
| `agent/reflector.py` | 删除 | LLM 通过 thinking tool 自我反思 |
| `agent/rewriter.py` | 删除 | LLM 自主决定查询改写 |
| `agent/router.py` | 删除 | 不再需要路由判断 |
| `agent/executor.py` | `agent/tools/registry.py` | 工具执行逻辑移入 Registry |
| `retrieval/hybrid.py` | 保留 | 被 knowledge_search tool 内部调用 |
| `retrieval/bm25.py` | 保留 | 被 grep_chunks tool 内部调用 |
| `api/chat.py` | 重构 | 接入 AgentEngine，改 SSE 事件格式 |
| `models/provider.py` | 扩展 | 新增 Function Calling 接口 |
### 工作流程：评估-侦察-规划-执行 循环

#### 意图评估
- 纯对话（问候、感谢）→ 直接 final_answer
- 需要检索 → 进入检索流程

#### Phase 1: 初步侦察
1. 执行 grep_chunks（关键词）和 knowledge_search（语义）
2. 分析结果：是否足够回答？信息完整还是部分？

#### Phase 2: 策略决策
- Path A（直接回答）：信息充分 → final_answer
- Path B（深度研究）：信息不足 → 制定检索计划

#### Phase 3: 执行与反思循环
对每个子任务：
1. 搜索 → 2. 深度阅读 → 3. 反思（信息是否充分？缺什么？）

#### Phase 4: 最终合成
所有任务完成后，综合所有检索内容，调用 final_answer。

### 工具使用规则
- knowledge_search：语义检索，1-5 个查询
- grep_chunks：精确关键词匹配
- list_knowledge_chunks：深度阅读（搜索后必须调用）
- thinking：规划和反思
- final_answer：必须作为最后一个动作
```

### 6. LLM Provider Function Calling 支持

**文件**: `backend/app/models/provider.py`（扩展）

```python
class LLMProvider(ABC):
    @abstractmethod
    async def generate(self, messages: list[dict], **kwargs) -> str: ...
    
    @abstractmethod
    async def stream(self, messages: list[dict], **kwargs) -> AsyncIterator[str]: ...
    
    # 新增：Function Calling 支持
    @abstractmethod
    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        **kwargs,
    ) -> ChatResponse: ...
    
    @abstractmethod
    async def stream_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        **kwargs,
    ) -> AsyncIterator[StreamChunk]: ...

@dataclass
class ChatResponse:
    content: str
    tool_calls: list[ToolCall]
    finish_reason: str  # "stop" | "tool_calls"
    usage: TokenUsage | None = None

@dataclass
class ToolCall:
    id: str
    function_name: str
    arguments: str  # JSON string

@dataclass
class StreamChunk:
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    finish_reason: str = ""
    response_type: str = "content"  # "content" | "thinking" | "tool_call"
```

### 7. 对话模式设计

保留三档模式，但语义变化：

| 模式 | 行为 | 适用场景 |
|------|------|---------|
| `direct` | 纯向量检索，不经过 Agent | 最快，简单查询 |
| `hybrid` | 三路混合检索 + Rerank，不经过 Agent | 中等复杂度 |
| `agent` | 完整 ReAct 循环，LLM 自主决策 | 复杂查询 |

`agent` 模式下，LLM 内部会自动调用 `knowledge_search`（等价于 hybrid 检索），但可以多次调用、换角度搜索、深度阅读。

### 8. Skill 扩展机制（Phase 2）

**文件**: `backend/app/agent/skills/`

```python
@dataclass
class SkillMetadata:
    name: str
    description: str
    base_path: str

class Skill:
    name: str
    description: str
    instructions: str  # SKILL.md 的 body
    base_path: str

class SkillManager:
    """技能管理器 - Progressive Disclosure 三级加载"""
    
    def __init__(self, skill_dirs: list[str], allowed_skills: list[str] | None = None):
        self.skill_dirs = skill_dirs
        self.allowed_skills = allowed_skills
        self._metadata_cache: list[SkillMetadata] = []
    
    def get_all_metadata(self) -> list[SkillMetadata]:
        """Level 1: 返回所有技能的名称+描述（注入 system prompt）"""
        ...
    
    def load_skill(self, name: str) -> Skill:
        """Level 2: 按需加载完整技能指令"""
        ...
```

### 9. MCP Server 对外暴露（Phase 3）

**文件**: `backend/app/mcp_server.py`

将 Artoo 的知识库能力暴露为 MCP 协议，让外部 AI 工具（Claude、Cursor 等）可以直接调用：

```python
# MCP Tools 暴露：
- knowledge_search(queries, kb_id) → 语义检索
- hybrid_search(query, kb_id) → 混合检索
- list_documents(kb_id) → 列出文档
- chat(session_id, query) → 完整对话
```

---

## 目录结构（重构后）

```
backend/app/agent/
├── __init__.py
├── engine.py              # AgentEngine 核心引擎（ReAct 循环）
├── config.py              # AgentConfig 配置
├── events.py              # EventBus 事件系统
├── state.py               # AgentState / AgentStep 数据结构
├── tools/
│   ├── __init__.py
│   ├── base.py            # BaseTool 接口 + ToolResult
│   ├── registry.py        # ToolRegistry 注册表
│   ├── knowledge_search.py  # 语义检索工具
│   ├── grep_chunks.py     # 关键词匹配工具
│   ├── list_chunks.py     # 深度阅读工具
│   ├── final_answer.py    # 最终答案工具
│   ├── thinking.py        # 思考工具
│   └── web_search.py      # 网页搜索工具（可选）
├── prompts/
│   ├── __init__.py
│   ├── progressive_rag.py # Progressive RAG 系统提示词
│   ├── pure_agent.py      # 纯 Agent 提示词（无知识库）
│   └── templates.py       # 提示词模板管理
├── skills/
│   ├── __init__.py
│   ├── manager.py         # SkillManager
│   ├── loader.py          # 技能文件加载
│   └── preloaded/         # 预装技能目录
│       └── document-analyzer/
│           └── SKILL.md
└── memory/
    ├── __init__.py
    └── context_manager.py # 上下文窗口管理（压缩/合并）
```

**删除的文件**:
- `agent/orchestrator.py` → 被 `engine.py` 替代
- `agent/planner.py` → 不再需要（LLM 自主规划）
- `agent/reflector.py` → 不再需要（LLM 通过 thinking tool 自我反思）
- `agent/rewriter.py` → 不再需要（LLM 自主决定查询改写）
- `agent/router.py` → 不再需要（LLM 自主决定检索策略）
- `agent/executor.py` → 被 `tools/registry.py` 替代

**保留的文件**:
- `retrieval/` 目录全部保留（hybrid.py, vector.py, sparse.py, bm25.py, base.py）
- `models/` 目录保留并扩展（加 Function Calling 支持）
- `pipeline/` 目录不变

---

## 前端事件流协议

### SSE 事件格式

```typescript
// Agent 思考过程（流式）
{ type: "thought", content: "让我先搜索...", iteration: 0, done: false }

// 工具调用通知
{ type: "tool_call", tool_name: "knowledge_search", 
  tool_call_id: "tc_001", hint: "搜索知识库(\"合同违约\")", iteration: 0 }

// 工具执行结果
{ type: "tool_result", tool_call_id: "tc_001", tool_name: "knowledge_search",
  success: true, duration_ms: 350, iteration: 0 }

// 最终答案（流式）
{ type: "final_answer", content: "根据检索结果...", done: false }
{ type: "final_answer", content: "", done: true }

// 引用来源
{ type: "references", references: [...] }

// 执行完成
{ type: "complete", total_steps: 3, total_duration_ms: 5200 }
```

---

## 分阶段实施计划

### Phase 1: 核心 ReAct 引擎（1-2 周）

1. 实现 `BaseTool` 接口和 `ToolRegistry`
2. 实现 `AgentEngine` ReAct 循环
3. 实现 `EventBus` 事件系统
4. 扩展 `LLMProvider` 支持 Function Calling（Ollama + vLLM）
5. 实现核心 Tools: `knowledge_search`, `grep_chunks`, `final_answer`
6. 实现 Progressive RAG system prompt
7. 改造 `chat.py` API 接入新引擎
8. 前端适配新的 SSE 事件格式

### Phase 2: 深度能力（1 周）

1. 实现 `list_knowledge_chunks` 深度阅读工具
2. 实现 `thinking` 思考工具
3. 实现上下文窗口管理（token 估算 + 压缩）
4. 实现 `web_search` 工具（可选）
5. 多知识库联合检索支持
6. Agent 配置管理（前端 UI）

### Phase 3: Skill 扩展 + MCP（1-2 周）

1. 实现 Skill 系统（Progressive Disclosure 三级加载）
2. 预装技能：文档分析器、数据分析师
3. 实现 MCP Server（对外暴露知识库能力）
4. 实现 MCP Client（集成外部 MCP 工具）

### Phase 4: 高级特性（持续迭代）

1. 并行工具调用（多个 tool_call 同时执行）
2. 人工审批机制（危险操作前确认）
3. Langfuse 可观测性集成
4. Agent 类型预设（快速问答 / 智能推理 / 数据分析）
5. 多轮对话 Query Rewrite（指代消解）

---

## 与现有代码的映射关系

| 现有模块 | 新模块 | 变化 |
|---------|--------|------|
| `agent/orchestrator.py` | `agent/engine.py` | 完全重写为 ReAct 循环 |
| `agent/planner.py` | 删除 | LLM 通过 prompt 自主规划 |
| `agent/reflector.py` | 删除 | LLM 通过 thinking tool 自我反思 |
| `agent/rewriter.py` | 删除 | LLM 自主决定查询改写 |
| `agent/router.py` | 删除 | 不再需要路由判断 |
| `agent/executor.py` | `agent/tools/registry.py` | 工具执行逻辑移入 Registry |
| `retrieval/hybrid.py` | 保留 | 被 knowledge_search tool 内部调用 |
| `retrieval/bm25.py` | 保留 | 被 grep_chunks tool 内部调用 |
| `api/chat.py` | 重构 | 接入 AgentEngine，改 SSE 事件格式 |
| `models/provider.py` | 扩展 | 新增 Function Calling 接口 |

---

## 关键设计决策

### 1. 为什么删除 Reflector？

WeKnora 没有独立的 Reflector 模块。反思能力通过两种方式实现：
- **System Prompt 引导**：prompt 中明确要求"每次检索后必须反思信息是否充分"
- **thinking tool**：LLM 可以调用 thinking tool 进行显式反思

这比独立 Reflector 更好，因为：
- 减少一次 LLM 调用（Reflector 本身就是一次完整的 LLM 调用）
- LLM 自己判断"够不够"比外部评估更准确（它知道自己需要什么信息）
- 避免了 Reflector 评估标准模糊的问题

### 2. 为什么用 final_answer tool 而不是自然停止？

WeKnora 同时支持两种停止方式：
- `finish_reason == "stop"` 且无 tool_calls → 自然停止，content 就是答案
- 调用 `final_answer` tool → 显式停止

`final_answer` 的好处：
- 强制 LLM 在提交答案前"想清楚"（调用 tool 是一个显式动作）
- 答案内容在 tool arguments 中，解析更可靠
- 可以在 prompt 中强调"必须调用 final_answer"，减少 LLM 提前停止的概率

### 3. direct/hybrid/agent 三档如何共存？

- `direct`：直接调 VectorRetriever，不经过 Agent，最快
- `hybrid`：直接调 HybridRetriever（Dense+Sparse+BM25+Rerank），不经过 Agent
- `agent`：启动 AgentEngine，LLM 自主决策（内部会调用 HybridRetriever）

前两档保留是因为：
- 简单查询不需要 Agent 的开销（Agent 至少 1 次 LLM 调用 + 1 次检索）
- 某些场景用户明确知道只需要检索，不需要 LLM 推理
- 兼容 API 调用方（只需要检索结果，不需要生成答案）

### 4. 上下文窗口管理

参考 WeKnora 的做法：
- 使用 token 估算器（BPE 或字符数近似）
- 当 token 数接近 `max_context_tokens` 时，压缩历史消息
- 压缩策略：保留 system prompt + 最近 N 轮 + 当前轮的所有 tool results
- 跨轮次的 KB 工具结果被 redact（强制每轮重新检索，避免使用过期数据）
