# Aladdin 技术架构详解

本文档详细描述系统的核心流程、切片策略、Agent 编排机制、环境变量配置等技术细节。

---

## 核心流程

### 文档处理管道

```
上传文件 → Loader 解析（PDF/DOCX/XLSX/PPTX/TXT/MD）
         → 同时提取文本和嵌入图片（写入临时目录，内容 hash 去重）
         → 文本为空时自动触发整文件 OCR（支持多 Provider + Fallback）
         → 文本非空但有嵌入图片时，并发 OCR 识别图片内容
         → 图片 OCR 文本按页位置插入到对应页面文本之后
         → Chunker 结构感知切分（父块 1500 字 / 子块 300 字，表格整块保护）
         → Embedder 生成稠密向量(1024维) + 稀疏向量
         → 写入 Milvus（向量）+ PostgreSQL（元数据）
         → 清理图片临时目录
```

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
- 子块切分同样感知结构标记，确保每个子块是一个完整的逻辑单元
- HTML 表格（`<table>...</table>`）整块保护，不会被切断到两个 chunk 中
- 识别 VL 模型特有标记（`[Non-Text]`、`[Image]` 等）作为分段点

### 为什么这样设计

传统 RAG 按固定字符数切分，容易把一个完整的逻辑段落（如"关于误工费的反驳"）切成两半，导致 embedding 向量表示的是混合语义，检索精度下降。结构感知切分保证每个子块是一个独立的语义单元，embedding 精确表示该主题，检索命中率更高。

### 不会丢失召回

- 检索命中子块后，通过父子映射返回完整的父块内容，LLM 获得充分上下文
- 跨段落的复杂问题由 Agent 模式处理——查询改写生成多个子查询，迭代检索命中多个子块，合并返回多个父块
- 结构切分 + 父块扩展 + Agent 迭代三者配合，召回率和精度同时提升

---

## 混合内容文档处理（图文混排）

对于包含文字和图片的混合文档（如带图表的 PDF、含截图的 Word、有图片的 PPT），系统采用以下策略确保信息不丢失：

### 处理流程

```
Loader 提取文本 + 提取嵌入图片（写入临时目录）
  │
  ├─ 文本为空（纯扫描件）→ 整文件 OCR
  │
  └─ 文本非空 + 有嵌入图片 → 并发 OCR 识别图片
       │
       └─ 按页位置将图片 OCR 文本插入到对应页面文本之后
```

### 生产级优化

| 优化项 | 实现方式 | 效果 |
|--------|----------|------|
| 内存控制 | 图片写入临时目录，不在内存中持有 bytes | 大文档不会 OOM |
| 并发 OCR | `asyncio.Semaphore(4)` 控制并行度 | 30 张图片处理时间降低 75% |
| 图片去重 | MD5 hash 去重，相同内容只 OCR 一次 | 水印/logo 不重复处理 |
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

## 检索模式详解

### 三档检索模式

| 模式 | 流程 | 适用场景 |
|------|------|----------|
| direct | 稠密向量 ANN 检索 | 简单查询、低延迟 |
| hybrid | 稠密+稀疏并行 → RRF 融合 → Rerank 精排 → 父块扩展 | 通用场景 |
| agent | 路由判定 → 查询改写 → 迭代检索+反思（最多3轮） | 复杂多跳查询 |

### 相比传统 RAG 的核心差异

| 能力 | 传统 RAG | 本系统 |
|------|---------|--------|
| 切片方式 | 固定字符数切分 | 结构感知切分，保持逻辑完整性 |
| 检索方式 | 单次向量检索 | 稠密+稀疏混合检索 + RRF 融合 + Rerank 精排 |
| 查询理解 | 原始 query 直接检索 | LLM 路由判定 + 查询改写（多角度检索） |
| 迭代能力 | 无 | 检索→反思→补充检索，最多 3 轮迭代 |
| 上下文返回 | 返回命中的小块 | 子块命中后扩展为父块，上下文完整 |
| 容错能力 | 无 | 多级降级（Agent异常→hybrid→纯检索） |
| 性能优化 | 无 | 查询去重、分数快判减少 60% LLM 调用、批量 Rerank 消除锁争用 |

---

## Agent 编排流程（agent 模式）

