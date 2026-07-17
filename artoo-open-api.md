# Artoo 开放接口文档（Open API）

面向第三方系统集成的接口手册。第三方系统可**沿用自己的用户体系**，通过一把「超管级代理 Key」+ 每次请求携带自有用户标识，即可让每个终端用户拥有**各自隔离**的知识库与对话空间，完整跑通「建库 → 上传 → 解析 → 检索 → 问答」全链路。

- 适用版本：backend 0.1.0（服务标题 `Agentic RAG System`）
- 默认服务地址：`http://<host>:8000`（默认 `HOST=0.0.0.0`、`PORT=8000`）
- 下文统一约定环境变量：
  - `BASE=http://localhost:8000`
  - `KEY=sk-<你的代理Key>`（见第 2 节获取）
  - `EU=alice-001`（你方系统内的终端用户唯一标识，随请求填当前登录用户）

---

## 1. 认证与集成模型

### 1.1 凭据形式

所有受保护接口统一用 HTTP 头携带凭据：

```
Authorization: Bearer <凭据>
```

按前缀区分（见 `app/api/deps.py`）：

- 以 `sk-` 开头 → **API Key 通道**（第三方集成走这里）
- 其余 → **JWT**（前端登录态）

API Key 明文格式为 `sk-` + 48 位十六进制串，服务端只存 SHA256 哈希，**明文仅创建时返回一次**。

> 除上面的明文 Bearer 外，还提供 **AK/SK 签名通道**（见 1.5）：不把长期密钥每次上行，改为对每次请求做 HMAC 签名。适合脚本 / 自动化平台 / 桌面客户端等"无独立后端但运行在可信环境"的直连场景，控制台/抓包里只看到每次现算的签名，看不到长期密钥。

### 1.2 三种 API Key 与选型

| 类型 | 标识 | 身份语义 | 知识库范围 | 能否写（建库/上传） | 多轮对话 |
| --- | --- | --- | --- | --- | --- |
| 租户级 Key | `tenant_level` | 机器身份（`role=None`） | 由 Key 的 `scope` 显式裁定 | 否（仅读检索） | 需自管历史 |
| 用户级 Key | `user_level` | 绑定某注册用户 | 该用户可读的全部库 | 是 | 支持 |
| **超管级代理 Key** | `external_agent` | **代表你方外部用户** | 外部用户自有私有库 | **是** | **支持（服务端托管）** |

> **第三方用自有用户体系接入，请选「超管级代理 Key」。** 它由平台超管签发，租户硬锁到内置「外部用户租户」；每次请求额外带头 `X-External-User-Id: <你方用户ID>`，平台按 `(代理Key, 外部用户ID)` 复合键自动懒创建并隔离独立身份，无需在 Artoo 逐个注册账号。外部用户固定 `member` 角色，可通过写权限闸门。

### 1.3 代理 Key 专属请求头

| 头 | 必填 | 说明 |
| --- | --- | --- |
| `Authorization: Bearer sk-...` | 是 | 代理 Key 明文 |
| `X-External-User-Id: <id>` | 是 | 你方系统内终端用户唯一标识；缺失 → `400` |

要点：

- 同一代理 Key 下，不同 `X-External-User-Id` 之间**私有库与会话互不可见**。
- 外部用户可访问范围 = 自有私有库（各外部用户互相隔离），**不能跨到平台其他业务租户**。
- 建库/传文档时 `owner_user_id` 自动盖成该外部用户，归属天然隔离。

### 1.4 通用错误码

| 状态码 | 含义 | 常见原因 |
| --- | --- | --- |
| `400` | 请求非法 | 缺 user 消息；代理 Key 缺 `X-External-User-Id`；不支持的文件类型 |
| `401` | 未认证 | Key 无效/已撤销/缺 `Authorization` |
| `403` | 无权限 | 用 API Key 调管理端点；租户/用户停用；写权限不足 |
| `404` | 资源不存在 | 访问不在授权范围的库/文档/会话（存在性非泄露） |
| `413` | 超限 | 单文件过大 / 知识库累计 chunk 超配额 |
| `500` | 服务端异常 | 检索链路或 LLM 生成失败（多与模型/索引配置相关，非鉴权） |
| `503` | 服务暂不可用 | 会话文件异步上传时队列（Redis）/ 对象存储暂不可用，请稍后重试（见第 8 节） |

错误响应统一为 `{ "detail": "<原因>" }`。

### 1.5 AK/SK 签名调用（免明文密钥上行）

面向"无独立后端、但运行在**可信环境**"的直连场景（脚本 / 自动化平台节点 / 桌面客户端）。用一把 `SK` 对每次请求做 HMAC-SHA256 签名，服务端重算比对；网络里只出现**每次现算的签名**，长期密钥不再逐请求上行。

**凭据来源**：任意类型 Key 创建时（见第 2 节），响应额外返回 `access_key`（AK）与 `secret_key`（SK）：

- `AK` = Key 的 `id`，可公开，仅用于定位密钥；
- `SK` 由服务端从平台密钥派生、**不落库**（DB 泄露也无法伪造签名），**仅创建时返回一次**，请妥善保存在你方可信环境。

**请求头格式**：

```
Authorization: SAG-HMAC-SHA256 ak=<AK>,ts=<unix秒>,nonce=<随机hex>,sign=<hex签名>
```

**签名串**（按顺序换行 `\n` 连接，客户端与服务端须逐字节一致）：

```
<HTTP方法大写>\n<请求路径>\n<原始query串>\n<ts>\n<nonce>\n<X-External-User-Id或空>
```

