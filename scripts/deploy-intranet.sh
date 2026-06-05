#!/bin/bash
# ============================================================
# 内网服务器部署脚本（在内网 Linux 服务器上执行）
# 前提：已将 deploy-package/ 目录拷贝到服务器
# ============================================================

set -e

echo "=== Artoo 内网部署 ==="

# 1. 加载 Docker 镜像
echo ""
echo "[1/4] 加载 Docker 镜像..."
for f in *.tar; do
    if [ -f "$f" ]; then
        echo "  加载 $f ..."
        docker load -i "$f"
    fi
done

# 2. 加载模型文件（如果存在）
echo ""
echo "[2/4] 加载模型文件..."
MODEL_DIR="/var/lib/artoo/models"
if [ -f "models.tar.gz" ]; then
    echo "  解压模型到 $MODEL_DIR ..."
    mkdir -p "$MODEL_DIR"
    tar -xzf models.tar.gz -C "$MODEL_DIR"
    echo "  模型加载完成"
    echo ""
    echo "  提示：模型文件仅用于 LLM 本地推理（如 Ollama），Embedding/Rerank 已改为远程服务。"
else
    echo "  未找到 models.tar.gz，跳过模型加载"
fi

# 3. 配置环境变量
echo ""
echo "[3/4] 配置环境变量..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "  已创建 .env 文件，请编辑配置："
    echo "  vim .env"
    echo ""
    echo "  必须配置的项："
    echo "    LLM_BASE_URL=你的LLM服务地址"
    echo "    LLM_MODEL=模型名称"
    echo "    LLM_API_KEY=密钥（如需要）"
    echo ""
    echo "  Embedding/Rerank 远程服务配置："
    echo "    EMBED_BASE_URL=http://embedding-server:8080/v1"
    echo "    RERANK_BASE_URL=http://rerank-server:8001/v1"
    echo ""
    echo "  也可以启动后通过前端「Embedding & Rerank 配置」页面添加"
    echo ""
    read -p "  编辑完成后按回车继续..."
fi

# 4. 启动服务
echo ""
echo "[4/4] 启动服务..."

# 如果有本地模型，修改 docker-compose 使用绑定挂载
if [ -d "$MODEL_DIR" ] && [ "$(ls -A $MODEL_DIR 2>/dev/null)" ]; then
    # 使用 sed 替换 model_data volume 为绑定挂载
    if grep -q "model_data:/root/.cache/huggingface/hub" docker-compose.yml; then
        sed -i "s|model_data:/root/.cache/huggingface/hub|${MODEL_DIR}:/root/.cache/huggingface/hub|g" docker-compose.yml
        echo "  已配置模型目录挂载: $MODEL_DIR"
    fi
fi

# 4.1 创建共享网络（中间件与应用通过它互通，幂等）
echo "  创建共享网络 arag-network（已存在则跳过）..."
docker network create arag-network 2>/dev/null || true

# 4.2 先起中间件（etcd/minio/milvus/postgres/redis），等其 healthy
if [ -f "middleware/docker-compose.yml" ]; then
    echo "  启动中间件（含 Milvus mmap 配置）..."
    (cd middleware && docker compose --env-file ../.env up -d)
    echo "  等待中间件就绪（30s）..."
    sleep 30
    (cd middleware && docker compose --env-file ../.env ps)
else
    echo "  ⚠️ 未找到 middleware/docker-compose.yml，跳过中间件启动"
    echo "     （若中间件已单独部署可忽略；否则应用将无法连接 DB/Milvus/Redis）"
fi

# 4.3 再起应用（backend 多进程 / worker / frontend）
echo "  启动应用服务..."
docker compose up -d

echo ""
echo "=== 部署完成 ==="
echo "前端: http://$(hostname -I | awk '{print $1}'):${FRONTEND_PORT:-8888}"
echo "后端: http://$(hostname -I | awk '{print $1}'):${BACKEND_PORT:-8088}"
echo ""
echo "提示："
echo "  - 可在前端 Embedding 页面动态切换本地/远程 Embedding 服务"
echo "  - 查看应用日志:   docker compose logs -f"
echo "  - 查看中间件日志: (cd middleware && docker compose --env-file ../.env logs -f)"
echo "  - 停止应用:       docker compose down"
echo "  - 停止中间件:     (cd middleware && docker compose --env-file ../.env down)"
