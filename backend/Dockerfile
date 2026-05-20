# 部署用 Dockerfile
# 构建参数：
#   INSTALL_ML=true  → 安装本地 ML 模型依赖（torch + sentence-transformers）
#   INSTALL_ML=false → 仅基础依赖，适用于远程 Embedding/Rerank 模式（默认）
#
# 示例：
#   docker build -t aladdin-backend .                          # 远程模式（轻量，约 500MB）
#   docker build --build-arg INSTALL_ML=true -t aladdin-backend .  # 本地模型模式（约 3GB）

FROM python:3.12-slim

ARG INSTALL_ML=false

WORKDIR /app

# 安装运行时系统依赖（不装 build-essential，依赖都有预编译 wheel）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 创建数据目录
RUN mkdir -p /app/data

# 安装基础 Python 依赖
# 如果下载超时，取消注释下面的镜像源参数：
#   -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
COPY requirements-base.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements-base.txt

# 可选：安装 ML 依赖（CPU 版 PyTorch）
COPY requirements-ml.txt .
RUN if [ "$INSTALL_ML" = "true" ]; then \
    pip install --no-cache-dir -r requirements-ml.txt \
    --extra-index-url https://download.pytorch.org/whl/cpu; \
    fi

# 复制应用源码
COPY app/ ./app/

# 暴露服务端口
EXPOSE 8000

# 启动 FastAPI 服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