- 不纳入 body：兼容 multipart 文件上传（客户端难以拿到最终多部分字节做哈希）；body 完整性依赖 HTTPS。
- 代理 Key 仍需带 `X-External-User-Id` 头，且其值已并入签名（在途被篡改会验签失败）。

**校验规则**：`|now - ts| > 300s`（可配 `APIKEY_SIGN_WINDOW_SECONDS`）拒绝；`nonce` 在时间窗内只能用一次（Redis 去重，防重放）；任一不过 → `401`。

**Node.js 示例（对代理 Key 调上传接口）**：

```js
const crypto = require("crypto");

const BASE = "http://localhost:8000";
const AK = "<创建返回的 access_key>";
const SK = "<创建返回的 secret_key>";
const EU = "alice-001";

const method = "POST";
const path = "/api/knowledge-bases/<kb_id>/documents/upload";
const query = ""; // 无 query 时为空串
const ts = Math.floor(Date.now() / 1000).toString();
const nonce = crypto.randomBytes(16).toString("hex");

const canonical = [method, path, query, ts, nonce, EU].join("\n");
const sign = crypto.createHmac("sha256", SK).update(canonical).digest("hex");

const form = new FormData();
form.append("file", new Blob([/* 文件字节 */]), "manual.pdf");

await fetch(`${BASE}${path}`, {
  method,
  headers: {
    "Authorization": `SAG-HMAC-SHA256 ak=${AK},ts=${ts},nonce=${nonce},sign=${sign}`,
    "X-External-User-Id": EU,
  },
  body: form,
});
```

> 签名通道与 Bearer 通道**等价**：认证通过后走完全相同的身份合成与权限判定，下文所有接口都可改用签名头调用（把示例里的 `-H "Authorization: Bearer $KEY"` 换成签名头即可）。签名通道**不能**放进不可信的公开浏览器前端——SK 一旦落入终端用户可控环境即可被提取，签名机制只防重放/防篡改/免明文上行，不解决"密钥被提取"。

---

## 2. 准备：签发代理 Key（一次性，管理员操作）

代理 Key 的签发属平台操作，**只能用超级管理员 JWT 调用**。

### 2.1 超管登录拿 JWT

`POST /api/auth/login`

```bash
curl -X POST $BASE/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"superadmin","password":"<口令>"}'
```

响应：

```json
{ "access_token": "eyJ...", "token_type": "bearer", "must_change_password": false, "is_super_admin": true }
```

### 2.2 签发代理 Key

`POST /api/api-keys/external-agent`（仅 Super_Admin）

```bash
curl -X POST $BASE/api/api-keys/external-agent \
  -H "Authorization: Bearer <超管JWT>" \
  -H "Content-Type: application/json" \
  -d '{"name":"第三方系统代理Key"}'
```

响应（明文 `key`、`secret_key` 均仅此一次，请妥善保存）：

```json
{
  "id": "835d75f1-...",
  "key": "sk-81a95f366177e3fa63de12fdc34f52215fa55555dd390ed9",
  "prefix": "sk-81a95f36...",
  "name": "第三方系统代理Key",
  "key_type": "external_agent",
  "created_at": "2026-07-01T03:11:16.642270",
  "access_key": "835d75f1-...",
  "secret_key": "9f3c...（仅此一次，AK/SK 签名通道用，见 1.5）"
}
```

两种用法二选一：

- **Bearer 明文通道**：设 `KEY=sk-...`，用 `Authorization: Bearer $KEY` + `X-External-User-Id` 调下文全部业务接口。
- **AK/SK 签名通道**（推荐用于无后端的可信直连）：用 `access_key` / `secret_key` 按 1.5 对每次请求签名，密钥不逐请求上行。

### 2.3 撤销代理 Key（仅 Super_Admin）

`DELETE /api/api-keys/{key_id}`

```bash
curl -X DELETE $BASE/api/api-keys/835d75f1-... \
  -H "Authorization: Bearer <超管JWT>"
```

响应：`{ "message": "API Key 已撤销", "id": "835d75f1-..." }`

---

## 3. 知识库

以下接口全部用代理 Key + `X-External-User-Id` 调用。

### 3.1 创建知识库

`POST /api/knowledge-bases`

请求字段：`name`（必填）、`description`、`config`、`visibility`（`private`|`organization`，默认 `private`）、`org_permission`（`read`|`write`，仅 organization 有效）。

```bash
curl -X POST $BASE/api/knowledge-bases \
  -H "Authorization: Bearer $KEY" \
  -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"name":"Alice私有库","description":"产品手册","visibility":"private"}'
```

响应 `201`（记下 `id` 作为后续 `<kb_id>`）：

```json
{
  "id": "kb-3f2a...",
  "name": "Alice私有库",
  "description": "产品手册",
  "config": null,
  "doc_count": 0,
  "created_at": "2026-07-01T03:20:00Z",
  "updated_at": "2026-07-01T03:20:00Z",
  "visibility": "private",
  "owner_user_id": "eu-9c1d...",
  "org_permission": "read"
}
```

### 3.2 知识库列表

`GET /api/knowledge-bases`

查询参数：`page`、`page_size`、`relation`（`mine`|`shared`|`org`|`others`）、`sort`（`recommended`|`updated`|`created`|`name`|`docs`）、`q`（名称搜索）。

