import { useState } from 'react'
import { X, FileText, AlertTriangle, CheckCircle2, Image as ImageIcon } from 'lucide-react'
import { Spinner } from '@/components/ui/spinner'
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from '@/components/ui/tooltip'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import type { SessionFileResponse } from '@/lib/api'
import { useArtifactStore, isPreviewable } from '@/stores/artifactStore'

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
  /** 移除单个已建索引文件（点击取消图标） */
  onRemove: (fileId: string) => void
  /** 取消一个上传中的占位（中止在飞的 POST） */
  onCancelPending: (localId: string) => void
  /** 关闭一个失败占位（仅本地清理） */
  onDismissPending: (localId: string) => void
  /** 文件名 → 图片预览 URL（仅本会话内上传的图片可用：服务端临时文件处理后即删） */
  imagePreviewUrls?: Record<string, string>
  /** 当前会话 ID：用于 Artifact 面板按会话拉取附件原件预览 */
  sessionId?: string | null
}

/** 归一化后的 chip 渲染模型，服务端文件与本地占位共用一套展示逻辑。 */
interface ChipModel {
  key: string
  filename: string
  sizeBytes: number | null
  status: 'completed' | 'processing' | 'failed'
  chunkCount?: number
  errorMessage?: string
  /** 取消/移除该 chip 的行为（全状态可取消，悬浮时由状态图标处触发） */
  onCancel: () => void
  /** 取消按钮的无障碍文案（上传中=取消上传 / 其余=移除） */
  cancelLabel: string
  /** 可在 Artifact 面板预览（已完成的服务端文件 + 支持的类型）；点击 chip 主体打开 */
  onPreview?: () => void
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
  onCancelPending,
  onDismissPending,
  imagePreviewUrls = {},
  sessionId = null,
}: SessionFileListProps) {
  // 放大预览的图片（点击图片 chip 缩略图时打开）。
  const [preview, setPreview] = useState<{ url: string; name: string } | null>(null)
  // 当前悬浮的 chip key：悬浮时把状态图标替换为取消图标（取消按钮不常驻）。
  const [hoveredKey, setHoveredKey] = useState<string | null>(null)
  const openArtifact = useArtifactStore((s) => s.openArtifact)

  if (files.length === 0 && pending.length === 0) return null

  // 文件类型扩展名（小写，无点）
  function fileExt(filename: string): string {
    return filename.includes('.') ? filename.split('.').pop()!.toLowerCase() : ''
  }

  // 服务端文件 -> 归一化模型（全状态可取消：处理中=取消，其余=移除）
  const serverChips: ChipModel[] = files.map((f) => {
    const ext = (f.file_type || fileExt(f.filename)).toLowerCase()
    // 图片已有内联缩略图 + 点击放大（客户端 blob，即时），不走 Artifact；
    // 其余可预览类型（pdf/txt/md/csv）点击 chip 主体在 Artifact 面板预览。
    const previewable =
      f.status === 'completed' &&
      !!sessionId &&
      isPreviewable(ext) &&
      !isImageFilename(f.filename)
    return {
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
      onCancel: () => onRemove(f.id),
      cancelLabel: f.status === 'processing' ? `取消上传 ${f.filename}` : `移除 ${f.filename}`,
      onPreview: previewable
        ? () =>
            openArtifact({
              id: f.id,
              filename: f.filename,
              fileType: ext,
              source: 'session-file',
              sessionId: sessionId!,
            })
        : undefined,
    }
  })

  // 本地占位 -> 归一化模型（上传中=中止 POST；失败=本地关闭）
  const pendingChips: ChipModel[] = pending.map((p) => ({
    key: p.localId,
    filename: p.filename,
    sizeBytes: p.size,
    status: p.status === 'failed' ? 'failed' : 'processing',
    errorMessage: p.errorMessage,
    onCancel:
      p.status === 'failed'
        ? () => onDismissPending(p.localId)
        : () => onCancelPending(p.localId),
    cancelLabel: p.status === 'failed' ? `关闭 ${p.filename}` : `取消上传 ${p.filename}`,
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
          const showCancel = hoveredKey === c.key

          // 取消按钮：悬浮时占据状态图标位置（不常驻）。
          const cancelButton = (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation()
                c.onCancel()
              }}
              className="h-full w-full flex items-center justify-center rounded-md text-muted-foreground hover:text-destructive transition-colors cursor-pointer"
              aria-label={c.cancelLabel}
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )

          // 状态图标（默认态）：处理中转圈 / 失败警告 / 图片或文件图标。
          const statusIcon =
            c.status === 'processing' ? (
              <Spinner size="sm" />
            ) : c.status === 'failed' ? (
              <AlertTriangle className="h-3.5 w-3.5 text-destructive" />
            ) : isImg ? (
              <ImageIcon className="h-3.5 w-3.5 text-muted-foreground" />
            ) : (
              <FileText className="h-3.5 w-3.5 text-muted-foreground" />
            )

          return (
            <Tooltip key={c.key}>
              <TooltipTrigger asChild>
                <div
                  className={cn(
                    'group inline-flex items-center gap-1.5 h-8 pl-1.5 pr-2 rounded-xl border text-xs transition-colors max-w-[15em]',
                    STATUS_CHIP_CLASS[c.status],
                    c.onPreview && 'cursor-pointer'
                  )}
                  onMouseEnter={() => setHoveredKey(c.key)}
                  onMouseLeave={() => setHoveredKey((k) => (k === c.key ? null : k))}
                  onFocus={() => setHoveredKey(c.key)}
                  onBlur={() => setHoveredKey((k) => (k === c.key ? null : k))}
                  onClick={() => c.onPreview?.()}
                >
                  {/* 前导槽：图片缩略图（可点开大图）；非图片时为状态图标，悬浮替换为取消按钮 */}
                  {canPreview ? (
                    <span className="h-6 w-6 shrink-0">
                      {showCancel ? (
                        cancelButton
                      ) : (
                        <button
                          type="button"
                          onClick={() => setPreview({ url: previewUrl!, name: c.filename })}
                          className="h-full w-full rounded-md overflow-hidden ring-1 ring-border hover:ring-primary/50 transition-all cursor-zoom-in"
                          aria-label={`预览图片 ${c.filename}`}
                        >
                          <img src={previewUrl} alt="" className="h-full w-full object-cover" />
                        </button>
                      )}
                    </span>
                  ) : (
                    <span className="h-6 w-6 shrink-0 flex items-center justify-center">
                      {showCancel ? cancelButton : statusIcon}
                    </span>
                  )}

                  <span className="truncate font-medium">{c.filename}</span>

                  {/* 尾随状态标识：就绪=绿勾；图片处理中补转圈。悬浮时（图片 chip）让位给前导取消按钮，此处隐藏。 */}
                  {!showCancel && c.status === 'completed' && (
                    <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-500" />
                  )}
                  {!showCancel && c.status === 'processing' && canPreview && (
                    <Spinner size="sm" className="shrink-0" />
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
