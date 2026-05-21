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

// 知识库相关接口
export const knowledgeBaseApi = {
  list: () => request<unknown[]>('/knowledge-bases'),
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
  list: (kbId: string, folderId?: string | null) =>
    request<unknown[]>(`/knowledge-bases/${kbId}/documents${folderId ? `?folder_id=${folderId}` : ''}`),
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
  chunks: (id: string) => request<unknown[]>(`/documents/${id}/chunks`),
}

// 文件夹相关接口
export const folderApi = {
  list: (kbId: string, parentId?: string | null) =>
    request<unknown[]>(`/knowledge-bases/${kbId}/folders${parentId ? `?parent_id=${parentId}` : ''}`),
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

// Agent 节点配置接口类型
export interface AgentNodeConfigResponse {
  router_model_id: string | null
  router_model_name: string | null
  rewriter_model_id: string | null
  rewriter_model_name: string | null
  reflector_model_id: string | null
  reflector_model_name: string | null
}

export interface AgentNodeConfigUpdate {
  router_model_id?: string | null
  rewriter_model_id?: string | null
  reflector_model_id?: string | null
}

// Agent 节点配置接口
export const agentNodeConfigApi = {
  get: () => request<AgentNodeConfigResponse>('/agent-node-configs'),
  update: (data: AgentNodeConfigUpdate) =>
    request<AgentNodeConfigResponse>('/agent-node-configs', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
}

// Embedding/Rerank 配置接口类型
export interface EmbedConfigItem {
  id: string
  name: string
  config_type: string  // embedding | rerank
  provider: string  // local | remote
  local_provider: string | null
  model_name: string
  device: string
  base_url: string | null
  api_key_set: boolean
  timeout: number
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface EmbedTestResult {
  success: boolean
  message: string
}

export interface EmbedCurrentConfig {
  embed_provider: string
  embed_model: string
  embed_device: string
  embed_base_url: string
  rerank_provider: string
  rerank_model: string
  rerank_device: string
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
    provider: string
    local_provider?: string
    model_name?: string
    device?: string
    base_url?: string
    api_key?: string
    timeout?: number
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
    provider: string
    config_type: string
    local_provider?: string
    model_name?: string
    device?: string
    base_url?: string
    api_key?: string
    timeout?: number
    config_id?: string
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

