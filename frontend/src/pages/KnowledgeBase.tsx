import { useState } from 'react'
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Plus, Pencil, Trash2, Database, FileText, FolderOpen } from 'lucide-react'
import { knowledgeBaseApi } from '@/lib/api'
import type { PageResult } from '@/lib/api'
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import CardGridSkeleton from '@/components/skeletons/CardGridSkeleton'

// 知识库数据类型
interface KnowledgeBaseItem {
  id: string
  name: string
  description: string
  doc_count: number
  created_at: string
}

// 表单数据类型
interface FormData {
  name: string
  description: string
}

// 知识库管理页面
function KnowledgeBase() {
  const queryClient = useQueryClient()
  const [showDialog, setShowDialog] = useState(false)
  const [editingItem, setEditingItem] = useState<KnowledgeBaseItem | null>(null)
  const [form, setForm] = useState<FormData>({ name: '', description: '' })

  // 获取知识库列表（分页 + 滚动加载）
  const PAGE_SIZE = 20
  const {
    data,
    isLoading,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery({
    queryKey: ['knowledge-bases', 'infinite'],
    queryFn: ({ pageParam }) =>
      knowledgeBaseApi.list({ page: pageParam, page_size: PAGE_SIZE }) as Promise<
        PageResult<KnowledgeBaseItem>
      >,
    initialPageParam: 1,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? lastPage.page + 1 : undefined,
  })

  const knowledgeBases = data?.pages.flatMap((p) => p.items) ?? []

  const sentinelRef = useInfiniteScroll(fetchNextPage, {
    hasMore: !!hasNextPage,
    loading: isFetchingNextPage,
  })

  // 创建知识库
  const createMutation = useMutation({
    mutationFn: (data: FormData) => knowledgeBaseApi.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-bases'] })
      closeDialog()
    },
  })

  // 更新知识库
  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: FormData }) => knowledgeBaseApi.update(id, data),
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

  function openCreate() {
    setEditingItem(null)
    setForm({ name: '', description: '' })
    setShowDialog(true)
  }

  function openEdit(item: KnowledgeBaseItem) {
    setEditingItem(item)
    setForm({ name: item.name, description: item.description || '' })
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
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">知识库</h1>
          <p className="text-muted-foreground text-sm mt-1">管理您的知识库，上传文档并配置检索策略</p>
        </div>
        <Button onClick={openCreate} className="gap-2">
          <Plus className="h-4 w-4" />
          新建知识库
        </Button>
      </div>

      {/* 知识库列表 */}
      {isLoading ? (
        <CardGridSkeleton count={6} />
      ) : knowledgeBases.length === 0 ? (
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
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 animate-in fade-in-0 duration-500">
          {knowledgeBases.map((kb) => (
            <Link
              key={kb.id}
              to={`/knowledge-bases/${kb.id}`}
              className="group relative block rounded-xl border border-border bg-card p-5 transition-all duration-200 hover:shadow-lg hover:border-primary/20 hover:-translate-y-0.5 cursor-pointer"
            >
              {/* 操作按钮 */}
              <div className="absolute top-3 right-3 flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                <button
                  onClick={(e) => { e.preventDefault(); e.stopPropagation(); openEdit(kb) }}
                  className="h-7 w-7 rounded-md flex items-center justify-center hover:bg-muted transition-colors"
                  title="编辑"
                >
                  <Pencil className="h-3.5 w-3.5 text-muted-foreground" />
                </button>
                <button
                  onClick={(e) => { e.preventDefault(); e.stopPropagation(); deleteMutation.mutate(kb.id) }}
                  className="h-7 w-7 rounded-md flex items-center justify-center hover:bg-destructive/10 transition-colors"
                  title="删除"
                >
                  <Trash2 className="h-3.5 w-3.5 text-destructive" />
                </button>
              </div>

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

              {/* 底部信息 */}
              <div className="flex items-center gap-3 mt-4 pt-3 border-t border-border/60">
                <span className="text-xs text-muted-foreground flex items-center gap-1">
                  <FileText className="h-3 w-3" />
                  {kb.doc_count} 篇文档
                </span>
              </div>
            </Link>
          ))}

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
    </div>
  )
}

export default KnowledgeBase
