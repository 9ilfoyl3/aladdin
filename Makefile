.PHONY: dev dev-backend dev-frontend install install-backend install-frontend test clean infra infra-down download-models download-models-cn \
	docker-build-arm docker-build-arm-full docker-export-arm docker-export-models docker-package-arm docker-package-arm-update \
	docker-build-amd64 docker-export-amd64 docker-package-amd64 docker-package-amd64-update prepare-models

# Python 解释器（可通过 make install-backend PYTHON=python3.12 覆盖）
PYTHON ?= python3

# 启动基础设施（Milvus 向量数据库 + Redis）
infra:
	@echo "启动基础设施（Milvus + Redis）..."
	docker compose up -d etcd minio milvus postgres redis
	@echo "等待服务就绪..."
	@sleep 5
	@echo "Milvus 已启动 (localhost:19530)"
	@echo "Redis 已启动 (localhost:6379)"

# 停止基础设施
infra-down:
	docker compose down

# 同时启动前后端 + Worker 开发服务
dev:
	@echo "启动前后端 + Worker 开发服务..."
	@make -j3 dev-backend dev-frontend dev-worker

# 启动后端开发服务
dev-backend:
	@echo "启动后端 (http://localhost:8000)..."
	cd backend && PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# 启动 Worker 开发服务
dev-worker:
	@echo "启动 Pipeline Worker..."
	cd backend && PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -B -m app.worker_main

# 启动前端开发服务
dev-frontend:
	@echo "启动前端 (http://localhost:5173)..."
	cd frontend && npm run dev

# 安装所有依赖
install: install-backend install-frontend

# 安装后端依赖
install-backend:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r backend/requirements.txt

# 安装前端依赖
install-frontend:
	cd frontend && npm install

# 下载模型到本地（必须，项目运行在 HF 离线模式）
download-models:
	@echo "下载 Embedding 模型 (BAAI/bge-m3)..."
	.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-m3')"
	@echo "下载 Rerank 模型 (BAAI/bge-reranker-v2-m3)..."
	.venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-reranker-v2-m3')"
	@echo "模型下载完成"

# 使用镜像源下载模型（国内推荐）
download-models-cn:
	@echo "通过 hf-mirror 下载 Embedding 模型..."
	HF_ENDPOINT=https://hf-mirror.com .venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-m3')"
	@echo "通过 hf-mirror 下载 Rerank 模型..."
	HF_ENDPOINT=https://hf-mirror.com .venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download('BAAI/bge-reranker-v2-m3')"
	@echo "模型下载完成"

# 运行后端测试
test:
	cd backend && ../.venv/bin/pytest tests/ -v

# 清理缓存
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf frontend/node_modules/.vite

# ============================================================
# Docker 镜像构建与导出（用于离线部署）
# ============================================================

# 镜像版本号（可通过 make docker-build-arm VERSION=1.0.1 覆盖）
VERSION ?= latest
DEPLOY_DIR_ARM = deploy-package-arm
DEPLOY_DIR_AMD64 = deploy-package-amd64

# --- ARM64 镜像（适用于 Apple Silicon Mac 直接构建，或部署到 ARM 服务器） ---

# 准备模型目录（从本地 HuggingFace 缓存复制）
prepare-models:
	@echo "准备模型文件..."
	@mkdir -p backend/models
	@HF_CACHE=$$HOME/.cache/huggingface/hub; \
	if [ -d "$$HF_CACHE/models--BAAI--bge-m3" ]; then \
		echo "  复制 bge-m3 模型..."; \
		cp -r "$$HF_CACHE/models--BAAI--bge-m3" backend/models/; \
	else \
		echo "  ⚠️  bge-m3 模型未找到，请先运行 make download-models"; \
		exit 1; \
	fi
	@HF_CACHE=$$HOME/.cache/huggingface/hub; \
	if [ -d "$$HF_CACHE/models--BAAI--bge-reranker-v2-m3" ]; then \
		echo "  复制 bge-reranker 模型..."; \
		cp -r "$$HF_CACHE/models--BAAI--bge-reranker-v2-m3" backend/models/; \
	else \
		echo "  ⚠️  bge-reranker 模型未找到，请先运行 make download-models"; \
		exit 1; \
	fi
	@echo "模型准备完成"

# 构建 ARM64 Docker 镜像（服务镜像，不含模型，默认远程模式）
docker-build-arm:
	@echo "构建 ARM64 后端镜像（远程模式，不含 ML 依赖）..."
	docker build --platform linux/arm64 -t artoo-backend:$(VERSION) -f backend/Dockerfile backend/
	@echo "构建 ARM64 前端镜像..."
	docker build --platform linux/arm64 -t artoo-frontend:$(VERSION) frontend/
	@echo "ARM64 镜像构建完成"

# 构建包含 ML 依赖的 ARM64 镜像（本地模型模式，CPU 版 PyTorch）
docker-build-arm-ml:
	@echo "构建 ARM64 后端镜像（含 ML 依赖，本地模型模式）..."
	docker build --platform linux/arm64 --build-arg INSTALL_ML=true -t artoo-backend:$(VERSION)-ml -f backend/Dockerfile backend/
	@echo "构建 ARM64 前端镜像..."
	docker build --platform linux/arm64 -t artoo-frontend:$(VERSION) frontend/
	@echo "ARM64 ML 镜像构建完成"