```bash
curl "$BASE/api/knowledge-bases?page=1&page_size=20&sort=updated" \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

响应：

```json
{
  "items": [
    { "id": "kb-3f2a...", "name": "Alice私有库", "doc_count": 2, "visibility": "private",
      "relation": "mine", "can_write": true, "capacity": { "used_chunks": 120, "total_chunks": 100000, "percent": 0.0012 } }
  ],
  "total": 1, "page": 1, "page_size": 20, "has_more": false
}
```

### 3.3 知识库详情

`GET /api/knowledge-bases/{kb_id}`

```bash
curl $BASE/api/knowledge-bases/<kb_id> \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

响应含 `capacity`（容量进度）与 `can_write`（当前身份是否可写）。

### 3.4 更新知识库（仅 owner）

`PUT /api/knowledge-bases/{kb_id}`　字段：`name`、`description`、`config`（均可选）。

```bash
curl -X PUT $BASE/api/knowledge-bases/<kb_id> \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"name":"Alice产品库(改名)"}'
```

### 3.5 删除知识库（仅 owner）

`DELETE /api/knowledge-bases/{kb_id}` → `204`

```bash
curl -X DELETE $BASE/api/knowledge-bases/<kb_id> \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 3.6 设置可见性（仅 owner）

`PUT /api/knowledge-bases/{kb_id}/visibility`　字段：`visibility`（必填）、`org_permission`（可选）。

```bash
curl -X PUT $BASE/api/knowledge-bases/<kb_id>/visibility \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"visibility":"organization","org_permission":"read"}'
```

> 说明：设为 `organization` 后，本库对「外部用户租户」内的其他外部用户可见（只读或读写取决于 `org_permission`）。若要严格按终端用户隔离，保持 `private`。

---

## 4. 文档

### 4.1 上传文档（需写权限）

`POST /api/knowledge-bases/{kb_id}/documents/upload`（multipart）

表单字段：`file`（必填）、`folder_id`（可选，query 或 form）。支持类型：`pdf, docx, xlsx, pptx, csv, txt, md, jpg, jpeg, png, mp3, wav, m4a, flac, ogg`。

```bash
curl -X POST "$BASE/api/knowledge-bases/<kb_id>/documents/upload" \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -F "file=@/path/to/manual.pdf"
```

响应 `201`（`status` 初始 `pending`，后台异步解析）：

```json
{
  "id": "doc-71bc...", "kb_id": "kb-3f2a...", "filename": "manual.pdf", "file_type": "pdf",
  "file_size": 820113, "status": "pending", "error_message": null, "chunk_count": 0,
  "progress": 0, "progress_message": null, "source_url": null, "created_at": "2026-07-01T03:25:00Z"
}
```

### 4.2 从 URL 转存网页（需写权限）

`POST /api/knowledge-bases/{kb_id}/documents/from-url`　字段：`url`（必填）、`folder_id`（可选）。

```bash
curl -X POST $BASE/api/knowledge-bases/<kb_id>/documents/from-url \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com/article"}'
```

### 4.3 文档列表

`GET /api/knowledge-bases/{kb_id}/documents?page=1&page_size=20`

```bash
curl "$BASE/api/knowledge-bases/<kb_id>/documents?page=1&page_size=20" \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

响应为分页结构，`items` 为 `DocumentResponse` 数组。

### 4.4 文档详情（轮询解析状态）

`GET /api/documents/{doc_id}`

```bash
curl $BASE/api/documents/<doc_id> \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

`status` 流转：`pending → processing → completed`（或 `failed`）。`completed` 时 `chunk_count > 0`、`progress=100`，即可检索。

### 4.5 重试解析（需写权限）

`POST /api/documents/{doc_id}/retry`

```bash
curl -X POST $BASE/api/documents/<doc_id>/retry \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 4.6 删除文档（需写权限）

`DELETE /api/documents/{doc_id}` → `204`

```bash
curl -X DELETE $BASE/api/documents/<doc_id> \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 4.7 批量重试 / 批量删除（需写权限）

`POST /api/documents/batch-retry`、`POST /api/documents/batch-delete`　字段：`doc_ids`（数组）。

```bash
curl -X POST $BASE/api/documents/batch-retry \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"doc_ids":["doc-71bc...","doc-82cd..."]}'

curl -X POST $BASE/api/documents/batch-delete \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"doc_ids":["doc-71bc..."]}'
```

### 4.8 文档切片列表

`GET /api/documents/{doc_id}/chunks?page=1&page_size=20`

```bash
curl "$BASE/api/documents/<doc_id>/chunks?page=1&page_size=20" \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

响应 `items` 为 `ChunkResponse`（`id`/`content`/`chunk_index`/`children` 等）。

### 4.9 文档原件 / 预览

`GET /api/documents/{doc_id}/raw`（原件字节流）、`GET /api/documents/{doc_id}/preview`（预览）。

```bash
curl $BASE/api/documents/<doc_id>/raw \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" --output manual.pdf
```

### 4.10 文档抽取事件

`GET /api/documents/{doc_id}/events?limit=50`

```bash
curl "$BASE/api/documents/<doc_id>/events?limit=50" \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 4.11 按 fileId 获取解析后原文文本（统一入口）

`GET /api/files/{file_id}/content`

按 **fileId 统一取解析后的原文文本**，自动识别两类来源，第三方无需预先知道该 id 是哪一类：

- **KB 文档**（`Document.id`，如 references / 上传回执里的 `doc_id`）；
- **会话临时文件**（`SessionFile.id`，如第 8 节上传返回的 `id`）。

两类 id 均为全局唯一 UUID，服务端按「先文档、后会话文件」顺序解析并回显命中的 `source`。返回的是**已解析的可读文本**（父块按顺序拼接），与原件字节流 `/raw` 区分，无需二次解析。文件未建索引完成（`status != completed`）时 `content` 可能为空串，建议等 `completed` 后再取。

```bash
curl $BASE/api/files/<file_id>/content \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

