import { useQuery } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import rehypeRaw from 'rehype-raw'
import { FileText, Copy } from 'lucide-react'
import { documentApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'

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
 * 在父块内容中用 <mark> 标签高亮子块文本
 */
function highlightChildren(parentContent: string, children: string[]): string {
  if (!children || children.length === 0) return parentContent

  let result = parentContent
  const sorted = [...children].sort((a, b) => b.length - a.length)

  for (const child of sorted) {
    if (!child) continue
    const escaped = child.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const regex = new RegExp(escaped)
    result = result.replace(regex, `<mark>${child}</mark>`)
  }

  return result
}

// 切片查看对话框
function ChunkViewer({ documentId, onClose }: ChunkViewerProps) {
  const { data: chunks = [] } = useQuery({
    queryKey: ['chunks', documentId],
    queryFn: () => documentApi.chunks(documentId!) as Promise<ChunkItem[]>,
    enabled: !!documentId,
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
              共 {chunks.length} 个切片
            </p>
          </div>
        </div>

        {/* 切片列表 */}
        <div className="flex-1 overflow-auto px-6 py-4">
          {chunks.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-16">
              <div className="w-12 h-12 rounded-xl bg-muted/60 flex items-center justify-center mb-3">
                <FileText className="h-6 w-6 text-muted-foreground/50" />
              </div>
              <p className="text-sm text-muted-foreground">暂无切片数据</p>
            </div>
          ) : (
            <div className="space-y-3">
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
                      onClick={() => navigator.clipboard.writeText(chunk.content)}
                      className="opacity-0 group-hover:opacity-100 transition-opacity text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 cursor-pointer"
                    >
                      <Copy className="h-3 w-3" />
                      复制
                    </button>
                  </div>
                  {/* 切片内容 */}
                  <div className="px-4 py-3 text-sm leading-relaxed max-h-64 overflow-auto prose prose-sm max-w-none dark:prose-invert [&>p]:mb-2 [&>p:last-child]:mb-0 [&_table]:text-xs [&_table]:w-full [&_table]:border-collapse [&_table]:rounded-md [&_table]:overflow-hidden [&_td]:border [&_td]:border-border/50 [&_td]:px-2.5 [&_td]:py-1.5 [&_th]:border [&_th]:border-border/50 [&_th]:px-2.5 [&_th]:py-1.5 [&_th]:bg-muted/40 [&_th]:font-medium [&_tr:nth-child(even)_td]:bg-muted/20 [&_h1]:text-base [&_h2]:text-sm [&_h3]:text-sm [&_mark]:bg-primary/15 [&_mark]:text-inherit [&_mark]:rounded-sm [&_mark]:px-0.5">
                    <ReactMarkdown rehypePlugins={[rehypeRaw]}>
                      {highlightChildren(chunk.content, chunk.children)}
                    </ReactMarkdown>
                  </div>
                </div>
              ))}
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
