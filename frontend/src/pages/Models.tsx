import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Pencil, Trash2, Star, Cpu, Zap, CheckCircle, XCircle, Globe, Server, Search, LayoutGrid, List } from 'lucide-react'
import { llmConfigApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'

interface LLMConfigItem {
  id: string
  name: string
  provider: string
  base_url: string
  model: string
  api_key_set: boolean
  is_default: boolean
  stream_enabled: boolean
  thinking_enabled: boolean
  max_context_tokens: number | null
  chat_visible: boolean
  created_at: string
}

interface FormData {
  name: string
  provider: string
  base_url: string
  model: string
  api_key: string
  is_default: boolean
  stream_enabled: boolean
  thinking_enabled: boolean
  max_context_tokens: string
  chat_visible: boolean
}

const emptyForm: FormData = {
  name: '',
  provider: 'vllm',
  base_url: '',
  model: '',
  api_key: '',
  is_default: false,
  stream_enabled: true,
  thinking_enabled: false,
  max_context_tokens: '',
  chat_visible: true,
}

function Models() {
  const queryClient = useQueryClient()
  const [showDialog, setShowDialog] = useState(false)
  const [editingItem, setEditingItem] = useState<LLMConfigItem | null>(null)
  const [form, setForm] = useState<FormData>(emptyForm)
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid')
  const [searchQuery, setSearchQuery] = useState('')
  const [filterProvider, setFilterProvider] = useState<string>('all')

  const { data: configs = [], isLoading } = useQuery({
    queryKey: ['llm-configs'],
    queryFn: () => llmConfigApi.list() as Promise<LLMConfigItem[]>,
  })

  // 搜索和筛选
  const filteredConfigs = useMemo(() => {
    return configs.filter((c) => {
      const matchesSearch = !searchQuery ||
        c.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
        c.model.toLowerCase().includes(searchQuery.toLowerCase())
      const matchesProvider = filterProvider === 'all' || c.provider === filterProvider
      return matchesSearch && matchesProvider
    })
  }, [configs, searchQuery, filterProvider])

  const createMutation = useMutation({
    mutationFn: (data: FormData) => llmConfigApi.create({
      name: data.name,
      provider: data.provider,
      base_url: data.base_url,
      model: data.model,
      api_key: data.api_key || undefined,
      is_default: data.is_default,
      stream_enabled: data.stream_enabled,
      thinking_enabled: data.thinking_enabled,
      max_context_tokens: data.max_context_tokens ? parseInt(data.max_context_tokens) : undefined,
      chat_visible: data.chat_visible,
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['llm-configs'] })
      closeDialog()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<FormData> }) => {
      const payload: Record<string, unknown> = { ...data }
      if (!payload.api_key) delete payload.api_key
      return llmConfigApi.update(id, payload)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['llm-configs'] })
      closeDialog()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => llmConfigApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['llm-configs'] })
    },
  })

  const [testResults, setTestResults] = useState<Record<string, { success: boolean; message: string; reply?: string }>>({})
  const [testingId, setTestingId] = useState<string | null>(null)

  const testMutation = useMutation({
    mutationFn: (id: string) => llmConfigApi.test(id),
    onMutate: (id) => { setTestingId(id) },
    onSuccess: (data, id) => {
      setTestResults(prev => ({ ...prev, [id]: data }))
      setTestingId(null)
    },
    onError: (_err, id) => {
      setTestResults(prev => ({ ...prev, [id]: { success: false, message: '请求失败' } }))
      setTestingId(null)
    },
  })

  const [dialogTestResult, setDialogTestResult] = useState<{ success: boolean; message: string; reply?: string } | null>(null)
  const [dialogTesting, setDialogTesting] = useState(false)

  async function handleTestInDialog() {
    setDialogTesting(true)
    setDialogTestResult(null)
    try {
      // 始终用表单当前值测试，API Key 为空时后端通过 config_id 从数据库补全
      const result = await llmConfigApi.testConnection({
        provider: form.provider,
        base_url: form.base_url,
        model: form.model,
        api_key: form.api_key || undefined,
        config_id: editingItem?.id || undefined,
      })
      setDialogTestResult(result)
    } catch {
      setDialogTestResult({ success: false, message: '请求失败' })
    } finally {
      setDialogTesting(false)
    }
  }

  function openCreate() {
    setEditingItem(null)
    setForm(emptyForm)
    setDialogTestResult(null)
    setShowDialog(true)
  }

  function openEdit(item: LLMConfigItem) {
    setEditingItem(item)
    setForm({
      name: item.name,
      provider: item.provider,
      base_url: item.base_url,
      model: item.model,
      api_key: '',
      is_default: item.is_default,
      stream_enabled: item.stream_enabled,
      thinking_enabled: item.thinking_enabled,
      max_context_tokens: item.max_context_tokens ? String(item.max_context_tokens) : '',
      chat_visible: item.chat_visible,
    })
    setShowDialog(true)
  }

  function closeDialog() {
    setShowDialog(false)
    setEditingItem(null)
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (editingItem) {
      updateMutation.mutate({ id: editingItem.id, data: form })
    } else {
      createMutation.mutate(form)
    }
  }

  return (
    <div>
      {/* 页面头部 */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">模型管理</h1>
          <p className="text-muted-foreground text-sm mt-1">配置多个 LLM 模型，在对话中灵活切换</p>
        </div>
        <div className="flex items-center gap-2">
          <Button onClick={openCreate} className="gap-2 cursor-pointer">
            <Plus className="h-4 w-4" />
            添加模型
          </Button>
        </div>
      </div>

      {/* 搜索栏 + 筛选 + 视图切换 */}
      {configs.length > 0 && (
        <div className="flex items-center gap-3 mb-4">
          <div className="relative flex-1 max-w-xs">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="搜索模型名称..."
              className="pl-9 h-9"
            />
          </div>
          <Select value={filterProvider} onValueChange={setFilterProvider}>
            <SelectTrigger className="w-[140px] h-9">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部 Provider</SelectItem>
              <SelectItem value="ollama">Ollama</SelectItem>
              <SelectItem value="vllm">OpenAI 兼容</SelectItem>
            </SelectContent>
          </Select>
          <div className="flex items-center border border-border rounded-lg p-0.5 ml-auto">
            <button
              onClick={() => setViewMode('grid')}
              className={`p-1.5 rounded-md cursor-pointer transition-colors ${viewMode === 'grid' ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'}`}
            >
              <LayoutGrid className="h-4 w-4" />
            </button>
            <button
              onClick={() => setViewMode('list')}
              className={`p-1.5 rounded-md cursor-pointer transition-colors ${viewMode === 'list' ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'}`}
            >
              <List className="h-4 w-4" />
            </button>
          </div>
        </div>
      )}

      {/* 模型列表 */}
      {isLoading ? (
        <div className="flex items-center justify-center py-20">
          <div className="text-center">
            <div className="h-8 w-8 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto mb-3" />
            <p className="text-sm text-muted-foreground">加载中...</p>
          </div>
        </div>
      ) : configs.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20">
          <div className="w-16 h-16 rounded-2xl bg-muted/60 flex items-center justify-center mb-4">
            <Cpu className="h-8 w-8 text-muted-foreground/60" />
          </div>
          <p className="text-muted-foreground mb-4">暂无模型配置，添加一个开始吧</p>
          <Button onClick={openCreate} variant="outline" className="gap-2 cursor-pointer">
            <Plus className="h-4 w-4" />
            添加模型
          </Button>
        </div>
      ) : filteredConfigs.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12">
          <p className="text-muted-foreground text-sm">没有匹配的模型</p>
        </div>
      ) : viewMode === 'grid' ? (
        <div>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {filteredConfigs.map((config) => (
              <div
                key={config.id}
                className="group relative rounded-xl border border-border bg-card p-5 transition-all duration-200 hover:shadow-lg hover:border-primary/20 hover:-translate-y-0.5"
              >
                {config.is_default && (
                  <div className="absolute top-4 right-4">
                    <Star className="h-4 w-4 text-yellow-500 fill-yellow-500" />
                  </div>
                )}
                <div className="w-10 h-10 rounded-lg bg-primary/8 flex items-center justify-center mb-3">
                  {config.provider === 'ollama' ? (
                    <Server className="h-5 w-5 text-primary" />
                  ) : (
                    <Globe className="h-5 w-5 text-primary" />
                  )}
                </div>
                <div className="flex items-center gap-2 mb-3">
                  <h3 className="font-semibold text-base truncate">{config.name}</h3>
                  {!config.chat_visible && (
                    <Badge variant="secondary" className="text-xs shrink-0">仅内部</Badge>
                  )}
                  <Badge variant="outline" className="text-xs bg-primary/5 text-primary border-primary/20 shrink-0">
                    {config.provider}
                  </Badge>
                </div>
                <div className="space-y-1.5 text-sm text-muted-foreground mb-4">
                  <p className="truncate"><span className="text-foreground/60">地址:</span> {config.base_url}</p>
                  <p className="truncate"><span className="text-foreground/60">模型:</span> {config.model}</p>
                  <p><span className="text-foreground/60">密钥:</span> {config.api_key_set ? '已设置' : '未设置'}</p>
                </div>
                <div className="flex items-center gap-1 pt-3 border-t border-border/60">
                  <Button variant="ghost" size="sm" className="h-8 text-xs gap-1 cursor-pointer" onClick={() => testMutation.mutate(config.id)} disabled={testingId === config.id}>
                    <Zap className="h-3.5 w-3.5" />
                    {testingId === config.id ? '测试中' : '测试'}
                  </Button>
                  <Button variant="ghost" size="sm" className="h-8 text-xs gap-1 cursor-pointer" onClick={() => openEdit(config)}>
                    <Pencil className="h-3.5 w-3.5" />
                    编辑
                  </Button>
                  <Button variant="ghost" size="sm" className="h-8 text-xs gap-1 text-destructive hover:text-destructive cursor-pointer" onClick={() => deleteMutation.mutate(config.id)}>
                    <Trash2 className="h-3.5 w-3.5" />
                    删除
                  </Button>
                </div>
                {testResults[config.id] && (
                  <div className={`mt-3 p-2.5 rounded-lg text-xs ${testResults[config.id]?.success ? 'bg-green-50 text-green-700 border border-green-200' : 'bg-red-50 text-red-700 border border-red-200'}`}>
                    <div className="flex items-center gap-1.5">
                      {testResults[config.id]?.success ? <CheckCircle className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
                      <span>{testResults[config.id]?.message}</span>
                    </div>
                    {testResults[config.id]?.reply && <p className="mt-1 text-muted-foreground line-clamp-2">回复: {testResults[config.id]?.reply}</p>}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      ) : (
        /* 列表视图 */
        <div className="border border-border rounded-xl">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-muted/80 backdrop-blur-sm border-b border-border">
              <tr>
                <th className="text-left font-medium px-4 py-2.5 text-muted-foreground">名称</th>
                <th className="text-left font-medium px-4 py-2.5 text-muted-foreground">Provider</th>
                <th className="text-left font-medium px-4 py-2.5 text-muted-foreground hidden md:table-cell">模型</th>
                <th className="text-left font-medium px-4 py-2.5 text-muted-foreground hidden lg:table-cell">地址</th>
                <th className="text-right font-medium px-4 py-2.5 text-muted-foreground">操作</th>
              </tr>
            </thead>
            <tbody>
              {filteredConfigs.map((config) => (
                <tr key={config.id} className="border-b border-border/50 last:border-0 hover:bg-muted/30 transition-colors">
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{config.name}</span>
                      {config.is_default && <Star className="h-3.5 w-3.5 text-yellow-500 fill-yellow-500" />}
                      {!config.chat_visible && <Badge variant="secondary" className="text-[10px] px-1 py-0">仅内部</Badge>}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant="outline" className="text-xs bg-primary/5 text-primary border-primary/20">
                      {config.provider}
                    </Badge>
                  </td>
                  <td className="px-4 py-3 text-muted-foreground truncate max-w-[160px] hidden md:table-cell">{config.model}</td>
                  <td className="px-4 py-3 text-muted-foreground truncate max-w-[200px] hidden lg:table-cell">{config.base_url}</td>
                  <td className="px-4 py-3">
                    <div className="flex items-center justify-end gap-1">
                      <Button variant="ghost" size="sm" className="h-7 text-xs gap-1 cursor-pointer" onClick={() => testMutation.mutate(config.id)} disabled={testingId === config.id}>
                        <Zap className="h-3 w-3" />
                        {testingId === config.id ? '...' : '测试'}
                      </Button>
                      <Button variant="ghost" size="sm" className="h-7 text-xs gap-1 cursor-pointer" onClick={() => openEdit(config)}>
                        <Pencil className="h-3 w-3" />
                      </Button>
                      <Button variant="ghost" size="sm" className="h-7 text-xs text-destructive hover:text-destructive cursor-pointer" onClick={() => deleteMutation.mutate(config.id)}>
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* 创建/编辑对话框 */}
      <Dialog open={showDialog} onOpenChange={closeDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editingItem ? '编辑模型' : '添加模型'}</DialogTitle>
          </DialogHeader>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <Label>名称</Label>
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如：DeepSeek Chat"
                className="mt-1.5"
                required
              />
            </div>
            <div>
              <Label>Provider</Label>
              <Select value={form.provider} onValueChange={(val) => setForm({ ...form, provider: val })}>
                <SelectTrigger className="mt-1.5">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="ollama">Ollama（本地）</SelectItem>
                  <SelectItem value="vllm">OpenAI 兼容（远端）</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground mt-1.5">
                Ollama 用于本地模型，OpenAI 兼容用于 DeepSeek/OpenAI/通义等远端 API
              </p>
            </div>
            <div>
              <Label>API 地址</Label>
              <Input
                value={form.base_url}
                onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                placeholder="如：https://api.deepseek.com"
                className="mt-1.5"
                required
              />
            </div>
            <div>
              <Label>模型名称</Label>
              <Input
                value={form.model}
                onChange={(e) => setForm({ ...form, model: e.target.value })}
                placeholder="如：deepseek-chat"
                className="mt-1.5"
                required
              />
            </div>
            <div>
              <Label>API Key</Label>
              <Input
                type="password"
                value={form.api_key}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                placeholder={editingItem ? '密钥已设置，点击修改' : '输入 API 密钥（可选）'}
                className="mt-1.5"
              />
            </div>
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="is_default"
                checked={form.is_default}
                onChange={(e) => setForm({ ...form, is_default: e.target.checked })}
                className="rounded border-border"
              />
              <Label htmlFor="is_default" className="text-sm font-normal cursor-pointer">设为默认模型</Label>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="chat_visible"
                checked={form.chat_visible}
                onChange={(e) => setForm({ ...form, chat_visible: e.target.checked })}
                className="rounded border-border"
              />
              <Label htmlFor="chat_visible" className="text-sm font-normal cursor-pointer">允许在对话中选择此模型</Label>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="stream_enabled"
                checked={form.stream_enabled}
                onChange={(e) => setForm({ ...form, stream_enabled: e.target.checked })}
                className="rounded border-border"
              />
              <Label htmlFor="stream_enabled" className="text-sm font-normal cursor-pointer">启用流式输出</Label>
              <span className="text-xs text-muted-foreground">（部分模型不支持流式，关闭后使用非流式生成）</span>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="thinking_enabled"
                checked={form.thinking_enabled}
                onChange={(e) => setForm({ ...form, thinking_enabled: e.target.checked })}
                className="rounded border-border"
              />
              <Label htmlFor="thinking_enabled" className="text-sm font-normal cursor-pointer">启用深度思考</Label>
              <span className="text-xs text-muted-foreground">（开启后模型会进行推理思考，适用于支持 thinking 的模型）</span>
            </div>
            <div>
              <Label>最大上下文长度（token）</Label>
              <Input
                type="number"
                value={form.max_context_tokens}
                onChange={(e) => setForm({ ...form, max_context_tokens: e.target.value })}
                placeholder="留空默认 200000（200K）"
                className="mt-1.5"
              />
              <p className="text-xs text-muted-foreground mt-1.5">
                填模型的完整上下文窗口（如 128000、1000000）。用于检索上下文裁剪和 Agent 上下文管理；留空默认 200K，百万上下文模型请手动填写
              </p>
            </div>
            {dialogTestResult && (
              <div className={`p-2.5 rounded-lg text-xs ${dialogTestResult.success ? 'bg-green-50 text-green-700 border border-green-200' : 'bg-red-50 text-red-700 border border-red-200'}`}>
                <div className="flex items-center gap-1.5">
                  {dialogTestResult.success ? <CheckCircle className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
                  <span>{dialogTestResult.message}</span>
                </div>
                {dialogTestResult.reply && <p className="mt-1 text-muted-foreground">回复: {dialogTestResult.reply}</p>}
              </div>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={handleTestInDialog} disabled={dialogTesting || !form.base_url || !form.model} className="cursor-pointer">
                <Zap className="h-4 w-4" />
                {dialogTesting ? '测试中...' : '测试连接'}
              </Button>
              <Button type="button" variant="outline" onClick={closeDialog} className="cursor-pointer">取消</Button>
              <Button type="submit" disabled={createMutation.isPending || updateMutation.isPending} className="cursor-pointer">
                {editingItem ? '保存' : '创建'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default Models


