import { Streamdown } from 'streamdown'
import { cjk } from '@streamdown/cjk'
import { Loader2, AlertCircle } from 'lucide-react'

interface MarkdownPreviewProps {
  /** 已读取的 Markdown 源文本；为 null 时显示加载中 */
  text: string | null
  loading: boolean
  error: string | null
}

/**
 * Markdown 预览器（md）：复用项目既有 streamdown + cjk 渲染，与对话/切片视图一致的
 * 排版样式（prose），零额外依赖。
 */
function MarkdownPreview({ text, loading, error }: MarkdownPreviewProps) {
  if (error) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-3 text-muted-foreground">
        <AlertCircle className="h-10 w-10 text-destructive/60" />
        <p className="text-sm">{error}</p>
      </div>
    )
  }

  if (loading || text === null) {
    return (
      <div className="h-full flex items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-primary/60" />
      </div>
    )
  }

  return (
    <div className="h-full overflow-auto bg-background">
      <div className="px-6 py-5 prose prose-sm max-w-none dark:prose-invert [&>p]:mb-2 [&_table]:text-xs [&_table]:w-full [&_table]:border-collapse [&_td]:border [&_td]:border-border/50 [&_td]:px-2.5 [&_td]:py-1.5 [&_th]:border [&_th]:border-border/50 [&_th]:px-2.5 [&_th]:py-1.5 [&_th]:bg-muted/40 [&_th]:font-medium">
        <Streamdown mode="static" plugins={{ cjk: cjk }}>
          {text}
        </Streamdown>
      </div>
    </div>
  )
}

export default MarkdownPreview
