import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw, Layers } from 'lucide-react'
import { systemApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { PasswordInput } from '@/components/ui/password-input'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Label } from '@/components/ui/label'
import SettingsFormSkeleton from '@/components/skeletons/SettingsFormSkeleton'

// 配置数据类型
interface SystemConfig {
  parent_chunk_size: number
  child_chunk_size: number
  chunk_overlap: number
  [key: string]: string | number | boolean
}

// 配置字段定义
interface ConfigField {
  key: string
  label: string
  type: string
  hint?: string
  options?: string[]
  optionLabels?: string[]
  visibleWhen?: (form: SystemConfig) => boolean
}

// 配置字段分组定义
interface ConfigGroup {
  title: string
  icon: React.ReactNode
  description: string
  fields: ConfigField[]
}

// 系统配置页面
function Settings() {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<SystemConfig | null>(null)
  const [saved, setSaved] = useState(false)

  const configGroups: ConfigGroup[] = [
    {
      title: '切片配置',
      icon: <Layers className="h-5 w-5 text-primary" />,
      description: '文档切片参数，影响检索粒度和上下文完整性',
      fields: [
        { key: 'parent_chunk_size', label: '父块大小', type: 'number' },
        { key: 'child_chunk_size', label: '子块大小', type: 'number' },
        { key: 'chunk_overlap', label: '重叠大小', type: 'number' },
      ],
    },
  ]

  const { data: config, isLoading } = useQuery({
    queryKey: ['system-config'],
    queryFn: () => systemApi.getConfig() as Promise<SystemConfig>,
  })

  useEffect(() => {
    if (config) {
      setForm({ ...config })
    }
  }, [config])

  const saveMutation = useMutation({
    mutationFn: (data: SystemConfig) => systemApi.updateConfig(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['system-config'] })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    },
  })

  function updateField(key: string, value: string | number | boolean) {
    if (!form) return
    setForm({ ...form, [key]: value })
  }

  function handleSave(e: React.FormEvent) {
    e.preventDefault()
    if (form) {
      saveMutation.mutate(form)
    }
  }

  if (isLoading || !form) {
    return (
      <div>
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-2xl font-bold tracking-tight">系统配置</h1>
            <p className="text-muted-foreground text-sm mt-1">配置模型参数、检索策略和系统设置</p>
          </div>
        </div>
        <SettingsFormSkeleton groups={4} fieldsPerGroup={4} />
      </div>
    )
  }

  return (
    <div className="animate-in fade-in-0 duration-500">
      {/* 页面头部 */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">系统配置</h1>
          <p className="text-muted-foreground text-sm mt-1">配置模型参数、检索策略和系统设置</p>
        </div>
        <Button onClick={handleSave} disabled={saveMutation.isPending} className="gap-2 cursor-pointer">
          {saveMutation.isPending ? (
            <RefreshCw className="h-4 w-4 animate-spin" />
          ) : (
            <Save className="h-4 w-4" />
          )}
          {saved ? '已保存' : '保存配置'}
        </Button>
      </div>

      {/* 配置表单 */}
      <form onSubmit={handleSave} className="space-y-5">
        {configGroups.map((group) => (
          <div
            key={group.title}
            className="rounded-xl border border-border bg-card p-5 transition-all duration-200 hover:border-primary/20"
          >
            {/* 分组头部 */}
            <div className="flex items-center gap-3 mb-4">
              <div className="w-9 h-9 rounded-lg bg-primary/8 flex items-center justify-center shrink-0">
                {group.icon}
              </div>
              <div>
                <h3 className="font-semibold text-sm">{group.title}</h3>
                <p className="text-xs text-muted-foreground">{group.description}</p>
              </div>
            </div>

            {/* 字段 */}
            <div className="grid gap-4 md:grid-cols-2">
              {group.fields
                .filter((field) => !field.visibleWhen || field.visibleWhen(form))
                .map((field) => (
                <div key={field.key} className="space-y-1.5 relative">
                  <Label className="text-xs">{field.label}</Label>
                  {field.type === 'switch' ? (
                    <div className="flex items-center h-9">
                      <Switch
                        checked={!!form[field.key]}
                        onCheckedChange={(val) => updateField(field.key, val)}
                      />
                    </div>
                  ) : field.type === 'select' && field.options ? (
                    <Select
                      value={String(form[field.key] ?? '')}
                      onValueChange={(val) => updateField(field.key, val === '__none__' ? '' : val)}
                    >
                      <SelectTrigger className="h-9">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {field.options.map((opt, idx) => (
                          <SelectItem key={opt || '__none__'} value={opt || '__none__'}>
                            {field.optionLabels ? field.optionLabels[idx] : opt}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : field.type === 'number' ? (
                    <Input
                      type="number"
                      value={form[field.key] != null ? Number(form[field.key]) : ''}
                      onChange={(e) => updateField(field.key, Number(e.target.value))}
                      className="h-9"
                    />
                  ) : field.type === 'password' ? (
                    <PasswordInput
                      value={String(form[field.key] || '')}
                      onChange={(e) => updateField(field.key, e.target.value)}
                      className="h-9"
                    />
                  ) : (
                    <Input
                      type="text"
                      value={String(form[field.key] || '')}
                      onChange={(e) => updateField(field.key, e.target.value)}
                      className="h-9"
                    />
                  )}
                  {field.hint && (
                    <p className="text-[11px] text-muted-foreground">{field.hint}</p>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}
      </form>

      {/* 保存错误提示 */}
      {saveMutation.isError && (
        <div className="mt-4 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
          保存失败: {saveMutation.error?.message || '未知错误'}
        </div>
      )}
    </div>
  )
}

export default Settings
