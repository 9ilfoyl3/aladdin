# Artoo 部署运维手册

---

## 部署架构

```
目标服务器 (Docker Compose):
  ├─ frontend        ← nginx 静态资源 + API 反向代理
  ├─ backend         ← FastAPI API 服务
  ├─ worker          ← 文档处理 Worker（独立进程）
  ├─ PostgreSQL      ← 元数据 + 配置存储
  ├─ Milvus          ← 向量存储
  ├─ Redis           ← 任务队列 + 检索缓存
  ├─ etcd + MinIO    ← Milvus 依赖
  └─ (网络可达)
       ├─ LLM API         ← 对话生成
       ├─ Embedding API   ← 向量化服务
       └─ Rerank API      ← 精排服务
```

---

## 一、镜像打包

### macOS / Linux

```bash
# ARM64 迭代更新（仅服务镜像，~500MB）
make docker-package-arm-update

# AMD64 迭代更新
make docker-package-amd64-update

# ARM64 首次完整包（服务 + 基础设施镜像）
make docker-package-arm

# AMD64 首次完整包
make docker-package-amd64
```

### Windows (PowerShell)

```powershell
# AMD64
.\scripts\package-amd64.ps1              # 首次（含基础设施）
.\scripts\package-amd64.ps1 -SkipInfra   # 迭代更新

# ARM64
.\scripts\package-arm64.ps1              # 首次
.\scripts\package-arm64.ps1 -SkipInfra   # 迭代更新
```

### 打包产出

```
deploy-amd64/ (或 deploy-arm64/)
├── app.tar            ← 应用镜像（backend + frontend）
├── infra.tar          ← 基础设施镜像（首次才有）
├── nginx.conf         ← nginx 配置
├── docker-compose.yml ← 生产编排文件
└── .env.example       ← 环境变量模板
```

---

## 二、服务器部署

### 首次部署

```bash
# 1. 创建部署目录
mkdir -p /opt/artoo && cd /opt/artoo

# 2. 加载镜像
docker load -i infra.tar
docker load -i app.tar

# 3. 配置环境变量
cp .env.example .env
vim .env    # 必须配置 LLM / Embedding / Rerank

# 4. 启动所有服务
docker compose up -d

# 5. 验证
docker compose ps    # 所有服务应为 healthy
```

> 首次启动需等待约 30 秒，PostgreSQL 和 Milvus 初始化完成后 backend 自动创建表结构。

### 迭代更新

```bash
cd /opt/artoo

# 加载新镜像
docker load -i app.tar

# 重建应用服务（基础设施不动）
docker compose up -d --force-recreate backend worker frontend

# 清理旧镜像
docker image prune -f
```

---

## 三、环境变量配置

### 必填项

```env
# === LLM ===
LLM_PROVIDER=vllm
LLM_BASE_URL=http://your-llm-server/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-your-key

# === Embedding 远程服务 ===
# 不配置也能启动，后续通过前端页面添加
EMBED_BASE_URL=http://your-embedding-server/v1
EMBED_MODEL=BAAI/bge-m3
EMBED_API_KEY=

# === Rerank 远程服务 ===
# 不配置也能启动，后续通过前端页面添加
RERANK_BASE_URL=http://your-rerank-server/v1
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_API_KEY=
```

### 可选项

```env
# PostgreSQL 密码（默认 postgres）
POSTGRES_PASSWORD=postgres

# 前端端口（默认 8888）
FRONTEND_PORT=8888

# Worker 并发配置
PIPELINE_MAX_CONCURRENT=3
PIPELINE_MAX_RETRIES=3
PIPELINE_TASK_TIMEOUT_MINUTES=30
```

### 远程服务地址规则

| 接口类型 | 地址填写方式 | 示例 |
|---------|-------------|------|
| OpenAI 兼容（TEI/Infinity/vLLM） | 填到 `/v1`，系统自动拼接 `/embeddings` 或 `/rerank` | `http://server:8080/v1` |
| 自定义接口 | 填完整端点路径 | `http://server:8001/ranking_score` |