响应：

```json
{
  "file_id": "doc-71bc...",
  "source": "document",
  "filename": "manual.pdf",
  "file_type": "pdf",
  "status": "completed",
  "content": "第一段父块文本…\n\n第二段父块文本…"
}
```

- `source`：`document`（KB 文档）或 `session_file`（会话临时文件）。
- `content`：完整解析原文（父块按 `chunk_index` 有序、以空行 `\n\n` 拼接）。
- 鉴权与隔离：KB 文档跨租户不可见即 `404`；会话临时文件叠加归属校验（仅本人可读，外部用户之间互不可见）。id 不存在 / 无权 → `404`（存在性非泄露）。
- 如只需要 KB 文档的分块（可分页、含子块高亮），仍可用 4.8 的 `GET /api/documents/{doc_id}/chunks`。

---

## 5. 文件夹

### 5.1 文件夹列表

`GET /api/knowledge-bases/{kb_id}/folders?parent_id=<可选>&page=1&page_size=20`

```bash
curl "$BASE/api/knowledge-bases/<kb_id>/folders?page=1&page_size=20" \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 5.2 创建文件夹（需写权限）

`POST /api/knowledge-bases/{kb_id}/folders`　字段：`name`（必填）、`parent_id`（可选）。

```bash
curl -X POST $BASE/api/knowledge-bases/<kb_id>/folders \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"name":"合同","parent_id":null}'
```

响应 `201` 为 `FolderResponse`（`id`/`kb_id`/`parent_id`/`name`/`doc_count`/`subfolder_count`）。

### 5.3 更新文件夹（需写权限）

`PUT /api/folders/{folder_id}`　字段：`name`、`parent_id`（均可选）。

```bash
curl -X PUT $BASE/api/folders/<folder_id> \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"name":"2026合同"}'
```

### 5.4 删除文件夹（需写权限）

`DELETE /api/folders/{folder_id}` → `204`

```bash
curl -X DELETE $BASE/api/folders/<folder_id> \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 5.5 移动文件/文件夹（需写权限）

`POST /api/knowledge-bases/{kb_id}/move`　字段：`item_ids`（数组）、`item_type`（`folder`|`document`）、`target_folder_id`（可选，null=根目录）。

```bash
curl -X POST $BASE/api/knowledge-bases/<kb_id>/move \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"item_ids":["doc-71bc..."],"item_type":"document","target_folder_id":"<folder_id>"}'
```

### 5.6 面包屑路径

`GET /api/knowledge-bases/{kb_id}/folders/{folder_id}/breadcrumb`

```bash
curl $BASE/api/knowledge-bases/<kb_id>/folders/<folder_id>/breadcrumb \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

响应：`[{ "id": null, "name": "根目录" }, { "id": "<folder_id>", "name": "合同" }]`

---

## 6. 检索与问答

### 6.1 纯检索召回（不经 LLM）

单轮召回，只返回命中的 chunk 及多维分数信号，不经 LLM 生成。两个等价路径：

- `POST /api/retrieval/search`：**对外集成推荐**，语义为「检索召回」。
- `POST /api/retrieval/test`：能力与 `/search` 完全一致（同一底层实现），保留供前端调参页调用。

字段：`query`（必填）、`mode`（`direct`|`hybrid`，默认 `hybrid`）、`top_k`（默认 10），以及检索范围（下列三者可组合，**至少提供其一**）：

- `knowledge_base_id`：单知识库 ID（与 `kb_ids` 二选一）。
- `kb_ids`：多知识库联合检索的知识库 ID 列表（与 `knowledge_base_id` 二选一）。
- `session_id`：把该会话**已上传的附件**作为一路检索源并入召回，**须为调用者本人会话**（非本人返回 404）。可单独使用，也可与知识库联合。

模式说明：

- `direct`：仅稠密向量单路召回，最快，`trace` 为 `null`。**仅在单库单源时生效。**
- `hybrid`：三路混合（Dense + Sparse + BM25）+ RRF + Rerank + MMR + 父块扩展。**当平台开启图谱（`GRAPH_ENABLE`）且图存储（Neo4j）可用时，自动并入图谱召回第四路（`graph`）**，与生产问答链路 `hybrid` 召回口径一致；未开启图谱时行为与三路完全相同。

多源说明：当指定 `kb_ids`（多库）或 `session_id`（会话附件）时，统一走**多源混合召回**（与问答链路同口径，各源同权、统一 rerank），此时 `trace` 为 `null`，改由响应顶层的 `degraded`（是否有源检索失败）与 `failed_source_count`（失败源数量）反映召回完整性。仅传 `session_id` 但该会话无附件时返回空结果（非错误）。

```bash
# 单库（保留完整 trace）
curl -X POST $BASE/api/retrieval/search \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"query":"保修期多久","knowledge_base_id":"<kb_id>","mode":"hybrid","top_k":10}'

