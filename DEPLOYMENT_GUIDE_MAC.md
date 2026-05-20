# Aladdin 打包部署指南（macOS 开发环境）

在 Mac（Apple Silicon）上构建 ARM64 Docker 镜像包，用于离线部署到内网服务器。  
模型和服务分开打包，后续迭代只需更新服务镜像，无需重复传输模型文件。

---

## 前置要求

| 依赖 | 说明 |
|------|------|
| Docker Desktop | 确认 `docker buildx version` 可用 |
| Make | macOS 自带 |
| 模型已下载 | `~/.cache/huggingface/hub/` 下有 bge-m3 和 bge-reranker-v2-m3 |

如果模型还没下载：
```bash
make install-backend
make download-models        # 直连 HuggingFace
# 或
make download-models-cn     # 国内镜像
```

---

## 打包命令

### 迭代更新（远程 Embedding 模式，推荐）

不含 ML 依赖和模型，镜像约 500MB，构建快：

```bash
# ARM64（Apple Silicon 原生构建）
make docker-package-arm-update

# AMD64（交叉编译，较慢）
make docker-package-amd64-update
```

### 首次部署（本地模型模式，含模型文件）

需要先下载模型，镜像含 CPU 版 PyTorch：

```bash
make docker-package-arm          # ARM64 完整包
make docker-package-amd64        # AMD64 完整包
```

### 本地模型模式（不含模型文件，仅含 ML 依赖）

```bash
make docker-build-arm-ml         # ARM64 含 ML 依赖
make docker-build-amd64-ml       # AMD64 含 ML 依赖
```

产出 `deploy-package-arm/`：
```
deploy-package-arm/
├── aladdin-backend.tar              # 后端服务镜像（不含模型，约 2GB）
├── aladdin-frontend.tar             # 前端镜像（约 30MB）
├── models.tar.gz                    # 模型文件（约 3GB，独立打包）
├── postgres_16-alpine.tar           # PostgreSQL
├── milvusdb_milvus_v2.4.6.tar       # Milvus
├── quay.io_coreos_etcd_v3.5.18.tar  # etcd
├── minio_minio_*.tar                # MinIO
├── docker-compose.yml               # 生产编排文件
├── .env.example                     # 环境变量模板
└── deploy-intranet.sh               # 一键部署脚本
```

产出目录 `deploy-package-arm/`（或 `deploy-package-amd64/`）：
```
deploy-package-arm/
├── aladdin-backend.tar              # 后端服务镜像
├── aladdin-frontend.tar             # 前端镜像
├── docker-compose.yml               # 生产编排文件
└── .env.example                     # 环境变量模板
```

首次完整包还包含：`models.tar.gz`、基础设施镜像 tar、`deploy-intranet.sh`。

---

## 目标服务器部署

```bash
chmod +x deploy-intranet.sh
./deploy-intranet.sh
```

脚本会自动：
1. 加载所有 Docker 镜像
2. 解压模型文件到 `/var/lib/aladdin/models/`
3. 引导配置 `.env`
4. 启动所有服务

---

## Embedding/Rerank 配置

系统支持两种模式，可在前端页面动态切换，也可通过环境变量设置初始默认值。

### 模式 A：本地模型（内网离线）

模型文件通过 `models.tar.gz` 部署到服务器，后端本地加载推理。

`.env` 配置：
```env
EMBED_PROVIDER=sentence-transformers    # 或 flag-embedding
EMBED_MODEL=BAAI/bge-m3
EMBED_DEVICE=cpu                        # 有 GPU 改为 cuda

RERANK_PROVIDER=sentence-transformers   # 或 flag-embedding
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_DEVICE=cpu
```

### 模式 B：远程服务

目标环境有独立 Embedding 服务（TEI、Infinity、vLLM 等），后端通过 HTTP 调用。

`.env` 配置（示例）：
```env
# Embedding：OpenAI 兼容接口，地址填到 /v1
EMBED_PROVIDER=remote
EMBED_BASE_URL=http://10.30.1.4:8902/v1
EMBED_MODEL=Qwen3-Embedding-0.6B
EMBED_API_KEY=e4f7b2c1a6d9483f9b2e5c7a1d8f6b4c

# Rerank：自定义接口，地址填完整端点
RERANK_PROVIDER=remote
RERANK_BASE_URL=http://10.30.1.3:8001/ranking_score
RERANK_MODEL=
RERANK_API_KEY=
```

### 页面动态切换

部署后可在前端 **Embedding** 页面：
- 添加多个配置（本地/远程）
- 测试连通性
- 一键切换启用的配置（立即生效，无需重启）

环境变量决定初始默认值，数据库中 `is_active=True` 的配置会覆盖环境变量。

---

## Provider 选择说明

