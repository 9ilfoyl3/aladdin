import { useState } from 'react'
import { Loader2, AlertCircle } from 'lucide-react'

interface ImagePreviewProps {
  /** 图片 blob objectURL */
  objectUrl: string | null
  error: string | null
}

/**
 * 图片预览器：<img> 直接渲染 blob objectURL，居中、保持比例、可适应面板宽高。
 * 棋盘格背景便于查看透明 PNG。
 */
function ImagePreview({ objectUrl, error }: ImagePreviewProps) {
  const [loaded, setLoaded] = useState(false)

  if (error) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-3 text-muted-foreground">
        <AlertCircle className="h-10 w-10 text-destructive/60" />
        <p className="text-sm">{error}</p>
      </div>
    )
  }

  return (
    <div className="relative h-full w-full overflow-auto bg-muted/20 flex items-center justify-center p-4">
      {(!objectUrl || !loaded) && (
        <div className="absolute inset-0 flex items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-primary/60" />
        </div>
      )}
      {objectUrl && (
        <img
          src={objectUrl}
          alt="图片预览"
          className="max-h-full max-w-full object-contain rounded-sm shadow-sm"
          onLoad={() => setLoaded(true)}
        />
      )}
    </div>
  )
}

export default ImagePreview
