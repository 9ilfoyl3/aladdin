import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Palette,
  Layers,
  Monitor,
  Sun,
  Moon,
  Check,
  RefreshCw,
  Save,
  Search,
  Combine,
  SlidersHorizontal,
  Filter,
  Database,
  Server,
  RotateCcw,
} from 'lucide-react'
import { toast } from 'sonner'
import { systemApi, type PlatformConfig } from '@/lib/api'
import { useTheme, type Theme } from '@/lib/theme-context'
import { useConfirm } from '@/lib/confirm-context'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
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
  /** 是否可管理切片/检索配置（租户管理员或超管）。非管理员仅展示「外观」。 */
  canManageChunk?: boolean
  /**
   * 目标租户：超管经「租户管理」列表打开时传具体租户 id，注入 X-Tenant-ID 配该租户；
   * 普通租户管理员从账号菜单打开时不传，后端据 JWT 定位自身租户。
   */
  tenantId?: string
  /** 当前用户是否超管：决定是否展示「平台配置」分项。 */
  isSuperAdmin?: boolean
}

type SectionId = 'appearance' | 'chunk' | 'retrieval' | 'platform'

// 检索配置（六档：分块/召回/融合/精排/去重/索引），与后端 SystemConfigResponse.retrieval 对应。
interface RetrievalConfig {
  parent_chunk_size: number
  child_chunk_size: number
  chunk_overlap: number
  recall_k: number
  rerank_candidate_k: number
  rrf_k: number
  composite_rerank_weight: number
  composite_base_weight: number
  composite_source_weight: number
  rerank_threshold: number
  rerank_top_k: number
  threshold_degradation_enabled: boolean
  mmr_lambda: number
  mmr_threshold: number
  hnsw_ef: number
  hnsw_ef_construction: number
  hnsw_m: number
  [key: string]: string | number | boolean
}

interface SystemConfig {
  parent_chunk_size: number
  child_chunk_size: number
  chunk_overlap: number
  retrieval?: RetrievalConfig
  [key: string]: string | number | boolean | RetrievalConfig | undefined
}

// 字段中文标签：保存确认弹窗展示变更明细用，覆盖 retrieval 17 字段 + 平台 TTL
const FIELD_LABELS: Record<string, string> = {
  parent_chunk_size: '父块大小',
  child_chunk_size: '子块大小',
  chunk_overlap: '重叠大小',
  recall_k: '每路召回数',
  rerank_candidate_k: 'rerank 候选数',
  rrf_k: 'RRF k',
  composite_rerank_weight: 'composite rerank 权重',
  composite_base_weight: 'composite base 权重',
  composite_source_weight: 'composite source 权重',
  rerank_threshold: 'rerank 相关性阈值',
  rerank_top_k: 'rerank top_k',
  threshold_degradation_enabled: '阈值降级开关',
  mmr_lambda: 'MMR lambda',
  mmr_threshold: 'MMR threshold',
  hnsw_ef: 'HNSW 查询 ef',
  hnsw_ef_construction: 'HNSW 建索引 efConstruction',
  hnsw_m: 'HNSW 建索引 M',
  load_cache_ttl: '加载缓存 TTL',
}

// 值格式化：布尔显示开/关，空值显示 -，其余转字符串
function fmtVal(v: unknown): string {
  if (typeof v === 'boolean') return v ? '开' : '关'
  if (v == null) return '-'
  return String(v)
}

// 检索字段定义（用于「切片配置」「检索配置」表单渲染）
interface RetrievalField {
  key: string
  label: string
  type: 'number' | 'switch'
  hint?: string
  step?: number
  min?: number
  max?: number
}

interface RetrievalGroup {
  title: string
  icon: typeof Search
  description: string
  fields: RetrievalField[]
}

// 索引档共用提示文案
const INDEX_BUILD_HINT = '修改仅对新建或重建的知识库生效，且增大将提高内存占用'

// 分块档（切片配置分项）
const CHUNK_GROUP: RetrievalGroup = {
  title: '切片配置',
  icon: Layers,
  description: '文档切片参数，影响检索粒度和上下文完整性（按租户生效）',
  fields: [
    { key: 'parent_chunk_size', label: '父块大小', type: 'number', min: 100, max: 8000, hint: '父块字符数，范围 [100, 8000]' },
    { key: 'child_chunk_size', label: '子块大小', type: 'number', min: 50, max: 4000, hint: '子块字符数，范围 [50, 4000]' },
    { key: 'chunk_overlap', label: '重叠大小', type: 'number', min: 0, max: 1000, hint: '相邻切片重叠字符数，范围 [0, 1000]' },
  ],
}

