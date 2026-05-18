# ============ 阶段一：构建前端资源 ============
FROM node:20-alpine AS build

WORKDIR /app

# 复制依赖文件并安装
COPY package.json package-lock.json ./
RUN npm install

# 复制源码并构建
COPY . .
RUN npm run build

# ============ 阶段二：nginx 托管静态资源 ============
FROM nginx:alpine

# 复制构建产物到 nginx 静态目录
COPY --from=build /app/dist /usr/share/nginx/html

# 复制自定义 nginx 配置（SPA 路由 + API 代理）
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 暴露 HTTP 端口
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
