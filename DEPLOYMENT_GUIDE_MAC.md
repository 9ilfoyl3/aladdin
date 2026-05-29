# Artoo 打包部署指南（macOS）

在 Mac 上构建 Docker 镜像包，用于部署到内网服务器。

---

## 环境要求

| 依赖 | 说明 |
|------|------|
| Docker Desktop | 确认 `docker buildx version` 可用 |
| Make | macOS 自带 |

---

## 打包命令

### 迭代更新（推荐，仅服务镜像）

```bash
# ARM64（Apple Silicon 原生构建，速度快）
make docker-package-arm-update

# AMD64（交叉编译，较慢）
make docker-package-amd64-update
```

### 首次完整部署（服务 + 基础设施镜像）

```bash
make docker-package-arm          # ARM64
make docker-package-amd64        # AMD64
```

### 打包产出

迭代更新包：
```
deploy-package-arm/
├── artoo-backend.tar          # 后端镜像（~500MB）
├── artoo-frontend.tar         # 前端镜像（~30MB）
├── docker-compose.yml           # 生产编排文件
└── .env.example                 # 环境变量模板
```

首次完整包额外包含：
```
├── postgres_16-alpine.tar       # PostgreSQL
├── milvusdb_milvus_v2.4.6.tar   # Milvus
├── quay.io_coreos_etcd_*.tar    # etcd
├── minio_minio_*.tar            # MinIO
├── redis_7-alpine.tar           # Redis
└── deploy-intranet.sh           # 一键部署脚本
```

---

## 目标服务器部署

### 首次部署

```bash
# 1. 上传部署包到服务器
scp -r deploy-package-arm/ user@server:/opt/artoo/

# 2. 执行部署脚本
cd /opt/artoo
chmod +x deploy-intranet.sh
./deploy-intranet.sh
```

脚本自动完成：加载镜像 → 引导配置 `.env` → 启动服务。

### 手动部署

```bash
cd /opt/artoo

# 加载镜像
docker load -i postgres_16-alpine.tar
docker load -i milvusdb_milvus_v2.4.6.tar
docker load -i quay.io_coreos_etcd_*.tar
docker load -i minio_minio_*.tar
docker load -i redis_7-alpine.tar
docker load -i artoo-backend.tar
docker load -i artoo-frontend.tar

# 配置
cp .env.example .env
vim .env

# 启动
docker compose up -d
```

### 迭代更新

```bash
cd /opt/artoo
docker load -i artoo-backend.tar
docker load -i artoo-frontend.tar
docker compose up -d --force-recreate backend worker frontend
docker image prune -f
```

---

## 环境变量配置

```env
# LLM
LLM_PROVIDER=vllm
LLM_BASE_URL=http://your-llm-server/v1
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-your-key

# Embedding 远程服务（可选，也可启动后在前端配置）
EMBED_BASE_URL=http://your-embedding-server/v1
EMBED_MODEL=BAAI/bge-m3
EMBED_API_KEY=

# Rerank 远程服务（可选，也可启动后在前端配置）
RERANK_BASE_URL=http://your-rerank-server/v1
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_API_KEY=

# 其他
POSTGRES_PASSWORD=your-secure-password
FRONTEND_PORT=8888
```

### 远程服务地址规则

| 接口类型 | 地址填写方式 | 示例 |
|---------|-------------|------|
| OpenAI 兼容（TEI/Infinity/vLLM） | 填到 `/v1` | `http://server:8080/v1` |
| 自定义接口 | 填完整端点路径 | `http://server:8001/ranking_score` |

---

## 打包命令速查

| 命令 | 说明 | 产出大小 |
|------|------|---------|
| `make docker-package-arm-update` | ARM64 迭代更新 | ~500MB |
| `make docker-package-amd64-update` | AMD64 迭代更新 | ~500MB |
| `make docker-package-arm` | ARM64 首次完整包 | ~5GB |
| `make docker-package-amd64` | AMD64 首次完整包 | ~5GB |

指定版本号：`make docker-package-arm-update VERSION=1.2.0`

---

## 常见问题

### Q: Apple Silicon 构建 AMD64 镜像很慢

交叉编译通过 QEMU 模拟，速度约为原生 1/5。建议在 x86 机器或 CI 上构建 AMD64 镜像。

### Q: 目标服务器有 GPU

在 `docker-compose.yml` 的 backend/worker 服务中添加 GPU 资源：
```yaml
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: all
          capabilities: [gpu]
```

### Q: 前端访问不了后端 API

检查 nginx.conf 中的 proxy_pass 地址是否指向 backend 容器。