// 检索五档（检索配置分项）
const RETRIEVAL_GROUPS: RetrievalGroup[] = [
  {
    title: '召回档',
    icon: Search,
    description: '控制三路召回与 rerank 候选规模',
    fields: [
      { key: 'recall_k', label: '每路召回数 (recall_k)', type: 'number', min: 1, max: 1000, hint: '每路检索召回数量，范围 [1, 1000]' },
      { key: 'rerank_candidate_k', label: 'rerank 候选数', type: 'number', min: 1, max: 200, hint: '送入 rerank 精排的候选数量，范围 [1, 200]' },
    ],
  },
  {
    title: '融合档',
    icon: Combine,
    description: 'RRF 融合常数与综合评分权重',
    fields: [
      { key: 'rrf_k', label: 'RRF k', type: 'number', min: 1, max: 1000, hint: 'RRF 融合常数，范围 [1, 1000]' },
      { key: 'composite_rerank_weight', label: 'composite rerank 权重', type: 'number', step: 0.05, min: 0, max: 1, hint: '综合评分中 rerank 分数权重，范围 [0, 1]' },
      { key: 'composite_base_weight', label: 'composite base 权重', type: 'number', step: 0.05, min: 0, max: 1, hint: '综合评分中基础分数权重，范围 [0, 1]' },
      { key: 'composite_source_weight', label: 'composite source 权重', type: 'number', step: 0.05, min: 0, max: 1, hint: '综合评分中来源分数权重，范围 [0, 1]' },
    ],
  },
  {
    title: '精排档',
    icon: SlidersHorizontal,
    description: 'rerank 软阈值过滤与结果数量',
    fields: [
      { key: 'rerank_threshold', label: 'rerank 相关性阈值', type: 'number', step: 0.05, min: 0, max: 1, hint: '低于此分数的结果被过滤，0 表示不过滤，范围 [0, 1]' },
      { key: 'rerank_top_k', label: 'rerank top_k', type: 'number', min: 1, max: 100, hint: '精排后保留的结果数量，范围 [1, 100]' },
      { key: 'threshold_degradation_enabled', label: '阈值降级开关', type: 'switch', hint: '过滤后结果为空时自动放宽阈值重试一次' },
    ],
  },
  {
    title: '去重档',
    icon: Filter,
    description: 'MMR 去冗余参数',
    fields: [
      { key: 'mmr_lambda', label: 'MMR lambda', type: 'number', step: 0.05, min: 0, max: 1, hint: 'MMR 相关性与多样性的平衡系数，范围 [0, 1]' },
      { key: 'mmr_threshold', label: 'MMR threshold', type: 'number', step: 0.05, min: 0, max: 1, hint: 'MMR 去冗余相似度阈值，范围 [0, 1]' },
    ],
  },
  {
    title: '索引档',
    icon: Database,
    description: 'HNSW 向量索引参数',
    fields: [
      { key: 'hnsw_ef', label: 'HNSW 查询 ef', type: 'number', min: 1, max: 2048, hint: '查询时的探索宽度，范围 [1, 2048]' },
      { key: 'hnsw_ef_construction', label: 'HNSW 建索引 efConstruction', type: 'number', min: 8, max: 512, hint: `范围 [8, 512]。${INDEX_BUILD_HINT}` },
      { key: 'hnsw_m', label: 'HNSW 建索引 M', type: 'number', min: 4, max: 64, hint: `范围 [4, 64]。${INDEX_BUILD_HINT}` },
    ],
  },
]