```
用户查询
  │
  ├─ Router + Rewriter 并行执行
  │    ├─ Router 判定 simple → 取消改写，直接走 hybrid 快路径
  │    └─ Router 判定 complex → 等待改写结果 ↓
  │
  ├─ Executor 并行检索
  │    ├─ 查询级去重（embedding cosine similarity > 0.92 跳过）
  │    ├─ 子查询跳过 rerank（纯向量+RRF，完全并行无锁）
  │    └─ 合并去重后统一 rerank + 父块扩展（只调一次，消除锁争用）
  │
  ├─ Reflector 两级评估
  │    ├─ 快速判定（无 LLM）：top-3 均分 ≥ 0.7 → 充分 / top-5 均分 < 0.3 → 不充分
  │    ├─ LLM 深度评估：分数中间地带，多维度评分（相关性/覆盖度/一致性）
  │    ├─ 充分 → 返回结果
  │    ├─ 覆盖度增幅 < 10% → 提前终止（继续迭代无意义）
  │    └─ 不充分 → 生成追加查询，回到 Executor（最多 3 轮）
  │
  └─ 异常 → 降级到 hybrid 快路径
```

### Agent 节点模型配置

可在前端"模型管理"页面为 Agent 各节点配置独立 LLM：

| 节点 | 推荐模型 | 作用 |
|------|---------|------|
| Router | 轻量模型 | 判断 simple/complex |
| Rewriter | 轻量模型 | 查询改写 |
| Reflector | 轻量模型 | 评估检索质量 |
| 最终回答 | 强模型（对话选择的模型） | 生成回答 |

不配置时，所有节点使用对话选择的模型。

### 容错降级机制

- **Agent 异常降级**：编排过程中任何异常自动回退到 hybrid 检索
- **LLM 不可用降级**：流式生成失败时直接返回检索到的原文
- **Reranker 异常降级**：跳过重排序，返回 RRF 融合结果
- 响应中 `metadata.degraded` 字段标识是否发生降级，`metadata.llm_degraded` 标识 LLM 是否降级

---

## OCR 服务管理

系统支持可配置的 OCR 服务，用于处理扫描件 PDF 等无文本层的文档。

### 支持的 OCR Provider

| Provider 类型 | 说明 | 配置要点 |
|--------------|------|----------|
| `paddleocr` | PaddleOCR 本地服务 | 需安装 PaddleOCR 依赖，通过 `extra_config` 配置 `lang` 和 `use_gpu` |
| `textin` | 合合信息 TextIn OCR | 响应格式 `{code, message, data: [{page, content}]}`，填写 API 地址和密钥 |
| `external_api` | 通用外部 API（兼容模式） | 自动识别常见响应格式，适合快速接入未专门适配的服务 |

### 架构设计

```
OCRProvider (抽象基类)
├── PaddleOCRProvider          # 本地 PaddleOCR
├── BaseExternalAPIProvider    # 外部 HTTP API 抽象基类（通用上传逻辑）
│   ├── TextInProvider         # TextIn OCR 适配
│   └── ExternalAPIProvider    # 通用兼容（自动识别响应格式）
└── 新增 Provider...           # 继承 BaseExternalAPIProvider 即可
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
| OpenAI 兼容（TEI/Infinity/vLLM） | 填到 `/v1`，系统自动拼接 `/embeddings` 或 `/rerank` | `http://server:8080/v1` |
| 自定义接口 | 填完整端点路径 | `http://server:8001/ranking_score` |

### 配置方式

- **环境变量**：`EMBED_PROVIDER=remote` + `EMBED_BASE_URL` + `EMBED_MODEL` + `EMBED_API_KEY`
- **前端页面**：启动后在 **Embedding 配置** 页面动态添加/切换，立即生效无需重启
- 数据库中 `is_active=True` 的配置优先级高于环境变量

---

