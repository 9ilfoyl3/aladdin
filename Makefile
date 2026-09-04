.PHONY: install install-backend install-frontend install-graph dev dev-backend dev-worker dev-frontend \
	infra infra-graph infra-down milvus-init milvus-describe milvus-reset milvus-prune-dims \
	purge-dry-run purge-data purge-keep-objects reindex-dry-run reindex-all \
	test clean build build-app build-graph build-app-graph

# Python 解释器（可通过 make install-backend PYTHON=python3.12 覆盖）
PYTHON ?= python3

# ============================================================
# 安装
# ============================================================
install: install-backend install-frontend

install-backend:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r backend/requirements.txt

install-frontend:
	cd frontend && npm install

# 可选：知识图谱依赖（Neo4j 驱动）。仅在需要开启图谱功能时执行。
install-graph:
	.venv/bin/pip install -r backend/requirements-graph.txt

# ============================================================
# 本地开发（中间件用 docker，应用跑在宿主机热重载）
# ============================================================
# 启动中间件（compose 自动叠加 docker-compose.override.yml，暴露端口到宿主机）
infra:
	@echo "启动中间件（etcd/minio/milvus/postgres/redis）..."
	docker compose --profile infra up -d
	@echo "Milvus: localhost:19530 | Postgres: localhost:5432 | Redis: localhost:6379"

infra-down:
	docker compose --profile infra down

# 启动中间件 + Neo4j（知识图谱）。开图谱开发时用这条替代 infra。
# 还需：1) make install-graph 装驱动  2) backend/.env 设 GRAPH_ENABLE=true
infra-graph:
	@echo "启动中间件 + Neo4j（图谱）..."
	docker compose --profile infra --profile graph up -d
	@echo "Neo4j: localhost:7687（Bolt） | localhost:7474（浏览器管理台）"

# 同时启动后端 + Worker + 前端
dev:
	@make -j3 dev-backend dev-worker dev-frontend

dev-backend:
	@echo "后端 http://localhost:8000"
	cd backend && PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev-worker:
	@echo "Pipeline Worker"
	cd backend && PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -B -m app.worker_main

dev-frontend:
	@echo "前端 http://localhost:3000"
	cd frontend && npm run dev

# ============================================================
# Milvus 拓扑（共享 collection + Partition Key + 按维度分表）
# ============================================================
# 幂等建表。服务启动时会自动做同样的事，这条用于部署前预建或排障。
milvus-init:
	cd backend && ../.venv/bin/python -m scripts.init_milvus
# 查看当前拓扑（各维度表 / Partition Key / 分区数 / 分片数 / 字段）
milvus-describe:
	cd backend && ../.venv/bin/python -m scripts.init_milvus --describe
# 破坏性重置：删受管表重建 + 清理旧拓扑遗留的 per-KB collection（kb_*）。
# 只动 Milvus，不清 PG / 对象存储。改 MILVUS_NUM_PARTITIONS 等建表固定项时用这条。
milvus-reset:
	cd backend && ../.venv/bin/python -m scripts.init_milvus --reset --drop-legacy
# 清理非当前 EMBED_DIM 的历史维度表（换 embedding 模型并验证新维度无误后执行）
milvus-prune-dims:
	cd backend && ../.venv/bin/python -m scripts.init_milvus --prune-dims

# ============================================================
# 数据清除 / 重建（拓扑切换用）
# ============================================================
# 体检：打印将被清除的数量与动作，不做任何修改
purge-dry-run:
	cd backend && ../.venv/bin/python -m scripts.purge_data --dry-run
# 【破坏性】清空知识内容（Milvus + PG 内容表 + 对象存储 + 图谱 + Redis），
# 保留租户/用户/API Key/知识库本体/模型配置。需交互输入 PURGE 确认。
purge-data:
	cd backend && ../.venv/bin/python -m scripts.purge_data
# 保留源文件不清对象存储：之后可用 reindex-all 从原件重建索引而非让用户重传
purge-keep-objects:
	cd backend && ../.venv/bin/python -m scripts.purge_data --keep-objects
# 体检：打印重建索引计划（含源文件缺失清单），不做任何修改
reindex-dry-run:
	cd backend && ../.venv/bin/python -m scripts.reindex_all --dry-run
# 从对象存储中的原件重建全部向量索引（需 worker 在运行）。
# 适用：换 embedding 模型 / 调 num_partitions 且源文件仍在的场景。
reindex-all:
	cd backend && ../.venv/bin/python -m scripts.reindex_all --watch

# ============================================================
# 测试 / 清理
# ============================================================
test:
	cd backend && ../.venv/bin/pytest tests/ -v

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf frontend/node_modules/.vite

# ============================================================
# 离线部署包（详见 deploy/build.sh）
# ============================================================
# 首次完整包（应用 + 中间件镜像）。指定架构：make build ARCH=arm64
build:
	deploy/build.sh $(if $(ARCH),--arch $(ARCH),)

# 迭代更新包（仅应用镜像）
build-app:
	deploy/build.sh $(if $(ARCH),--arch $(ARCH),) --app-only