// 系统设置弹窗：左侧分类导航 + 右侧设置内容。
// 分项：外观（主题，所有人）/ 切片配置 + 检索配置（管理员，租户级）/ 平台配置（超管，TTL）。
export default function SettingsDialog({
  open,
  onOpenChange,
  canManageChunk = false,
  tenantId,
  isSuperAdmin = false,
}: SettingsDialogProps) {
  // 配置指定租户场景（超管经租户管理列表进入）：只展示切片/检索分项，
  // 不展示「外观」（超管个人主题偏好）与「平台配置」（全局，由超管自身入口管理）。
  const isTenantScoped = !!tenantId
  const sections = ALL_SECTIONS.filter((s) => {
    if (s.id === 'appearance') return !isTenantScoped
    if (s.id === 'platform') return !isTenantScoped && isSuperAdmin
    // chunk / retrieval：需管理员权限
    return canManageChunk
  })
  const [active, setActive] = useState<SectionId>(isTenantScoped ? 'chunk' : 'appearance')

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-5xl w-[92vw] gap-0 p-0 overflow-hidden">
        <DialogHeader className="px-8 pt-7 pb-5 space-y-0.5">
          <DialogTitle className="text-base font-semibold">设置</DialogTitle>
          <DialogDescription className="text-xs">
            {tenantId
              ? `正在配置指定租户的切片与检索参数（租户 ${tenantId}）。`
              : '根据你的偏好调整界面外观与文档处理行为。'}
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
          <div className="flex-1 min-w-0 overflow-auto max-h-[calc(90vh-160px)] pr-1">
            {active === 'appearance' && <AppearanceSection />}
            {active === 'chunk' && <RetrievalConfigSection tenantId={tenantId} mode="chunk" />}
            {active === 'retrieval' && <RetrievalConfigSection tenantId={tenantId} mode="retrieval" />}
            {active === 'platform' && <PlatformSection />}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

const ALL_SECTIONS: { id: SectionId; label: string; icon: typeof Palette }[] = [
  { id: 'appearance', label: '外观', icon: Palette },
  { id: 'chunk', label: '切片配置', icon: Layers },
  { id: 'retrieval', label: '检索配置', icon: Search },
  { id: 'platform', label: '平台配置', icon: Server },
]

// ============================================================
// 外观设置：主题切换（系统 / 浅色 / 深色）
// ============================================================

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

// ============================================================
// 切片配置 / 检索配置（租户级，经 RetrievalConfigStore 读写）
// mode='chunk'：仅渲染分块三档；mode='retrieval'：渲染召回/融合/精排/去重/索引五档。
// 两者共享同一份 form（GET /system/config 的 retrieval 分区），保存时整体 PUT。
// ============================================================

function RetrievalConfigSection({ tenantId, mode }: { tenantId?: string; mode: 'chunk' | 'retrieval' }) {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const [form, setForm] = useState<SystemConfig | null>(null)

  const { data: config, isLoading } = useQuery({
    queryKey: ['system-config', tenantId],
    queryFn: () => systemApi.getConfig(tenantId) as Promise<SystemConfig>,
    retry: false,
  })

  useEffect(() => {
    if (config) setForm({ ...config })
  }, [config])

  const saveMutation = useMutation({
    mutationFn: (data: SystemConfig) => systemApi.updateConfig(data, tenantId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['system-config', tenantId] })
      toast.success('配置已保存')
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : '保存失败'),
  })

  const resetMutation = useMutation({
    mutationFn: () => systemApi.resetRetrievalConfig(tenantId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['system-config', tenantId] })
      toast.success('已恢复默认值')
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : '恢复默认失败'),
  })

  const groups = mode === 'chunk' ? [CHUNK_GROUP] : RETRIEVAL_GROUPS
  // 本分项涉及的字段集合（用于 diff 时只对比本页字段）
  const sectionKeys = new Set(groups.flatMap((g) => g.fields.map((f) => f.key)))

  function updateRetrievalField(key: string, value: string | number | boolean) {
    if (!form) return
    setForm({
      ...form,
      retrieval: { ...(form.retrieval as RetrievalConfig), [key]: value },
    })
  }

  async function handleSave() {
    if (!form) return
    const serverRetrieval = config?.retrieval
    const formRetrieval = form.retrieval
    const localChanges: { field: string; label: string; old: unknown; new: unknown }[] = []
    if (formRetrieval) {
      for (const k of Object.keys(formRetrieval)) {
        // 仅对比本分项的真实字段
        if (!sectionKeys.has(k)) continue
        const oldVal = serverRetrieval?.[k]
        const newVal = formRetrieval[k]
        if (oldVal !== newVal) {
          localChanges.push({ field: k, label: FIELD_LABELS[k] ?? k, old: oldVal, new: newVal })
        }
      }
    }

    // 无变更短路：不调接口，提示用户
    if (localChanges.length === 0) {
      toast('未检测到修改')
      return
    }

    const ok = await confirm({
      title: '确认保存配置变更',
      description: (
        <div className="space-y-1">
          {localChanges.map((c) => (
            <div key={c.field}>
              {c.label}：{fmtVal(c.old)} → {fmtVal(c.new)}
            </div>
          ))}
        </div>
      ),
      confirmText: '确认保存',
      variant: 'default',
    })
    // 整个 form（含 retrieval 嵌套）PUT 给后端，后端按嵌套 retrieval 处理
    if (ok) saveMutation.mutate(form)
  }

  async function handleReset() {
    const ok = await confirm({
      title: '恢复默认值',
      description: '该操作会重置全部分块（父块/子块/重叠）与召回/融合/精排/去重/索引参数为默认值，确定继续？',
      confirmText: '恢复默认',
      variant: 'destructive',
    })
    if (ok) resetMutation.mutate()
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
    <div className="space-y-5">
      {groups.map((group) => (
        <div key={group.title} className="space-y-3">
          <div className="flex items-center gap-2.5">
            <group.icon className="h-4 w-4 text-primary shrink-0" />
            <div>
              <h3 className="text-sm font-semibold">{group.title}</h3>
              <p className="text-[11px] text-muted-foreground">{group.description}</p>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            {group.fields.map((field) => {
              const value = form.retrieval?.[field.key]
              return (
                <div key={field.key} className="space-y-1.5">
                  <Label className="text-xs">{field.label}</Label>
                  {field.type === 'switch' ? (
                    <div className="flex items-center h-9">
                      <Switch
                        checked={!!value}
                        onCheckedChange={(val) => updateRetrievalField(field.key, val)}
                      />
                    </div>
                  ) : (
                    <Input
                      type="number"
                      value={value != null ? Number(value) : ''}
                      step={field.step}
                      min={field.min}
                      max={field.max}
                      onChange={(e) => updateRetrievalField(field.key, Number(e.target.value))}
                      className="h-9"
                    />
                  )}
                  {field.hint && <p className="text-[11px] text-muted-foreground">{field.hint}</p>}
                </div>
              )
            })}
          </div>
        </div>
      ))}

      {/* 操作区：恢复默认 + 保存 */}
      <div className="flex justify-end gap-2 pt-2 border-t border-border">
        <Button
          type="button"
          variant="outline"
          onClick={handleReset}
          disabled={resetMutation.isPending}
          className="gap-2 cursor-pointer mt-4"
        >
          {resetMutation.isPending ? <RefreshCw className="h-4 w-4 animate-spin" /> : <RotateCcw className="h-4 w-4" />}
          恢复默认值
        </Button>
        <Button onClick={handleSave} disabled={saveMutation.isPending} className="gap-2 cursor-pointer mt-4">
          {saveMutation.isPending ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          保存
        </Button>
      </div>
    </div>
  )
}