# 多库 + 会话附件联合召回
curl -X POST $BASE/api/retrieval/search \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"query":"保修期多久","kb_ids":["<kb_id_1>","<kb_id_2>"],"session_id":"<session_id>","mode":"hybrid","top_k":10}'
```

响应：

```json
{
  "query": "保修期多久", "mode": "hybrid", "total": 3, "elapsed_ms": 148,
  "results": [
    { "chunk_id": "ck-1", "doc_id": "doc-71bc...", "filename": "manual.pdf",
      "source_type": "knowledge_base",
      "content": "父块文本…", "child_content": "命中子块…", "score": 0.83,
      "rrf_score": 0.031, "rerank_score": 0.79, "routes": ["dense","bm25","graph"] }
  ],
  "trace": {
    "routes": [
      { "name":"dense","recalled":20,"enabled":true },
      { "name":"sparse","recalled":20,"enabled":true },
      { "name":"bm25","recalled":18,"enabled":true },
      { "name":"graph","recalled":6,"enabled":true }
    ],
    "funnel": [{ "stage":"Rerank 输出","count":10 }]
  },
  "degraded": false,
  "failed_source_count": 0
}
```

> `routes` 中 `graph` 项的 `enabled` 反映本次是否注入了图谱第四路（平台未开启图谱 / 图存储不可用时为 `false`、`recalled` 为 0）。

> 每条结果的 `source_type` 标注命中来源：`knowledge_base`（知识库文档）或 `session`（会话附件）。需要**原件**时，前端据此按 `doc_id` 选对接口：知识库文档 `GET /api/documents/{doc_id}/raw`，会话附件 `GET /api/sessions/{session_id}/files/{doc_id}/raw`。

多源（多库或含会话附件）响应：`trace` 为 `null`，命中项可能混含 `knowledge_base` 与 `session` 两类来源，顶层 `degraded` / `failed_source_count` 反映是否有源检索失败。

```json
{
  "query": "保修期多久", "mode": "hybrid", "total": 2, "elapsed_ms": 176,
  "results": [
    { "chunk_id": "ck-1", "doc_id": "doc-71bc...", "filename": "manual.pdf",
      "source_type": "knowledge_base",
      "content": "父块文本…", "child_content": "命中子块…", "score": 0.83,
      "rrf_score": 0.031, "rerank_score": 0.79, "routes": ["dense","bm25"] },
    { "chunk_id": "ck-9", "doc_id": "file-2a3d...", "filename": "合同.pdf",
      "source_type": "session",
      "content": "会话附件父块…", "child_content": "命中子块…", "score": 0.77,
      "rrf_score": 0.028, "rerank_score": 0.74, "routes": ["dense"] }
  ],
  "trace": null,
  "degraded": false,
  "failed_source_count": 0
}
```

### 6.1.1 Agent 检索召回（多步推理召回）

`POST /api/retrieval/agent`

区别于 6.1 的单轮召回：跑 ReAct Agent 引擎，围绕问题**多步检索、反思、改写子查询**后汇聚证据，返回其召回的引用来源与最终作答。召回口径（含图谱第四路）与 `/v1/chat/completions` 的 `agent` 模式一致。无会话概念（不落库、不加载历史、不接入会话临时文件），一次请求一个独立推理链。

字段：`query`（必填）、`knowledge_base_id` 或 `kb_ids`（二选一，至少其一）、`agent_preset_id`（可选）、`model_config_id`（可选）。

```bash
curl -X POST $BASE/api/retrieval/agent \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"query":"保修期多久，超期如何收费","knowledge_base_id":"<kb_id>"}'
```

响应：

```json
{
  "query": "保修期多久，超期如何收费",
  "answer": "根据检索到的资料，保修期为 12 个月……",
  "references": [
    { "doc_id":"doc-71bc...", "chunk_id":"ck-1", "filename":"manual.pdf",
      "content":"父块…", "child_content":"子块…", "score":0.83 }
  ],
  "agent_steps": [
    { "type":"thought", "content":"先检索保修期，再检索超期收费" },
    { "type":"tool_call", "name":"knowledge_search" },
    { "type":"tool_result" },
    { "type":"final_answer" }
  ],
  "degraded": false,
  "elapsed_ms": 2360
}
```

> `agent_steps` 用于在第三方界面还原 Agent 的检索/推理过程；只需召回来源时取 `references` 即可。

### 6.2 对话问答（OpenAI 兼容）

`POST /v1/chat/completions`（别名 `POST /api/chat/completions`）

请求字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `model` | string | 占位，默认 `"rag"` |
| `messages` | array | `{role, content}`，`role`∈`system/user/assistant` |
| `stream` | bool | 是否 SSE 流式，默认 `false` |
| `knowledge_base_id` | string | 单库检索 |
| `kb_ids` | string[] | 多库联合检索 |
| `retrieval_mode` | string | `direct`/`hybrid`/`agent`，空则用预设或默认 `agent` |
| `model_config_id` | string | 指定 LLM 配置 ID |
| `agent_preset_id` | string | Agent 预设 ID |
| `filter_doc_ids` | string[] | 限定文档范围 |
| `session_id` | string | 传入则自动加载历史并落库（见 6.4） |
| `temperature` / `max_tokens` | number | 生成参数 |

**非流式示例：**

```bash
curl -X POST $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{
        "model":"rag",
        "messages":[{"role":"user","content":"产品的保修期是多久？"}],
        "stream":false,
        "knowledge_base_id":"<kb_id>",
        "retrieval_mode":"hybrid"
      }'
