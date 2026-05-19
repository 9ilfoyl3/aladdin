#!/bin/bash
# ============================================================
# 内网服务器部署脚本（在内网 Linux 服务器上执行）
# 前提：已将 deploy-package/ 目录拷贝到服务器
# ============================================================

set -e

echo "=== Aladdin 内网部署 ==="

# 1. 加载 Docker 镜像
echo ""
echo "[1/3] 加载 Docker 镜像..."
for f in *.tar; do
    echo "  加载 $f ..."
    docker load -i "$f"
done

# 2. 配置环境变量
echo ""
echo "[2/3] 配置环境变量..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "  已创建 .env 文件，请编辑配置 LLM 地址和密钥："
    echo "  vim .env"
    echo ""
    echo "  必须配置的项："
    echo "    LLM_BASE_URL=你的内网LLM服务地址"
    echo "    LLM_MODEL=模型名称"
    echo "    LLM_API_KEY=密钥（如需要）"
    echo ""
    echo "  有 GPU 时设置："
    echo "    EMBED_DEVICE=cuda"
    echo "    RERANK_DEVICE=cuda"
    echo ""
    read -p "  编辑完成后按回车继续..."
fi

# 3. 启动服务
echo ""
echo "[3/3] 启动服务..."
docker compose up -d

echo ""
echo "=== 部署完成 ==="
echo "前端: http://$(hostname -I | awk '{print $1}'):8888"
echo "后端: http://$(hostname -I | awk '{print $1}'):8000"
echo "API 文档: http://$(hostname -I | awk '{print $1}'):8000/docs"
echo ""
echo "查看日志: docker compose logs -f"
echo "停止服务: docker compose down"
