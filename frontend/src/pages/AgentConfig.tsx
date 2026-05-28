import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Pencil, Trash2, Star, Bot, Thermometer, RotateCcw, Brain } from 'lucide-react'
import { agentPresetApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'

interface AgentPresetItem {
  id: string
  name: string
  description: string | null
  config_json: {
    max_iterations?: number
    temperature?: number
    thinking_enabled?: boolean
    allowed_tools?: string[]
  } | null
  is_default: boolean
  created_at: string
  updated_at: string
}

interface FormData {
  name: string
  description: string
  max_iterations: string
  temperature: string
  thinking_enabled: boolean
  allowed_tools: string[]
}

const emptyForm: FormData = {
  name: '',
  description: '',
  max_iterations: '20',
  temperature: '0.7',
  thinking_enabled: true,
  allowed_tools: ['knowledge_search', 'grep_chunks', 'list_knowledge_chunks', 'final_answer'],
}

const ALL_TOOLS = [
  { value: 'knowledge_search', label: '语义检索' },
  { value: 'grep_chunks', label: '关键词检索' },
  { value: 'list_knowledge_chunks', label: '分页浏览' },
  { value: 'web_search', label: '网页搜索' },
  { value: 'thinking', label: '内部思考' },
  { value: 'final_answer', label: '最终答案' },
]

function AgentConfig() {
  const queryClient = useQueryClient()
  const [showDialog, setShowDialog] = useState(false)
  const [editingItem, setEditingItem] = useState<AgentPresetItem | null>(null)
  const [form, setForm] = useState<FormData>(emptyForm)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState<string | null>(null)

  const { data: presets = [], isLoading } = useQuery({
    queryKey: ['agent-presets'],
    queryFn: () => agentPresetApi.list() as Promise<AgentPresetItem[]>,
  })

  const createMutation = useMutation({
    mutationFn: (data: FormData) => agentPresetApi.create({
      name: data.name,
      description: data.description || undefined,
      config_json: {
        max_iterations: parseInt(data.max_iterations) || 20,
        temperature: parseFloat(data.temperature) || 0.7,
        thinking_enabled: data.thinking_enabled,
        allowed_tools: data.allowed_tools,
      },
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-presets'] })
      closeDialog()
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: FormData }) => agentPresetApi.update(id, {
      name: data.name,
      description: data.description || undefined,
      config_json: {
        max_iterations: parseInt(data.max_iterations) || 20,
        temperature: parseFloat(data.temperature) || 0.7,
        thinking_enabled: data.thinking_enabled,
        allowed_tools: data.allowed_tools,
      },
    }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-presets'] })
      closeDialog()
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => agentPresetApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-presets'] })
      setShowDeleteConfirm(null)
    },
  })

  function openCreate() {
    setEditingItem(null)
    setForm(emptyForm)
    setShowDialog(true)
  }

  function openEdit(item: AgentPresetItem) {
    setEditingItem(item)
    setForm({
      name: item.name,
      description: item.description || '',
      max_iterations: String(item.config_json?.max_iterations ?? 20),
      temperature: String(item.config_json?.temperature ?? 0.7),
      thinking_enabled: item.config_json?.thinking_enabled ?? true,
      allowed_tools: item.config_json?.allowed_tools ?? emptyForm.allowed_tools,
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

  function toggleTool(tool: string) {
    setForm(prev => ({
      ...prev,
      allowed_tools: prev.allowed_tools.includes(tool)
        ? prev.allowed_tools.filter(t => t !== tool)
        : [...prev.allowed_tools, tool],
    }))
  }

  return (
    <div>
      {/* 页面头部 */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Agent 配置</h1>
          <p className="text-muted-foreground text-sm mt-1">管理 Agent 运行预设，控制迭代次数、温度等参数</p>
        </div>
        <Button onClick={openCreate} className="gap-2 cursor-pointer">
          <Plus className="h-4 w-4" />
          新建预设
        </Button>
      </div>

      {/* 预设卡片列表 */}
      {isLoading ? (
        <div className="flex items-center justify-center py-20">
          <div className="text-center">
            <div className="h-8 w-8 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto mb-3" />
            <p className="text-sm text-muted-foreground">加载中...</p>
          </div>
        </div>
      ) : presets.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20">
          <div className="w-16 h-16 rounded-2xl bg-muted/60 flex items-center justify-center mb-4">
            <Bot className="h-8 w-8 text-muted-foreground/60" />
          </div>
          <p className="text-muted-foreground mb-4">暂无 Agent 预设</p>
          <Button onClick={openCreate} variant="outline" className="gap-2 cursor-pointer">
            <Plus className="h-4 w-4" />
            新建预设
          </Button>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {presets.map((preset) => (
            <div
              key={preset.id}
              className="group relative rounded-xl border border-border bg-card p-5 transition-all duration-200 hover:shadow-lg hover:border-primary/20 hover:-translate-y-0.5"
            >
              {preset.is_default && (
                <div className="absolute top-4 right-4">
                  <Star className="h-4 w-4 text-yellow-500 fill-yellow-500" />
                </div>
              )}
              <div className="w-10 h-10 rounded-lg bg-primary/8 flex items-center justify-center mb-3">
                <Bot className="h-5 w-5 text-primary" />
              </div>
              <div className="flex items-center gap-2 mb-2">
                <h3 className="font-semibold text-base">{preset.name}</h3>
                {preset.is_default && (
                  <Badge variant="secondary" className="text-xs">默认</Badge>
                )}
              </div>
              {preset.description && (
                <p className="text-sm text-muted-foreground mb-3">{preset.description}</p>
              )}
              <div className="space-y-1.5 text-sm text-muted-foreground mb-4">
                <p className="flex items-center gap-1.5">
                  <RotateCcw className="h-3.5 w-3.5" />
                  <span>最大迭代: {preset.config_json?.max_iterations ?? '-'}</span>
                </p>
                <p className="flex items-center gap-1.5">
                  <Thermometer className="h-3.5 w-3.5" />
                  <span>温度: {preset.config_json?.temperature ?? '-'}</span>
                </p>
                <p className="flex items-center gap-1.5">
                  <Brain className="h-3.5 w-3.5" />
                  <span>深度思考: {preset.config_json?.thinking_enabled ? '开启' : '关闭'}</span>
                </p>
              </div>
              <div className="flex items-center gap-1 pt-3 border-t border-border/60">
                <Button variant="ghost" size="sm" className="h-8 text-xs gap-1 cursor-pointer" onClick={() => openEdit(preset)}>
                  <Pencil className="h-3.5 w-3.5" />
                  编辑
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 text-xs gap-1 text-destructive hover:text-destructive cursor-pointer"
                  onClick={() => setShowDeleteConfirm(preset.id)}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  删除
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 创建/编辑对话框 */}
      <Dialog open={showDialog} onOpenChange={closeDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editingItem ? '编辑预设' : '新建预设'}</DialogTitle>
          </DialogHeader>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <Label>名称</Label>
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如：快速问答"
                className="mt-1.5"
                required
              />
            </div>
            <div>
              <Label>描述</Label>
              <Input
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="预设用途说明（可选）"
                className="mt-1.5"
              />
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <Label>最大迭代次数</Label>
                <Input
                  type="number"
                  value={form.max_iterations}
                  onChange={(e) => setForm({ ...form, max_iterations: e.target.value })}
                  min="1"
                  max="50"
                  className="mt-1.5"
                />
              </div>
              <div>
                <Label>温度</Label>
                <Input
                  type="number"
                  value={form.temperature}
                  onChange={(e) => setForm({ ...form, temperature: e.target.value })}
                  min="0"
                  max="2"
                  step="0.1"
                  className="mt-1.5"
                />
              </div>
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
            </div>
            <div>
              <Label className="mb-2 block">允许的工具</Label>
              <div className="flex flex-wrap gap-2">
                {ALL_TOOLS.map((tool) => (
                  <button
                    key={tool.value}
                    type="button"
                    onClick={() => toggleTool(tool.value)}
                    className={`px-3 py-1.5 rounded-md text-xs border cursor-pointer transition-colors ${
                      form.allowed_tools.includes(tool.value)
                        ? 'bg-primary/10 border-primary/30 text-primary'
                        : 'bg-muted/30 border-border text-muted-foreground hover:border-primary/20'
                    }`}
                  >
                    {tool.label}
                  </button>
                ))}
              </div>
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={closeDialog} className="cursor-pointer">取消</Button>
              <Button type="submit" disabled={createMutation.isPending || updateMutation.isPending} className="cursor-pointer">
                {editingItem ? '保存' : '创建'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* 删除确认对话框 */}
      <Dialog open={!!showDeleteConfirm} onOpenChange={() => setShowDeleteConfirm(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">确定要删除此预设吗？此操作不可撤销。</p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setShowDeleteConfirm(null)} className="cursor-pointer">取消</Button>
            <Button
              variant="destructive"
              onClick={() => showDeleteConfirm && deleteMutation.mutate(showDeleteConfirm)}
              disabled={deleteMutation.isPending}
              className="cursor-pointer"
            >
              删除
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default AgentConfig
