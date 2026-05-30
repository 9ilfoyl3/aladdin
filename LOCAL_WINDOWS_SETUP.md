# Aladdin 本地 Windows 运行 & 测试指南（含 tenant-auth 租户认证）

本文档面向在 **Windows 本地**跑通 aladdin（含本次的租户认证体系）并做测试。
全流程：装中间件（Docker）→ 配后端 → 配前端 → 启动 → 验证登录与隔离。

> 本机已确认的 Python 环境：`C:\Users\gaofe\.conda\envs\aladdin\python.exe`（已装 fastapi/
> sqlalchemy/bcrypt/PyJWT/hypothesis 等）。下文统一用它，记为 `%PY%`。
> PowerShell 里可先设：`$PY = "C:\Users\gaofe\.conda\envs\aladdin\python.exe"`

---

## 0. 架构与端口速览

| 组件 | 本地地址 | 说明 |
| --- | --- | --- |
| 前端 dev server | http://localhost:3000 | vite，反代 `/api` `/v1` 到后端 |
| 后端 API | http://localhost:8000 | uvicorn（**端口必须 8000**，前端代理写死了它） |
| Pipeline Worker | 无端口 | 独立进程，消费 Redis 队列处理文档 |
| PostgreSQL | localhost:5432 | 元数据 |
| Milvus | localhost:19530 | 向量 |
| Redis | localhost:6379 | 任务队列 + 检索缓存 |
| Embedding/Rerank | 远程服务 | 需可达的 bge-m3 / reranker 服务 |
| LLM | 远程/本地 | vLLM 或 Ollama |

> ⚠️ **端口一致性（本地 dev）**：前端 `vite.config.ts` 默认把 `/api`、`/v1` 代理到
> `http://localhost:8000`，所以后端默认用 `--port 8000` 启动。若要改端口，见下方「端口可配置」。

### 端口可配置（容器内固定 / 对外可改）

设计原则：**容器内服务端口固定**（后端 `8000`、前端 nginx `80`），**对外暴露端口一律可配置**。

- **容器化部署**（`deploy-arm64` / `deploy-server` / `deploy-amd64` / `docker-compose-production.yml`）：
  对外端口由 `.env` 的 `BACKEND_PORT`（默认 8000）和 `FRONTEND_PORT`（默认 8888）决定，
  映射形如 `"${BACKEND_PORT:-8000}:8000"`、`"${FRONTEND_PORT:-8888}:80"`。改 `.env` 即可，
  容器内部仍是 8000/80，无需动镜像或 nginx 配置。
- **本地 dev**：`vite.config.ts` 支持环境变量覆盖（默认值不变）：
  - `FRONTEND_PORT`：前端 dev 端口（默认 3000）
  - `BACKEND_PROXY_TARGET`：反代后端地址（默认 `http://localhost:8000`）
  - 后端端口由 uvicorn `--port` 决定；改了后端端口要同步设 `BACKEND_PROXY_TARGET`。
  例：后端跑 8010、前端跑 3001：
  ```powershell
  # 终端：后端
  & $PY -m uvicorn app.main:app --reload --port 8010
  # 终端：前端（PowerShell 设环境变量后再 npm run dev）
  $env:FRONTEND_PORT="3001"; $env:BACKEND_PROXY_TARGET="http://localhost:8010"; npm run dev
  ```

---

## 1. 前置条件

- **Docker Desktop**（跑 PostgreSQL/Milvus/Redis 等中间件）
- **Node.js ≥ 18**（前端，含 npm）
- **Python 环境**：用现成的 conda env（见顶部 `%PY%`）。若要新建：
  `conda create -n aladdin python=3.12` 然后 `pip install -r backend/requirements.txt`
- **Embedding/Rerank 服务**：bge-m3 + bge-reranker，必须有一个本机可达的地址
  （没有的话文档处理与检索会失败；纯粹验证登录/租户隔离可暂时不依赖它）

---

## 2. 启动中间件（Docker）

在 `aladdin/` 目录：

```powershell
docker compose up -d etcd minio milvus postgres redis
```

