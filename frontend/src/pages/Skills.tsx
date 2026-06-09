import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Plus, Pencil, Trash2, Sparkles, XCircle, Wand2, Loader2 } from 'lucide-react'
import { skillsApi } from '@/lib/api'
import type { CustomSkillItem } from '@/lib/api'
import { useConfirm } from '@/lib/confirm-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import CardGridSkeleton from '@/components/skeletons/CardGridSkeleton'

interface SkillFormData {
  name: string
  description: string
  instructions: string
  enabled: boolean
}

const emptyForm: SkillFormData = {
  name: '',
  description: '',
  instructions: '',
  enabled: true,
}

function Skills() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const [showDialog, setShowDialog] = useState(false)
  const [editingItem, setEditingItem] = useState<CustomSkillItem | null>(null)
  const [form, setForm] = useState<SkillFormData>(emptyForm)
  const [formError, setFormError] = useState<string | null>(null)
  // AI 生成：用户用一句话描述想要的技能，生成后回填表单供编辑确认
  const [aiInstruction, setAiInstruction] = useState('')

  const { data: skills = [], isLoading } = useQuery({
    queryKey: ['skills'],
    queryFn: () => skillsApi.list(),
  })

  const createMutation = useMutation({
    mutationFn: (data: SkillFormData) =>
      skillsApi.create({
        name: data.name,
        description: data.description,
        instructions: data.instructions,
        enabled: data.enabled,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['skills'] })
      closeDialog()
      toast('技能已创建')
    },
    onError: (err: Error) => setFormError(err.message),
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: SkillFormData }) =>
      skillsApi.update(id, {
        name: data.name,
        description: data.description,
        instructions: data.instructions,
        enabled: data.enabled,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['skills'] })
      closeDialog()
      toast('技能已保存')
    },
    onError: (err: Error) => setFormError(err.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => skillsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['skills'] })
      toast('技能已删除')
    },
    onError: (err: Error) => toast(`删除失败: ${err.message}`),
  })

  // 列表内快速启停：直接 PUT enabled，乐观提示由 invalidate 兜底
  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      skillsApi.update(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['skills'] }),
    onError: (err: Error) => toast(`操作失败: ${err.message}`),
  })

  // AI 生成技能：把一句话描述交给模型，产出 name/description/instructions 回填表单
  const generateMutation = useMutation({
    mutationFn: (instruction: string) => skillsApi.generate({ instruction }),
    onSuccess: (result) => {
      setForm((prev) => ({
        ...prev,
        name: result.name,
        description: result.description,
        instructions: result.instructions,
      }))
      toast('已生成，请检查后保存')
    },
    onError: (err: Error) => setFormError(err.message),
  })

  async function handleDelete(skill: CustomSkillItem) {
    const ok = await confirm({
      title: '删除技能',
      description: <>确定要删除技能「{skill.name}」吗？此操作不可撤销。</>,
    })
    if (ok) deleteMutation.mutate(skill.id)
  }

  function openCreate() {
    setEditingItem(null)
    setForm(emptyForm)
    setFormError(null)
    setAiInstruction('')
    setShowDialog(true)
  }

  function openEdit(item: CustomSkillItem) {
    setEditingItem(item)
    setForm({
      name: item.name,
      description: item.description,
      instructions: item.instructions,
      enabled: item.enabled,
    })
    setFormError(null)
    setAiInstruction('')
    setShowDialog(true)
  }

  function closeDialog() {
    setShowDialog(false)
    setEditingItem(null)
    setFormError(null)
    setAiInstruction('')
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setFormError(null)
    if (editingItem) {
      updateMutation.mutate({ id: editingItem.id, data: form })
    } else {
      createMutation.mutate(form)
    }
  }

  return (
    <div>
      {/* 页面头部 */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">技能</h1>
          <p className="text-muted-foreground text-sm mt-1">
            维护你专属的 Agent 技能。对话时智能体会根据问题按需加载匹配的技能指令，与平台内置技能一起生效。
          </p>
        </div>
        <Button onClick={openCreate} className="gap-2 cursor-pointer">
          <Plus className="h-4 w-4" />
          新建技能
        </Button>
      </div>

      {/* 技能列表 */}
      {isLoading ? (
        <CardGridSkeleton count={3} />
      ) : skills.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20">
          <div className="w-16 h-16 rounded-2xl bg-muted/60 flex items-center justify-center mb-4">
            <Sparkles className="h-8 w-8 text-muted-foreground/60" />
          </div>
          <p className="text-muted-foreground mb-4">还没有自定义技能，新建一个开始吧</p>
          <Button onClick={openCreate} variant="outline" className="gap-2 cursor-pointer">
            <Plus className="h-4 w-4" />
            新建技能
          </Button>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 animate-in fade-in-0 duration-500">
          {skills.map((skill) => (
            <div
              key={skill.id}
              className="group relative rounded-xl border border-border bg-card p-5 transition-all duration-200 hover:shadow-lg hover:border-primary/20 hover:-translate-y-0.5"
            >
              {/* 类型图标 */}
              <div className="w-10 h-10 rounded-lg bg-primary/8 flex items-center justify-center mb-3">
                <Sparkles className="h-5 w-5 text-primary" />
              </div>

              {/* 名称 + 启用状态 */}
              <div className="flex items-center gap-2 mb-2">
                <h3 className="font-semibold text-base truncate">{skill.name}</h3>
                {skill.enabled ? (
                  <Badge variant="outline" className="text-xs bg-green-50 text-green-700 border-green-200 shrink-0">
                    已启用
                  </Badge>
                ) : (
                  <Badge variant="outline" className="text-xs text-muted-foreground shrink-0">
                    已停用
                  </Badge>
                )}
              </div>

              {/* 描述 */}
              <p className="text-sm text-muted-foreground mb-4 line-clamp-3 min-h-15">
                {skill.description}
              </p>

              {/* 操作按钮 */}
              <div className="flex items-center gap-1 pt-3 border-t border-border/60">
                <div className="flex items-center gap-2 mr-auto">
                  <Switch
                    checked={skill.enabled}
                    onCheckedChange={(checked) => toggleMutation.mutate({ id: skill.id, enabled: checked })}
                  />
                  <span className="text-xs text-muted-foreground">启用</span>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 text-xs gap-1 cursor-pointer"
                  onClick={() => openEdit(skill)}
                >
                  <Pencil className="h-3.5 w-3.5" />
                  编辑
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 text-xs gap-1 text-destructive hover:text-destructive cursor-pointer"
                  onClick={() => handleDelete(skill)}
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
            <DialogTitle>{editingItem ? '编辑技能' : '新建技能'}</DialogTitle>
          </DialogHeader>
          <form onSubmit={handleSubmit} className="space-y-4">
            {/* AI 生成：一句话描述 → 生成 name/description/instructions 回填 */}
            <div className="rounded-lg border border-primary/20 bg-primary/5 p-3 space-y-2">
              <div className="flex items-center gap-1.5 text-sm font-medium text-primary">
                <Wand2 className="h-4 w-4" />
                AI 生成
              </div>
              <Textarea
                value={aiInstruction}
                onChange={(e) => setAiInstruction(e.target.value)}
                placeholder="用一句话描述你想要的技能，例如：我想要一个审查合同风险条款的技能"
                className="min-h-[60px] bg-background"
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="gap-1.5 cursor-pointer"
                disabled={generateMutation.isPending || !aiInstruction.trim()}
                onClick={() => {
                  setFormError(null)
                  generateMutation.mutate(aiInstruction.trim())
                }}
              >
                {generateMutation.isPending ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Wand2 className="h-3.5 w-3.5" />
                )}
                {generateMutation.isPending ? '生成中...' : '生成技能'}
              </Button>
              <p className="text-xs text-muted-foreground">
                生成结果会填入下方表单，你可以修改后再保存。
              </p>
            </div>

            <div>
              <Label>技能名称</Label>
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如：合同条款审查"
                className="mt-1.5"
                required
              />
            </div>
            <div>
              <Label>技能描述</Label>
              <Textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="一句话说明这个技能擅长什么、什么时候该用。智能体据此判断是否加载该技能。"
                className="mt-1.5 min-h-[72px]"
                required
              />
              <p className="text-xs text-muted-foreground mt-1">
                描述要点明触发场景（如"当用户需要……时使用"），智能体只看得到名称和描述来决定是否加载。
              </p>
            </div>
            <div>
              <Label>技能指令</Label>
              <Textarea
                value={form.instructions}
                onChange={(e) => setForm({ ...form, instructions: e.target.value })}
                placeholder={'技能被加载后，智能体将遵循这里的完整操作指南。支持 Markdown。\n\n例如：\n## 工作流程\n1. 先检索相关条款\n2. 逐条精读原文\n3. 输出结构化结论'}
                className="mt-1.5 min-h-[200px] font-mono text-xs"
                required
              />
            </div>
            <div className="flex items-center gap-2">
              <Switch
                checked={form.enabled}
                onCheckedChange={(checked) => setForm({ ...form, enabled: checked })}
                id="skill_enabled"
              />
              <Label htmlFor="skill_enabled" className="text-sm font-normal cursor-pointer">
                启用（停用后对话时不加载此技能）
              </Label>
            </div>

            {/* 表单错误提示 */}
            {formError && (
              <div className="p-2.5 rounded-lg text-xs bg-red-50 text-red-700 border border-red-200">
                <div className="flex items-center gap-1.5">
                  <XCircle className="h-3.5 w-3.5" />
                  <span>{formError}</span>
                </div>
              </div>
            )}

            <DialogFooter>
              <Button type="button" variant="outline" onClick={closeDialog} className="cursor-pointer">
                取消
              </Button>
              <Button type="submit" disabled={createMutation.isPending || updateMutation.isPending} className="cursor-pointer">
                {createMutation.isPending || updateMutation.isPending ? '提交中...' : editingItem ? '保存' : '创建'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default Skills
