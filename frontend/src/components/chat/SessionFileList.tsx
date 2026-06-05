import { useState } from 'react'
import { X, FileText, AlertTriangle, CheckCircle2, Image as ImageIcon } from 'lucide-react'
import { Spinner } from '@/components/ui/spinner'
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from '@/components/ui/tooltip'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import type { SessionFileResponse } from '@/lib/api'

/** 同步上传中的本地占位条目（POST 在飞、尚无服务端 ID）。 */
export interface PendingSessionFile {
  /** 本地占位 ID，区分未完成的并发上传 */
  localId: string
  filename: string
  size: number
  /** 'uploading' = 解析/切分/向量化处理中；'failed' = 已知失败（携带错误文案） */
  status: 'uploading' | 'failed'
  errorMessage?: string
}

interface SessionFileListProps {
  /** 已建索引完成的服务端文件 */
  files: SessionFileResponse[]
  /** 同步上传中的本地占位 */
  pending: PendingSessionFile[]
  /** 移除单个已建索引文件（点击 X） */
  onRemove: (fileId: string) => void
  /** 关闭一个失败占位（点击 X 仅本地清理） */
  onDismissPending: (localId: string) => void
  /** 文件名 → 图片预览 URL（仅本会话内上传的图片可用：服务端临时文件处理后即删） */
  imagePreviewUrls?: Record<string, string>
}

/** 归一化后的 chip 渲染模型，服务端文件与本地占位共用一套展示逻辑。 */
interface ChipModel {
  key: string
  filename: string
  sizeBytes: number | null
  status: 'completed' | 'processing' | 'failed'
  chunkCount?: number
  errorMessage?: string
  /** 点击 X 的行为；null 表示该状态不可移除（如处理中） */
  onRemove: (() => void) | null
}

const IMAGE_EXTS = new Set(['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg'])

/** 依扩展名判断是否为图片文件。 */
export function isImageFilename(filename: string): boolean {
  const ext = filename.includes('.') ? filename.split('.').pop()!.toLowerCase() : ''
  return IMAGE_EXTS.has(ext)
}