```

响应：

```json
{
  "id": "chatcmpl-xxx", "object": "chat.completion",
  "choices": [{ "index":0, "message":{ "role":"assistant", "content":"根据[1]，保修期为12个月……" }, "finish_reason":"stop" }],
  "usage": { "prompt_tokens":12, "completion_tokens":80, "total_tokens":92 },
  "references": [{ "doc_id":"doc-71bc...", "chunk_id":"ck-1", "filename":"manual.pdf", "content":"父块…", "child_content":"子块…", "score":0.83 }],
  "metadata": { "retrieval_mode":"hybrid", "degraded":false }
}
```

### 6.3 流式问答（SSE）

设 `"stream": true`，返回 `text/event-stream`，逐条 `data:` 为一段 JSON：

- 增量：`{"id":...,"object":"chat.completion.chunk","choices":[{"delta":{"content":"片段"}}]}`
- 结束：`choices[0].finish_reason == "stop"`
- 引用与元数据：`{"references":[...],"metadata":{...}}`
- 落库回执（传了 `session_id` 时）：`{"type":"message_saved","message_id":"..."}`
- `agent` 模式额外推送：`{"type":"thought",...}` / `{"type":"tool_call",...}` / `{"type":"tool_result",...}` / `{"type":"final_answer",...}`

```bash
curl -N -X POST $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"model":"rag","messages":[{"role":"user","content":"你好"}],"stream":true,"kb_ids":["<kb_id>"],"retrieval_mode":"agent"}'
```

### 6.4 多轮对话（平台托管历史）

传 `session_id`，平台自动加载该会话最近 N 轮（默认 10 轮）历史拼进上下文，并把本轮 user 消息与 assistant 回答落库。第三方每轮只需发当前这一条 user 消息。会话严格按 `X-External-User-Id` 隔离。

```bash
# 第 1 轮
curl -X POST $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"model":"rag","messages":[{"role":"user","content":"保修期多久？"}],"knowledge_base_id":"<kb_id>","retrieval_mode":"hybrid","session_id":"<session_id>"}'

# 第 2 轮（追问，无需重复上文）
curl -X POST $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"model":"rag","messages":[{"role":"user","content":"那超过保修期怎么收费？"}],"knowledge_base_id":"<kb_id>","retrieval_mode":"hybrid","session_id":"<session_id>"}'
```

> 也可完全自管历史：不传 `session_id`，自行把多轮上下文按顺序塞进 `messages` 数组。

---

## 7. 会话管理

会话按 `X-External-User-Id` 严格隔离（他人 `session_id` 一律 `404`）。

### 7.1 创建会话

`POST /api/sessions`　字段：`title`（默认「新对话」）、`kb_id`（可选）、`model_config_id`（可选）。

```bash
curl -X POST $BASE/api/sessions \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"title":"保修咨询","kb_id":"<kb_id>"}'
```

响应含 `id`（作为 `<session_id>`）。

### 7.2 会话列表

`GET /api/sessions`

```bash
curl $BASE/api/sessions \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 7.3 会话消息

`GET /api/sessions/{session_id}/messages`

```bash
curl $BASE/api/sessions/<session_id>/messages \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

每条含 `id`/`role`/`content`/`references`/`created_at`，可据此在第三方界面还原带溯源的对话。

### 7.4 重命名会话

`PUT /api/sessions/{session_id}`　字段：`title`。

```bash
curl -X PUT $BASE/api/sessions/<session_id> \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" -d '{"title":"保修政策咨询"}'
```

### 7.5 删除会话

`DELETE /api/sessions/{session_id}`

```bash
curl -X DELETE $BASE/api/sessions/<session_id> \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 7.6 清空会话消息

`DELETE /api/sessions/{session_id}/messages`

```bash
curl -X DELETE $BASE/api/sessions/<session_id>/messages \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 7.7 消息反馈（点赞/踩）

`PUT /api/sessions/{session_id}/messages/{message_id}/feedback`　字段：`feedback`（`like`|`dislike`|`null`）。

```bash
curl -X PUT $BASE/api/sessions/<session_id>/messages/<message_id>/feedback \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" -d '{"feedback":"like"}'
```

### 7.8 重试最新一轮

`POST /api/sessions/{session_id}/messages/retry`

```bash
curl -X POST $BASE/api/sessions/<session_id>/messages/retry \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

---

## 8. 会话临时文件（传一个文件立刻问它）

在某会话内上传文件并建索引，可直接在该会话问答中被检索。前缀 `/api/sessions/{session_id}/files`。

**上传为「秒回 + 后台异步建索引」模型**：上传接口在完成落盘 + 存储原件 + 建 `queued` 记录 + 入队后**立即返回 `202`**，不等待解析/切分/向量化。建索引进度由独立后台进程推进，客户端有两种方式获取进展：

- **WebSocket 实时推送**（推荐，见 8.5）：`queued → processing → progress(×N) → completed/failed`。
- **HTTP 轮询兜底**（见 8.2）：轮询会话文件列表，读取每个文件的 `status/progress/error_message`。

文件 `status` 取值：`queued`（已入队待处理）→ `processing`（建索引中）→ `completed`（就绪可检索）/ `failed`（失败，见 `error_message`）。仅 `completed` 后该文件才会参与该会话问答的检索召回。

### 8.1 上传会话文件（秒回，异步建索引）

`POST /api/sessions/{session_id}/files`（multipart，`file`）

```bash
curl -X POST $BASE/api/sessions/<session_id>/files \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -F "file=@/path/to/contract.pdf"
```

响应 `202`（`status` 初始为 `queued`，`chunk_count=0`、`progress=0`；建索引在后台进行）：

```json
{ "id": "sf-9a2b...", "session_id": "<session_id>", "filename": "contract.pdf",
  "file_type": "pdf", "file_size": 33120, "chunk_count": 0, "status": "queued",
  "progress": 0, "progress_message": null, "error_message": null,
  "created_at": "2026-07-01T04:00:00Z" }
```

错误：文件类型不支持 → `400`；单文件过大 → `413`（不入队、不留残留）；后台队列（Redis）或对象存储暂不可用 → `503`（快速失败，不创建记录，请稍后重试）。