# 构建包含模型的 ARM64 后端镜像（首次部署用）
docker-build-arm-full: prepare-models
	@echo "构建 ARM64 后端镜像（含模型）..."
	docker build --platform linux/arm64 -t artoo-backend:$(VERSION)-full -f backend/Dockerfile.production backend/
	@echo "构建 ARM64 前端镜像..."
	docker build --platform linux/arm64 -t artoo-frontend:$(VERSION) frontend/
	@echo "ARM64 完整镜像构建完成"

# 导出模型为独立 tar 包（与服务镜像分离，后续迭代不需要重复打包）
docker-export-models: prepare-models
	@echo "打包模型文件..."
	@mkdir -p $(DEPLOY_DIR_ARM)
	tar -czf $(DEPLOY_DIR_ARM)/models.tar.gz -C backend/models .
	@echo "模型包: $(DEPLOY_DIR_ARM)/models.tar.gz"
	@du -sh $(DEPLOY_DIR_ARM)/models.tar.gz

# 导出 ARM64 服务镜像为 tar 包（不含模型，用于后续迭代更新）
docker-export-arm:
	@echo "导出 ARM64 服务镜像..."
	@mkdir -p $(DEPLOY_DIR_ARM)
	docker save artoo-backend:$(VERSION) -o $(DEPLOY_DIR_ARM)/artoo-backend.tar
	docker save artoo-frontend:$(VERSION) -o $(DEPLOY_DIR_ARM)/artoo-frontend.tar
	@echo "拉取并导出基础设施镜像（ARM64）..."
	docker pull --platform linux/arm64 postgres:16-alpine
	docker pull --platform linux/arm64 milvusdb/milvus:v2.4.6
	docker pull --platform linux/arm64 quay.io/coreos/etcd:v3.5.25
	docker pull --platform linux/arm64 minio/minio:RELEASE.2024-05-28T17-19-04Z
	docker save postgres:16-alpine -o $(DEPLOY_DIR_ARM)/postgres_16-alpine.tar
	docker save milvusdb/milvus:v2.4.6 -o $(DEPLOY_DIR_ARM)/milvusdb_milvus_v2.4.6.tar
	docker save quay.io/coreos/etcd:v3.5.25 -o $(DEPLOY_DIR_ARM)/quay.io_coreos_etcd_v3.5.18.tar
	docker save minio/minio:RELEASE.2024-05-28T17-19-04Z -o $(DEPLOY_DIR_ARM)/minio_minio_RELEASE.2024-05-28T17-19-04Z.tar
	docker pull --platform linux/arm64 redis:7-alpine
	docker save redis:7-alpine -o $(DEPLOY_DIR_ARM)/redis_7-alpine.tar
	@cp docker-compose-production.yml $(DEPLOY_DIR_ARM)/docker-compose.yml
	@cp backend/.env.example $(DEPLOY_DIR_ARM)/.env.example
	@cp scripts/deploy-intranet.sh $(DEPLOY_DIR_ARM)/
	@mkdir -p $(DEPLOY_DIR_ARM)/middleware
	@cp deploy-middleware/docker-compose.yml $(DEPLOY_DIR_ARM)/middleware/docker-compose.yml
	@cp deploy-middleware/milvus-user.yaml $(DEPLOY_DIR_ARM)/middleware/milvus-user.yaml
	@echo "导出完成: $(DEPLOY_DIR_ARM)/"
	@du -sh $(DEPLOY_DIR_ARM)

# 首次部署：完整打包（服务 + 模型 + 基础设施）
docker-package-arm: docker-build-arm docker-export-arm docker-export-models
	@echo ""
	@echo "=== ARM64 完整部署包打包完成 ==="
	@echo "输出目录: $(DEPLOY_DIR_ARM)/"
	@echo "包含: 服务镜像 + 基础设施镜像 + 模型文件"
	@echo "将整个目录拷贝到 ARM 服务器，执行 deploy-intranet.sh 即可部署"
	@du -sh $(DEPLOY_DIR_ARM)

# 迭代更新：仅打包服务镜像（不含模型，体积小，传输快）
docker-package-arm-update: docker-build-arm
	@echo "导出服务镜像（仅更新用）..."
	@mkdir -p $(DEPLOY_DIR_ARM)
	docker save artoo-backend:$(VERSION) -o $(DEPLOY_DIR_ARM)/artoo-backend.tar
	docker save artoo-frontend:$(VERSION) -o $(DEPLOY_DIR_ARM)/artoo-frontend.tar
	@cp docker-compose-production.yml $(DEPLOY_DIR_ARM)/docker-compose.yml
	@cp backend/.env.example $(DEPLOY_DIR_ARM)/.env.example
	@echo ""
	@echo "=== 迭代更新包打包完成 ==="
	@echo "仅包含服务镜像，不含模型和基础设施"
	@du -sh $(DEPLOY_DIR_ARM)

