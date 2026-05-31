// API 客户端：统一请求封装

import { authHeaders, handleUnauthorized } from './auth'

const BASE_URL = '/api'

// 通用请求方法
async function request<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${BASE_URL}${endpoint}`
  const response = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(options.headers as Record<string, string> | undefined),
    },
    ...options,
  })

  if (!response.ok) {
    // 401：清除登录态并跳转登录页（展示层防御；真正鉴权在后端）
    if (response.status === 401) {
      handleUnauthorized()
    }
    const error = await response.json().catch(() => ({}))
    throw new Error(error.detail || `请求失败: ${response.status}`)
  }

  // 204 No Content 无响应体
  if (response.status === 204) {
    return undefined as T
  }

  return response.json()
}

// 通用分页响应结构（与后端 PageResult 对应，用于滚动加载）
export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  page_size: number
  has_more: boolean
}

// 知识库相关接口
export const knowledgeBaseApi = {
  list: (params?: { page?: number; page_size?: number }) =>
    request<PageResult<unknown>>(
      `/knowledge-bases?page=${params?.page ?? 1}&page_size=${params?.page_size ?? 20}`
    ),
  get: (id: string) => request<unknown>(`/knowledge-bases/${id}`),
  create: (data: unknown) =>
    request<unknown>('/knowledge-bases', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: unknown) =>
    request<unknown>(`/knowledge-bases/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/knowledge-bases/${id}`, { method: 'DELETE' }),
}

// 文档相关接口
export const documentApi = {
  list: (
    kbId: string,
    folderId?: string | null,
    params?: { page?: number; page_size?: number }
  ) => {
    const qs = new URLSearchParams()
    if (folderId) qs.set('folder_id', folderId)
    qs.set('page', String(params?.page ?? 1))
    qs.set('page_size', String(params?.page_size ?? 20))
    return request<PageResult<unknown>>(`/knowledge-bases/${kbId}/documents?${qs.toString()}`)
  },
  upload: (kbId: string, file: File, folderId?: string | null) => {
    const formData = new FormData()
    formData.append('file', file)
    const url = folderId
      ? `${BASE_URL}/knowledge-bases/${kbId}/documents/upload?folder_id=${folderId}`
      : `${BASE_URL}/knowledge-bases/${kbId}/documents/upload`
    return fetch(url, {
      method: 'POST',
      headers: authHeaders(),
      body: formData,
    }).then((res) => {
      if (res.status === 401) handleUnauthorized()
      return res.json()
    })
  },
  validateFolder: (kbId: string, paths: string[]) =>
    request<{
      supported_files: { relative_path: string; filename: string; file_type: string; supported: boolean; reason?: string }[]
      unsupported_files: { relative_path: string; filename: string; file_type: string; supported: boolean; reason?: string }[]
      folder_structure: string[]
    }>(`/knowledge-bases/${kbId}/documents/validate-folder`, {
      method: 'POST',
      body: JSON.stringify({ paths }),
    }),
  uploadFolder: (kbId: string, files: File[], paths: string[], parentFolderId?: string | null) => {
    const formData = new FormData()
    files.forEach((file) => formData.append('files', file))
    formData.append('paths', JSON.stringify(paths))
    if (parentFolderId) {
      formData.append('parent_folder_id', parentFolderId)
    }
    return fetch(`${BASE_URL}/knowledge-bases/${kbId}/documents/upload-folder`, {
      method: 'POST',
      headers: authHeaders(),
      body: formData,
    }).then(async (res) => {
      if (res.status === 401) handleUnauthorized()
      if (!res.ok) {
        const error = await res.json().catch(() => ({}))
        throw new Error(error.detail || `请求失败: ${res.status}`)
      }
      return res.json()
    }) as Promise<{
      total_files: number
      uploaded_count: number
      skipped_count: number
      created_folders: string[]
      results: { relative_path: string; filename: string; doc_id?: string; folder_id?: string; status: string; message?: string }[]
    }>
  },
  get: (id: string) => request<unknown>(`/documents/${id}`),
  delete: (id: string) =>
    request<void>(`/documents/${id}`, { method: 'DELETE' }),
  batchDelete: (docIds: string[]) =>
    request<{ deleted_count: number; total_requested: number }>('/documents/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ doc_ids: docIds }),
    }),
  batchRetry: (docIds: string[]) =>
    request<{ retried_count: number; skipped_count: number; total_requested: number }>('/documents/batch-retry', {
      method: 'POST',
      body: JSON.stringify({ doc_ids: docIds }),
    }),
  retry: (id: string) =>
    request<unknown>(`/documents/${id}/retry`, { method: 'POST' }),
  chunks: (id: string, params?: { page?: number; page_size?: number }) =>
    request<PageResult<unknown>>(
      `/documents/${id}/chunks?page=${params?.page ?? 1}&page_size=${params?.page_size ?? 20}`
    ),
}

// 文件夹相关接口
export const folderApi = {
  list: (
    kbId: string,
    parentId?: string | null,
    params?: { page?: number; page_size?: number }
  ) => {
    const qs = new URLSearchParams()
    if (parentId) qs.set('parent_id', parentId)
    qs.set('page', String(params?.page ?? 1))
    qs.set('page_size', String(params?.page_size ?? 20))
    return request<PageResult<unknown>>(`/knowledge-bases/${kbId}/folders?${qs.toString()}`)
  },
  create: (kbId: string, data: { name: string; parent_id?: string | null }) =>
    request<unknown>(`/knowledge-bases/${kbId}/folders`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (folderId: string, data: { name?: string; parent_id?: string | null }) =>
    request<unknown>(`/folders/${folderId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (folderId: string) =>
    request<void>(`/folders/${folderId}`, { method: 'DELETE' }),
  breadcrumb: (kbId: string, folderId: string) =>
    request<{ id: string | null; name: string }[]>(`/knowledge-bases/${kbId}/folders/${folderId}/breadcrumb`),
  move: (kbId: string, data: { item_ids: string[]; item_type: string; target_folder_id: string | null }) =>
    request<unknown>(`/knowledge-bases/${kbId}/move`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
}

// 检索测试接口（纯检索，不经过 LLM 生成）
export interface RetrievalResultItem {
  chunk_id: string
  doc_id: string
  filename: string
  content: string
  child_content: string
  score: number
  rrf_score: number | null
  rerank_score: number | null
  routes: string[]
  metadata?: Record<string, unknown>
}

export interface RetrievalTrace {
  routes: { name: string; recalled: number; enabled: boolean }[]
  funnel: { stage: string; count: number }[]
}

export interface RetrievalTestResponse {
  query: string
  mode: string
  total: number
  elapsed_ms: number
  results: RetrievalResultItem[]
  trace: RetrievalTrace | null
}

export const retrievalApi = {
  test: (data: { query: string; knowledge_base_id: string; mode?: string; top_k?: number }) =>
    request<RetrievalTestResponse>('/retrieval/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
}

// API Key 相关接口
export const apiKeyApi = {
  list: () => request<{ items: unknown[]; total: number }>('/api-keys').then(res => res.items),
  create: (data: { name?: string }) =>
    request<unknown>('/api-keys', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/api-keys/${id}`, { method: 'DELETE' }),
}

// 系统配置接口
export const systemApi = {
  health: () => request<unknown>('/system/health'),
  getConfig: () => request<unknown>('/system/config'),
  updateConfig: (data: unknown) =>
    request<unknown>('/system/config', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  getFrontendConfig: () =>
    request<{ upload_max_concurrent: number; upload_max_file_size_mb: number }>('/system/frontend-config'),
}

// LLM 模型配置接口
export const llmConfigApi = {
  list: (chatVisible?: boolean) =>
    request<unknown[]>(chatVisible !== undefined ? `/llm-configs?chat_visible=${chatVisible}` : '/llm-configs'),
  create: (data: { name: string; provider: string; base_url: string; model: string; api_key?: string; is_default?: boolean; stream_enabled?: boolean; thinking_enabled?: boolean; max_context_tokens?: number; chat_visible?: boolean }) =>
    request<unknown>('/llm-configs', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Record<string, unknown>) =>
    request<unknown>(`/llm-configs/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/llm-configs/${id}`, { method: 'DELETE' }),
  test: (id: string) =>
    request<{ success: boolean; message: string; reply?: string }>(`/llm-configs/${id}/test`, { method: 'POST' }),
  testConnection: (data: { provider: string; base_url: string; model: string; api_key?: string; config_id?: string }) =>
    request<{ success: boolean; message: string; reply?: string }>('/llm-configs/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
}

// Embedding/Rerank 配置接口类型
export interface EmbedConfigItem {
  id: string
  name: string
  config_type: string  // embedding | rerank
  provider: string  // remote
  model_name: string
  base_url: string | null
  api_key_set: boolean
  timeout: number
  sparse_enabled: boolean
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface EmbedTestResult {
  success: boolean
  message: string
}

export interface EmbedCurrentConfig {
  embed_model: string
  embed_base_url: string
  embed_sparse_enabled: boolean
  rerank_model: string
  rerank_base_url: string
}

// Embedding/Rerank 配置接口
export const embedConfigApi = {
  list: (configType?: string) =>
    request<EmbedConfigItem[]>(configType ? `/embed-configs?config_type=${configType}` : '/embed-configs'),
  current: () => request<EmbedCurrentConfig>('/embed-configs/current'),
  create: (data: {
    name: string
    config_type: string
    model_name?: string
    base_url: string
    api_key?: string
    timeout?: number
    sparse_enabled?: boolean
    is_active?: boolean
  }) =>
    request<EmbedConfigItem>('/embed-configs', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Record<string, unknown>) =>
    request<EmbedConfigItem>(`/embed-configs/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/embed-configs/${id}`, { method: 'DELETE' }),
  test: (data: {
    config_type: string
    model_name?: string
    base_url: string
    api_key?: string
    timeout?: number
    config_id?: string
    sparse_enabled?: boolean
  }) =>
    request<EmbedTestResult>('/embed-configs/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  testSaved: (id: string) =>
    request<EmbedTestResult>(`/embed-configs/${id}/test`, { method: 'POST' }),
}

export interface OCRConfigItem {
  id: string
  name: string
  provider_type: string
  api_url: string
  api_key_set: boolean
  timeout: number
  is_default: boolean
  is_fallback: boolean
  extra_config: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface OCRTestResult {
  success: boolean
  message: string
  elapsed_ms: number | null
}

// OCR 服务配置接口
export const ocrConfigApi = {
  list: () => request<OCRConfigItem[]>('/ocr-configs'),
  create: (data: { name: string; provider_type: string; api_url: string; api_key?: string; timeout?: number; is_default?: boolean; is_fallback?: boolean; extra_config?: Record<string, unknown> }) =>
    request<OCRConfigItem>('/ocr-configs', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Record<string, unknown>) =>
    request<OCRConfigItem>(`/ocr-configs/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/ocr-configs/${id}`, { method: 'DELETE' }),
  test: (data: { provider_type: string; api_url: string; api_key?: string; timeout?: number }) =>
    request<OCRTestResult>('/ocr-configs/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  testSaved: (id: string) =>
    request<OCRTestResult>(`/ocr-configs/${id}/test`, { method: 'POST' }),
}



// Agent 预设配置接口
export interface AgentPresetItem {
  id: string
  name: string
  description: string | null
  config_json: {
    agent_mode?: 'agent' | 'hybrid'
    max_iterations?: number
    temperature?: number
    thinking_enabled?: boolean
    allowed_tools?: string[]
  } | null
  is_default: boolean
  created_at: string
  updated_at: string
}

export const agentPresetApi = {
  list: () => request<AgentPresetItem[]>('/agent-presets'),
  placeholders: () => request<{
    placeholders: { name: string; description: string }[]
    default_prompt: string
  }>('/agent-presets/placeholders'),
  rewritePrompt: (data: { instruction: string; current_prompt?: string }) =>
    request<{ prompt: string }>('/agent-presets/rewrite-prompt', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  create: (data: { name: string; description?: string; config_json?: Record<string, unknown>; is_default?: boolean }) =>
    request<unknown>('/agent-presets', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: Record<string, unknown>) =>
    request<unknown>(`/agent-presets/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/agent-presets/${id}`, { method: 'DELETE' }),
}

// 会话相关接口类型
export interface SessionItem {
  id: string
  title: string
  kb_id: string | null
  model_config_id: string | null
  message_count: number
  created_at: string
  updated_at: string
}

export interface SessionMessageItem {
  id: string
  role: string
  content: string
  references: unknown[] | null
  agent_steps: {
    type: string
    content: boolean
    tool_call_id: string
    tool_name: string
    success: any
    duration_ms: number | undefined
    step: string
    detail: string
    max_context_tokens?: number
    current_context_tokens?: number
  }[] | null
  kb_id: string | null
  kb_ids: string[] | null
  created_at: string
}

// 会话管理接口
export const sessionApi = {
  list: () => request<SessionItem[]>('/sessions'),
  create: (data: { title?: string; kb_id?: string; model_config_id?: string }) =>
    request<SessionItem>('/sessions', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  update: (id: string, data: { title?: string }) =>
    request<SessionItem>(`/sessions/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  delete: (id: string) =>
    request<void>(`/sessions/${id}`, { method: 'DELETE' }),
  getMessages: (id: string) =>
    request<SessionMessageItem[]>(`/sessions/${id}/messages`),
  clearMessages: (id: string) =>
    request<void>(`/sessions/${id}/messages`, { method: 'DELETE' }),
}


// ============================================================
// 认证相关接口（tenant-auth）
// ============================================================

export interface LoginResponse {
  access_token: string
  token_type: string
  must_change_password: boolean
  is_super_admin: boolean
}

export interface PermissionItem {
  code: string
  type: string // api | menu | btn
}

export interface MePermissionsResponse {
  user_id: string
  tenant_id: string | null
  is_super_admin: boolean
  permissions: PermissionItem[]
}

export const authApi = {
  login: (username: string, password: string, tenantId?: string) =>
    request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password, tenant_id: tenantId ?? null }),
    }),
  changePassword: (oldPassword: string, newPassword: string) =>
    request<{ detail: string }>('/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  myPermissions: () => request<MePermissionsResponse>('/auth/me/permissions'),
}