### 8.2 会话文件列表（轮询兜底 / 断线重连对账）

`GET /api/sessions/{session_id}/files`

```bash
curl $BASE/api/sessions/<session_id>/files \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

响应为文件数组，每项含最新 `status / progress / progress_message / error_message / chunk_count`，可作为不便使用 WebSocket 时的轮询入口、以及 WS 断线重连后的状态对账入口：

```json
[
  { "id": "sf-9a2b...", "session_id": "<session_id>", "filename": "contract.pdf",
    "file_type": "pdf", "file_size": 33120, "chunk_count": 18, "status": "completed",
    "progress": 100, "progress_message": null, "error_message": null,
    "created_at": "2026-07-01T04:00:00Z" }
]
```

### 8.2.1 会话文件列表（含解析原文内容）

`GET /api/sessions/{session_id}/files/with-content`

与 8.2 相同的列表语义（仅本人可见、按上传时间倒序），但**每项额外携带该文件解析后的原文文本**：`content`（父块按序拼接的完整原文）与 `chunks`（父块粒度文本数组）。适合「一次拉取即拿到所有附件正文」的场景（如批量喂给自有模型 / 一并展示原文），省去逐个文件再调 8.4.1 的往返。

未建索引完成（`status != completed`）的文件其 `content` 为空串、`chunks` 为空数组。内容体积可能较大，若只需元数据请用 8.2 的不带内容列表。

```bash
curl $BASE/api/sessions/<session_id>/files/with-content \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

响应（在 8.2 各字段基础上，每项多出 `content` / `chunks`）：

```json
[
  { "id": "sf-9a2b...", "session_id": "<session_id>", "filename": "contract.pdf",
    "file_type": "pdf", "file_size": 33120, "chunk_count": 18, "status": "completed",
    "progress": 100, "progress_message": null, "error_message": null,
    "created_at": "2026-07-01T04:00:00Z",
    "content": "第一段父块文本…\n\n第二段父块文本…",
    "chunks": ["第一段父块文本…", "第二段父块文本…"] }
]
```

### 8.3 删除会话文件

`DELETE /api/sessions/{session_id}/files/{file_id}` → `204`

```bash
curl -X DELETE $BASE/api/sessions/<session_id>/files/<file_id> \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

删除后会向订阅该会话的 WebSocket 推送一条 `file.removed` 事件（见 8.5）。

### 8.4 获取会话文件原件

`GET /api/sessions/{session_id}/files/{file_id}/raw`

```bash
curl $BASE/api/sessions/<session_id>/files/<file_id>/raw \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" --output contract.pdf
```

> 需要拿某个会话文件**解析后的原文文本**（而非二进制原件）时，用统一入口 `GET /api/files/{file_id}/content`（见 4.11）——它对 KB 文档与会话临时文件两类 id 都适用。

### 8.5 会话文件状态实时推送（WebSocket）

`WS /api/sessions/{session_id}/files/events`

实时订阅该会话所有文件的建索引状态。连接建立后服务端**先推一帧当前快照**（该会话所有文件的最新状态），随后按需增量推送状态事件；服务端会周期发送 `ping` 保活。会话严格按 `X-External-User-Id` 隔离，仅归属主体可订阅。

**鉴权（握手前完成）**：浏览器/客户端无法自定义 WebSocket 请求头，凭据经 query 参数传入。两种通道二选一：

_通道一：明文 token_

| query 参数 | 必填 | 说明 |
| --- | --- | --- |
| `access_token` | 是 | 代理 Key 明文（`sk-...`）；亦可回退 `Authorization: Bearer` 头 |
| `external_user_id` | 是（代理 Key） | 你方终端用户唯一标识；亦可回退 `X-External-User-Id` 头 |

```bash
wscat -c "ws://localhost:8000/api/sessions/<session_id>/files/events?access_token=$KEY&external_user_id=$EU"
```

_通道二：AK/SK 签名_（见 1.5，query 带 `ak`+`sign` 即走此通道）

| query 参数 | 必填 | 说明 |
| --- | --- | --- |
| `ak` / `ts` / `nonce` / `sign` | 是 | 签名四要素；WS 握手为 GET，故经 query 传入 |
| `external_user_id` | 是（代理 Key） | 你方终端用户唯一标识（已并入签名） |

签名串与 1.5 相同，但 **WS 的 `path` 取握手路径、`query` 固定为空串**（签名无法覆盖含自身的 query，资源由 path 内 `session_id` 唯一确定）：

```
GET\n/api/sessions/<session_id>/files/events\n\n<ts>\n<nonce>\n<external_user_id>
```

```bash
# ts/nonce/sign 由你方按上式对空 query 计算
wscat -c "ws://localhost:8000/api/sessions/<session_id>/files/events?ak=$AK&ts=$TS&nonce=$NONCE&sign=$SIGN&external_user_id=$EU"
```

**快照帧**（连接后首帧）：

```json
{
  "type": "snapshot",
  "session_id": "<session_id>",
  "files": [
    { "file_id": "sf-9a2b...", "filename": "contract.pdf", "status": "processing",
      "progress": 42, "progress_message": "正在解析与切分", "error_message": null, "chunk_count": 0 }
  ]
}
```

**增量事件帧**（`type` 取值 `queued` / `processing` / `progress` / `completed` / `failed` / `removed`）：

```json
{ "type": "progress", "session_id": "<session_id>", "file_id": "sf-9a2b...",
  "filename": "contract.pdf", "status": "processing", "progress": 60,
  "stage": "embed", "message": "正在向量化", "chunk_count": null, "error": null, "ts": 1751342400.12 }