| Provider | 特点 | 推荐场景 |
|----------|------|---------|
| `sentence-transformers` | 跨平台兼容好，稀疏向量为 BM25 近似 | 通用场景、开发调试 |
| `flag-embedding` | 原生稠密+稀疏向量，检索质量更高 | 生产环境、有 GPU |
| `remote` | 调用外部 API，后端不加载模型 | 有独立 Embedding/Rerank 服务 |

本地 provider 切换不需要重新构建镜像，只需修改配置或在页面切换。

### 远程服务地址填写规则

| 接口类型 | 地址填写方式 | 示例 |
|---------|-------------|------|
| OpenAI 兼容（TEI/Infinity/vLLM） | 填到 `/v1`，系统自动拼接 `/embeddings` 或 `/rerank` | `http://server:8080/v1` |
| 自定义接口 | 填完整端点路径 | `http://server:8001/ranking_score` |

---

## 打包命令速查

| 命令 | 说明 | 镜像体积 |
|------|------|---------|
| `make docker-package-arm-update` | ARM64 服务镜像（远程模式，不含 ML 依赖） | ~500MB |
| `make docker-package-amd64-update` | AMD64 服务镜像（远程模式，不含 ML 依赖） | ~500MB |
| `make docker-package-arm` | ARM64 完整包（服务 + 模型 + 基础设施） | ~8GB |
| `make docker-package-amd64` | AMD64 完整包（服务 + 模型 + 基础设施） | ~8GB |
| `make docker-build-arm-ml` | ARM64 含 ML 依赖（CPU 版 torch，本地模型模式） | ~1.5GB |
| `make docker-build-amd64-ml` | AMD64 含 ML 依赖（CPU 版 torch，本地模型模式） | ~1.5GB |
| `make docker-build-arm` | 仅构建 ARM64 服务镜像（不导出） | - |
| `make docker-export-models` | 仅导出模型 tar.gz | ~3GB |
| `make prepare-models` | 从 HuggingFace 缓存复制模型到 backend/models/ | - |
| `make download-models` | 下载模型到本地缓存 | - |
| `make download-models-cn` | 通过国内镜像下载模型 | - |

版本号：`make docker-package-arm-update VERSION=1.2.0`

### Dockerfile 构建参数

后端 Dockerfile 通过 `INSTALL_ML` 构建参数控制是否安装 ML 依赖：

```bash
# 默认：不装 torch，适用于远程 Embedding/Rerank（镜像约 500MB）
docker build -t aladdin-backend .

# 安装 CPU 版 PyTorch + sentence-transformers（镜像约 1.5GB）
docker build --build-arg INSTALL_ML=true -t aladdin-backend .
```

依赖拆分：
- `requirements-base.txt` — Web 框架、数据库、文档解析等基础依赖
- `requirements-ml.txt` — torch、sentence-transformers、transformers（仅本地模型需要）
- `requirements.txt` — 开发用，包含全部依赖（本地开发时 `pip install -r requirements.txt`）

---

## 部署架构

```
目标服务器 (Docker Compose):
  ├─ aladdin-backend     ← FastAPI 服务
  │   ├─ 本地模型（通过 volume 挂载 models.tar.gz 解压内容）
  │   └─ 或调用远程 Embedding/Rerank API
  ├─ aladdin-frontend    ← nginx 静态资源 + API 代理
  ├─ PostgreSQL          ← 元数据 + 配置存储
  ├─ Milvus             ← 向量存储
  ├─ etcd + MinIO       ← Milvus 依赖
  └─ (网络可达) LLM API  ← 对话生成

可选独立服务:
  └─ TEI / Infinity / vLLM  ← 提供 /v1/embeddings 和 /rerank 接口
```

---

## 常见问题

### Q: Apple Silicon 上构建 AMD64 镜像很慢

交叉编译通过 QEMU 模拟，速度约为原生 1/5。建议在 x86 机器或 CI 上构建。

### Q: 模型文件太大，传输困难

模型和服务已分离打包。首次部署传输 `models.tar.gz`（约 3GB），后续迭代只需传服务镜像（约 2GB）。可用 `gzip` 进一步压缩 tar 包。

### Q: 目标服务器有 GPU

修改 `.env`：
```env
EMBED_DEVICE=cuda
RERANK_DEVICE=cuda
```

在 `docker-compose.yml` 的 backend 服务中添加：
```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: all
          capabilities: [gpu]
```

### Q: 切换 Embedding 配置后需要重启吗

通过前端页面切换：**不需要重启**，立即生效。  
通过修改 `.env`：需要重启容器 `docker compose restart backend`。

### Q: 远程 Embedding 服务需要什么接口格式

- Embedding：兼容 OpenAI `/v1/embeddings` 接口（TEI、Infinity、vLLM 等均支持）
- Rerank：标准接口兼容 `/rerank`（TEI、Jina 格式），自定义接口支持 `{query, candidate}` 格式（如返回 `{ranking_scores: [...]}`）
- 地址填写：OpenAI 标准填到 `/v1`，自定义填完整端点如 `/ranking_score`