// ============================================================
// 管理接口（tenant-auth）：平台级租户管理 + 租户级用户/角色管理
// ============================================================

export interface TenantItem {
  id: string
  name: string
  tenant_type: string
  is_active: boolean
}

export interface TenantCreateResult extends TenantItem {
  admin_username: string
  admin_temp_password: string | null
}

export interface AdminUserItem {
  id: string
  tenant_id: string | null
  username: string
  is_active: boolean
  must_change_password: boolean
}

export interface AdminUserCreateResult extends AdminUserItem {
  temp_password: string | null
}

export interface AuditLogItem {
  id: string
  actor_user_id: string | null
  actor_username: string | null
  actor_tenant_id: string | null
  actor_is_super_admin: boolean
  action: string
  target_type: string | null
  target_id: string | null
  target_name: string | null
  detail: Record<string, unknown> | null
  result: string
  ip: string | null
  created_at: string
}

export interface InvitationItem {
  id: string
  scope: string
  tenant_id: string | null
  role_names: string[] | null
  max_uses: number | null
  used_count: number
  expires_at: string
  is_active: boolean
  created_by_username: string | null
  created_at: string
}

export interface InvitationCreateResult {
  id: string
  token: string
  scope: string
  tenant_id: string | null
  expires_at: string
  max_uses: number | null
}