```

- 完成：`{"type":"completed", "status":"completed", "progress":100, "chunk_count":18, ...}`
- 失败：`{"type":"failed", "status":"failed", "error":"<原因>", ...}`
- 移除：`{"type":"removed", "file_id":"...", ...}`
- 保活：`{"type":"ping", "ts":...}`（客户端忽略即可）

**关闭码矩阵**（4xxx 为应用自定义区）：

| close code | 含义 |
| --- | --- |
| `4401` | 未认证 / token 无效或过期 |
| `4400` | 代理 Key 缺 `external_user_id` |
| `4403` | 账号需强制改密后才能使用 |
| `4404` | 会话不存在 / 非本人（存在性非泄露） |
| `4429` | 单会话连接数超上限 |

客户端遇 `4401/4400/4403/4404/4429` 属永久失败，**不应重连**；遇网络类关闭建议按指数退避重连，重连后依赖首帧快照或 `GET .../files` 做状态对账。

> 上传后，在该会话的 `/v1/chat/completions`（带同一 `session_id`）中，会话文件建索引 `completed` 后会作为一路检索源自动参与召回。上传处于 `queued/processing` 期间该文件尚未可检索，建议等 `completed` 事件到达后再提问，或直接轮询列表确认。

---

## 9. 知识图谱（前缀 `/api/kb`）

### 9.1 图谱总览 / ego 子图

`GET /api/kb/{kb_id}/graph`　参数：`mode`（`overview`|`ego`）、`center`（ego 必填，实体 id 或名）、`depth`、`types`（逗号分隔）、`limit`、`include_events`。

```bash
# 总览
curl "$BASE/api/kb/<kb_id>/graph?mode=overview&limit=50" \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"

# ego 邻居子图
curl "$BASE/api/kb/<kb_id>/graph?mode=ego&center=保修政策&depth=1" \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

响应：`{ "nodes": [...], "edges": [...], "meta": { "mode":"overview", "total":n, "returned":n, "truncated":false } }`

### 9.2 图谱统计

`GET /api/kb/{kb_id}/graph/stats`

```bash
curl $BASE/api/kb/<kb_id>/graph/stats \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 9.3 实体详情

`GET /api/kb/{kb_id}/graph/entity/{entity_id}`

```bash
curl $BASE/api/kb/<kb_id>/graph/entity/<entity_id> \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 9.4 更新图谱配置（需写权限）

`PUT /api/kb/{kb_id}/graph/config`　字段（均可选，部分更新）：`enabled`、`entity_types`、`relation_types`、`extract_granularity`（`parent`|`child`）、`extract_model_id`、`enable_alias_dedup`、`alias_sim_threshold`。

```bash
curl -X PUT $BASE/api/kb/<kb_id>/graph/config \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU" \
  -H "Content-Type: application/json" \
  -d '{"enabled":true,"extract_granularity":"parent"}'
```

### 9.5 图谱任务台账

`GET /api/kb/{kb_id}/graph/jobs?limit=20`

```bash
curl "$BASE/api/kb/<kb_id>/graph/jobs?limit=20" \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

### 9.6 全库重建图谱（需写权限）

`POST /api/kb/{kb_id}/graph/rebuild`

```bash
curl -X POST $BASE/api/kb/<kb_id>/graph/rebuild \
  -H "Authorization: Bearer $KEY" -H "X-External-User-Id: $EU"
```

> 图谱能力需平台开启 `GRAPH_ENABLE` 且图存储（Neo4j）可用；否则相关端点返回降级提示。

---

## 10. 系统健康检查（公开）

`GET /api/system/health`（无需鉴权）

```bash
curl $BASE/api/system/health
```

响应：

```json
{ "status": "ok", "services": { "database": "ok", "milvus": "ok", "llm": "ok" } }
```

---

## 11. 集成自检清单

1. `GET /api/system/health` → `200`，`database`/`milvus` 为 `ok`
2. 超管 `POST /api/auth/login` → 拿 JWT
3. 用 JWT `POST /api/api-keys/external-agent` → 拿 `sk-...`
4. 用 `sk-...` + `X-External-User-Id: alice-001` `POST /api/knowledge-bases` 建库 → `201`
5. `POST .../documents/upload` 传一个文件 → `201`，轮询 `GET /api/documents/{id}` 至 `completed`
6. `POST /v1/chat/completions` 指定该库 → 拿到带 `references` 的回答
7. 隔离校验：换 `X-External-User-Id: bob-002` 访问 alice 的库/会话 → `404`

---

## 12. 安全建议

- 代理 Key（含明文 `key` 与签名 `secret_key`）等同密码，仅可信调用方持有，切勿下发到浏览器/移动端等**不可信前端**。AK/SK 签名只防重放/防篡改/免明文上行，**不解决 SK 被提取**——公开前端场景无解，需自建一层可信代理。
- 优先用 **AK/SK 签名通道**（1.5）替代明文 Bearer：网络与日志里不再出现长期密钥，只有每次现算的签名。
- `X-External-User-Id` 由调用方按当前登录用户注入，不要让终端用户可篡改（签名通道下其值已并入签名，可防在途篡改，但持有 SK 者仍可自行改签，故仍以可信环境为前提）。
- Key 泄露后立即撤销（`DELETE /api/api-keys/{id}`）并重签；撤销后其 AK/SK 签名同时失效。
- 生产环境建议收敛 CORS（当前默认 `allow_origins=["*"]`）并启用 HTTPS（签名不纳入 body，body 完整性依赖 TLS）。