# --- AMD64 镜像（交叉编译，适用于部署到 x86_64 服务器） ---

# 构建 AMD64 Docker 镜像
docker-build-amd64:
	@echo "构建 AMD64 后端镜像（远程模式，交叉编译）..."
	docker build --platform linux/amd64 -t artoo-backend:$(VERSION)-amd64 -f backend/Dockerfile backend/
	@echo "构建 AMD64 前端镜像..."
	docker build --platform linux/amd64 -t artoo-frontend:$(VERSION)-amd64 frontend/
	@echo "AMD64 镜像构建完成"

# 构建包含 ML 依赖的 AMD64 镜像
docker-build-amd64-ml:
	@echo "构建 AMD64 后端镜像（含 ML 依赖，交叉编译，耗时较长）..."
	docker build --platform linux/amd64 --build-arg INSTALL_ML=true -t artoo-backend:$(VERSION)-amd64-ml -f backend/Dockerfile backend/
	@echo "构建 AMD64 前端镜像..."
	docker build --platform linux/amd64 -t artoo-frontend:$(VERSION)-amd64 frontend/
	@echo "AMD64 ML 镜像构建完成"

# 导出 AMD64 镜像为 tar 包
docker-export-amd64:
	@echo "导出 AMD64 镜像..."
	@mkdir -p $(DEPLOY_DIR_AMD64)
	docker save artoo-backend:$(VERSION)-amd64 -o $(DEPLOY_DIR_AMD64)/artoo-backend.tar
	docker save artoo-frontend:$(VERSION)-amd64 -o $(DEPLOY_DIR_AMD64)/artoo-frontend.tar
	@echo "拉取并导出基础设施镜像（AMD64）..."
	docker pull --platform linux/amd64 postgres:16-alpine
	docker pull --platform linux/amd64 milvusdb/milvus:v2.4.6
	docker pull --platform linux/amd64 quay.io/coreos/etcd:v3.5.25
	docker pull --platform linux/amd64 minio/minio:RELEASE.2024-05-28T17-19-04Z
	docker save postgres:16-alpine -o $(DEPLOY_DIR_AMD64)/postgres_16-alpine.tar
	docker save milvusdb/milvus:v2.4.6 -o $(DEPLOY_DIR_AMD64)/milvusdb_milvus_v2.4.6.tar
	docker save quay.io/coreos/etcd:v3.5.25 -o $(DEPLOY_DIR_AMD64)/quay.io_coreos_etcd_v3.5.18.tar
	docker save minio/minio:RELEASE.2024-05-28T17-19-04Z -o $(DEPLOY_DIR_AMD64)/minio_minio_RELEASE.2024-05-28T17-19-04Z.tar
	docker pull --platform linux/amd64 redis:7-alpine
	docker save redis:7-alpine -o $(DEPLOY_DIR_AMD64)/redis_7-alpine.tar
	@cp docker-compose-production.yml $(DEPLOY_DIR_AMD64)/docker-compose.yml
	@cp backend/.env.example $(DEPLOY_DIR_AMD64)/.env.example
	@cp scripts/deploy-intranet.sh $(DEPLOY_DIR_AMD64)/
	@mkdir -p $(DEPLOY_DIR_AMD64)/middleware
	@cp deploy-middleware/docker-compose.yml $(DEPLOY_DIR_AMD64)/middleware/docker-compose.yml
	@cp deploy-middleware/milvus-user.yaml $(DEPLOY_DIR_AMD64)/middleware/milvus-user.yaml
	@echo "导出完成: $(DEPLOY_DIR_AMD64)/"
	@du -sh $(DEPLOY_DIR_AMD64)

# 一键构建 + 导出 AMD64
docker-package-amd64: docker-build-amd64 docker-export-amd64 docker-export-models
	@echo ""
	@echo "=== AMD64 部署包打包完成 ==="
	@echo "输出目录: $(DEPLOY_DIR_AMD64)/"
	@echo "将整个目录拷贝到 x86_64 服务器，执行 deploy-intranet.sh 即可部署"

# 迭代更新：仅打包 AMD64 服务镜像（不含模型）
docker-package-amd64-update: docker-build-amd64
	@echo "导出 AMD64 服务镜像（仅更新用）..."
	@mkdir -p $(DEPLOY_DIR_AMD64)
	docker save artoo-backend:$(VERSION)-amd64 -o $(DEPLOY_DIR_AMD64)/artoo-backend.tar
	docker save artoo-frontend:$(VERSION)-amd64 -o $(DEPLOY_DIR_AMD64)/artoo-frontend.tar
	@cp docker-compose-production.yml $(DEPLOY_DIR_AMD64)/docker-compose.yml
	@cp backend/.env.example $(DEPLOY_DIR_AMD64)/.env.example
	@echo ""
	@echo "=== AMD64 迭代更新包打包完成 ==="
	@echo "仅包含服务镜像，不含模型和基础设施"
	@du -sh $(DEPLOY_DIR_AMD64)

