import { useState, useEffect } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Plus, Pencil, Trash2, Database, FileText, FolderOpen, Share2, Globe, Lock, Search, ArrowUpDown, X, Link2, Copy } from 'lucide-react'
import { authApi, knowledgeBaseApi, kbShareLinkApi } from '@/lib/api'
import { copyToClipboard } from '@/lib/clipboard'
import type { PageResult, KnowledgeBaseListParams, KBCapacity } from '@/lib/api'
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll'
import { useConfirm } from '@/lib/confirm-context'
import { useAuth } from '@/lib/auth-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
} from '@/components/ui/dropdown-menu'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import CardGridSkeleton from '@/components/skeletons/CardGridSkeleton'
import KBCapacityBar from '@/components/KBCapacityBar'
import KbShareAcceptDialog from '@/components/KbShareAcceptDialog'
import { toast } from 'sonner'

// 知识库数据类型（kb-sharing-refinement：附带归属、可见性与组织开放维度，用于前端按钮显隐 + 关系标签）
interface KnowledgeBaseItem {
  id: string
  name: string
  description: string
  doc_count: number
  created_at: string
  visibility: string | null
  owner_user_id: string | null
  org_permission?: string | null
  owner_username?: string | null
  tenant_name?: string | null
  share_count?: number | null
  // 容量进度条（session-file-upload Req 7）。后端列表/详情按需填充，未计算时为 null。
  capacity?: KBCapacity | null
}

// 表单数据类型
interface FormData {
  name: string
  description: string
  visibility: VisibilityChoice
}

// 可见性三档：私有 / 组织·只读 / 组织·读写
type VisibilityChoice = 'private' | 'org_read' | 'org_write'

// 关系筛选档位（与后端 relation 参数对应）。others=他人私有，仅管理员可见。
type RelationFilter = 'all' | 'mine' | 'shared' | 'org' | 'others'
// 排序档位（与后端 sort 参数对应）
type SortKey = NonNullable<KnowledgeBaseListParams['sort']>

// 关系筛选分段（管理员额外可见「他人私有」）
const RELATION_TABS: { key: RelationFilter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'mine', label: '我的' },
  { key: 'shared', label: '共享给我' },
  { key: 'org', label: '组织公共' },
]

// 排序选项
const SORT_OPTIONS: { key: SortKey; label: string }[] = [
  { key: 'recommended', label: '推荐' },
  { key: 'updated', label: '最近更新' },
  { key: 'created', label: '最近创建' },
  { key: 'name', label: '名称' },
  { key: 'docs', label: '文档数' },
]

