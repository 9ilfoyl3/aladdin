import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Palette, Layers, Monitor, Sun, Moon, Check, RefreshCw, Save } from 'lucide-react'
import { toast } from 'sonner'
import { systemApi } from '@/lib/api'
import { useTheme, type Theme } from '@/lib/theme-context'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

interface SettingsDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** 是否可管理 Chunk 切片配置（租户管理员）。非管理员仅展示「外观」。 */
  canManageChunk?: boolean
}

type SectionId = 'appearance' | 'chunk'

const ALL_SECTIONS: { id: SectionId; label: string; icon: typeof Palette }[] = [
  { id: 'appearance', label: '外观', icon: Palette },
  { id: 'chunk', label: 'Chunk 设置', icon: Layers },
]

// 系统设置弹窗：左侧分类导航 + 右侧设置内容。
// 当前包含「外观」（主题切换）与「Chunk 设置」（切片参数，原系统配置页迁移而来）。
// 后续 embedding / ocr 等配置也可作为新的 section 加入。
export default function SettingsDialog({ open, onOpenChange, canManageChunk = false }: SettingsDialogProps) {
  const sections = ALL_SECTIONS.filter((s) => s.id !== 'chunk' || canManageChunk)
  const [active, setActive] = useState<SectionId>('appearance')

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-5xl w-[92vw] gap-0 p-0 overflow-hidden">
        <DialogHeader className="px-8 pt-7 pb-5 space-y-0.5">
          <DialogTitle className="text-base font-semibold">设置</DialogTitle>
          <DialogDescription className="text-xs">
            根据你的偏好调整界面外观与文档处理行为。
          </DialogDescription>
        </DialogHeader>

        <div className="flex gap-8 min-h-[600px] px-8 pb-8">
          {/* 左侧分类导航：靠选中胶囊底色区分，不用分割线 */}
          <nav className="w-48 shrink-0 space-y-0.5">
            {sections.map((s) => (
              <button
                key={s.id}
                onClick={() => setActive(s.id)}
                className={cn(
                  'w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm transition-colors cursor-pointer',
                  active === s.id
                    ? 'bg-accent text-accent-foreground font-medium'
                    : 'text-muted-foreground hover:bg-accent/60 hover:text-accent-foreground'
                )}
              >
                <s.icon className="h-4 w-4 shrink-0" />
                {s.label}
              </button>
            ))}
          </nav>

          {/* 右侧内容 */}
          <div className="flex-1 min-w-0 overflow-auto">
            {active === 'appearance' && <AppearanceSection />}
            {active === 'chunk' && <ChunkSection onClose={() => onOpenChange(false)} />}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

// 外观设置：主题切换（系统 / 浅色 / 深色）
const THEME_OPTIONS: { value: Theme; label: string; hint: string; icon: typeof Monitor }[] = [
  { value: 'system', label: '系统', hint: '自动跟随系统主题', icon: Monitor },
  { value: 'light', label: '浅色', hint: '明亮配色，适合白天使用', icon: Sun },
  { value: 'dark', label: '深色', hint: '柔和暗色，减少夜间疲劳', icon: Moon },
]

// 迷你应用界面缩略图：顶栏（红绿灯 + 胶囊按钮）+ 侧栏 + 头像块 + 文本行 + 虚线占位框
function ThemePreview({ dark }: { dark: boolean }) {
  const bg = dark ? 'bg-neutral-900' : 'bg-white'
  const topbar = dark ? 'bg-neutral-800' : 'bg-gray-100'
  const sidebar = dark ? 'border-neutral-700' : 'border-gray-200'
  const block = dark ? 'bg-neutral-700' : 'bg-gray-200'
  const line = dark ? 'bg-neutral-700/70' : 'bg-gray-200'
  const dashed = dark ? 'border-neutral-700' : 'border-gray-300'

  return (
    <div className={cn('rounded-lg border overflow-hidden', sidebar, bg)}>
      {/* 顶栏 */}
      <div className={cn('flex items-center gap-1.5 px-2 py-1.5', topbar)}>
        <span className="h-1.5 w-1.5 rounded-full bg-primary" />
        <span className={cn('h-1.5 w-6 rounded-full', block)} />
        <span className={cn('h-1.5 w-4 rounded-full', block)} />
      </div>
      {/* 主体：侧栏 + 内容 */}
      <div className="flex gap-2 p-2">
        {/* 侧栏竖线 */}
        <div className={cn('w-0.5 self-stretch rounded-full', block)} />
        <div className="flex-1 space-y-1.5">
          {/* 头像块 + 两行 */}
          <div className="flex items-center gap-1.5">
            <span className={cn('h-4 w-4 rounded', block)} />
            <span className={cn('h-1.5 w-10 rounded-full', line)} />
          </div>
          <span className={cn('block h-1.5 w-8 rounded-full', line)} />
          {/* 虚线占位框 */}
          <div className={cn('rounded border border-dashed p-1.5 space-y-1', dashed)}>
            <span className={cn('block h-1.5 w-full rounded-full', line)} />
            <span className={cn('block h-1.5 w-2/3 rounded-full', line)} />
          </div>
        </div>
      </div>
    </div>
  )
}

function AppearanceSection() {
  const { theme, setTheme } = useTheme()
  // 系统主题：读取当前系统偏好，决定缩略图渲染浅色还是深色
  const [systemDark, setSystemDark] = useState(
    () => window.matchMedia('(prefers-color-scheme: dark)').matches,
  )
  useEffect(() => {
    const mql = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = (e: MediaQueryListEvent) => setSystemDark(e.matches)
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [])

  return (
    <div className="space-y-3">
      <div className="space-y-0.5">
        <h3 className="text-sm font-semibold">主题</h3>
        <p className="text-xs text-muted-foreground">选择固定主题或跟随系统的界面模式。</p>
      </div>
      <div className="grid grid-cols-3 gap-3">
        {THEME_OPTIONS.map((opt) => {
          const selected = theme === opt.value
          // 系统：按当前系统偏好渲染；浅色/深色：固定渲染
          const previewDark = opt.value === 'dark' || (opt.value === 'system' && systemDark)
          return (
            <button
              key={opt.value}
              onClick={() => setTheme(opt.value)}
              className={cn(
                'group relative rounded-xl border p-3 text-left transition-colors cursor-pointer',
                selected
                  ? 'border-primary ring-1 ring-inset ring-primary'
                  : 'border-border hover:border-primary/40'
              )}
            >
              {selected && (
                <span className="absolute top-2 right-2 h-4 w-4 rounded-full bg-primary text-primary-foreground flex items-center justify-center">
                  <Check className="h-3 w-3" />
                </span>
              )}
              <div className="flex items-center gap-1.5 text-sm font-medium">
                <opt.icon className="h-4 w-4 text-muted-foreground" />
                {opt.label}
              </div>
              <p className="mt-1 text-xs text-muted-foreground leading-snug">{opt.hint}</p>
              {/* 迷你界面预览 */}
              <div className="mt-3">
                <ThemePreview dark={previewDark} />
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// 配置数据类型
interface SystemConfig {
  parent_chunk_size: number
  child_chunk_size: number
  chunk_overlap: number
  [key: string]: string | number | boolean
}

const CHUNK_FIELDS: { key: keyof SystemConfig; label: string; hint: string }[] = [
  { key: 'parent_chunk_size', label: '父块大小', hint: '父块字符数，用于补全上下文' },
  { key: 'child_chunk_size', label: '子块大小', hint: '子块字符数，用于向量检索' },
  { key: 'chunk_overlap', label: '重叠大小', hint: '相邻块之间的重叠字符数' },
]

// Chunk 设置：文档切片参数（原系统配置页迁移而来）
function ChunkSection({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<SystemConfig | null>(null)

  const { data: config, isLoading } = useQuery({
    queryKey: ['system-config'],
    queryFn: () => systemApi.getConfig() as Promise<SystemConfig>,
    enabled: true,
  })

  useEffect(() => {
    if (config) setForm({ ...config })
  }, [config])

  const saveMutation = useMutation({
    mutationFn: (data: SystemConfig) => systemApi.updateConfig(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['system-config'] })
      toast.success('配置已保存')
      onClose()
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : '保存失败')
    },
  })

  function updateField(key: string, value: number) {
    if (!form) return
    setForm({ ...form, [key]: value })
  }

  if (isLoading || !form) {
    return (
      <div className="flex items-center justify-center h-40 text-muted-foreground text-sm gap-2">
        <RefreshCw className="h-4 w-4 animate-spin" />
        加载中…
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="space-y-0.5">
        <h3 className="text-sm font-semibold">切片配置</h3>
        <p className="text-xs text-muted-foreground">文档切片参数，影响检索粒度和上下文完整性。</p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        {CHUNK_FIELDS.map((field) => (
          <div key={String(field.key)} className="space-y-1.5">
            <Label htmlFor={String(field.key)} className="text-xs">{field.label}</Label>
            <Input
              id={String(field.key)}
              type="number"
              value={form[field.key] != null ? Number(form[field.key]) : ''}
              onChange={(e) => updateField(String(field.key), Number(e.target.value))}
              className="h-9"
            />
            <p className="text-[11px] text-muted-foreground">{field.hint}</p>
          </div>
        ))}
      </div>

      <div className="flex justify-end pt-2">
        <Button
          onClick={() => form && saveMutation.mutate(form)}
          disabled={saveMutation.isPending}
          className="gap-2 cursor-pointer"
        >
          {saveMutation.isPending ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          保存
        </Button>
      </div>
    </div>
  )
}
