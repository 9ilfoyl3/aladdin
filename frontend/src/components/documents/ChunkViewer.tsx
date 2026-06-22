import { useInfiniteQuery } from '@tanstack/react-query'
import { Streamdown } from 'streamdown'
import { cjk } from '@streamdown/cjk'
import { copyToClipboard } from '@/lib/clipboard'
import { toast } from 'sonner'
import { FileText, Copy } from 'lucide-react'
import { documentApi } from '@/lib/api'
import type { PageResult } from '@/lib/api'
import { useInfiniteScroll } from '@/hooks/useInfiniteScroll'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import ChunkListSkeleton from '@/components/skeletons/ChunkListSkeleton'

// Chunk 数据类型
interface ChunkItem {
  id: string
  content: string
  chunk_index: number
  children: string[]
}

interface ChunkViewerProps {
  documentId: string | null
  onClose: () => void
}

/**
 * 在父块内容中用 <mark> 标签高亮子块文本。
 *
 * 子块文本未必是父块文本的「精确」子串：切分器在拼接子块时会对空白做归一化
 * （例如把段落间的双换行压成单换行、用 \n 连接多个结构段），导致字符级 indexOf
 * 失败。因此这里采用「空白无关」匹配：把子块按空白拆成 token，token 之间允许
 * 任意空白（含全角空格、换行），在父块原文中定位后高亮**原文**片段，保证视觉
 * 高亮与原文一致，且对历史/新切分数据都生效。
 */
function highlightChildren(parentContent: string, children: string[]): string {
  if (!children || children.length === 0) return parentContent

  // 记录已高亮区间，避免重叠（长子块优先占位）
  const ranges: Array<[number, number]> = []
  const sorted = [...children]
    .map((c, i) => ({ c, i }))
    .sort((a, b) => b.c.length - a.c.length)

  const overlaps = (s: number, e: number) =>
    ranges.some(([rs, re]) => s < re && e > rs)

  for (const { c } of sorted) {
    if (!c || !c.trim()) continue
    // 按空白拆 token，每个 token 转义后用 \s* 连接，允许空白差异
    const tokens = c.trim().split(/\s+/).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    if (tokens.length === 0) continue
    const pattern = tokens.join('\\s*')
    let regex: RegExp
    try {
      regex = new RegExp(pattern, 'g')
    } catch {
      continue
    }
    let m: RegExpExecArray | null
    while ((m = regex.exec(parentContent)) !== null) {
      const s = m.index
      const e = m.index + m[0].length
      if (m[0].length === 0) { regex.lastIndex++; continue }
      if (!overlaps(s, e)) {
        ranges.push([s, e])
        break // 每个子块只高亮第一处不重叠的匹配
      }
    }
  }

  if (ranges.length === 0) return parentContent

  // 按起点升序，从后往前插入 <mark>，避免位移影响索引
  ranges.sort((a, b) => a[0] - b[0])
  let result = parentContent
  for (let k = ranges.length - 1; k >= 0; k--) {
    const [s, e] = ranges[k]
    result = result.slice(0, s) + '<mark>' + result.slice(s, e) + '</mark>' + result.slice(e)
  }
  return result
}