> `.env` 中的 Embedding/Rerank 配置是初始默认值，不配置也能启动。启动后可在前端页面动态管理 LLM / Embedding / Rerank 配置，数据库配置优先级更高。

---

## 四、访问地址

| 服务 | 地址 |
|------|------|
| 前端界面 | `http://服务器IP:8888` |
| API 文档 | `http://服务器IP:8000/docs` |

---

## 五、运维命令

```bash
# 查看状态
docker compose ps

# 查看日志
docker compose logs backend -f          # API 日志
docker compose logs worker -f           # Worker 日志（文档处理）
docker compose logs frontend -f         # nginx 日志

# 重启服务
docker compose restart backend          # 重启 API
docker compose restart worker           # 重启 Worker

# 停止 / 启动
docker compose down                     # 停止所有服务
docker compose up -d                    # 启动所有服务

# 清理（慎用）
docker compose down -v                  # 停止并删除所有数据卷
docker image prune -f                   # 清理无用镜像
```

### Worker 说明

- Worker 独立于 API 运行，负责文档解析、Embedding、索引写入
- Worker 卡死不影响 API 响应，可独立重启
- Worker 支持熔断机制：连续失败 N 次暂停消费，自动恢复
- 启动时自动重置被中断的任务（processing → failed）

---

## 六、本地开发

### 环境要求

- Python 3.12（⚠️ 3.13+ 不兼容）
- Node.js 18+
- Docker Desktop

### macOS / Linux

```bash
# 1. 启动基础设施
make infra

# 2. 安装依赖（首次）
make install

# 3. 配置
cp backend/.env.example backend/.env
# 编辑 .env 配置 LLM / Embedding / Rerank

# 4. 启动开发服务（三合一）
make dev

# 或分别启动：
make dev-backend    # API (http://localhost:8000)
make dev-worker     # Worker
make dev-frontend   # 前端 (http://localhost:5173)
```

### Windows (conda)

```powershell
# 1. 启动基础设施
docker compose up -d

# 2. 创建 Python 环境
conda create -n artoo python=3.12 -y
conda activate artoo
pip install -r backend/requirements-base.txt

# 3. 安装前端依赖
cd frontend && npm install && cd ..

# 4. 配置
cp backend/.env.example backend/.env
# 编辑 .env

# 5. 启动（分三个终端）
# 终端 1: API
cd backend && uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 终端 2: Worker
cd backend && python -m app.worker_main

# 终端 3: 前端
cd frontend && npm run dev
```

### LLM 配置示例

| 厂商 | LLM_BASE_URL | LLM_MODEL |
|------|-------------|-----------|
| DeepSeek | https://api.deepseek.com/v1 | deepseek-chat |
| 通义千问 | https://dashscope.aliyuncs.com/compatible-mode/v1 | qwen-plus |
| 火山引擎 | https://ark.cn-beijing.volces.com/api/v3 | 推理接入点 ID |
| OpenAI | https://api.openai.com/v1 | gpt-4o |
| 智谱 GLM | https://open.bigmodel.cn/api/paas/v4 | glm-4-flash |

---

## 七、常见问题

### Q: 启动后 Worker 不消费任务

检查 Redis 连接：
```bash
docker compose logs worker | grep -i redis
```
确认 `.env` 中 `REDIS_URL` 配置正确，且 Redis 容器 healthy。

### Q: 文档处理一直 pending

Worker 可能未启动或已崩溃：
```bash
docker compose ps worker
docker compose restart worker
```

### Q: Embedding 服务连接失败

1. 确认 Embedding 服务地址可从容器内访问
2. 检查地址格式：OpenAI 兼容接口填到 `/v1`，自定义接口填完整路径
3. 在前端 Embedding 配置页面点击"测试连通性"

### Q: PostgreSQL 连接失败

```bash
docker compose ps postgres
# 确认状态为 healthy
docker compose logs postgres
```