确认就绪：

```powershell
docker compose ps
# postgres / milvus / redis 都应为 running/healthy
```

> ⚠️ **数据库名注意**：`docker-compose.yml` 默认建库名是 **`artoo`**，而 `backend/.env`
> 里写的是 `aladdin`。二选一对齐（见第 3 节），否则后端连不上库。

---

## 3. 配置后端 `.env`

编辑 `backend/.env`（已存在）。**本地运行需要改这几处**：

```dotenv
# ===== 基础设施 =====
# 关键：数据库名要和 docker-compose 建的库一致。
# docker-compose.yml 建的是 artoo；这里直接用 artoo 最省事：
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/artoo
MILVUS_HOST=localhost
MILVUS_PORT=19530
REDIS_URL=redis://localhost:6379/0

# ===== Embedding / Rerank =====
# 原值 http://infinity:7997 是 docker 内网主机名，本地跑解析不了，必须改成本机可达地址。
# 例（按你的实际服务改）：
EMBED_PROVIDER=remote
EMBED_BASE_URL=http://10.30.1.6:7997/v1
EMBED_MODEL=models/bge-m3
EMBED_API_KEY=
EMBED_SPARSE_ENABLED=false

RERANK_PROVIDER=remote
RERANK_BASE_URL=http://10.30.1.6:7998/v1
RERANK_MODEL=models/bge-reranker-v2-m3
RERANK_API_KEY=

# ===== LLM（也可启动后在前端"模型管理"页配置） =====
LLM_PROVIDER=ollama
LLM_BASE_URL=http://localhost:11434
LLM_MODEL=qwen2.5:7b
LLM_API_KEY=

# ===== 认证与授权（tenant-auth，本次新增，必须配） =====
# 联调建议：先 false 跑通原功能，再改 true 验鉴权（见第 6 节）。
AUTH_ENABLED=true
# JWT 密钥（auth_enabled=true 必填，缺失则启动失败）。生成见下方命令。
JWT_SECRET=请粘贴下方命令生成的随机串
JWT_EXPIRE_MINUTES=720
# 初始超级管理员（首次启动自动创建，强制首次登录改密）。必填，别用弱口令。
SUPER_ADMIN_USERNAME=root
SUPER_ADMIN_PASSWORD=ChangeMe#2024
# 注册模式：invite_only（默认，只能管理员建号）
REGISTRATION_MODE=invite_only
# 超管内容可见边界：false=超管看不到业务正文（卷宗隐私）
CONTENT_VIEW_BOUNDARY_OPEN=false
```

生成 `JWT_SECRET`（PowerShell）：

```powershell
& "C:\Users\gaofe\.conda\envs\aladdin\python.exe" -c "import secrets; print(secrets.token_urlsafe(48))"
```

把输出粘到 `JWT_SECRET=`。

> 说明：`BACKEND_PORT`/`FRONTEND_PORT` 这两个变量在本地 dev 启动方式下**不生效**（端口由
> uvicorn `--port` 和 vite 配置决定），无需理会。

---

## 4. 安装依赖

### 后端（用 conda env，已装则跳过）
```powershell
& "C:\Users\gaofe\.conda\envs\aladdin\python.exe" -m pip install -r backend\requirements.txt
```

### 前端
```powershell
cd frontend
npm install
cd ..
```

---

## 5. 启动服务（开三个终端）

> 注意 Windows 用 conda python，不是 Makefile 里的 `.venv/bin/...`（那是 Unix 路径）。
> 每个终端都先 `cd` 到 `backend`（后端/Worker）或 `frontend`（前端）。

