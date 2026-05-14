# 基础镜像：Python 3.11 精简版
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖（pymupdf、编译工具等）
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# 创建数据目录（SQLite 数据库 + 上传文件）
RUN mkdir -p /app/data

# 复制依赖文件并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用源码
COPY app/ ./app/

# 暴露服务端口
EXPOSE 8000

# 启动 FastAPI 服务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
