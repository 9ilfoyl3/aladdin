// API 客户端：统一请求封装

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
      ...options.headers,
    },
    ...options,
  })

  if (!response.ok) {
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
      body: formData,
    }).then((res) => res.json())
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
      body: formData,
    }).then(async (res) => {
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

// 检索测试接口
export const retrievalApi = {
  test: (data: { query: string; knowledge_base_id: string; mode?: string; top_k?: number }) =>
    request<unknown>('/retrieval/test', {
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