## 环境变量完整列表

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_PROVIDER` | ollama | LLM 提供者（ollama / vllm） |
| `LLM_BASE_URL` | http://localhost:11434 | LLM 服务地址 |
| `LLM_MODEL` | qwen2.5:7b | LLM 模型名称 |
| `LLM_API_KEY` | - | API 密钥 |
| `EMBED_PROVIDER` | remote | Embedding 后端（remote 推荐） |
| `EMBED_BASE_URL` | - | Embedding 服务地址 |
| `EMBED_MODEL` | BAAI/bge-m3 | Embedding 模型名称 |
| `EMBED_API_KEY` | - | Embedding 服务密钥 |
| `RERANK_PROVIDER` | remote | Rerank 后端（remote 推荐） |
| `RERANK_BASE_URL` | - | Rerank 服务地址 |
| `RERANK_MODEL` | BAAI/bge-reranker-v2-m3 | Rerank 模型 |
| `RERANK_API_KEY` | - | Rerank 服务密钥 |
| `DATABASE_URL` | postgresql+asyncpg://...localhost:5432/aladdin | PostgreSQL 连接地址 |
| `MILVUS_HOST` | localhost | Milvus 地址 |
| `MILVUS_PORT` | 19530 | Milvus 端口 |
| `REDIS_URL` | redis://localhost:6379/0 | Redis 连接地址（任务队列 + 缓存） |
| `AGENT_MAX_ITERATIONS` | 3 | Agent 最大迭代次数 |
| `AGENT_TIMEOUT` | 30.0 | Agent 超时时间（秒） |
| `PARENT_CHUNK_SIZE` | 1500 | 父块大小（字符） |
| `CHILD_CHUNK_SIZE` | 300 | 子块大小（字符） |
| `CHUNK_OVERLAP` | 50 | 子块重叠（字符） |
| `PIPELINE_MAX_CONCURRENT` | 3 | Worker 最大并发文档处理数 |
| `PIPELINE_MAX_RETRIES` | 3 | 文档处理最大重试次数 |
| `PIPELINE_TASK_TIMEOUT_MINUTES` | 30 | 单文档处理超时（分钟） |

---

## 支持的完整能力列表

- **多格式文档**：PDF、Word、Excel、PPT、TXT、Markdown
- **混合内容处理**：自动提取 PDF/Word/PPT 中嵌入的图片，并发 OCR 识别后按页位置插入文本
- **图片智能处理**：内容 hash 去重、装饰性小图过滤、单文档最多 50 张图片上限保护
- **混合检索**：稠密语义检索 + 稀疏关键词检索，RRF 融合
- **智能路由**：自动判断查询复杂度，简单问题走快路径（路由与改写并行，零等待）
- **查询改写**：多策略扩展（关键词提取、假设文档生成 HyDE、视角转换），生成 2-4 个检索查询
- **查询去重**：基于 embedding 余弦相似度跨迭代去重，避免重复检索
- **迭代反思**：两级评估（分数快判 + LLM 深度评估），覆盖度增幅不足时提前终止
- **结构性碎片惩罚**：Rerank 阶段对标题/目录等无实质信息的短文本施加分数惩罚
- **多模型管理**：数据库持久化多个 LLM 配置，支持创建/编辑/删除/设为默认/连通性测试，对话时动态切换
- **Embedding/Rerank 可配置**：支持本地模型和远程服务两种模式，前端页面动态切换，无需重启
- **OCR 服务管理**：可视化管理多个 OCR 服务，支持默认+Fallback 自动切换
- **Markdown 切片优化**：VL 模型返回的 Markdown 内容智能切分，表格整块保护不切断
- **上下文窗口管理**：可配置每个模型的最大上下文 token 数，按 chunk 相关性智能截断
- **流式响应**：SSE 流式输出，兼容 OpenAI API 格式，Agent 模式实时推送思考进度事件
- **引用溯源**：回答附带引用来源（文件名、子块内容、父块上下文、相关性分数）
- **API Key 认证**：SHA256 哈希存储，支持创建/撤销/调用统计，仅 `/v1/` 路径需认证
- **检索测试**：独立的检索测试页面，对比不同模式效果

---

## 未来扩展方向

- **语义切分**：基于 embedding 相似度变化点切分，进一步提升 chunk 质量
- **LLM Rerank**：用大模型做精排，替代小模型 Reranker
- **Chunk 富化**：启用 Enricher，为每个 chunk 生成摘要和关键词
- **对话记忆**：多轮对话上下文管理，支持指代消解
- **知识图谱**：从文档中抽取实体关系，支持图谱增强检索
- **评估体系**：集成 RAGAS 等评估框架，量化检索和生成质量
- **分布式部署**：支持多 Worker 水平扩展，文档处理队列化
- **权限管理**：知识库级别的访问控制
- **增量更新**：文档修改后仅重新处理变更部分
```
