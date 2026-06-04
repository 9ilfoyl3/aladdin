# Artoo 技术架构详解

本文档详细描述 Artoo 的核心流程、ReAct Agent 引擎、工具层、上下文管理、切片策略、检索机制与环境变量配置等技术细节。

---

## 目录

- [整体分层](#整体分层)
- [ReAct Agent 引擎](#react-agent-引擎)
- [工具层](#工具层)
- [Progressive RAG 系统提示词](#progressive-rag-系统提示词)
- [三层递进式上下文管理](#三层递进式上下文管理)
- [Agent 技能（Skills）](#agent-技能skills)
- [MCP 集成](#mcp-集成)
- [检索模式与混合检索](#检索模式与混合检索)
- [文档处理管道](#文档处理管道)
- [切片策略](#切片策略)
- [图文混排文档处理](#图文混排文档处理)
- [OCR 服务管理](#ocr-服务管理)
- [Embedding / Rerank 服务配置](#embedding--rerank-服务配置)
- [容错降级机制](#容错降级机制)
- [环境变量完整列表](#环境变量完整列表)

---

## 整体分层

```
接入层        Chat API (OpenAI 兼容 · SSE) / Admin API / MCP Server
ReAct 引擎    Think → Analyze → Act → Observe + EventBus + 上下文管理
工具层        knowledge_search / grep_chunks / list_knowledge_chunks
              thinking / web_search / final_answer / MCP Tools
检索工具层    Dense + Sparse + BM25 → RRF → Rerank → MMR → 父块扩展
索引/存储层   Milvus（稠密+稀疏向量） / PostgreSQL（元数据+配置）
数据处理层    Loader → OCR → Chunker → Embedder → Indexer / Worker
模型服务层    LLM / Embedding / Rerank / OCR（全部外部 HTTP 调用）
```

设计原则：全流程模块化解耦，AI 推理全部外置为 HTTP 服务，后端无状态、轻量、可私有化离线部署。

---

## ReAct Agent 引擎

`AgentEngine`（`backend/app/agent/engine.py`）是系统的智能核心。引擎本身**无状态**，每次 `execute()` 创建一个新的 `AgentState`，在一个 Reasoning + Acting 循环中由 LLM 自主决策。

### 核心循环

```
execute(session_id, query, llm_context)
  │
  ├─ 构建系统提示词（Progressive RAG，注入 KB 名称、可用工具、时间）
  ├─ 追加历史上下文 → 脱敏历史 KB 检索结果（强制每轮重新检索）
  ├─ 有历史时改写 query（指代消解）
  │
  └─ while current_round < max_iterations：
       │
       ├─ ① Think：流式调用 LLM
       │     · 逐 token 发射 THOUGHT 事件
       │     · 累积 tool_calls 与 finish_reason
       │     · 瞬态错误（429/5xx/timeout）指数退避重试
       │
       ├─ ② 上下文管理（每轮调用前）
       │     · UsageTracker 估算当前 token
       │     · 超过 50% → MemoryConsolidator LLM 摘要
       │     · 超过 80% → compress_context 分组截断
       │     · 发射 TOKEN_USAGE 事件
       │
       ├─ ③ Analyze：判定是否终止
       │     · final_answer 工具 → 提交答案
       │     · 自然停止（finish_reason=stop 且无工具调用）→ 引导调用 final_answer
       │     · stuck loop（连续 3 轮相同 content 且无工具调用）→ 终止
       │     · 空响应 → nudge 重试（最多 2 次）
       │     · max_iterations → 合成最终答案
       │
       ├─ ④ Act：执行工具调用
       │     · 过滤 final_answer（已在 Analyze 处理）
       │     · 并行（parallel_tool_calls）或顺序执行
       │     · 发射 TOOL_CALL / TOOL_RESULT 事件
       │
       └─ ⑤ Observe：工具结果追加到 messages，进入下一轮
```

### 关键设计

| 机制 | 说明 |
|------|------|
| 流式思考 | LLM 输出逐 token 通过 EventBus 发射 THOUGHT / FINAL_ANSWER 事件，前端实时渲染 |
| 历史检索脱敏 | 历史轮次的 KB 工具结果替换为占位符，强制 Agent 对每个新问题重新检索（防止知识库更新后用旧结果作答） |
| 查询改写 | 有历史上下文时，先消解 query 中的指代词，提升检索准确性 |
| stuck loop 检测 | 连续 3 轮相同 content 且无工具调用时自动终止，避免无意义循环 |
| 空响应处理 | content 与 tool_calls 均为空时 nudge 引导，重试耗尽后从已有结果合成答案 |
| 瞬态错误重试 | 429 / 500 / 502 / 503 / 504 / timeout 指数退避重试（1s / 2s），最多 2 次 |
| 答案合成兜底 | max_iterations 耗尽或永久错误时，从已检索结果合成最终答案 |
| Prompt Caching | 工具定义按名称字母序稳定排序，保证 JSON 字节级稳定，最大化 LLM prompt prefix 缓存命中 |

### EventBus 事件系统

`EventBus`（`events.py`）以同步顺序发射事件，保证前端渲染顺序。事件类型：

| 事件 | 触发时机 |
|------|----------|
| `THOUGHT` | LLM 流式思考内容 / thinking 工具输出 |
| `TOOL_CALL` | 即将执行某个工具 |
| `TOOL_RESULT` | 工具执行完成（含耗时、成功标记） |
| `FINAL_ANSWER` | 最终答案流式片段 / 完成标记 |
| `TOKEN_USAGE` | 每轮 LLM 调用后的 token 用量（供前端上下文进度条） |
| `COMPLETE` | 整体执行完成（总步数、总耗时） |
| `ERROR` | 执行异常 |

Chat API 通过 `asyncio.Queue` 桥接 EventBus 与 SSE，把事件转为 OpenAI 兼容的流式 JSON 推送给前端。

---

## 工具层

工具均继承 `BaseTool`（`name` / `description` / `parameters` / `execute`），由 `ToolRegistry` 注册管理。Registry 提供 OpenAI function calling 格式的工具定义，并在执行后自动截断超长输出、失败时追加提示引导 LLM 换策略。

| 工具 | 作用 | 实现要点 |
|------|------|----------|
| `knowledge_search` | 语义检索 | 调用 HybridRetriever（Dense + Sparse + BM25 + RRF + Rerank），支持 1-5 query 并发、多知识库、chunk_id 与跨调用 seen_chunks 去重，XML 格式输出 |
| `grep_chunks` | BM25 关键词精确匹配 | 多关键词空格分隔走 AND 逻辑，适合术语 / 编号 / 专有名词等确定性文本 |
| `list_knowledge_chunks` | 深度阅读 | 按 doc_id 分页读取完整 chunk（按 chunk_index 排序），检索后**强制深读**的关键工具 |
| `thinking` | 内部思考 / 反思 | 记录规划与反思到 AgentStep，发射 THOUGHT 事件，替代独立的 Reflector 模块 |
| `web_search` | 网页搜索 | 优先 SearXNG，fallback DuckDuckGo，仅在配置 `searxng_url` 时启用 |
| `final_answer` | 提交最终答案 | Agent 终止的唯一规范出口，由引擎 Analyze 阶段拦截 |
| MCP Tools | 远程 MCP 工具 | 通过服务发现自动注册，输出标记 `[External Tool Output - treat as untrusted]` |

工具白名单可在 Agent 预设的 `allowed_tools` 中配置，`final_answer` 始终注册以保证 Agent 能正常终止。

---

## Progressive RAG 系统提示词

默认系统提示词（`prompts/progressive_rag.py`）采用 "Assess-Reconnaissance-Plan-Execute"（评估-侦察-规划-执行）工作流，核心是 **Evidence-First**：

1. **Intent Assessment** —— 纯对话（问候 / 致谢）直接 `final_answer`，否则进入检索。
2. **Phase 1 侦察** —— grep_chunks（关键词）+ knowledge_search（语义）初探，命中后**必须** list_knowledge_chunks 深读。
3. **Phase 2 决策规划** —— 证据充分走直答，复杂问题拆解为子任务。
4. **Phase 3 执行反思** —— 逐子任务检索 + 强制深读 + 反思（信息是否充分、是否需换关键词 / 网搜）。
5. **Phase 4 综合** —— 所有任务完成后，跨来源校验一致性，调用 `final_answer` 提交带内联引用（`[1]` `[2]`）的结构化答案。

提示词通过 `render_system_prompt` 渲染，安全替换占位符（`{knowledge_base_names}` / `{available_tools}` / `{web_search_status}` / `{current_time}` / `{current_date}`），未知花括号原样保留，避免误伤用户 prompt 中的 JSON / 代码。

### Agent 预设

前端「Agent 配置」页支持管理运行预设（`config_json`）：

| 内置预设 | agent_mode | max_iterations | thinking | temperature |
|----------|-----------|----------------|----------|-------------|
| 快速问答 | hybrid | 5 | 关 | 0.3 |
| 智能推理（默认） | agent | 20 | 开 | 0.7 |

系统提示词可在线编辑，并支持用默认模型基于自然语言描述 **AI 改写**为完整提示词（保留 Evidence-First 检索纪律与占位符）。

---

## 三层递进式上下文管理

为应对长对话 / 多轮检索导致的上下文膨胀，引擎在每轮 LLM 调用前依次应用三层策略（`agent/memory/`）：

| 层 | 组件 | 触发 | 作用 |
|----|------|------|------|
| ① Token 估算 | `TokenEstimator` + `UsageTracker` | 每轮 | 以 LLM API 返回的 usage 为权威值，仅对新增消息做 BPE（tiktoken cl100k_base）增量估算，避免全量重算 |
| ② LLM 摘要合并 | `MemoryConsolidator` | > 50% 窗口 | 用 LLM 把早期历史摘要为一条 `[Memory Summary]` system 消息，保留 system prompt + 摘要 + 最近历史 + 当前轮；失败降级为纯文本归档 |
| ③ 分组截断兜底 | `compress_context` | > 80% 窗口 | 从最旧消息组开始移除，直到降到阈值以下；tool_call / tool_result 配对成组不拆分 |

三层均保留 system prompt 与当前轮（tail），并保证 tool_call / tool_result 配对完整。`max_context_tokens` 默认 200000，可按模型配置。

---

## Agent 技能（Skills）

`SkillManager`（`agent/skills/`）实现 **Progressive Disclosure** 渐进式加载：

- **Level 1（元数据）** —— 扫描技能目录下的 `SKILL.md`，仅解析 frontmatter 的 `name` + `description`，轻量注入系统提示词供 LLM 感知。
- **Level 2（按需加载）** —— LLM 调用 `read_skill` 工具时，才加载该技能的完整指令正文。

`SKILL.md` 采用 YAML frontmatter + Markdown body 格式。内置 `document-analyzer` 技能演示长文档结构化分析流程。支持白名单（`allowed_skills`）控制可用技能。

---

## MCP 集成

Artoo 同时是 MCP 协议的**服务端**与**客户端**：

- **对外（MCP Server）** —— `backend/app/mcp_server.py` 暴露 `/mcp/tools/list` 与 `/mcp/tools/call`，把 `knowledge_search` / `hybrid_search` / `list_documents` / `chat` 等能力提供给 Claude、Cursor 等外部 AI 工具。
- **对内（MCP Client）** —— `MCPServiceDiscovery` 从 `mcp_servers.json` 读取远程 MCP Server 列表，自动拉取工具定义并包装为 `MCPToolWrapper` 注册到 ToolRegistry。外部工具输出统一加 untrusted 前缀，提醒 LLM 谨慎对待。

---

## 检索模式与混合检索

### 三档检索模式

| 模式 | 流程 | 适用场景 |
|------|------|----------|
| direct | 稠密向量 ANN 检索 | 简单查询、低延迟 |
| hybrid | 三路并行 → RRF → Rerank → 复合评分 → MMR → 父块扩展 | 通用场景 |
| agent | ReAct 循环，LLM 自主编排工具迭代检索 | 复杂多跳查询、需推理综合 |

### HybridRetriever 流程

```
query
  ├─ 三路并行召回（各取 128 条候选）
  │    ├─ Dense（稠密向量，语义相似度）
  │    ├─ Sparse（BGE-M3 稀疏向量，subword 级模糊匹配）
  │    └─ BM25（全文检索，精确关键词：编号 / 人名 / 案号）
  ├─ RRF 融合（k=60，table 类型默认 0.8 降权，CSV 来源不降权）
  ├─ Rerank 精排（候选池固定 50 条，结构性碎片施加 0.5 惩罚）
  ├─ 复合评分（0.6·rerank + 0.3·RRF + 0.1·位置先验，clamp 到 [0,1]）
  ├─ MMR 去冗余（Jaccard 相似度 > 0.7 视为冗余跳过）
  └─ 父块扩展（子块命中后用父块内容替换 content，子块保留到 child_content）
```

`skip_rerank=True` 时跳过 rerank / 父块扩展直接返回 RRF 结果（用于完全并行无锁的子查询场景）。BM25 检索器对旧 schema collection 自动降级为空结果，不影响兼容性。

### 相比传统 RAG 的差异

| 能力 | 传统 RAG | Artoo |
|------|---------|--------|
| 切片 | 固定字符数 | 结构感知，逻辑完整 |
| 检索 | 单次向量检索 | 三路混合 + RRF + Rerank + MMR |
| 查询理解 | 原始 query 直检 | Agent 自主拆解 + 多角度改写 |
| 迭代 | 无 | ReAct 循环，自主决定是否继续检索 |
| 深度阅读 | 仅命中片段 | 强制 list_knowledge_chunks 读完整内容 |
| 上下文返回 | 命中小块 | 子块命中扩展为父块 |
| 容错 | 无 | 多级降级（Agent → hybrid → 纯检索） |

---

## 文档处理管道

```
上传文件 → Loader 解析（PDF/DOCX/XLSX/PPTX/TXT/MD）
         → 同时提取文本和嵌入图片（写入临时目录，内容 hash 去重）
         → 文本为空时自动触发整文件 OCR（多 Provider + Fallback）
         → 文本非空但有嵌入图片时，并发 OCR 识别图片内容
         → 图片 OCR 文本按页位置插入到对应页面文本之后
         → Chunker 结构感知切分（父块 / 子块，表格整块保护）
         → Embedder 生成稠密向量 + 稀疏向量
         → 写入 Milvus（向量）+ PostgreSQL（元数据）
         → 清理图片临时目录
```

管道由独立 Worker 进程消费 Redis Stream 任务队列处理，与 API 解耦。支持并发处理、重试、熔断与单文档超时控制。

### 支持的文件格式

| 格式 | 处理方式 |
|------|---------|
| PDF | PyMuPDF 提取文本，空文本自动走 OCR |
| DOCX | python-docx |
| XLSX | openpyxl |
| PPTX | python-pptx |
| TXT/MD | 直接读取 |
| JPG/JPEG/PNG | OCR 服务识别（需配置 OCR 服务） |

---

## 切片策略

采用**结构感知的父子 chunk 切分**：

- 优先识别文档结构标记（条款编号、法律文书关键词、Markdown 标题等）按逻辑段落切分
- 无结构标记时回退到段落边界切分
- 子块用于精准检索（语义集中），父块用于上下文返回（信息完整）
- HTML 表格（`<table>...</table>`）整块保护，不会被切断到两个 chunk 中
- 识别 VL 模型特有标记（`[Non-Text]`、`[Image]` 等）作为分段点

### 为什么这样设计

传统 RAG 按固定字符数切分，容易把一个完整的逻辑段落切成两半，导致 embedding 表示混合语义、检索精度下降。结构感知切分保证每个子块是独立语义单元，embedding 精确表示该主题，命中率更高。

### 不会丢失召回

- 检索命中子块后，通过父子映射返回完整父块内容，LLM 获得充分上下文
- 跨段落的复杂问题由 Agent 模式处理——多角度检索命中多个子块、深读后合并返回
- 结构切分 + 父块扩展 + Agent 迭代深读三者配合，召回率和精度同时提升

---

## 图文混排文档处理

对于包含文字和图片的混合文档（带图表的 PDF、含截图的 Word、有图片的 PPT），系统确保信息不丢失：

```
Loader 提取文本 + 提取嵌入图片（写入临时目录）
  ├─ 文本为空（纯扫描件）→ 整文件 OCR
  └─ 文本非空 + 有嵌入图片 → 并发 OCR 识别图片
       └─ 按页位置将图片 OCR 文本插入到对应页面文本之后
```

### 生产级优化

| 优化项 | 实现方式 | 效果 |
|--------|----------|------|
| 内存控制 | 图片写入临时目录，不在内存中持有 bytes | 大文档不会 OOM |
| 并发 OCR | `asyncio.Semaphore` 控制并行度 | 多图处理大幅提速 |
| 图片去重 | MD5 hash 去重，相同内容只 OCR 一次 | 水印 / logo 不重复处理 |
| 数量上限 | 单文档最多提取 50 张图片 | 防止异常文件打爆 OCR 服务 |
| 小图过滤 | 尺寸 < 50px 或数据 < 1KB 的图片跳过 | 过滤装饰性图标 |
| 位置关联 | 图片文本插入到对应页面之后 | 检索时图片内容与上下文在同一 chunk |
| 资源清理 | `finally` 块中 `shutil.rmtree` 清理临时目录 | 无磁盘泄漏 |

### 各格式支持情况

| 文档格式 | 文本提取 | 图片提取方式 | 按页定位 |
|----------|----------|-------------|----------|
| PDF | `pymupdf` get_text() | `page.get_images()` + `extract_image()` | ✅ 精确到页 |
| Word | `python-docx` paragraphs | `doc.part.rels` image relationships | 按图片序号 |
| PPTX | `python-pptx` text_frame | `shape.image.blob`（PICTURE 类型 shape） | ✅ 精确到幻灯片 |
| 纯图片 | 无（返回空文本） | 整文件作为图片处理 | N/A |

---

## OCR 服务管理

系统支持可配置的 OCR 服务，用于处理扫描件 PDF 等无文本层的文档。OCR 统一通过远程 API 调用，不在业务进程内运行本地 OCR 引擎。

### 支持的 OCR Provider

| Provider 类型 | 说明 | 配置要点 |
|--------------|------|----------|
| `textin` | 合合信息 TextIn OCR | 响应格式 `{code, message, data: [{page, content}]}`，填写 API 地址和密钥 |
| `external_api` | 通用外部 API（兼容模式） | 自动识别常见响应格式，适合快速接入未专门适配的服务 |

### 架构设计

```
OCRProvider (抽象基类)
└── BaseExternalAPIProvider    # 外部 HTTP API 抽象基类（通用上传逻辑）
    ├── TextInProvider         # TextIn OCR 适配
    └── ExternalAPIProvider    # 通用兼容（自动识别响应格式）
    └── 新增 Provider...       # 继承 BaseExternalAPIProvider 即可
```

### 接入新的 OCR 服务

1. 在 `backend/app/pipeline/ocr/` 下新建 `xxx_provider.py`
2. 继承 `BaseExternalAPIProvider`，实现 `_adapt_response` 方法解析该服务的响应格式
3. 在 `backend/app/pipeline/ocr/manager.py` 的 `_create_provider` 工厂方法中注册新类型
4. 在 `backend/app/api/ocr_config.py` 的校验逻辑中添加新的 `provider_type`
5. 在前端 `OcrServices.tsx` 的 Select 中添加选项

### 默认服务与 Fallback

- 同一时刻最多一个默认服务、一个 Fallback 服务
- 同一配置不能同时为默认和 Fallback
- 文档处理时优先使用默认服务，失败后自动切换到 Fallback 重试一次
- 数据库中无 OCR 配置时，Pipeline 正常运行（跳过 OCR 步骤）

### API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/ocr-configs` | 获取所有配置（api_key 脱敏） |
| POST | `/api/ocr-configs` | 创建配置 |
| PUT | `/api/ocr-configs/{id}` | 更新配置（部分更新） |
| DELETE | `/api/ocr-configs/{id}` | 删除配置 |
| POST | `/api/ocr-configs/test` | 临时配置连通性测试 |
| POST | `/api/ocr-configs/{id}/test` | 已保存配置连通性测试 |

---

## Embedding / Rerank 服务配置

系统通过 HTTP API 调用外部 Embedding 和 Rerank 服务，支持任意 OpenAI 兼容接口。

### 远程服务地址填写规则

| 接口类型 | 地址填写方式 | 示例 |
|---------|-------------|------|
| OpenAI 兼容（TEI/Infinity/vLLM） | 填到 `/v1`，系统自动拼接 `/embeddings`、`/embed_sparse` 或 `/rerank` | `http://server:8080/v1` |
| 自定义接口 | 填完整端点路径 | `http://server:8001/ranking_score` |

### 配置方式

- **环境变量**：`EMBED_BASE_URL` + `EMBED_MODEL` + `EMBED_API_KEY`（启动时生效）
- **前端页面**：启动后在 **Embedding & Rerank 配置** 页面动态添加 / 切换，立即生效无需重启
- 数据库中 `is_active=True` 的配置优先级高于环境变量
- 不配置 Embedding / Rerank 地址也能启动服务，后续通过前端配置即可

---

## 容错降级机制

| 降级场景 | 行为 |
|----------|------|
| Agent 编排异常 | 自动回退到 hybrid 快路径 |
| LLM 流式生成失败 | 直接返回检索到的原文（`metadata.llm_degraded=true`） |
| Reranker 异常 | 跳过重排序，返回 RRF 融合结果 |
| LLM 永久错误 / max_iterations | 从已检索结果合成最终答案 |
| 空响应 | nudge 重试，耗尽后合成答案 |
| stuck loop | 连续 3 轮相同 content 且无工具调用时终止 |
| MCP / 网搜后端不可用 | 跳过该后端 / fallback，记录警告日志 |

响应 `metadata.degraded` 标识是否发生降级，`metadata.llm_degraded` 标识 LLM 是否降级。

---

## 环境变量完整列表

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_PROVIDER` | ollama | LLM 提供者（ollama / vllm） |
| `LLM_BASE_URL` | http://localhost:11434 | LLM 服务地址 |
| `LLM_MODEL` | qwen2.5:7b | LLM 模型名称 |
| `LLM_API_KEY` | - | API 密钥（vllm provider 使用） |
| `EMBED_BASE_URL` | - | Embedding 远程服务地址 |
| `EMBED_MODEL` | BAAI/bge-m3 | Embedding 模型名称 |
| `EMBED_API_KEY` | - | Embedding 服务密钥 |
| `EMBED_SPARSE_ENABLED` | true | 是否启用 Sparse 向量（需服务支持 `/embed_sparse`） |
| `RERANK_BASE_URL` | - | Rerank 远程服务地址 |
| `RERANK_MODEL` | BAAI/bge-reranker-v2-m3 | Rerank 模型 |
| `RERANK_API_KEY` | - | Rerank 服务密钥 |
| `DATABASE_URL` | postgresql+asyncpg://...localhost:5432/artoo | PostgreSQL 连接地址 |
| `MILVUS_HOST` | localhost | Milvus 地址 |
| `MILVUS_PORT` | 19530 | Milvus 端口 |
| `REDIS_URL` | redis://localhost:6379/0 | Redis 连接地址（任务队列 + 检索缓存） |
| `RETRIEVAL_CACHE_TTL` | 1800 | 检索缓存 TTL（秒） |
| `AGENT_MAX_ITERATIONS` | 10 | Agent 最大迭代次数 |
| `AGENT_TIMEOUT` | 30.0 | Agent 超时时间（秒） |
| `SEARXNG_URL` | http://localhost:8080 | 网页搜索 SearXNG 地址（配置后启用 web_search） |
| `PARENT_CHUNK_SIZE` | 2500 | 父块大小（字符） |
| `CHILD_CHUNK_SIZE` | 450 | 子块大小（字符） |
| `CHUNK_OVERLAP` | 70 | 子块重叠（字符） |
| `OCR_ENABLED` | true | 是否启用 OCR |
| `OCR_PROVIDER` | external_api | 默认 OCR Provider（远程 API） |
| `PIPELINE_MAX_CONCURRENT` | 2 | Worker 最大并发文档处理数 |
| `PIPELINE_MAX_RETRIES` | 3 | 文档处理最大重试次数 |
| `PIPELINE_TASK_TIMEOUT_MINUTES` | 60 | 单文档处理超时（分钟） |
| `PIPELINE_EMBED_BATCH_SIZE` | 32 | Embedding 每批文本数 |
| `PIPELINE_EMBED_CONCURRENCY` | 4 | Embedding 并发请求数 |
| `PIPELINE_EMBED_MAX_CONNECTIONS` | 20 | httpx 连接池上限（≥ MAX_CONCURRENT × EMBED_CONCURRENCY） |

> Agent 引擎层级参数（`max_context_tokens` 默认 200000、`consolidation_threshold` 默认 0.5、`parallel_tool_calls`、`max_tool_output_chars` 默认 16000 等）通过 `AgentConfig` 与 Agent 预设的 `config_json` 控制，详见 `backend/app/agent/config.py`。

---

## 未来扩展方向

- **评估体系**：集成 RAGAS 等框架，量化检索和生成质量
- **Chunk 富化**：启用 Enricher，为每个 chunk 生成摘要和关键词 + 过滤检索
- **数据库迁移**：引入 Alembic 管理 schema 变更
- **知识图谱**：从文档中抽取实体关系，支持图谱增强检索（GraphRAG）
- **数据源连接器**：飞书 / Notion 自动同步
- **分布式部署**：多 Worker 水平扩展
- **增量更新**：文档修改后仅重新处理变更部分
