import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Pencil, Trash2, CheckCircle, XCircle, Zap, Globe, Power } from 'lucide-react'
import { embedConfigApi } from '@/lib/api'
import type { EmbedConfigItem } from '@/lib/api'
import { useConfirm } from '@/lib/confirm-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { PasswordInput } from '@/components/ui/password-input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import CardGridSkeleton from '@/components/skeletons/CardGridSkeleton'
import { Skeleton } from '@/components/ui/skeleton'

interface FormData {
  name: string
  config_type: string
  model_name: string
  base_url: string
  api_key: string
  timeout: string
  sparse_enabled: boolean
  is_active: boolean
}

const emptyForm: FormData = {
  name: '',
  config_type: 'embedding',
  model_name: 'BAAI/bge-m3',
  base_url: '',
  api_key: '',
  timeout: '60',
  sparse_enabled: true,
  is_active: false,
}

function EmbedConfig() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
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
      model_name: data.model_name,
      base_url: data.base_url,
      api_key: data.api_key || undefined,
      timeout: parseFloat(data.timeout) || 60,
      sparse_enabled: data.config_type === 'embedding' ? data.sparse_enabled : undefined,
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

  // 删除配置（统一确认交互）
  async function handleDelete(item: EmbedConfigItem) {
    const ok = await confirm({
      title: '删除配置',
      description: <>确定要删除「{item.name}」吗？此操作不可撤销。</>,
    })
    if (ok) deleteMutation.mutate(item.id)
  }

  const activateMutation = useMutation({
    mutationFn: (id: string) => embedConfigApi.update(id, { is_active: true }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['embed-configs'] })
    },
  })

  const [testResult, setTestResult] = useState<{ success: boolean; message: string } | null>(null)
  const [testing, setTesting] = useState(false)

  // 卡片列表中的测试状态：支持多个同时测试
  const [cardTestingIds, setCardTestingIds] = useState<Set<string>>(new Set())
  const [cardTestResults, setCardTestResults] = useState<Record<string, { success: boolean; message: string }>>({})

  async function handleTest() {
    setTesting(true)
    setTestResult(null)
    try {
      const result = await embedConfigApi.test({
        config_type: form.config_type,
        model_name: form.model_name,
        base_url: form.base_url,
        api_key: form.api_key || undefined,
        timeout: parseFloat(form.timeout) || 60,
        config_id: editingItem?.id,
        sparse_enabled: form.config_type === 'embedding' ? form.sparse_enabled : undefined,
      })
      setTestResult(result)
    } catch {
      setTestResult({ success: false, message: '请求失败' })
    } finally {
      setTesting(false)
    }
  }

  async function handleCardTest(item: EmbedConfigItem) {
    setCardTestingIds((prev) => new Set(prev).add(item.id))
    setCardTestResults((prev) => {
      const next = { ...prev }
      delete next[item.id]
      return next
    })
    try {
      const result = await embedConfigApi.testSaved(item.id)
      setCardTestResults((prev) => ({ ...prev, [item.id]: result }))
    } catch {
      setCardTestResults((prev) => ({ ...prev, [item.id]: { success: false, message: '请求失败' } }))
    } finally {
      setCardTestingIds((prev) => {
        const next = new Set(prev)
        next.delete(item.id)
        return next
      })
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
      model_name: item.model_name,
      base_url: item.base_url || '',
      api_key: '',
      timeout: String(item.timeout),
      sparse_enabled: item.sparse_enabled,
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
        model_name: form.model_name,
        base_url: form.base_url,
        timeout: parseFloat(form.timeout) || 60,
        sparse_enabled: form.config_type === 'embedding' ? form.sparse_enabled : undefined,
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
            <Globe className="h-4 w-4 text-blue-500" />
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
            <span>模型</span>
            <span className="truncate max-w-[180px]">{item.model_name}</span>
          </div>
          {item.base_url && (
            <div className="flex justify-between">
              <span>地址</span>
              <span className="truncate max-w-[180px]">{item.base_url}</span>
            </div>
          )}
          {item.config_type === 'embedding' && (
            <div className="flex justify-between">
              <span>Sparse 向量</span>
              <span>{item.sparse_enabled ? '已启用' : '未启用'}</span>
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
              <Power className="h-3 w-3" />
              启用
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            className="h-7 text-xs gap-1 cursor-pointer"
            onClick={() => handleCardTest(item)}
            disabled={cardTestingIds.has(item.id)}
          >
            <Zap className="h-3 w-3" />
            {cardTestingIds.has(item.id) ? '测试中...' : '测试'}
          </Button>
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
            onClick={() => handleDelete(item)}
            disabled={item.is_active}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>

        {/* 卡片测试结果 */}
        {cardTestResults[item.id] && (
          <div className={`flex items-center gap-2 text-xs mt-2 p-1.5 rounded ${cardTestResults[item.id].success ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'}`}>
            {cardTestResults[item.id].success ? <CheckCircle className="h-3 w-3 shrink-0" /> : <XCircle className="h-3 w-3 shrink-0" />}
            <span className="truncate">{cardTestResults[item.id].message}</span>
          </div>
        )}
      </div>
    )
  }

  if (isLoading) {
    return (
      <div>
        <div className="mb-6">
          <h1 className="text-2xl font-bold tracking-tight">Embedding & Rerank 配置</h1>
          <p className="text-muted-foreground text-sm mt-1">
            配置向量化和重排序远程服务地址
          </p>
        </div>
        {/* Embedding 区骨架 */}
        <div className="mb-8">
          <div className="flex items-center justify-between mb-4">
            <Skeleton className="h-6 w-32" />
            <Skeleton className="h-8 w-24 rounded-md" />
          </div>
          <CardGridSkeleton count={3} />
        </div>
        {/* Rerank 区骨架 */}
        <div>
          <div className="flex items-center justify-between mb-4">
            <Skeleton className="h-6 w-32" />
            <Skeleton className="h-8 w-24 rounded-md" />
          </div>
          <CardGridSkeleton count={3} />
        </div>
      </div>
    )
  }

  return (
    <div className="animate-in fade-in-0 duration-500">
      <div className="mb-6">
        <h1 className="text-2xl font-bold tracking-tight">Embedding & Rerank 配置</h1>
        <p className="text-muted-foreground text-sm mt-1">
          配置向量化和重排序远程服务地址
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
            暂无配置，请添加远程 Embedding 服务
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
            暂无配置，请添加远程 Rerank 服务
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
                placeholder="如：TEI Embedding 服务、Jina Rerank"
                required
              />
            </div>

            <div className="space-y-2">
              <Label>服务地址</Label>
              <Input
                value={form.base_url}
                onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                placeholder={form.config_type === 'embedding' ? 'http://server:8080/v1' : 'http://server:8001/v1'}
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
                placeholder={form.config_type === 'embedding' ? 'BAAI/bge-m3' : 'BAAI/bge-reranker-v2-m3'}
              />
            </div>

            <div className="space-y-2">
              <Label>API Key（可选）</Label>
              <PasswordInput
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

            {form.config_type === 'embedding' && (
              <div className="flex items-center gap-2">
                <input
                  type="checkbox"
                  id="sparse_enabled"
                  checked={form.sparse_enabled}
                  onChange={(e) => setForm({ ...form, sparse_enabled: e.target.checked })}
                  className="rounded"
                />
                <Label htmlFor="sparse_enabled" className="text-sm font-normal">
                  启用 Sparse 向量（需远程服务支持 /embed_sparse 端点，如 TEI 部署的 BGE-M3）
                </Label>
              </div>
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
