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
  list: (kbId: string) =>
    request<unknown[]>(`/knowledge-bases/${kbId}/documents`),
  upload: (kbId: string, file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return fetch(`${BASE_URL}/knowledge-bases/${kbId}/documents/upload`, {
      method: 'POST',
      body: formData,
    }).then((res) => res.json())
  },
  get: (id: string) => request<unknown>(`/documents/${id}`),
  delete: (id: string) =>
    request<void>(`/documents/${id}`, { method: 'DELETE' }),
  chunks: (id: string) => request<unknown[]>(`/documents/${id}/chunks`),
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
  list: () => request<unknown[]>('/llm-configs'),
  create: (data: { name: string; provider: string; base_url: string; model: string; api_key?: string; is_default?: boolean; stream_enabled?: boolean; max_context_tokens?: number }) =>
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
  testConnection: (data: { provider: string; base_url: string; model: string; api_key?: string }) =>
    request<{ success: boolean; message: string; reply?: string }>('/llm-configs/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
}
