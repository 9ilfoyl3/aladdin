import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Pencil, Trash2, CheckCircle, XCircle, Zap, Globe, Server } from 'lucide-react'
import { embedConfigApi } from '@/lib/api'
import type { EmbedConfigItem } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'

interface FormData {
  name: string
  config_type: string
  provider: string
  local_provider: string
  model_name: string
  device: string
  base_url: string
  api_key: string
  timeout: string
  is_active: boolean
}

const emptyForm: FormData = {
  name: '',
  config_type: 'embedding',
  provider: 'local',
  local_provider: 'sentence-transformers',
  model_name: 'BAAI/bge-m3',
  device: 'cpu',
  base_url: '',
  api_key: '',
  timeout: '60',
  is_active: false,
}

function EmbedConfig() {
  const queryClient = useQueryClient()
  const [showDialog, setShowDialog] = useState(false)
  const [editingItem, setEditingItem] = useState<EmbedConfigItem | null>(null)
  const [form, setForm] = useState<FormData>(emptyForm)

  const { data: configs = [], isLoading } = useQuery({
    queryKey: ['embed-configs'],
    queryFn: () => embedConfigApi.list(),
  })

  const embeddingConfigs = configs.filter(c => c.config_type === 'embedding')
  const rerankConfigs = configs.filter(c => c.config_type === 'rerank')

  const createMutation = useMutation({
    mutationFn: (data: FormData) => embedConfigApi.create({
      name: data.name,
      config_type: data.config_type,
      provider: data.provider,
      local_provider: data.provider === 'local' ? data.local_provider : undefined,
      model_name: data.model_name,
      device: data.device,
      base_url: data.provider === 'remote' ? data.base_url : undefined,
      api_key: data.provider === 'remote' ? data.api_key : undefined,
      timeout: parseFloat(data.timeout) || 60,
      is_active: data.is_active,
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['embed-configs'] })
      closeDialog()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Record<string, unknown> }) =>
      embedConfigApi.update(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['embed-configs'] })
      closeDialog()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => embedConfigApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['embed-configs'] })
    },
  })

  const activateMutation = useMutation({
    mutationFn: (id: string) => embedConfigApi.update(id, { is_active: true }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['embed-configs'] })
    },
  })

  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null)
  const [testing, setTesting] = useState(false)

  async function handleTest() {
    setTesting(true)
    setTestResult(null)
    try {
      if (editingItem && editingItem.id) {
        const result = await embedConfigApi.testSaved(editingItem.id)
        setTestResult(result)
      } else {
        const result = await embedConfigApi.test({
          provider: form.provider,
          config_type: form.config_type,
          local_provider: form.provider === 'local' ? form.local_provider : undefined,
          model_name: form.model_name,
          device: form.device,
          base_url: form.provider === 'remote' ? form.base_url : undefined,
          api_key: form.provider === 'remote' ? form.api_key : undefined,
          timeout: parseFloat(form.timeout) || 60,
        })
        setTestResult(result)
      }
    } catch {
      setTestResult({ success: false, message: '请求失败' })
    } finally {
      setTesting(false)
    }
  }

  function openCreate(configType: 'embedding' | 'rerank') {
    setEditingItem(null)
    const defaultModel = configType === 'embedding' ? 'BAAI/bge-m3' : 'BAAI/bge-reranker-v2-m3'
    setForm({ ...emptyForm, config_type: configType, model_name: defaultModel })
    setTestResult(null)
    setShowDialog(true)
  }

  function openEdit(item: EmbedConfigItem) {
    setEditingItem(item)
    setForm({
      name: item.name,
      config_type: item.config_type,
      provider: item.provider,
      local_provider: item.local_provider || 'sentence-transformers',
      model_name: item.model_name,
      device: item.device,
      base_url: item.base_url || '',
      api_key: '',
      timeout: String(item.timeout),
      is_active: item.is_active,
    })
    setTestResult(null)
    setShowDialog(true)
  }

  function closeDialog() {
    setShowDialog(false)
    setEditingItem(null)
    setTestResult(null)
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (editingItem) {
      const payload: Record<string, unknown> = {
        name: form.name,
        provider: form.provider,
        local_provider: form.provider === 'local' ? form.local_provider : null,
        model_name: form.model_name,
        device: form.device,
        base_url: form.provider === 'remote' ? form.base_url : null,
        timeout: parseFloat(form.timeout) || 60,
        is_active: form.is_active,
      }
      if (form.api_key) payload.api_key = form.api_key
      updateMutation.mutate({ id: editingItem.id, data: payload })
    } else {
      createMutation.mutate(form)
    }
  }

  function renderConfigCard(item: EmbedConfigItem) {
    return (
      <div
        key={item.id}
        className={`rounded-xl border p-4 transition-all duration-200 hover:shadow-md ${
          item.is_active ? 'border-green-500/50 bg-green-50/50 dark:bg-green-950/10' : 'border-border bg-card'
        }`}
      >
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2">
            {item.provider === 'remote' ? (
              <Globe className="h-4 w-4 text-blue-500" />
            ) : (
              <Server className="h-4 w-4 text-purple-500" />
            )}
            <h3 className="font-medium text-sm">{item.name}</h3>
          </div>
          <div className="flex items-center gap-1">
            {item.is_active && (
              <Badge variant="default" className="text-[10px] bg-green-600">启用中</Badge>
            )}
          </div>
        </div>

        <div className="space-y-1 text-xs text-muted-foreground mb-3">
          <div className="flex justify-between">
            <span>类型</span>
            <span>{item.provider === 'remote' ? '远程服务' : `本地 (${item.local_provider})`}</span>
          </div>
          <div className="flex justify-between">
            <span>模型</span>
            <span className="truncate max-w-[180px]">{item.model_name}</span>
          </div>
          {item.provider === 'local' && (
            <div className="flex justify-between">
              <span>设备</span>
              <span>{item.device}</span>
            </div>
          )}
          {item.provider === 'remote' && item.base_url && (
            <div className="flex justify-between">
              <span>地址</span>
              <span className="truncate max-w-[180px]">{item.base_url}</span>
            </div>
          )}
        </div>

        <div className="flex items-center gap-1.5">
          {!item.is_active && (
            <Button
              size="sm"
              variant="outline"
              className="h-7 text-xs gap-1 cursor-pointer"
              onClick={() => activateMutation.mutate(item.id)}
              disabled={activateMutation.isPending}
            >
              <Zap className="h-3 w-3" />
              启用
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            className="h-7 w-7 p-0 cursor-pointer"
            onClick={() => openEdit(item)}
          >
            <Pencil className="h-3.5 w-3.5" />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 w-7 p-0 text-destructive cursor-pointer"
            onClick={() => { if (confirm('确定删除？')) deleteMutation.mutate(item.id) }}
            disabled={item.is_active}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
    )
  }

  if (isLoading) {
    return <div className="flex items-center justify-center h-64 text-muted-foreground">加载中...</div>
  }

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight">Embedding & Rerank 配置</h1>
        <p className="text-muted-foreground text-sm mt-1">
          配置向量化和重排序服务，支持本地模型或远程 API
        </p>
      </div>

      {/* Embedding 配置区域 */}
      <div className="mb-8">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Embedding 向量化</h2>
          <Button size="sm" onClick={() => openCreate('embedding')} className="gap-1.5 cursor-pointer">
            <Plus className="h-4 w-4" />
            添加配置
          </Button>
        </div>
        {embeddingConfigs.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border p-8 text-center text-muted-foreground text-sm">
            暂无配置
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {embeddingConfigs.map(renderConfigCard)}
          </div>
        )}
      </div>

      {/* Rerank 配置区域 */}
      <div>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Rerank 重排序</h2>
          <Button size="sm" onClick={() => openCreate('rerank')} className="gap-1.5 cursor-pointer">
            <Plus className="h-4 w-4" />
            添加配置
          </Button>
        </div>
        {rerankConfigs.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border p-8 text-center text-muted-foreground text-sm">
            暂无配置
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {rerankConfigs.map(renderConfigCard)}
          </div>
        )}
      </div>

      {/* 创建/编辑对话框 */}
      <Dialog open={showDialog} onOpenChange={setShowDialog}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {editingItem ? '编辑' : '添加'}
              {form.config_type === 'embedding' ? ' Embedding' : ' Rerank'} 配置
            </DialogTitle>
          </DialogHeader>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label>名称</Label>
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如：本地 bge-m3、远程 TEI 服务"
                required
              />
            </div>

            <div className="space-y-2">
              <Label>服务类型</Label>
              <Select value={form.provider} onValueChange={(val) => setForm({ ...form, provider: val })}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="local">本地模型</SelectItem>
                  <SelectItem value="remote">远程服务</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {form.provider === 'local' && (
              <>
                <div className="space-y-2">
                  <Label>Provider</Label>
                  <Select value={form.local_provider} onValueChange={(val) => setForm({ ...form, local_provider: val })}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="sentence-transformers">sentence-transformers（跨平台）</SelectItem>
                      <SelectItem value="flag-embedding">flag-embedding（稠密+稀疏）</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label>模型名称</Label>
                  <Input
                    value={form.model_name}
                    onChange={(e) => setForm({ ...form, model_name: e.target.value })}
                    placeholder="BAAI/bge-m3"
                  />
                </div>
                <div className="space-y-2">
                  <Label>设备</Label>
                  <Select value={form.device} onValueChange={(val) => setForm({ ...form, device: val })}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="cpu">CPU</SelectItem>
                      <SelectItem value="cuda">CUDA (NVIDIA GPU)</SelectItem>
                      <SelectItem value="mps">MPS (Apple Silicon)</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              </>
            )}

            {form.provider === 'remote' && (
              <>
                <div className="space-y-2">
                  <Label>服务地址</Label>
                  <Input
                    value={form.base_url}
                    onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                    placeholder={form.config_type === 'embedding' ? 'http://server:8080/v1' : 'http://server:8001/rerank'}
                    required
                  />
                  <p className="text-[11px] text-muted-foreground">
                    {form.config_type === 'embedding'
                      ? 'OpenAI 兼容接口填到 /v1（自动拼接 /embeddings），自定义接口填完整端点'
                      : '标准接口填到 /v1（自动拼接 /rerank），自定义接口填完整端点如 /ranking_score'}
                  </p>
                </div>
                <div className="space-y-2">
                  <Label>模型名称</Label>
                  <Input
                    value={form.model_name}
                    onChange={(e) => setForm({ ...form, model_name: e.target.value })}
                    placeholder="BAAI/bge-m3"
                  />
                </div>
                <div className="space-y-2">
                  <Label>API Key（可选）</Label>
                  <Input
                    type="password"
                    value={form.api_key}
                    onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                    placeholder={editingItem?.api_key_set ? '已设置，留空不修改' : '无需密钥可留空'}
                  />
                </div>
                <div className="space-y-2">
                  <Label>超时时间（秒）</Label>
                  <Input
                    type="number"
                    value={form.timeout}
                    onChange={(e) => setForm({ ...form, timeout: e.target.value })}
                    placeholder="60"
                  />
                </div>
              </>
            )}

            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="is_active"
                checked={form.is_active}
                onChange={(e) => setForm({ ...form, is_active: e.target.checked })}
                className="rounded"
              />
              <Label htmlFor="is_active" className="text-sm font-normal">
                立即启用（将替换当前使用的{form.config_type === 'embedding' ? ' Embedding' : ' Rerank'} 服务）
              </Label>
            </div>

            {/* 测试结果 */}
            {testResult && (
              <div className={`flex items-center gap-2 text-sm p-2 rounded ${testResult.success ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'}`}>
                {testResult.success ? <CheckCircle className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
                {testResult.message}
              </div>
            )}

            <DialogFooter className="gap-2">
              <Button type="button" variant="outline" onClick={handleTest} disabled={testing} className="cursor-pointer">
                {testing ? '测试中...' : '测试连通性'}
              </Button>
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

export default EmbedConfig
