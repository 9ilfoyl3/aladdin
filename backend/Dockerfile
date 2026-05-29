# 部署用 Dockerfile
# Embedding/Rerank 统一使用远程服务，无需 ML 模型库
#
# 示例：
#   docker build -t artoo-backend .

FROM python:3.12-slim

WORKDIR /app

# 安装运行时系统依赖（不装 build-essential，依赖都有预编译 wheel）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 创建数据目录
RUN mkdir -p /app/data

# 安装 Python 依赖
# 如果下载超时，取消注释下面的镜像源参数：
#   -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
COPY requirements-base.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements-base.txt

# 复制应用源码
COPY app/ ./app/

# 暴露服务端口
EXPOSE 8000

# 启动 FastAPI 服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