### 终端 1：后端 API（端口 8000）
```powershell
cd backend
& "C:\Users\gaofe\.conda\envs\aladdin\python.exe" -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
启动日志应能看到建表、TenantBootstrap（创建内置租户/超管/角色权限）。

### 终端 2：Pipeline Worker（处理上传文档）
```powershell
cd backend
& "C:\Users\gaofe\.conda\envs\aladdin\python.exe" -m app.worker_main
```
> 不开 Worker 也能登录/建库/做隔离测试，只是上传的文档不会被处理（停在 pending）。

### 终端 3：前端（端口 3000）
```powershell
cd frontend
npm run dev
```
浏览器开 http://localhost:3000 → 应跳转到登录页。

---

## 6. 首次登录与验证

### 6.1 超管首登 + 改密
1. 浏览器访问 http://localhost:3000 → 自动跳 `/login`。
2. 用 `.env` 里的 `SUPER_ADMIN_USERNAME` / `SUPER_ADMIN_PASSWORD` 登录。
3. 因 `must_change_password`，会被强制跳改密页。改密后需用新口令重新登录
   （旧 JWT 因 token_version 自增已失效，这是预期行为）。

### 6.2 建租户 + 租户管理员
超管登录后，调用管理接口（前端暂无管理 UI，可用 curl/Postman/REST 客户端）：

```powershell
# 先登录拿超管 token（改密后的新口令）
$body = '{"username":"root","password":"<新口令>"}'
$tok = (Invoke-RestMethod -Uri http://localhost:8000/api/auth/login -Method Post -ContentType application/json -Body $body).access_token

# 建租户（返回初始租户管理员的临时口令）
$h = @{ Authorization = "Bearer $tok" }
$tbody = '{"name":"法院A","admin_username":"judgeadmin"}'
Invoke-RestMethod -Uri http://localhost:8000/api/admin/tenants -Method Post -Headers $h -ContentType application/json -Body $tbody
# 记下返回的 id 和 admin_temp_password
```

### 6.3 租户管理员登录建用户
```powershell
# 租户管理员登录（带 tenant_id）
$b = '{"username":"judgeadmin","password":"<admin_temp_password>","tenant_id":"<租户id>"}'
$ta = (Invoke-RestMethod -Uri http://localhost:8000/api/auth/login -Method Post -ContentType application/json -Body $b).access_token
# 同样需改密后重登（must_change_password）
```

之后用租户管理员/普通用户在前端登录，即可建知识库、上传文档、问答 —— 数据按租户隔离。

---

## 7. 灰度联调（推荐先做）

如果只想先确认"租户改造没破坏原功能"，把 `.env` 设 `AUTH_ENABLED=false` 重启后端：
- Guard 旁路（匿名 platform 身份），前端不跳登录，原功能照常用。
- 确认建库/上传/问答都正常后，再改回 `AUTH_ENABLED=true` 验鉴权。

---

## 7.5 30 秒快速自检鉴权（无需 Docker / 中间件）

只想确认"租户认证体系本身能跑通"（建超管、登录、强制改密、token 失效），可以用
SQLite 临时库起一个最小后端，**不依赖 Postgres/Milvus/Redis**（Redis 不可用会自动降级）：

```powershell
cd backend
$env:DATABASE_URL="sqlite+aiosqlite:///./_smoke.db"
$env:AUTH_ENABLED="true"
$env:JWT_SECRET="local-smoke-secret-0123456789abcdef"
$env:SUPER_ADMIN_USERNAME="root"
$env:SUPER_ADMIN_PASSWORD="ChangeMe#2024"
& "C:\Users\gaofe\.conda\envs\aladdin\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8011
```

看到 `Application startup complete` 即引导成功（已自动创建内置租户/超管/角色权限）。
另开一个终端验证登录与改密闭环：

```powershell
# 1) 超管首登（must_change_password=true）
$r = Invoke-RestMethod http://127.0.0.1:8011/api/auth/login -Method Post -ContentType application/json -Body '{"username":"root","password":"ChangeMe#2024"}'
$h = @{ Authorization = "Bearer $($r.access_token)" }

# 2) 未改密前访问业务接口会被拦（403「请先修改初始口令」）—— 预期行为
# 3) 改密
Invoke-RestMethod http://127.0.0.1:8011/api/auth/change-password -Method Post -Headers $h -ContentType application/json -Body '{"old_password":"ChangeMe#2024","new_password":"NewPass#2024"}'

# 4) 旧 token 已失效（401）；用新口令重登
$r2 = Invoke-RestMethod http://127.0.0.1:8011/api/auth/login -Method Post -ContentType application/json -Body '{"username":"root","password":"NewPass#2024"}'
$h2 = @{ Authorization = "Bearer $($r2.access_token)" }
Invoke-RestMethod http://127.0.0.1:8011/api/auth/me/permissions -Method Get -Headers $h2
# 超管 is_super_admin=true、permissions 为空属正常：超管隐含全权，无需逐条权限点
```

验证完删掉临时库：`Remove-Item _smoke.db`。

> 注意：SQLite 仅用于快速验鉴权，**正式本地联调仍用 Postgres**（见上文）。SQLite 下个别
> Postgres 专用迁移语句会被跳过（日志里的 `Migration 跳过` 属正常，不影响鉴权）。

> ✅ 本节命令已在本机实测通过：超管引导 → 首登强制改密 → 旧 JWT 失效（token_version）→
> 新口令重登 → 取到权限点，全链路符合预期。

---

## 8. 跑后端自动化测试（不依赖外部服务）

租户认证的 30 个测试用内存/文件 sqlite + TestClient，**不需要 Docker 中间件**：

```powershell
cd backend
& "C:\Users\gaofe\.conda\envs\aladdin\python.exe" -m pytest `
  tests/test_tenant_auth_properties.py `
  tests/test_tenant_auth_db_properties.py `
  tests/test_tenant_auth_integration.py `
  tests/test_tenant_auth_integration_extra.py -q
# 预期 30 passed（含 bcrypt，约 2-3 分钟）
```

---

## 9. 常见问题排查

| 现象 | 原因 / 解决 |
| --- | --- |
| 后端启动报 `jwt_secret 未配置` | `.env` 没填 `JWT_SECRET`，且 `AUTH_ENABLED=true`。填上即可。 |
| 后端启动报缺 Super_Admin 配置 | 填 `SUPER_ADMIN_USERNAME` / `SUPER_ADMIN_PASSWORD`。 |
| 后端连不上数据库 | `DATABASE_URL` 的库名与 docker-compose（`artoo`）不一致；或 Docker 没起。 |
| 前端请求 404/连不上 | 后端没跑在 **8000**；或没用 `npm run dev`（vite 代理只在 dev 生效）。 |
| 文档上传后一直 pending | Worker（终端 2）没启动，或 Embedding 服务不可达。 |
| 上传/检索报 embedding 错误 | `EMBED_BASE_URL` 还是 `http://infinity:7997`（docker 内网名），改成本机可达地址。 |
| 登录后立刻又跳登录 | JWT 失效（改密后旧 token 失效属正常，用新口令重登）；或 `JWT_SECRET` 重启后变了。 |
| 改密后提示重新登录 | 预期行为：改密会使旧 JWT 失效（token_version 自增）。 |
| 跨租户访问返回 404 | 预期：跨租户硬隔离，不泄露资源存在性。 |
| API Key 调管理接口 403 | 预期：API Key 通道永不可执行管理/平台操作。 |

---

## 10. 认证接口速查（联调常用）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | 登录取 JWT，body: `{username, password, tenant_id?}` |
| POST | `/api/auth/change-password` | 改密，body: `{old_password, new_password}` |
| GET | `/api/auth/me/permissions` | 当前用户权限点（前端据此显隐菜单） |
| POST | `/api/admin/tenants` | 建租户（超管），返回初始管理员临时口令 |
| POST | `/api/admin/users` | 建用户（`user:manage`） |
| POST | `/api/admin/roles` | 自定义角色（`role:manage`） |
| POST | `/api/api-keys` | 建租户级 Key（`apikey:manage`） |
| POST | `/api/api-keys/me` | 建用户级 Key（普通用户） |
| POST | `/api/api-keys/external-agent` | 建超管代理 Key（仅超管） |

所有受保护请求带头：`Authorization: Bearer <jwt 或 sk-...>`。
业务系统用 API Key 调 `/v1/chat/completions` 等。
