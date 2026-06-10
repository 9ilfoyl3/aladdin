import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Pencil, Trash2, Star, Bot, Thermometer, RotateCcw, Brain, Wand2, Loader2, Share2, Lock, Globe } from 'lucide-react'
import { agentPresetApi } from '@/lib/api'
import { useConfirm } from '@/lib/confirm-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import CardGridSkeleton from '@/components/skeletons/CardGridSkeleton'
import { toast } from 'sonner'

interface AgentPresetItem {
  id: string
  name: string
  description: string | null
  config_json: {
    agent_mode?: 'agent' | 'hybrid'
    max_iterations?: number
    temperature?: number
    thinking_enabled?: boolean
    allowed_tools?: string[]
    custom_instructions?: string
  } | null
  is_default: boolean
  created_at: string
  updated_at: string
  // 归属与可见性（agent-preset-sharing）
  is_shared: boolean
  is_builtin: boolean
  is_owner: boolean
  owner_user_id: string | null
  owner_username: string | null
}

interface FormData {
  name: string
  description: string
  agent_mode: 'agent' | 'hybrid'
  max_iterations: string
  temperature: string
  thinking_enabled: boolean
  allowed_tools: string[]
  custom_instructions: string
  is_shared: boolean
}

const emptyForm: FormData = {
  name: '',
  description: '',
  agent_mode: 'agent',
  max_iterations: '20',
  temperature: '0.7',
  thinking_enabled: true,
  allowed_tools: ['knowledge_search', 'grep_chunks', 'list_knowledge_chunks', 'final_answer'],
  custom_instructions: '',
  is_shared: false,
}

