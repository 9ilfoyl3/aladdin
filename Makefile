.PHONY: dev dev-backend dev-frontend install install-backend install-frontend test clean infra infra-down download-models download-models-cn

# Python 解释器（可通过 make install-backend PYTHON=python3.12 覆盖）
PYTHON ?= python3

# 启动基础设施（Milvus 向量数据库）
infra:
	@echo "启动 Milvus 向量数据库..."
	docker compose up -d
	@echo "等待 Milvus 就绪..."
	@sleep 5
	@echo "Milvus 已启动 (localhost:19530)"

# 停止基础设施
infra-down:
	docker compose down

# 同时启动前后端开发服务
dev:
	@echo "启动前后端开发服务..."
	@make -j2 dev-backend dev-frontend

# 启动后端开发服务
dev-backend:
	@echo "启动后端 (http://localhost:8000)..."
	cd backend && ../.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

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