// ============================================================
// 平台配置（超管专属）：collection 加载缓存 TTL（Load_Cache_TTL）
// ============================================================

function PlatformSection() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const [ttl, setTtl] = useState<number | null>(null)

  const { data, isLoading, isError } = useQuery({
    queryKey: ['platform-config'],
    queryFn: () => systemApi.getPlatformConfig(),
    retry: false,
  })

  useEffect(() => {
    if (data) setTtl(data.load_cache_ttl)
  }, [data])

  const saveMutation = useMutation({
    mutationFn: (payload: PlatformConfig) => systemApi.updatePlatformConfig(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['platform-config'] })
      toast.success('平台配置已保存')
    },
    onError: (err) => toast.error(err instanceof Error ? err.message : '保存失败'),
  })

  async function handleSave() {
    if (ttl == null) return
    // 无变更短路：与服务器当前值一致则不提交
    if (ttl === data?.load_cache_ttl) {
      toast('未检测到修改')
      return
    }
    const ok = await confirm({
      title: '确认保存平台配置',
      description: (
        <div>
          {FIELD_LABELS.load_cache_ttl}：{fmtVal(data?.load_cache_ttl)} → {fmtVal(ttl)}（秒）
        </div>
      ),
      confirmText: '确认保存',
      variant: 'default',
    })
    if (ok) saveMutation.mutate({ load_cache_ttl: ttl })
  }

  if (isError) {
    return (
      <div className="text-sm text-muted-foreground">平台配置加载失败。</div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="space-y-0.5">
        <h3 className="text-sm font-semibold">平台配置</h3>
        <p className="text-xs text-muted-foreground">全平台基础设施参数，仅超级管理员可见，对全平台生效。</p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-1.5">
          <Label className="text-xs">加载缓存 TTL（load_cache_ttl）</Label>
          <Input
            type="number"
            min={0}
            max={3600}
            value={ttl != null ? ttl : ''}
            onChange={(e) => setTtl(Number(e.target.value))}
            className="h-9"
            disabled={isLoading}
          />
          <p className="text-[11px] text-muted-foreground">
            控制 collection 加载缓存有效期（秒），影响检索延迟与新数据可见性，对全平台生效。范围 [0, 3600]。
          </p>
        </div>
      </div>

      <div className="flex justify-end pt-2">
        <Button
          type="button"
          onClick={handleSave}
          disabled={isLoading || saveMutation.isPending || ttl == null}
          className="gap-2 cursor-pointer"
        >
          {saveMutation.isPending ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
          保存平台配置
        </Button>
      </div>
    </div>
  )
}