function formatSize(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return ''
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`
}

const STATUS_TEXT: Record<ChipModel['status'], string> = {
  completed: '已就绪',
  processing: '处理中',
  failed: '失败',
}

// 不同状态用不同边框/底色区分（Req：分状态给不同的边框）。
const STATUS_CHIP_CLASS: Record<ChipModel['status'], string> = {
  completed:
    'bg-card border-border text-foreground hover:border-primary/40 hover:bg-muted/40',
  processing:
    'bg-primary/5 border-primary/40 text-foreground',
  failed:
    'bg-destructive/5 border-destructive/40 text-destructive',
}

/**
 * 会话已上传文件列表（chip 横向布局）。
 *
 * - 分状态边框：completed=中性描边 / processing=主色描边 / failed=红色描边。
 * - 悬浮 tooltip 展示文件名全称 + 体积/分段/状态；超长文件名在 chip 内截断，全称看 tooltip。
 * - 图片文件：chip 内联缩略图 + 点击放大预览（仅本会话内上传的图片，源自客户端 blob）。
 *
 * 仅在「会话已开 + (有文件 OR 有占位)」时由父组件挂载，无文件时整块隐藏。
 */
function SessionFileList({
  files,
  pending,
  onRemove,
  onDismissPending,
  imagePreviewUrls = {},
}: SessionFileListProps) {
  // 放大预览的图片（点击图片 chip 缩略图时打开）。
  const [preview, setPreview] = useState<{ url: string; name: string } | null>(null)

  if (files.length === 0 && pending.length === 0) return null

  // 服务端文件 -> 归一化模型
  const serverChips: ChipModel[] = files.map((f) => ({
    key: f.id,
    filename: f.filename,
    sizeBytes: f.file_size,
    status:
      f.status === 'processing'
        ? 'processing'
        : f.status === 'failed'
          ? 'failed'
          : 'completed',
    chunkCount: f.chunk_count,
    onRemove: f.status === 'processing' ? null : () => onRemove(f.id),
  }))

  // 本地占位 -> 归一化模型
  const pendingChips: ChipModel[] = pending.map((p) => ({
    key: p.localId,
    filename: p.filename,
    sizeBytes: p.size,
    status: p.status === 'failed' ? 'failed' : 'processing',
    errorMessage: p.errorMessage,
    // 上传中不可移除；失败可本地关闭
    onRemove: p.status === 'failed' ? () => onDismissPending(p.localId) : null,
  }))

  const chips = [...serverChips, ...pendingChips]

  function metaText(c: ChipModel): string {
    if (c.status === 'failed') return c.errorMessage || '上传失败'
    const parts: string[] = []
    const sz = formatSize(c.sizeBytes)
    if (sz) parts.push(sz)
    if (typeof c.chunkCount === 'number' && c.chunkCount > 0) parts.push(`${c.chunkCount} 段`)
    parts.push(STATUS_TEXT[c.status])
    return parts.join(' · ')
  }

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex items-center gap-1.5 flex-wrap px-2.5 pt-2.5">
        {chips.map((c) => {
          const isImg = isImageFilename(c.filename)
          const previewUrl = isImg ? imagePreviewUrls[c.filename] : undefined
          const canPreview = !!previewUrl

          return (
            <Tooltip key={c.key}>
              <TooltipTrigger asChild>
                <div
                  className={cn(
                    'group inline-flex items-center gap-1.5 h-8 pl-1.5 pr-1 rounded-xl border text-xs transition-colors max-w-[15em]',
                    STATUS_CHIP_CLASS[c.status]
                  )}
                >
                  {/* 前导：图片缩略图（可点开大图）或文件/状态图标 */}
                  {canPreview ? (
                    <button
                      type="button"
                      onClick={() => setPreview({ url: previewUrl!, name: c.filename })}
                      className="h-6 w-6 shrink-0 rounded-md overflow-hidden ring-1 ring-border hover:ring-primary/50 transition-all cursor-zoom-in"
                      aria-label={`预览图片 ${c.filename}`}
                    >
                      <img src={previewUrl} alt="" className="h-full w-full object-cover" />
                    </button>
                  ) : (
                    <span className="h-6 w-6 shrink-0 flex items-center justify-center">
                      {c.status === 'processing' ? (
                        <Spinner size="sm" />
                      ) : c.status === 'failed' ? (
                        <AlertTriangle className="h-3.5 w-3.5 text-destructive" />
                      ) : isImg ? (
                        <ImageIcon className="h-3.5 w-3.5 text-muted-foreground" />
                      ) : (
                        <FileText className="h-3.5 w-3.5 text-muted-foreground" />
                      )}
                    </span>
                  )}

                  <span className="truncate font-medium">{c.filename}</span>

                  {/* 尾随状态标识：就绪=绿勾；处理中（有缩略图时补一个小转圈）；失败用前导图标已表达 */}
                  {c.status === 'completed' && (
                    <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
                  )}
                  {c.status === 'processing' && canPreview && <Spinner size="sm" className="shrink-0" />}

                  {/* 移除/关闭按钮 */}
                  {c.onRemove ? (
                    <button
                      type="button"
                      onClick={c.onRemove}
                      className={cn(
                        'h-5 w-5 shrink-0 flex items-center justify-center rounded-md cursor-pointer transition-colors',
                        c.status === 'failed'
                          ? 'text-destructive/80 hover:text-destructive hover:bg-destructive/15'
                          : 'text-muted-foreground hover:text-destructive hover:bg-destructive/10'
                      )}
                      aria-label={`移除 ${c.filename}`}
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  ) : (
                    <span className="w-1 shrink-0" />
                  )}
                </div>
              </TooltipTrigger>

              <TooltipContent side="top" className="max-w-xs p-2">
                {canPreview && (
                  <img
                    src={previewUrl}
                    alt=""
                    className="mb-1.5 max-h-40 w-auto rounded-md object-contain"
                  />
                )}
                <div className="font-medium break-all leading-snug">{c.filename}</div>
                <div
                  className={cn(
                    'mt-0.5 text-xs',
                    c.status === 'failed' ? 'text-destructive' : 'text-muted-foreground'
                  )}
                >
                  {metaText(c)}
                </div>
              </TooltipContent>
            </Tooltip>
          )
        })}
      </div>

      {/* 图片放大预览 */}
      <Dialog open={!!preview} onOpenChange={(open) => !open && setPreview(null)}>
        <DialogContent className="max-w-3xl p-3 bg-background">
          <DialogTitle className="sr-only">{preview?.name ?? '图片预览'}</DialogTitle>
          {preview && (
            <div className="flex flex-col gap-2">
              <img
                src={preview.url}
                alt={preview.name}
                className="w-full max-h-[78vh] rounded-md object-contain"
              />
              <p className="text-center text-xs text-muted-foreground break-all">{preview.name}</p>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </TooltipProvider>
  )
}

export default SessionFileList