// 知识库管理页面
function KnowledgeBase() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const { isOwner, isAdmin } = useAuth()
  const [showDialog, setShowDialog] = useState(false)
  const [editingItem, setEditingItem] = useState<KnowledgeBaseItem | null>(null)
  const [form, setForm] = useState<FormData>({ name: '', description: '', visibility: 'private' })

  // 列表筛选/排序/搜索状态
  const [relation, setRelation] = useState<RelationFilter>('all')
  const [sort, setSort] = useState<SortKey>('recommended')
  const [searchInput, setSearchInput] = useState('') // 输入框即时值
  const [search, setSearch] = useState('')            // 防抖后用于请求的值

  // 搜索防抖（300ms）：避免每次按键都打请求
  useEffect(() => {
    const t = setTimeout(() => setSearch(searchInput.trim()), 300)
    return () => clearTimeout(t)
  }, [searchInput])

  // 管理员可额外筛选「他人私有 · 只读」
  const relationTabs = isAdmin
    ? [...RELATION_TABS, { key: 'others' as RelationFilter, label: '他人私有' }]
    : RELATION_TABS
  const sortLabel = SORT_OPTIONS.find((o) => o.key === sort)?.label ?? '推荐'

  // 共享对话框：把一个 KB 分享给同租户多个用户（仅 owner 可发起）
  const [shareKb, setShareKb] = useState<KnowledgeBaseItem | null>(null)
  const [sharePermission, setSharePermission] = useState<'read' | 'write'>('read')
  const [selectedUserIds, setSelectedUserIds] = useState<string[]>([])
  const [shareSearch, setShareSearch] = useState('') // 用户名模糊搜索关键字

  // 可见性对话框：三档切换（私有 / 组织只读 / 组织读写）
  const [visibilityKb, setVisibilityKb] = useState<KnowledgeBaseItem | null>(null)
  const [visibilityValue, setVisibilityValue] = useState<VisibilityChoice>('private')

  // 归属轴：库实体操作（改/删/共享/改可见性）仅 owner 可见（与后端 owner-only 守卫一致，前端为展示层防御）
  function canMutate(kb: KnowledgeBaseItem): boolean {
    return isOwner(kb.owner_user_id)
  }

  // 列表关系标签：我的（蓝）/ 组织公共（灰）/ 共享给我（绿）/ 他人私有·管理员只读（琥珀）
  // detail：共享给我 -> 「谁分享的」；组织公共 -> 来源租户名；管理员只读 -> 归属人。
  // 「我的」库的当前状态以可点击 chip 单独渲染（见卡片底部），不走 detail。
  function relationBadge(kb: KnowledgeBaseItem): { text: string; cls: string; detail?: string } {
    if (isOwner(kb.owner_user_id)) {
      return { text: '我的', cls: 'bg-blue-100 text-blue-700 border-blue-200' }
    }
    if (kb.visibility === 'organization') {
      return {
        text: '组织公共',
        cls: 'bg-muted text-muted-foreground',
        detail: kb.tenant_name ? `来自 ${kb.tenant_name}` : undefined,
      }
    }
    // 非自己、非组织公共的私有库：管理员是监管只读可见，普通成员则是被共享
    if (isAdmin) {
      return {
        text: '他人私有 · 只读',
        cls: 'bg-amber-100 text-amber-700 border-amber-200',
        detail: kb.owner_username ? `归属 ${kb.owner_username}` : undefined,
      }
    }
    return {
      text: '共享给我',
      cls: 'bg-green-100 text-green-700 border-green-200',
      detail: kb.owner_username ? `来自 ${kb.owner_username}` : undefined,
    }
  }

  // 「我的」库可见性状态文案（可点击 chip 显示）：组织读写 / 组织只读 / 私有
  function ownVisibilityText(kb: KnowledgeBaseItem): string {
    if (kb.visibility === 'organization') {
      return kb.org_permission === 'write' ? '组织 · 读写' : '组织 · 只读'
    }
    return '私有'
  }

  // 获取知识库列表（分页 + 滚动加载，按筛选/排序/搜索）
  const PAGE_SIZE = 20
  const {
    data,
    isLoading,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ['knowledge-bases', 'infinite', relation, sort, search],
    queryFn: ({ pageParam }) =>
      knowledgeBaseApi.list({
        page: pageParam,
        page_size: PAGE_SIZE,
        relation: relation === 'all' ? undefined : relation,
        sort,
        q: search || undefined,
      }) as Promise<PageResult<KnowledgeBaseItem>>,
    initialPageParam: 1,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? lastPage.page + 1 : undefined,
  })

  const knowledgeBases = data?.pages.flatMap((p) => p.items) ?? []
  const totalCount = data?.pages[0]?.total ?? 0

  const sentinelRef = useInfiniteScroll(fetchNextPage, {
    hasMore: !!hasNextPage,
    loading: isFetchingNextPage,
  })

  // 共享对话框打开时，按用户名模糊搜索同租户可选用户（任意登录成员可调，无需 403 回退）
  const { data: selectableUsers } = useQuery({
    queryKey: ['kb-selectable-users', shareSearch],
    queryFn: () => authApi.selectableUsers(shareSearch || undefined),
    enabled: !!shareKb,
    retry: false,
  })
  const userOptions = selectableUsers ?? []

  // 已共享用户列表（仅 owner 可查）
  const { data: sharedUsers } = useQuery({
    queryKey: ['kb-shares', shareKb?.id],
    queryFn: () => knowledgeBaseApi.shares(shareKb!.id),
    enabled: !!shareKb,
  })

  // 创建知识库
  const createMutation = useMutation({
    mutationFn: (data: FormData) => {
      const visibility = data.visibility === 'private' ? 'private' : 'organization'
      const org_permission =
        data.visibility === 'org_write' ? 'write' : data.visibility === 'org_read' ? 'read' : undefined
      return knowledgeBaseApi.create({
        name: data.name,
        description: data.description,
        visibility,
        org_permission,
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-bases'] })
      closeDialog()
    },
  })

  // 更新知识库（仅改名称/描述；可见性由专门的可见性对话框处理）
  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: FormData }) =>
      knowledgeBaseApi.update(id, { name: data.name, description: data.description }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-bases'] })
      closeDialog()
    },
  })

  // 删除知识库
  const deleteMutation = useMutation({
    mutationFn: (id: string) => knowledgeBaseApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-bases'] })
    },
  })

  // 共享知识库给指定用户
  const shareMutation = useMutation({
    mutationFn: ({ kbId, userIds }: { kbId: string; userIds: string[] }) =>
      knowledgeBaseApi.share(kbId, { user_ids: userIds, permission: sharePermission }),
    onSuccess: () => {
      toast.success('已共享')
      if (shareKb) queryClient.invalidateQueries({ queryKey: ['kb-shares', shareKb.id] })
      // 同步刷新列表，使卡片上的「分享给 N 人」人数即时更新
      queryClient.invalidateQueries({ queryKey: ['knowledge-bases'] })
      setSelectedUserIds([])
    },
    onError: (e: Error) => toast.error(e.message || '共享失败'),
  })

  // 撤销某个用户的共享授权
  const revokeShareMutation = useMutation({
    mutationFn: ({ kbId, userId }: { kbId: string; userId: string }) =>
      knowledgeBaseApi.revokeShare(kbId, userId),
    onSuccess: (_data, vars) => {
      toast.success('已撤销共享')
      queryClient.invalidateQueries({ queryKey: ['kb-shares', vars.kbId] })
      // 同步刷新列表，使卡片上的「分享给 N 人」人数即时更新
      queryClient.invalidateQueries({ queryKey: ['knowledge-bases'] })
    },
    onError: (e: Error) => toast.error(e.message || '撤销失败'),
  })

  // 跨租户分享链接（cross-tenant-kb-share）：生成只读链接发给其他团队的用户领取
  const [shareLink, setShareLink] = useState<string | null>(null)
  const createLinkMutation = useMutation({
    mutationFn: (kbId: string) =>
      kbShareLinkApi.create({ kb_id: kbId, expires_in_hours: 24 * 7 }),
    onSuccess: async (res: { token: string }) => {
      const url = `${window.location.origin}/knowledge-bases?share=${res.token}`
      setShareLink(url)
      // 用统一的剪贴板工具（兼容非 HTTPS 部署的 execCommand 降级）
      const ok = await copyToClipboard(url)
      toast.success(ok ? '已生成并复制链接（有效期 7 天）' : '已生成链接（有效期 7 天），请手动复制')
    },
    onError: (e: Error) => toast.error(e.message || '生成链接失败'),
  })

  // 变更可见性（private / organization + org_permission）
  const visibilityMutation = useMutation({
    mutationFn: ({ kbId, visibility, orgPermission }: { kbId: string; visibility: string; orgPermission?: string }) =>
      knowledgeBaseApi.setVisibility(kbId, visibility, orgPermission),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-bases'] })
      toast.success('已更新可见性')
      closeVisibility()
    },
    onError: (e: Error) => toast.error(e.message || '变更可见性失败'),
  })

  function openCreate() {
    setEditingItem(null)
    setForm({ name: '', description: '', visibility: 'private' })
    setShowDialog(true)
  }

  function openEdit(item: KnowledgeBaseItem) {
    setEditingItem(item)
    const vis: VisibilityChoice =
      item.visibility === 'organization'
        ? item.org_permission === 'write'
          ? 'org_write'
          : 'org_read'
        : 'private'
    setForm({ name: item.name, description: item.description || '', visibility: vis })
    setShowDialog(true)
  }

  // 删除知识库（统一确认交互）
  async function handleDelete(kb: KnowledgeBaseItem) {
    const ok = await confirm({
      title: '删除知识库',
      description: (
        <>
          确定要删除知识库「{kb.name}」吗？该知识库下的所有文档与向量数据将被一并清除，此操作不可撤销。
        </>
      ),
    })
    if (ok) deleteMutation.mutate(kb.id)
  }

  // 打开可见性三档对话框，依据当前 kb 初始化选项
  function openVisibility(kb: KnowledgeBaseItem) {
    setVisibilityKb(kb)
    const init: VisibilityChoice =
      kb.visibility === 'organization'
        ? kb.org_permission === 'write'
          ? 'org_write'
          : 'org_read'
        : 'private'
    setVisibilityValue(init)
  }

  function closeVisibility() {
    setVisibilityKb(null)
  }

  // 确认可见性变更：映射三档到后端入参
  function submitVisibility() {
    if (!visibilityKb) return
    const id = visibilityKb.id
    if (visibilityValue === 'private') {
      visibilityMutation.mutate({ kbId: id, visibility: 'private' })
    } else if (visibilityValue === 'org_read') {
      visibilityMutation.mutate({ kbId: id, visibility: 'organization', orgPermission: 'read' })
    } else {
      visibilityMutation.mutate({ kbId: id, visibility: 'organization', orgPermission: 'write' })
    }
  }

  function openShare(kb: KnowledgeBaseItem) {
    setShareKb(kb)
    setSharePermission('read')
    setSelectedUserIds([])
    setShareSearch('')
  }

  function closeShare() {
    setShareKb(null)
    setSelectedUserIds([])
    setShareSearch('')
    setShareLink(null)
  }

  function toggleUser(id: string) {
    setSelectedUserIds((prev) => (prev.includes(id) ? prev.filter((u) => u !== id) : [...prev, id]))
  }

  function submitShare() {
    if (!shareKb) return
    if (selectedUserIds.length === 0) {
      toast.error('请至少选择一个用户')
      return
    }
    shareMutation.mutate({ kbId: shareKb.id, userIds: selectedUserIds })
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
      {/* 跨租户分享领取弹窗：URL 带 ?share=<token> 时自动弹出确认 */}
      <KbShareAcceptDialog />
      {/* 页面头部 */}
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">知识库</h1>
          <p className="text-muted-foreground text-sm mt-1">管理您的知识库，上传文档并配置检索策略</p>
        </div>
        <Button onClick={openCreate} className="gap-2">
          <Plus className="h-4 w-4" />
          新建知识库
        </Button>
      </div>

      {/* 工具栏：关系分段筛选 + 搜索 + 排序 */}
      <div className="flex items-center justify-between gap-3 mb-6 flex-wrap">
        {/* 关系分段控件 */}
        <div className="flex items-center gap-3">
          <div className="inline-flex items-center gap-0.5 rounded-lg bg-muted/60 p-0.5">
            {relationTabs.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setRelation(tab.key)}
                className={`px-3 py-1.5 text-sm rounded-md transition-colors cursor-pointer ${
                  relation === tab.key
                    ? 'bg-background text-foreground shadow-sm font-medium'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>
          {!isLoading && (
            <span className="text-xs text-muted-foreground">共 {totalCount} 个</span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {/* 搜索框 */}
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground pointer-events-none" />
            <Input
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="搜索知识库"
              className="pl-8 pr-8 h-9 w-48"
            />
            {searchInput && (
              <button
                onClick={() => setSearchInput('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground cursor-pointer"
                title="清除"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>

          {/* 排序下拉 */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm" className="h-9 gap-1.5">
                <ArrowUpDown className="h-3.5 w-3.5" />
                {sortLabel}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-36">
              <DropdownMenuRadioGroup value={sort} onValueChange={(v) => setSort(v as SortKey)}>
                {SORT_OPTIONS.map((opt) => (
                  <DropdownMenuRadioItem key={opt.key} value={opt.key} className="cursor-pointer">
                    {opt.label}
                  </DropdownMenuRadioItem>
                ))}
              </DropdownMenuRadioGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      {/* 知识库列表 */}
      {isLoading ? (
        <CardGridSkeleton count={6} />
      ) : knowledgeBases.length === 0 ? (
        // 区分「真的没有库」与「筛选/搜索无结果」两种空态
        relation !== 'all' || search ? (
          <div className="flex flex-col items-center justify-center py-20">
            <div className="w-16 h-16 rounded-2xl bg-muted/60 flex items-center justify-center mb-4">
              <Search className="h-8 w-8 text-muted-foreground/60" />
            </div>
            <p className="text-muted-foreground mb-1">没有符合条件的知识库</p>
            <p className="text-sm text-muted-foreground/70 mb-4">试试切换筛选条件或清空搜索</p>
            <Button
              variant="outline"
              size="sm"
              className="gap-2"
              onClick={() => { setRelation('all'); setSearchInput('') }}
            >
              <X className="h-4 w-4" />
              清除筛选
            </Button>
          </div>
        ) : (
          <div className="flex flex-col items-center justify-center py-20">
            <div className="w-16 h-16 rounded-2xl bg-muted/60 flex items-center justify-center mb-4">
              <Database className="h-8 w-8 text-muted-foreground/60" />
            </div>
            <p className="text-muted-foreground mb-4">还没有知识库，创建一个开始吧</p>
            <Button onClick={openCreate} variant="outline" className="gap-2">
              <Plus className="h-4 w-4" />
              新建知识库
            </Button>
          </div>
        )
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 animate-in fade-in-0 duration-500">
          {knowledgeBases.map((kb) => {
            const mutable = canMutate(kb)
            const rel = relationBadge(kb)
            return (
            <Link
              key={kb.id}
              to={`/knowledge-bases/${kb.id}`}
              className="group relative block rounded-xl border border-border bg-card p-5 transition-all duration-200 hover:shadow-lg hover:border-primary/20 hover:-translate-y-0.5 cursor-pointer"
            >
              {/* 操作按钮：仅 owner 可见（展示层防御，真正鉴权在后端 owner-only 守卫） */}
              {mutable && (
                <div className="absolute top-3 right-3 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    onClick={(e) => { e.preventDefault(); e.stopPropagation(); openVisibility(kb) }}
                    className="h-7 w-7 rounded-md flex items-center justify-center hover:bg-muted transition-colors"
                    title="可见性"
                  >
                    {kb.visibility === 'organization'
                      ? <Globe className="h-3.5 w-3.5 text-muted-foreground" />
                      : <Lock className="h-3.5 w-3.5 text-muted-foreground" />}
                  </button>
                  <button
                    onClick={(e) => { e.preventDefault(); e.stopPropagation(); openShare(kb) }}
                    className="h-7 w-7 rounded-md flex items-center justify-center hover:bg-muted transition-colors"
                    title="共享"
                  >
                    <Share2 className="h-3.5 w-3.5 text-muted-foreground" />
                  </button>
                  <button
                    onClick={(e) => { e.preventDefault(); e.stopPropagation(); openEdit(kb) }}
                    className="h-7 w-7 rounded-md flex items-center justify-center hover:bg-muted transition-colors"
                    title="编辑"
                  >
                    <Pencil className="h-3.5 w-3.5 text-muted-foreground" />
                  </button>
                  <button
                    onClick={(e) => { e.preventDefault(); e.stopPropagation(); handleDelete(kb) }}
                    className="h-7 w-7 rounded-md flex items-center justify-center hover:bg-destructive/10 transition-colors"
                    title="删除"
                  >
                    <Trash2 className="h-3.5 w-3.5 text-destructive" />
                  </button>
                </div>
              )}

              {/* 图标 */}
              <div className="w-10 h-10 rounded-lg bg-primary/8 flex items-center justify-center mb-3">
                <FolderOpen className="h-5 w-5 text-primary" />
              </div>

              {/* 标题 */}
              <h3 className="font-semibold text-base truncate pr-16 group-hover:text-primary transition-colors">
                {kb.name}
              </h3>

              {/* 描述 */}
              <p className="text-sm text-muted-foreground mt-1.5 line-clamp-2 min-h-10">
                {kb.description || '暂无描述'}
              </p>

              {/* 容量进度条（chunk 真实度量；接近/已满变色，Req 7） */}
              {kb.capacity && (
                <div className="mt-3">
                  <KBCapacityBar capacity={kb.capacity} compact />
                </div>
              )}

              {/* 底部信息：文档数 + 关系标签 + 来源说明/可点击状态 chip */}
              <div className="flex items-center gap-2 mt-4 pt-3 border-t border-border/60 flex-wrap">
                <span className="text-xs text-muted-foreground flex items-center gap-1">
                  <FileText className="h-3 w-3" />
                  {kb.doc_count} 篇文档
                </span>
                <Badge variant="outline" className={`text-xs ${rel.cls}`}>{rel.text}</Badge>
                {/* 我的库：可见性状态 chip（点击改可见性）+ 分享人数 chip（点击打开共享） */}
                {mutable ? (
                  <>
                    <button
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); openVisibility(kb) }}
                      className="text-xs px-2 py-0.5 rounded-md border border-border bg-background text-muted-foreground hover:bg-muted hover:text-foreground transition-colors inline-flex items-center gap-1"
                      title="点击修改可见性 / 读写权限"
                    >
                      {kb.visibility === 'organization'
                        ? <Globe className="h-3 w-3" />
                        : <Lock className="h-3 w-3" />}
                      {ownVisibilityText(kb)}
                    </button>
                    <button
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); openShare(kb) }}
                      className="text-xs px-2 py-0.5 rounded-md border border-border bg-background text-muted-foreground hover:bg-muted hover:text-foreground transition-colors inline-flex items-center gap-1"
                      title="点击查看 / 管理共享"
                    >
                      <Share2 className="h-3 w-3" />
                      {(kb.share_count ?? 0) > 0 ? `已分享 ${kb.share_count} 人` : '未分享'}
                    </button>
                  </>
                ) : (
                  <>
                    {rel.detail && (
                      <span className="text-xs text-muted-foreground">{rel.detail}</span>
                    )}
                    {kb.visibility === 'organization' && (
                      <Badge variant="outline" className="text-xs gap-1">
                        <Globe className="h-3 w-3" />
                        {kb.org_permission === 'write' ? '读写' : '只读'}
                      </Badge>
                    )}
                  </>
                )}
              </div>
            </Link>
            )
          })}

          {/* 滚动加载哨兵 + 加载状态 */}
          {hasNextPage && (
            <div ref={sentinelRef} className="col-span-full flex items-center justify-center py-6">
              {isFetchingNextPage && (
                <div className="h-6 w-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
              )}
            </div>
          )}
        </div>
      )}

      {/* 创建/编辑对话框 */}
      <Dialog open={showDialog} onOpenChange={closeDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editingItem ? '编辑知识库' : '新建知识库'}</DialogTitle>
          </DialogHeader>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <Label>名称</Label>
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="输入知识库名称"
                className="mt-1.5"
                required
              />
            </div>
            <div>
              <Label>描述</Label>
              <Textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="输入知识库描述（可选）"
                className="mt-1.5"
                rows={3}
              />
            </div>
            {!editingItem && (
              <div>
                <Label>可见性</Label>
                <Select value={form.visibility} onValueChange={(v) => setForm({ ...form, visibility: v as VisibilityChoice })}>
                  <SelectTrigger className="mt-1.5"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="private">私有（仅自己与被授权用户）</SelectItem>
                    <SelectItem value="org_read">组织 · 只读（空间成员可读）</SelectItem>
                    <SelectItem value="org_write">组织 · 读写（空间成员可读写）</SelectItem>
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground mt-1.5">创建后也可在卡片上随时调整可见性。</p>
              </div>
            )}
            <DialogFooter>
              <Button type="button" variant="outline" onClick={closeDialog}>
                取消
              </Button>
              <Button type="submit" disabled={createMutation.isPending || updateMutation.isPending}>
                {editingItem ? '保存' : '创建'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* 可见性三档对话框：私有 / 组织·只读 / 组织·读写 */}
      <Dialog open={!!visibilityKb} onOpenChange={(o) => { if (!o) closeVisibility() }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>可见性 · {visibilityKb?.name}</DialogTitle>
            <DialogDescription>
              私有库仅创建人与被授权用户可访问；组织公共库空间成员可读，读写档允许成员上传/删除文档。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 mt-2">
            <div>
              <Label>可见性</Label>
              <Select value={visibilityValue} onValueChange={(v) => setVisibilityValue(v as VisibilityChoice)}>
                <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="private">私有</SelectItem>
                  <SelectItem value="org_read">组织 · 只读</SelectItem>
                  <SelectItem value="org_write">组织 · 读写</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeVisibility}>取消</Button>
            <Button onClick={submitVisibility} disabled={visibilityMutation.isPending}>确定</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 共享对话框：用户名模糊搜索 + 多选 + 已共享列表/撤销 */}
      <Dialog open={!!shareKb} onOpenChange={(o) => { if (!o) closeShare() }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>共享知识库 · {shareKb?.name}</DialogTitle>
            <DialogDescription>把该知识库点对点分享给同空间的一个或多个用户，并指定读 / 写权限。</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 mt-2">
            <div>
              <Label>权限</Label>
              <Select value={sharePermission} onValueChange={(v) => setSharePermission(v as 'read' | 'write')}>
                <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="read">只读</SelectItem>
                  <SelectItem value="write">读写</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>被分享用户</Label>
              <Input
                value={shareSearch}
                onChange={(e) => setShareSearch(e.target.value)}
                placeholder="搜索用户名"
                className="mt-1"
              />
              {userOptions.length === 0 ? (
                <p className="text-sm text-muted-foreground mt-2">未找到匹配的用户。</p>
              ) : (
                <div className="mt-2 max-h-48 overflow-auto border rounded-md p-2 flex flex-wrap gap-2">
                  {userOptions.map((u) => (
                    <button
                      type="button"
                      key={u.id}
                      onClick={() => toggleUser(u.id)}
                      className={`px-2.5 py-1 rounded-md text-xs border transition-colors ${selectedUserIds.includes(u.id) ? 'bg-primary text-primary-foreground border-primary' : 'bg-background hover:bg-muted'}`}
                    >
                      {u.username}
                    </button>
                  ))}
                </div>
              )}
              {selectedUserIds.length > 0 && (
                <p className="text-xs text-muted-foreground mt-1.5">已选 {selectedUserIds.length} 个用户</p>
              )}
            </div>

            {/* 已共享用户列表 + 按人撤销 */}
            <div>
              <Label>已共享用户</Label>
              {(sharedUsers?.length ?? 0) === 0 ? (
                <p className="text-sm text-muted-foreground mt-1">尚未共享给任何用户。</p>
              ) : (
                <div className="mt-1 max-h-48 overflow-auto border rounded-md divide-y">
                  {sharedUsers!.map((s) => (
                    <div key={s.user_id} className="flex items-center justify-between px-3 py-2">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-sm truncate">{s.username}</span>
                        <Badge variant="outline" className="text-xs">
                          {s.permission === 'write' ? '读写' : '只读'}
                        </Badge>
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 px-2 text-xs text-destructive hover:text-destructive"
                        disabled={revokeShareMutation.isPending}
                        onClick={() => shareKb && revokeShareMutation.mutate({ kbId: shareKb.id, userId: s.user_id })}
                      >
                        撤销
                      </Button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* 跨租户分享：生成只读链接，发给其他团队的用户登录后领取 */}
            <div className="border-t pt-4">
              <Label className="flex items-center gap-1.5">
                <Link2 className="h-3.5 w-3.5" /> 跨团队分享（只读链接）
              </Label>
              <p className="text-xs text-muted-foreground mt-1">
                生成一个只读分享链接，发给其他团队的成员；对方登录后领取即可访问该知识库。
              </p>
              {shareLink ? (
                <div className="mt-2 flex items-center gap-2">
                  <Input readOnly value={shareLink} className="text-xs" onFocus={(e) => e.currentTarget.select()} />
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="shrink-0"
                    onClick={async () => { const ok = await copyToClipboard(shareLink); toast.success(ok ? '已复制' : '复制失败，请手动复制') }}
                  >
                    <Copy className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ) : (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-2"
                  disabled={!shareKb || createLinkMutation.isPending}
                  onClick={() => shareKb && createLinkMutation.mutate(shareKb.id)}
                >
                  <Link2 className="h-3.5 w-3.5 mr-1" />
                  {createLinkMutation.isPending ? '生成中…' : '生成只读分享链接'}
                </Button>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={closeShare}>取消</Button>
            <Button onClick={submitShare} disabled={shareMutation.isPending}>共享</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default KnowledgeBase
