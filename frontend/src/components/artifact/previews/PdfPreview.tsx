import { useEffect, useRef, useState } from 'react'
import { Loader2, AlertCircle } from 'lucide-react'

interface PdfPreviewProps {
  /** 已生成的 PDF blob objectURL（带鉴权拉取后转 blob）。为 null 时显示加载中。 */
  objectUrl: string | null
  /** 是否加载出错 */
  error: string | null
}

/**
 * PDF 预览器：用浏览器原生 PDF 渲染（<iframe> 指向 blob objectURL），
 * 自带分页/缩放/搜索工具栏，零额外依赖。
 *
 * objectUrl 由上层 ArtifactPanel 统一拉取并管理生命周期（含 revoke），
 * 本组件只负责渲染，保持数据流单向清晰。
 */
function PdfPreview({ objectUrl, error }: PdfPreviewProps) {
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const [iframeLoaded, setIframeLoaded] = useState(false)

  useEffect(() => {
    setIframeLoaded(false)
  }, [objectUrl])

  if (error) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-3 text-muted-foreground">
        <AlertCircle className="h-10 w-10 text-destructive/60" />
        <p className="text-sm">{error}</p>
      </div>
    )
  }

  return (
    <div className="relative h-full w-full bg-muted/30">
      {(!objectUrl || !iframeLoaded) && (
        <div className="absolute inset-0 flex items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-primary/60" />
        </div>
      )}
      {objectUrl && (
        <iframe
          ref={iframeRef}
          src={`${objectUrl}#toolbar=1&navpanes=0&view=FitH`}
          title="PDF 预览"
          className="h-full w-full border-0"
          onLoad={() => setIframeLoaded(true)}
        />
      )}
    </div>
  )
}

export default PdfPreview
