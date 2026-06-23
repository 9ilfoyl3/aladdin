// 模型服务商预设（移植自 WeKnora ModelEditorDialog.vue 的 defaultUrls + thinkingControl）。
//
// 作用：
//   1. 用户在「添加/编辑模型」时选服务商 → 自动回填对应的 base_url（按模型类别区分）。
//   2. LLM 类别额外预选「思考模式参数格式」（thinking_control），取代后端脆弱的自动匹配。
//
// 数据流向保持清晰：前端仅负责「选服务商 → 填 URL + 预选思考格式」的便捷交互，
// 选中的 vendor / thinking_control 显式写入配置存库，运行时后端据此精确分派，
// 不再依赖 base_url 猜测。自定义（generic）服务商不填 URL，由用户手填。

export type ModelCategory = 'chat' | 'embedding' | 'rerank' | 'asr'

// 思考模式参数格式（与后端 thinking_dialect._EXPLICIT_CONTROLS 对齐）
export type ThinkingControlValue =
  | 'none'
  | 'chat_template_kwargs'
  | 'enable_thinking'
  | 'thinking_type'

export interface ProviderPreset {
  // vendor 值：与后端 LLMProviderName 枚举对齐（generic = 自定义 OpenAI 兼容；ollama = 本地）
  value: string
  label: string
  description: string
  // 各模型类别的默认 base_url（缺某类别表示该服务商不提供该类别的预设 URL）
  defaultUrls: Partial<Record<ModelCategory, string>>
  // 该服务商支持的模型类别（用于按当前类别过滤下拉项）
  categories: ModelCategory[]
  // 后端基础设施类型（DB 的 provider 字段）：决定走 OllamaLLM 还是 VllmLLM。
  // 仅 ollama 为 'ollama'，其余 OpenAI 兼容厂商均为 'vllm'（默认）。
  infra?: 'ollama' | 'vllm'
}

// 服务商预设表。顺序即下拉展示顺序：自定义/本地置顶（最常用兜底），其余按常见度排列。
export const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    value: 'generic',
    label: '自定义 (OpenAI 兼容接口)',
    description: '通用 OpenAI 兼容端点 / 自建 vLLM·SGLang·TEI·Infinity·Whisper',
    defaultUrls: {},
    categories: ['chat', 'embedding', 'rerank', 'asr'],
  },
  {
    value: 'ollama',
    label: 'Ollama（本地）',
    description: '本地运行的 Ollama 模型，如 qwen3、deepseek-r1 等',
    defaultUrls: {
      chat: 'http://localhost:11434',
    },
    categories: ['chat'],
    infra: 'ollama',
  },
  {
    value: 'aliyun',
    label: '阿里云 DashScope',
    description: 'qwen-plus、tongyi-embedding、qwen3-rerank、paraformer 等',
    defaultUrls: {
      chat: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      embedding: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      rerank: 'https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank',
      asr: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    },
    categories: ['chat', 'embedding', 'rerank', 'asr'],
  },
  {
    value: 'volcengine',
    label: '火山引擎 Volcengine',
    description: 'doubao-1-5-pro、doubao-embedding 等',
    defaultUrls: {
      chat: 'https://ark.cn-beijing.volces.com/api/v3',
      embedding: 'https://ark.cn-beijing.volces.com/api/v3',
    },
    categories: ['chat', 'embedding'],
  },
  {
    value: 'deepseek',
    label: 'DeepSeek',
    description: 'deepseek-chat、deepseek-reasoner 等',
    defaultUrls: {
      chat: 'https://api.deepseek.com/v1',
    },
    categories: ['chat'],
  },
  {
    value: 'zhipu',
    label: '智谱 BigModel',
    description: 'glm-4.7、embedding-3 等',
    defaultUrls: {
      chat: 'https://open.bigmodel.cn/api/paas/v4',
      embedding: 'https://open.bigmodel.cn/api/paas/v4/embeddings',
    },
    categories: ['chat', 'embedding'],
  },
  {
    value: 'siliconflow',
    label: '硅基流动 SiliconFlow',
    description: 'deepseek-ai/DeepSeek-V3.1、BAAI/bge-m3、SenseVoiceSmall 等',
    defaultUrls: {
      chat: 'https://api.siliconflow.cn/v1',
      embedding: 'https://api.siliconflow.cn/v1',
      rerank: 'https://api.siliconflow.cn/v1',
      asr: 'https://api.siliconflow.cn/v1',
    },
    categories: ['chat', 'embedding', 'rerank', 'asr'],
  },
  {
    value: 'openai',
    label: 'OpenAI',
    description: 'gpt-5.2、text-embedding-3、whisper-1 等',
    defaultUrls: {
      chat: 'https://api.openai.com/v1',
      embedding: 'https://api.openai.com/v1',
      asr: 'https://api.openai.com/v1',
    },
    categories: ['chat', 'embedding', 'asr'],
  },
  {
    value: 'openrouter',
    label: 'OpenRouter',
    description: 'openai/gpt-5.2-chat、google/gemini-3-flash 等',
    defaultUrls: {
      chat: 'https://openrouter.ai/api/v1',
      embedding: 'https://openrouter.ai/api/v1',
    },
    categories: ['chat', 'embedding'],
  },
  {
    value: 'nvidia',
    label: 'NVIDIA NIM',
    description: 'NVIDIA 托管 / 自建 NIM 推理服务',
    defaultUrls: {
      chat: 'https://integrate.api.nvidia.com/v1',
      embedding: 'https://integrate.api.nvidia.com/v1',
      rerank: 'https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking',
    },
    categories: ['chat', 'embedding', 'rerank'],
  },
  {
    value: 'gemini',
    label: 'Google Gemini',
    description: 'gemini-3-flash、text-embedding 等（OpenAI 兼容端点）',
    defaultUrls: {
      chat: 'https://generativelanguage.googleapis.com/v1beta/openai',
      embedding: 'https://generativelanguage.googleapis.com/v1beta/openai',
    },
    categories: ['chat', 'embedding'],
  },
  {
    value: 'jina',
    label: 'Jina',
    description: 'jina-embeddings、jina-reranker 等',
    defaultUrls: {
      embedding: 'https://api.jina.ai/v1',
      rerank: 'https://api.jina.ai/v1',
    },
    categories: ['embedding', 'rerank'],
  },
]