export interface RoleItem {
  id: string
  tenant_id: string
  name: string
  is_builtin: boolean
  permission_codes: string[]
}

export interface PermissionDictItem {
  code: string
  type: string // api | menu | btn
}

export const adminApi = {
  // —— 租户（Super_Admin / tenant:manage）——
  listTenants: () => request<TenantItem[]>('/admin/tenants'),
  createTenant: (name: string, adminUsername: string, adminPassword?: string) =>
    request<TenantCreateResult>('/admin/tenants', {
      method: 'POST',
      body: JSON.stringify({ name, admin_username: adminUsername, admin_password: adminPassword ?? null }),
    }),
  setTenantStatus: (tenantId: string, isActive: boolean) =>
    request<TenantItem>(`/admin/tenants/${tenantId}/status`, {
      method: 'PUT',
      body: JSON.stringify({ is_active: isActive }),
    }),

  // —— 用户（user:manage）——
  listUsers: (params?: { page?: number; page_size?: number; q?: string }) => {
    const qs = new URLSearchParams()
    qs.set('page', String(params?.page ?? 1))
    qs.set('page_size', String(params?.page_size ?? 20))
    if (params?.q) qs.set('q', params.q)
    return request<PageResult<AdminUserItem>>(`/admin/users?${qs.toString()}`)
  },
  createUser: (username: string, roleNames: string[], password?: string) =>
    request<AdminUserCreateResult>('/admin/users', {
      method: 'POST',
      body: JSON.stringify({ username, role_names: roleNames, password: password ?? null }),
    }),
  setUserStatus: (userId: string, isActive: boolean) =>
    request<AdminUserItem>(`/admin/users/${userId}/status`, {
      method: 'PUT',
      body: JSON.stringify({ is_active: isActive }),
    }),
  resetPassword: (userId: string) =>
    request<AdminUserCreateResult>(`/admin/users/${userId}/reset-password`, { method: 'POST' }),
  transferKnowledgeBases: (userId: string, targetUserId: string) =>
    request<{ detail: string; transferred_count: number }>(`/admin/users/${userId}/transfer-knowledge-bases`, {
      method: 'POST',
      body: JSON.stringify({ target_user_id: targetUserId }),
    }),
  getUserRoles: (userId: string) =>
    request<{ user_id: string; role_ids: string[] }>(`/admin/users/${userId}/roles`),
  setUserRoles: (userId: string, roleIds: string[]) =>
    request<{ detail: string; role_ids: string[] }>(`/admin/users/${userId}/roles`, {
      method: 'PUT',
      body: JSON.stringify({ role_ids: roleIds }),
    }),

  // —— 角色与权限点（role:manage）——
  listRoles: () => request<RoleItem[]>('/admin/roles'),
  permissionDict: () => request<PermissionDictItem[]>('/admin/permissions'),
  createRole: (name: string, permissionCodes: string[], description?: string) =>
    request<RoleItem>('/admin/roles', {
      method: 'POST',
      body: JSON.stringify({ name, permission_codes: permissionCodes, description: description ?? null }),
    }),
  setRolePermissions: (roleId: string, permissionCodes: string[]) =>
    request<RoleItem>(`/admin/roles/${roleId}/permissions`, {
      method: 'PUT',
      body: JSON.stringify({ permission_codes: permissionCodes }),
    }),
  deleteRole: (roleId: string) =>
    request<void>(`/admin/roles/${roleId}`, { method: 'DELETE' }),

  // —— 审计日志（user:manage 可读；租管限本租户，超管全局）——
  auditLogs: (params?: { page?: number; page_size?: number; action?: string; actor?: string }) => {
    const qs = new URLSearchParams()
    qs.set('page', String(params?.page ?? 1))
    qs.set('page_size', String(params?.page_size ?? 20))
    if (params?.action) qs.set('action', params.action)
    if (params?.actor) qs.set('actor', params.actor)
    return request<PageResult<AuditLogItem>>(`/admin/audit-logs?${qs.toString()}`)
  },

  // —— 邀请链接 ——
  listInvitations: (params?: { page?: number; page_size?: number }) => {
    const qs = new URLSearchParams()
    qs.set('page', String(params?.page ?? 1))
    qs.set('page_size', String(params?.page_size ?? 20))
    return request<PageResult<InvitationItem>>(`/admin/invitations?${qs.toString()}`)
  },
  createInvitation: (data: { scope: string; expires_in_hours: number; max_uses?: number | null; role_names?: string[] }) =>
    request<InvitationCreateResult>('/admin/invitations', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  revokeInvitation: (id: string) =>
    request<void>(`/admin/invitations/${id}`, { method: 'DELETE' }),
}

// 免登录邀请接受（无 token 注入；接受页用）
export const inviteApi = {
  info: (token: string) =>
    request<{ scope: string; tenant_name: string | null; valid: boolean }>(`/invitations/${token}`),
  accept: (token: string, data: { username: string; password: string; tenant_name?: string }) =>
    request<{ detail: string; tenant_id?: string; user_id?: string }>(`/invitations/${token}/accept`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
}
