import { Loader2, AlertCircle } from 'lucide-react'

interface TextPreviewProps {
  /** 已读取的纯文本内容；为 null 时显示加载中 */
  text: string | null
  loading: boolean
  error: string | null
}

/**
 * 纯文本预览器（txt）：等宽字体、保留换行/空白、自动换行长行。
 */
function TextPreview({ text, loading, error }: TextPreviewProps) {
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
      <pre className="px-5 py-4 text-[13px] leading-relaxed font-mono whitespace-pre-wrap wrap-break-word text-foreground">
        {text}
      </pre>
    </div>
  )
}

export default TextPreview