/** 按模型类别过滤可选服务商。 */
export function providersForCategory(category: ModelCategory): ProviderPreset[] {
  return PROVIDER_PRESETS.filter((p) => p.categories.includes(category))
}

/** 取某服务商在指定类别下的默认 base_url（无则返回空串）。 */
export function defaultBaseUrl(vendor: string, category: ModelCategory): string {
  const preset = PROVIDER_PRESETS.find((p) => p.value === vendor)
  return preset?.defaultUrls[category] ?? ''
}

/** 取某服务商的后端基础设施类型（DB provider 字段）：ollama 走原生协议，其余走 OpenAI 兼容。 */
export function infraForVendor(vendor: string): 'ollama' | 'vllm' {
  const preset = PROVIDER_PRESETS.find((p) => p.value === vendor)
  return preset?.infra ?? 'vllm'
}

/** 取服务商展示名（用于卡片/列表徽标）；未知 vendor 回退原值。 */
export function vendorLabel(vendor: string | null | undefined): string {
  if (!vendor) return '自定义'
  const preset = PROVIDER_PRESETS.find((p) => p.value === vendor)
  return preset?.label ?? vendor
}

// --- 思考模式参数格式预选（与后端 thinking_dialect.default_thinking_control 对齐）---

function isQwenThinkingModel(model: string): boolean {
  const m = model.trim().toLowerCase()
  return (
    m.startsWith('qwen3') ||
    m.startsWith('qwen-plus') ||
    m.startsWith('qwen-max') ||
    m.startsWith('qwen-turbo')
  )
}

/**
 * 按服务商 + 模型名预选思考模式参数格式。
 * 与后端 default_thinking_control 保持一致，作为 UI 默认值；用户可手动覆盖。
 */
export function defaultThinkingControl(vendor: string, model = ''): ThinkingControlValue {
  const v = (vendor || '').trim().toLowerCase()
  const name = (model || '').trim()
  switch (v) {
    case 'aliyun':
      return isQwenThinkingModel(name) ? 'enable_thinking' : 'none'
    case 'generic':
    case 'nvidia':
      return 'chat_template_kwargs'
    case 'volcengine':
    case 'deepseek':
      return 'thinking_type'
    default:
      // openai / zhipu / gemini / siliconflow / openrouter / … → 不写入
      return 'none'
  }
}

// 「思考模式参数格式」下拉选项（含说明，移植自 WeKnora i18n）
export const THINKING_CONTROL_OPTIONS: {
  value: ThinkingControlValue
  label: string
  hint: string
}[] = [
  { value: 'none', label: '不写入思考参数', hint: '思考开关不生效，请求中不写入任何思考相关参数' },
  { value: 'chat_template_kwargs', label: 'chat_template_kwargs', hint: '自建 vLLM/SGLang、NVIDIA NIM、本地 Qwen 部署' },
  { value: 'enable_thinking', label: 'enable_thinking', hint: '阿里云 DashScope：qwen3、qwen-plus、qwen-max、qwen-turbo' },
  { value: 'thinking_type', label: 'thinking.type', hint: '火山引擎 Ark、DeepSeek 官方、腾讯云 LKEAP（V3 等）' },
]