// 切片查看对话框
function ChunkViewer({ documentId, onClose }: ChunkViewerProps) {
  const PAGE_SIZE = 20
  const {
    data,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    isLoading,
  } = useInfiniteQuery({
    queryKey: ['chunks', documentId],
    queryFn: ({ pageParam }) =>
      documentApi.chunks(documentId!, { page: pageParam, page_size: PAGE_SIZE }) as Promise<
        PageResult<ChunkItem>
      >,
    initialPageParam: 1,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.page + 1 : undefined),
    enabled: !!documentId,
  })

  const chunks = data?.pages.flatMap((p) => p.items) ?? []
  const total = data?.pages[0]?.total ?? 0

  // 弹窗内滚动容器的触底哨兵
  const sentinelRef = useInfiniteScroll(fetchNextPage, {
    hasMore: !!hasNextPage,
    loading: isFetchingNextPage,
  })

  return (
    <Dialog open={!!documentId} onOpenChange={() => onClose()}>
      <DialogContent className="max-w-4xl max-h-[85vh] flex flex-col p-0 gap-0">
        {/* 头部 */}
        <div className="flex items-center justify-between px-6 py-4 border-b bg-muted/30">
          <div>
            <DialogHeader>
              <DialogTitle className="text-lg">文档切片预览</DialogTitle>
            </DialogHeader>
            <p className="text-xs text-muted-foreground mt-1">
              {isLoading ? '加载切片中…' : `共 ${total} 个切片`}
            </p>
          </div>
        </div>

        {/* 切片列表 */}
        <div className="flex-1 overflow-auto px-6 py-4">
          {isLoading ? (
            <ChunkListSkeleton count={4} />
          ) : chunks.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16">
              <div className="w-12 h-12 rounded-xl bg-muted/60 flex items-center justify-center mb-3">
                <FileText className="h-6 w-6 text-muted-foreground/50" />
              </div>
              <p className="text-sm text-muted-foreground">暂无切片数据</p>
            </div>
          ) : (
            <div className="space-y-3 animate-in fade-in-0 duration-500">
              {chunks.map((chunk, idx) => (
                <div
                  key={chunk.id || idx}
                  className="group relative rounded-xl border border-border/60 bg-card hover:border-border transition-colors"
                >
                  {/* 切片头部 */}
                  <div className="flex items-center justify-between px-4 py-2.5 border-b border-border/40 bg-muted/20 rounded-t-xl">
                    <div className="flex items-center gap-2">
                      <span className="inline-flex items-center justify-center w-6 h-6 rounded-md bg-primary/10 text-primary text-xs font-semibold">
                        {chunk.chunk_index ?? idx + 1}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {chunk.content.length} 字符
                      </span>
                      {chunk.children.length > 0 && (
                        <span className="text-xs text-muted-foreground">
                          · {chunk.children.length} 个子块
                        </span>
                      )}
                    </div>
                    <button
                      onClick={() => { copyToClipboard(chunk.content); toast('已复制') }}
                      className="opacity-0 group-hover:opacity-100 transition-opacity text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 cursor-pointer"
                    >
                      <Copy className="h-3 w-3" />
                      复制
                    </button>
                  </div>
                  {/* 切片内容 */}
                  <div className="px-4 py-3 text-sm leading-relaxed max-h-64 overflow-auto prose prose-sm max-w-none dark:prose-invert [&>p]:mb-2 [&>p:last-child]:mb-0 [&_table]:text-xs [&_table]:w-full [&_table]:border-collapse [&_table]:rounded-md [&_table]:overflow-hidden [&_td]:border [&_td]:border-border/50 [&_td]:px-2.5 [&_td]:py-1.5 [&_th]:border [&_th]:border-border/50 [&_th]:px-2.5 [&_th]:py-1.5 [&_th]:bg-muted/40 [&_th]:font-medium [&_tr:nth-child(even)_td]:bg-muted/20 [&_h1]:text-base [&_h2]:text-sm [&_h3]:text-sm [&_mark]:bg-primary/15 [&_mark]:text-inherit [&_mark]:rounded-sm [&_mark]:px-0.5">
                    <Streamdown mode="static" plugins={{ cjk: cjk }}>
                      {highlightChildren(chunk.content, chunk.children)}
                    </Streamdown>
                  </div>
                </div>
              ))}

              {/* 滚动加载哨兵 + 加载状态 */}
              {hasNextPage && (
                <div ref={sentinelRef} className="flex items-center justify-center py-4">
                  {isFetchingNextPage && (
                    <div className="h-5 w-5 border-2 border-primary border-t-transparent rounded-full animate-spin" />
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* 底部 */}
        <div className="flex justify-end px-6 py-3 border-t bg-muted/20">
          <Button variant="outline" size="sm" onClick={onClose} className="cursor-pointer">
            关闭
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default ChunkViewer
