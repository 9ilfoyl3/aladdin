import {
  Loader2,
  FileText,
  File,
  FileSpreadsheet,
  Presentation,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'

// 文档数据类型
export interface DocumentItem {
  id: string
  kb_id: string
  filename: string
  file_type: string
  file_size: number
  status: string
  error_message: string | null
  chunk_count: number
  created_at: string
  folder_id?: string | null
}

// 本地上传中的文件
export interface UploadingFile {
  id: string
  filename: string
  file_size: number
  status: 'uploading' | 'uploaded'
}

// 合并后的文件类型
export interface MergedFile {
  id: string
  filename: string
  file_size: number
  status: string
  error_message: string | null
  chunk_count: number
  isLocal: boolean
}

interface FileItemProps {
  doc: MergedFile
  isSelected: boolean
  onSelect: (id: string) => void
}

// 根据文件类型返回对应图标
function FileIcon({ filename, className }: { filename: string; className?: string }) {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  const iconClass = className || 'h-6 w-6'

  if (['pdf'].includes(ext)) {
    return <FileText className={`${iconClass} text-red-400`} />
  }
  if (['doc', 'docx'].includes(ext)) {
    return <FileText className={`${iconClass} text-blue-400`} />
  }
  if (['xls', 'xlsx', 'csv'].includes(ext)) {
    return <FileSpreadsheet className={`${iconClass} text-green-500`} />
  }
  if (['ppt', 'pptx'].includes(ext)) {
    return <Presentation className={`${iconClass} text-orange-400`} />
  }
  if (['md', 'txt'].includes(ext)) {
    return <FileText className={`${iconClass} text-muted-foreground`} />
  }
  return <File className={`${iconClass} text-muted-foreground`} />
}

// 状态标签
export function statusLabel(status: string) {
  switch (status) {
    case 'completed': return '已完成'
    case 'processing': return '解析中'
    case 'pending': return '排队中'
    case 'failed': return '失败'
    case 'uploading': return '上传中'
    default: return status
  }
}

// 状态颜色
export function statusColor(status: string) {
  switch (status) {
    case 'completed': return 'bg-green-100 text-green-700 border-green-200'
    case 'processing': return 'bg-yellow-100 text-yellow-700 border-yellow-200'
    case 'pending': return 'bg-blue-100 text-blue-700 border-blue-200'
    case 'failed': return 'bg-red-100 text-red-700 border-red-200'
    case 'uploading': return 'bg-orange-100 text-orange-700 border-orange-200'
    default: return ''
  }
}

// 格式化文件大小
export function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// 截断文件名
function truncateFilename(name: string, maxLen = 18) {
  if (name.length <= maxLen) return name
  const ext = name.lastIndexOf('.') > 0 ? name.slice(name.lastIndexOf('.')) : ''
  const base = name.slice(0, name.lastIndexOf('.') > 0 ? name.lastIndexOf('.') : name.length)
  const keep = maxLen - ext.length - 3
  if (keep <= 0) return name.slice(0, maxLen - 3) + '...'
  return base.slice(0, keep) + '...' + ext
}

// 获取文件扩展名
function getFileExt(filename: string) {
  return filename.split('.').pop()?.toLowerCase() || ''
}

// 文件缩略图预览
function FileThumbnail({ filename, status }: { filename: string; status: string }) {
  if (status === 'uploading') {
    return (
      <div className="w-full h-full flex items-center justify-center">
        <Loader2 className="h-6 w-6 text-primary/60 animate-spin" />
      </div>
    )
  }

  // 模拟文档预览：骨架线在上，图标在下
  return (
    <div className="w-full h-full flex flex-col items-center p-2.5 pt-3">
      {/* 骨架线条 */}
      <div className="flex flex-col gap-[3px] w-full px-1">
        <div className="h-[2px] bg-muted-foreground/12 rounded-full w-full" />
        <div className="h-[2px] bg-muted-foreground/12 rounded-full w-[80%]" />
        <div className="h-[2px] bg-muted-foreground/12 rounded-full w-[90%]" />
        <div className="h-[2px] bg-muted-foreground/12 rounded-full w-[60%]" />
        <div className="h-[2px] bg-muted-foreground/12 rounded-full w-full" />
      </div>
      {/* 文件类型图标 - 线条下方 */}
      <div className="flex items-center justify-center mt-auto pb-0.5">
        <FileIcon filename={filename} className="h-5 w-5" />
      </div>
    </div>
  )
}

// Finder 风格文件项
function FileItem({ doc, isSelected, onSelect }: FileItemProps) {
  const ext = getFileExt(doc.filename)

  return (
    <div
      className={`group relative flex flex-col items-center rounded-lg px-2 py-3 transition-all duration-150 cursor-default select-none ${
        isSelected ? 'bg-primary/8 ring-1 ring-primary/30' : 'hover:bg-muted/40'
      }`}
      onClick={(e) => { e.stopPropagation(); onSelect(doc.id) }}
    >
      {/* 文件缩略图 */}
      <div className="w-16 h-20 rounded bg-white border border-border/60 flex items-center justify-center mb-2.5 relative overflow-hidden shadow-sm">
        <FileThumbnail filename={doc.filename} status={doc.status} />

        {/* 文件类型角标 - 右上角外侧 */}
        {doc.status !== 'uploading' && (
          <div className="absolute -top-0.5 -right-0.5">
            <span className="text-[9px] font-semibold uppercase px-1 py-0.5 rounded bg-muted text-muted-foreground leading-none border border-border/40">
              {ext}
            </span>
          </div>
        )}

        {/* 非完成状态指示器 - 底部居中 */}
        {doc.status !== 'completed' && doc.status !== 'uploading' && (
          <div className="absolute bottom-1 inset-x-1 flex justify-center">
            <Badge variant="outline" className={`text-[8px] px-1.5 py-0 leading-tight ${statusColor(doc.status)}`}>
              {doc.status === 'processing' && <Loader2 className="h-2 w-2 mr-0.5 animate-spin" />}
              {statusLabel(doc.status)}
            </Badge>
          </div>
        )}
      </div>

      {/* 文件名 + 切片数 tag */}
      <p className="text-[11px] text-center text-foreground leading-tight w-full px-0.5 line-clamp-2" title={doc.filename}>
        {truncateFilename(doc.filename)}
        {doc.status === 'completed' && doc.chunk_count > 0 && (
          <span className="inline-block ml-1 text-[9px] font-medium text-primary/80 bg-primary/8 rounded px-1 py-0 leading-normal align-middle">
            {doc.chunk_count}片
          </span>
        )}
      </p>

      {/* 文件大小 */}
      <p className="text-[9px] text-muted-foreground mt-0.5">
        {formatSize(doc.file_size)}
      </p>
    </div>
  )
}

export default FileItem
