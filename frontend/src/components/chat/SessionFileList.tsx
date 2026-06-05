import { Paperclip, X, FileText, Loader2, AlertTriangle, CheckCircle2 } from 'lucide-react'
import { Spinner } from '@/components/ui/spinner'
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
}

function formatSize(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return ''
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`
}

/**
 * 会话已上传文件列表（chip 横向布局）。
 *
 * - 服务端文件：展示文件名 + 状态徽标 + 移除按钮；状态以颜色区分
 *   completed=绿勾 / processing=蓝旋 / failed=红叹号。
 * - 本地占位：上传 POST 在飞期间显示"处理中"旋转图标 + 文件名 + 大小；
 *   后端同步处理完成后由父组件刷新列表 + 清掉占位。
 *
 * 仅在「会话已开 + (有文件 OR 有占位)」时由父组件挂载，无文件时整块隐藏。
 */
function SessionFileList({ files, pending, onRemove, onDismissPending }: SessionFileListProps) {
  if (files.length === 0 && pending.length === 0) return null

  return (
    <div className="flex items-center gap-1.5 flex-wrap px-1 pb-2">
      <Paperclip className="h-3 w-3 text-muted-foreground/70 shrink-0" />
      <span className="text-[11px] text-muted-foreground/80 mr-1">本会话文件</span>

      {/* 服务端已建索引文件 */}
      {files.map((f) => {
        const isProcessing = f.status === 'processing'
        const isFailed = f.status === 'failed'
        return (
          <div
            key={f.id}
            className="group flex items-center gap-1.5 h-7 pl-2 pr-1 bg-muted/50 hover:bg-muted rounded-lg text-xs text-foreground border border-transparent hover:border-border transition-colors max-w-[14em]"
            title={`${f.filename}${f.file_size ? ` · ${formatSize(f.file_size)}` : ''} · ${f.chunk_count} 段`}
          >
            <FileText className="h-3 w-3 shrink-0 text-muted-foreground" />
            <span className="truncate">{f.filename}</span>
            {isProcessing && <Loader2 className="h-3 w-3 shrink-0 animate-spin text-primary" />}
            {isFailed && <AlertTriangle className="h-3 w-3 shrink-0 text-destructive" />}
            {!isProcessing && !isFailed && <CheckCircle2 className="h-3 w-3 shrink-0 text-emerald-500" />}
            <button
              type="button"
              onClick={() => onRemove(f.id)}
              className="h-5 w-5 flex items-center justify-center rounded text-muted-foreground hover:text-destructive hover:bg-destructive/10 cursor-pointer transition-colors"
              title="移除"
              aria-label={`移除 ${f.filename}`}
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        )
      })}

      {/* 本地上传中占位 */}
      {pending.map((p) => {
        const isFailed = p.status === 'failed'
        return (
          <div
            key={p.localId}
            className={
              'flex items-center gap-1.5 h-7 pl-2 pr-1 rounded-lg text-xs border max-w-[16em] ' +
              (isFailed
                ? 'bg-destructive/10 border-destructive/30 text-destructive'
                : 'bg-primary/5 border-primary/20 text-foreground')
            }
            title={isFailed ? p.errorMessage || '上传失败' : `处理中：${p.filename}`}
          >
            {isFailed ? (
              <AlertTriangle className="h-3 w-3 shrink-0" />
            ) : (
              <Spinner size="sm" />
            )}
            <span className="truncate">{p.filename}</span>
            {!isFailed && p.size > 0 && (
              <span className="text-muted-foreground shrink-0">· {formatSize(p.size)}</span>
            )}
            {!isFailed && <span className="text-muted-foreground shrink-0">· 处理中</span>}
            {isFailed && (
              <button
                type="button"
                onClick={() => onDismissPending(p.localId)}
                className="h-5 w-5 flex items-center justify-center rounded text-destructive/80 hover:text-destructive hover:bg-destructive/15 cursor-pointer"
                aria-label="关闭"
              >
                <X className="h-3 w-3" />
              </button>
            )}
          </div>
        )
      })}
    </div>
  )
}

export default SessionFileList