const CUSTOM_INSTRUCTIONS_MAX = 2000

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
  const confirm = useConfirm()
  const [showDialog, setShowDialog] = useState(false)
  const [editingItem, setEditingItem] = useState<AgentPresetItem | null>(null)
  const [form, setForm] = useState<FormData>(emptyForm)
  const [showRewrite, setShowRewrite] = useState(false)
  const [rewriteInput, setRewriteInput] = useState('')

  const { data: presets = [], isLoading } = useQuery({
    queryKey: ['agent-presets'],
    queryFn: () => agentPresetApi.list() as Promise<AgentPresetItem[]>,
  })

  const createMutation = useMutation({
    mutationFn: (data: FormData) => agentPresetApi.create({
      name: data.name,
      description: data.description || undefined,
      is_shared: data.is_shared,
      config_json: {
        agent_mode: data.agent_mode,
        max_iterations: parseInt(data.max_iterations) || 20,
        temperature: parseFloat(data.temperature) || 0.7,
        thinking_enabled: data.thinking_enabled,
        allowed_tools: data.allowed_tools,
        custom_instructions: data.custom_instructions.trim() || undefined,
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
      is_shared: data.is_shared,
      config_json: {
        agent_mode: data.agent_mode,
        max_iterations: parseInt(data.max_iterations) || 20,
        temperature: parseFloat(data.temperature) || 0.7,
        thinking_enabled: data.thinking_enabled,
        allowed_tools: data.allowed_tools,
        custom_instructions: data.custom_instructions.trim() || undefined,
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
    },
  })

  // 快捷开放/关闭：直接切换 is_shared，无需进编辑弹窗（参考知识库卡片上的可见性 chip）
  const shareToggleMutation = useMutation({
    mutationFn: ({ id, is_shared }: { id: string; is_shared: boolean }) =>
      agentPresetApi.update(id, { is_shared }),
    onSuccess: (_d, vars) => {
      queryClient.invalidateQueries({ queryKey: ['agent-presets'] })
      toast.success(vars.is_shared ? '已开放给本空间' : '已设为私有')
    },
    onError: (e) => toast.error(e instanceof Error ? e.message : '操作失败'),
  })

  // 删除预设（统一确认交互）
  async function handleDelete(preset: AgentPresetItem) {
    const ok = await confirm({
      title: '删除预设',
      description: <>确定要删除预设「{preset.name}」吗？此操作不可撤销。</>,
    })
    if (ok) deleteMutation.mutate(preset.id)
  }

  const rewriteMutation = useMutation({
    mutationFn: (instruction: string) =>
      agentPresetApi.rewritePrompt({
        instruction,
        current_prompt: form.custom_instructions.trim() || undefined,
      }),
    onSuccess: (res) => {
      setForm((prev) => ({ ...prev, custom_instructions: res.prompt }))
      setShowRewrite(false)
      setRewriteInput('')
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
      agent_mode: item.config_json?.agent_mode ?? 'agent',
      max_iterations: String(item.config_json?.max_iterations ?? 20),
      temperature: String(item.config_json?.temperature ?? 0.7),
      thinking_enabled: item.config_json?.thinking_enabled ?? true,
      allowed_tools: item.config_json?.allowed_tools ?? emptyForm.allowed_tools,
      custom_instructions: item.config_json?.custom_instructions ?? '',
      is_shared: item.is_shared,
    })
    setShowDialog(true)
  }

  function closeDialog() {
    setShowDialog(false)
    setEditingItem(null)
    setShowRewrite(false)
    setRewriteInput('')
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
          <h1 className="text-2xl font-bold tracking-tight">智能体</h1>
          <p className="text-muted-foreground text-sm mt-1">管理智能体运行预设，控制迭代次数、温度等参数</p>
        </div>
        <Button onClick={openCreate} className="gap-2 cursor-pointer">
          <Plus className="h-4 w-4" />
          新建预设
        </Button>
      </div>

      {/* 预设卡片列表 */}
      {isLoading ? (
        <CardGridSkeleton count={3} />
      ) : presets.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20">
          <div className="w-16 h-16 rounded-2xl bg-muted/60 flex items-center justify-center mb-4">
            <Bot className="h-8 w-8 text-muted-foreground/60" />
          </div>
          <p className="text-muted-foreground mb-4">暂无智能体预设</p>
          <Button onClick={openCreate} variant="outline" className="gap-2 cursor-pointer">
            <Plus className="h-4 w-4" />
            新建预设
          </Button>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 animate-in fade-in-0 duration-500">
          {presets.map((preset) => (
            <div
              key={preset.id}
              className="group relative flex flex-col rounded-xl border border-border bg-card p-5 transition-all duration-200 hover:shadow-lg hover:border-primary/20 hover:-translate-y-0.5"
            >
              {preset.is_default && (
                <div className="absolute top-4 right-4">
                  <Star className="h-4 w-4 text-yellow-500 fill-yellow-500" />
                </div>
              )}
              <div className="w-10 h-10 rounded-lg bg-primary/8 flex items-center justify-center mb-3">
                <Bot className="h-5 w-5 text-primary" />
              </div>
              <div className="flex items-center gap-2 mb-2 flex-wrap">
                <h3 className="font-semibold text-base">{preset.name}</h3>
                {preset.is_default && (
                  <Badge variant="secondary" className="text-xs">默认</Badge>
                )}
                {preset.is_builtin ? (
                  <Badge variant="outline" className="text-xs gap-1"><Globe className="h-3 w-3" />内置</Badge>
                ) : preset.is_shared ? (
                  <Badge variant="outline" className="text-xs gap-1 text-primary border-primary/30"><Share2 className="h-3 w-3" />已开放</Badge>
                ) : (
                  <Badge variant="outline" className="text-xs gap-1 text-muted-foreground"><Lock className="h-3 w-3" />私有</Badge>
                )}
              </div>
              {/* 描述：固定两行高度，保证多卡片对齐 */}
              <p className="text-sm text-muted-foreground mb-3 line-clamp-2 min-h-10">
                {preset.description || '暂无描述'}
              </p>
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

              {/* 底部操作区：mt-auto 贴底，保证所有卡片底栏对齐 */}
              <div className="mt-auto pt-3 border-t border-border/60">
                {preset.is_owner ? (
                  <div className="flex items-center gap-1 flex-wrap">
                    {/* 开放/关闭快捷切换（参考知识库可见性 chip，点击即切，无需进编辑） */}
                    <button
                      onClick={() => shareToggleMutation.mutate({ id: preset.id, is_shared: !preset.is_shared })}
                      disabled={shareToggleMutation.isPending}
                      className={`text-xs px-2 py-1 rounded-md border inline-flex items-center gap-1 cursor-pointer transition-colors ${
                        preset.is_shared
                          ? 'border-primary/30 text-primary bg-primary/5 hover:bg-primary/10'
                          : 'border-border text-muted-foreground bg-background hover:bg-muted'
                      }`}
                      title={preset.is_shared ? '点击关闭开放（设为私有）' : '点击开放给本空间全体成员使用'}
                    >
                      {preset.is_shared ? <Share2 className="h-3.5 w-3.5" /> : <Lock className="h-3.5 w-3.5" />}
                      {preset.is_shared ? '已开放' : '开放'}
                    </button>
                    <div className="flex-1" />
                    <Button variant="ghost" size="sm" className="h-8 text-xs gap-1 cursor-pointer" onClick={() => openEdit(preset)}>
                      <Pencil className="h-3.5 w-3.5" />
                      编辑
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-8 text-xs gap-1 text-destructive hover:text-destructive cursor-pointer"
                      onClick={() => handleDelete(preset)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                      删除
                    </Button>
                  </div>
                ) : (
                  <div className="flex items-center gap-1.5 text-xs text-muted-foreground px-1 h-8">
                    {preset.is_builtin ? (
                      <>
                        <Globe className="h-3.5 w-3.5" />
                        <span>平台内置预设</span>
                      </>
                    ) : (
                      <>
                        <Share2 className="h-3.5 w-3.5" />
                        <span>来自 {preset.owner_username || '其他成员'}</span>
                      </>
                    )}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* 创建/编辑对话框 */}
      <Dialog open={showDialog} onOpenChange={closeDialog}>
        <DialogContent className="sm:max-w-3xl max-h-[90vh] overflow-y-auto">
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
            <div>
              <Label className="mb-2 block">运行模式</Label>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setForm({ ...form, agent_mode: 'agent' })}
                  className={`flex-1 px-3 py-2 rounded-md text-xs border cursor-pointer transition-colors ${
                    form.agent_mode === 'agent'
                      ? 'bg-primary/10 border-primary/30 text-primary'
                      : 'bg-muted/30 border-border text-muted-foreground hover:border-primary/20'
                  }`}
                >
                  智能推理（多步 ReAct）
                </button>
                <button
                  type="button"
                  onClick={() => setForm({ ...form, agent_mode: 'hybrid' })}
                  className={`flex-1 px-3 py-2 rounded-md text-xs border cursor-pointer transition-colors ${
                    form.agent_mode === 'hybrid'
                      ? 'bg-primary/10 border-primary/30 text-primary'
                      : 'bg-muted/30 border-border text-muted-foreground hover:border-primary/20'
                  }`}
                >
                  快速问答（单轮检索）
                </button>
              </div>
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
            <div className="flex items-center justify-between rounded-lg border border-border bg-muted/30 px-3 py-2.5">
              <div className="flex items-center gap-2">
                <Brain className="h-4 w-4 text-muted-foreground" />
                <div>
                  <Label htmlFor="thinking_enabled" className="text-sm cursor-pointer">启用深度思考</Label>
                  <p className="text-xs text-muted-foreground">让模型在回答前进行扩展推理（需模型支持）</p>
                </div>
              </div>
              <Switch
                id="thinking_enabled"
                checked={form.thinking_enabled}
                onCheckedChange={(checked) => setForm({ ...form, thinking_enabled: checked })}
              />
            </div>
            {/* 开放给本空间：开启后本空间全体成员可见可用（仅创建者可改/删） */}
            <div className="flex items-center justify-between rounded-lg border border-border bg-muted/30 px-3 py-2.5">
              <div className="flex items-center gap-2">
                <Share2 className="h-4 w-4 text-muted-foreground" />
                <div>
                  <Label htmlFor="is_shared" className="text-sm cursor-pointer">开放给本空间</Label>
                  <p className="text-xs text-muted-foreground">开启后，本空间所有成员可在问答中选择使用该智能体（仅你本人可修改或删除）</p>
                </div>
              </div>
              <Switch
                id="is_shared"
                checked={form.is_shared}
                onCheckedChange={(checked) => setForm({ ...form, is_shared: checked })}
              />
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
            {/* 自定义指令：仅「智能推理」模式生效，快速问答模式下隐藏。
                不覆盖核心系统提示词，只追加角色 / 语气 / 工作流 / 边界等设定。 */}
            {form.agent_mode === 'agent' && (
            <div>
              <div className="flex items-center justify-between mb-1.5">
                <Label>自定义指令</Label>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-7 text-xs gap-1 text-primary cursor-pointer"
                  onClick={() => setShowRewrite((v) => !v)}
                >
                  <Wand2 className="h-3.5 w-3.5" />
                  AI 润色
                </Button>
              </div>

              {/* AI 润色面板 */}
              {showRewrite && (
                <div className="mb-2 rounded-lg border border-primary/20 bg-primary/5 p-3">
                  <div className="flex items-center gap-1.5 mb-2 text-xs font-medium text-primary">
                    <Wand2 className="h-3.5 w-3.5" />
                    用 AI 生成自定义指令
                  </div>
                  <p className="text-xs text-muted-foreground mb-2">
                    用自然语言描述你想要的角色、语气与工作风格，AI 会帮你整理成一段规范的自定义指令（使用默认模型）。
                  </p>
                  <Input
                    value={rewriteInput}
                    onChange={(e) => setRewriteInput(e.target.value)}
                    placeholder="例如：一个严谨的法律顾问，语气正式，回答时分点说明并提示风险"
                    className="mb-2 text-xs"
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && rewriteInput.trim() && !rewriteMutation.isPending) {
                        e.preventDefault()
                        rewriteMutation.mutate(rewriteInput.trim())
                      }
                    }}
                  />
                  {rewriteMutation.isError && (
                    <p className="text-xs text-destructive mb-2">
                      生成失败，请检查默认模型配置后重试
                    </p>
                  )}
                  <div className="flex items-center gap-2">
                    <Button
                      type="button"
                      size="sm"
                      className="h-7 text-xs gap-1 cursor-pointer"
                      disabled={!rewriteInput.trim() || rewriteMutation.isPending}
                      onClick={() => rewriteMutation.mutate(rewriteInput.trim())}
                    >
                      {rewriteMutation.isPending ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Wand2 className="h-3.5 w-3.5" />
                      )}
                      {rewriteMutation.isPending ? '生成中...' : (form.custom_instructions.trim() ? '生成并替换' : '生成')}
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="h-7 text-xs text-muted-foreground cursor-pointer"
                      onClick={() => { setShowRewrite(false); setRewriteInput('') }}
                    >
                      取消
                    </Button>
                    {form.custom_instructions.trim() && (
                      <span className="text-xs text-muted-foreground">将基于当前指令润色</span>
                    )}
                  </div>
                </div>
              )}

              <Textarea
                value={form.custom_instructions}
                onChange={(e) =>
                  setForm({ ...form, custom_instructions: e.target.value.slice(0, CUSTOM_INSTRUCTIONS_MAX) })
                }
                placeholder="留空则使用默认行为。可在此描述助手的角色设定、语气风格、回答方法与边界约束，例如：以资深产品经理的身份回答，语气专业友好，回答先给结论再展开；不讨论与产品无关的话题。"
                rows={10}
                className="font-mono text-xs leading-relaxed max-h-[360px]"
              />
              <div className="flex items-center justify-between mt-1.5">
                <p className="text-xs text-muted-foreground">
                  仅在「智能推理」模式下生效，会在系统内置提示词基础上追加，不影响核心检索能力
                </p>
                <span className="text-xs text-muted-foreground tabular-nums">
                  {form.custom_instructions.length} / {CUSTOM_INSTRUCTIONS_MAX}
                </span>
              </div>
            </div>
            )}
            <DialogFooter>
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

export default AgentConfig
