import { useState, useCallback, useRef } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown from 'react-markdown'
import {
  Upload,
  FileText,
  ArrowLeft,
  Loader2,
  File,
  FileSpreadsheet,
  Presentation,
  Eye,
  Trash2,
  Copy,
} from 'lucide-react'
import { documentApi, knowledgeBaseApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
} from '@/components/ui/context-menu'

// 文档数据类型
interface DocumentItem {
  id: string
  kb_id: string
  filename: string
  file_type: string
  file_size: number
  status: string
  error_message: string | null
  chunk_count: number
  created_at: string
}

// 本地上传中的文件
interface UploadingFile {
  id: string
  filename: string
  file_size: number
  status: 'uploading' | 'uploaded'
}

// Chunk 数据类型
interface ChunkItem {
  id: string
  content: string
  chunk_index: number
}

// 知识库数据类型
interface KnowledgeBaseItem {
  id: string
  name: string
}

// 根据文件类型返回对应图标
function FileIcon({ filename, className }: { filename: string; className?: string }) {
  const ext = filename.split('.').pop()?.toLowerCase() || ''
  const iconClass = className || 'h-8 w-8'

  if (['pdf'].includes(ext)) {
    return <FileText className={`${iconClass} text-red-400`} />
  }
  if (['doc', 'docx'].includes(ext)) {
    return <FileText className={`${iconClass} text-blue-400`} />
  }
  if (['xls', 'xlsx'].includes(ext)) {
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

// 文档管理页面
function Documents() {
  const { id: kbId } = useParams<{ id: string }>()
  const queryClient = useQueryClient()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [viewingChunks, setViewingChunks] = useState<string | null>(null)
  const [uploadingFiles, setUploadingFiles] = useState<UploadingFile[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)

  // 获取知识库信息
  const { data: kb } = useQuery({
    queryKey: ['knowledge-base', kbId],
    queryFn: () => knowledgeBaseApi.get(kbId!) as Promise<KnowledgeBaseItem>,
    enabled: !!kbId,
  })

  // 获取文档列表
  const { data: documents = [], isLoading } = useQuery({
    queryKey: ['documents', kbId],
    queryFn: () => documentApi.list(kbId!) as Promise<DocumentItem[]>,
    enabled: !!kbId,
    refetchInterval: 5000,
  })

  // 获取切片列表
  const { data: chunks = [] } = useQuery({
    queryKey: ['chunks', viewingChunks],
    queryFn: () => documentApi.chunks(viewingChunks!) as Promise<ChunkItem[]>,
    enabled: !!viewingChunks,
  })

  // 上传文件
  const uploadMutation = useMutation({
    mutationFn: ({ file, localId }: { file: File; localId: string }) => {
      return documentApi.upload(kbId!, file).then((res) => ({ res, localId }))
    },
    onSuccess: ({ localId }) => {
      setUploadingFiles((prev) => prev.filter((f) => f.id !== localId))
      queryClient.invalidateQueries({ queryKey: ['documents', kbId] })
    },
    onError: (_err, { localId }) => {
      setUploadingFiles((prev) => prev.filter((f) => f.id !== localId))
    },
  })

  // 删除文档
  const deleteMutation = useMutation({
    mutationFn: (id: string) => documentApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents', kbId] })
    },
  })

  // 处理文件选择
  function handleFileSelect(files: FileList | null) {
    if (!files) return
    Array.from(files).forEach((file) => {
      const localId = `local_${Date.now()}_${Math.random().toString(36).slice(2)}`
      setUploadingFiles((prev) => [
        ...prev,
        { id: localId, filename: file.name, file_size: file.size, status: 'uploading' },
      ])
      uploadMutation.mutate({ file, localId })
    })
  }

  // 拖拽事件处理
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setIsDragging(true)
  }, [])

  const handleDragLeave = useCallback(() => {
    setIsDragging(false)
  }, [])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setIsDragging(false)
    handleFileSelect(e.dataTransfer.files)
  }, [kbId])

  // 状态标签
  function statusLabel(status: string) {
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
  function statusColor(status: string) {
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
  function formatSize(bytes: number) {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  // 截断文件名
  function truncateFilename(name: string, maxLen = 20) {
    if (name.length <= maxLen) return name
    const ext = name.lastIndexOf('.') > 0 ? name.slice(name.lastIndexOf('.')) : ''
    const base = name.slice(0, name.lastIndexOf('.') > 0 ? name.lastIndexOf('.') : name.length)
    const keep = maxLen - ext.length - 3
    if (keep <= 0) return name.slice(0, maxLen - 3) + '...'
    return base.slice(0, keep) + '...' + ext
  }

  // 合并列表
  const allFiles = [
    ...uploadingFiles.map((f) => ({
      id: f.id,
      filename: f.filename,
      file_size: f.file_size,
      status: f.status,
      error_message: null,
      chunk_count: 0,
      isLocal: true,
    })),
    ...documents.map((doc) => ({ ...doc, isLocal: false })),
  ]

  // 点击空白区域取消选中
  function handleBackgroundClick() {
    setSelectedId(null)
  }

  // 文件 Item 组件
  function FileItem({ doc }: { doc: typeof allFiles[number] }) {
    const isSelected = selectedId === doc.id

    return (
      <div
        className={`group relative flex flex-col items-center rounded-xl p-3 transition-all duration-150 cursor-default select-none ${
          isSelected ? 'bg-primary/8 ring-1 ring-primary/30' : 'hover:bg-muted/50'
        }`}
        onClick={(e) => { e.stopPropagation(); setSelectedId(doc.id) }}
      >
        {/* 文件预览缩略图 */}
        <div className="w-full aspect-4/5 rounded-lg bg-white border border-border/60 flex items-center justify-center mb-2 relative overflow-hidden shadow-sm">
          {doc.status === 'uploading' ? (
            <Loader2 className="h-8 w-8 text-orange-400 animate-spin" />
          ) : (
            <FileIcon filename={doc.filename} className="h-10 w-10" />
          )}

          {/* 状态指示器 */}
          {doc.status !== 'completed' && doc.status !== 'uploading' && (
            <div className="absolute bottom-1.5 left-1.5">
              <Badge variant="outline" className={`text-[10px] px-1.5 py-0 ${statusColor(doc.status)}`}>
                {doc.status === 'processing' && <Loader2 className="h-2.5 w-2.5 mr-0.5 animate-spin" />}
                {statusLabel(doc.status)}
              </Badge>
            </div>
          )}

          {/* 切片数 */}
          {doc.status === 'completed' && doc.chunk_count > 0 && (
            <div className="absolute bottom-1.5 right-1.5">
              <span className="text-[10px] text-muted-foreground bg-white/90 backdrop-blur-sm rounded px-1.5 py-0.5 border border-border/50">
                {doc.chunk_count} 片
              </span>
            </div>
          )}
        </div>

        {/* 文件名 */}
        <p className="text-xs text-center text-foreground leading-tight w-full px-0.5 line-clamp-2" title={doc.filename}>
          {truncateFilename(doc.filename)}
        </p>

        {/* 文件大小 */}
        <p className="text-[10px] text-muted-foreground mt-0.5">
          {formatSize(doc.file_size)}
        </p>
      </div>
    )
  }

  return (
    <div
      className="relative h-full"
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      onClick={handleBackgroundClick}
    >
      {/* 拖拽覆盖层 */}
      {isDragging && (
        <div className="fixed inset-0 z-50 bg-primary/5 backdrop-blur-sm flex items-center justify-center">
          <div className="bg-card border-2 border-dashed border-primary rounded-2xl p-12 text-center shadow-2xl">
            <Upload className="h-12 w-12 mx-auto mb-4 text-primary" />
            <p className="text-lg font-medium text-foreground">释放文件以上传</p>
            <p className="text-sm text-muted-foreground mt-1">支持 PDF、Word、Excel、PPT、TXT、Markdown</p>
          </div>
        </div>
      )}

      {/* 页面头部 */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Link to="/knowledge-bases">
            <button className="h-8 w-8 rounded-lg flex items-center justify-center hover:bg-muted transition-colors cursor-pointer">
              <ArrowLeft className="h-4 w-4 text-muted-foreground" />
            </button>
          </Link>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">{kb?.name || '文档管理'}</h1>
            <p className="text-muted-foreground text-sm mt-0.5">
              {allFiles.length} 个文件
            </p>
          </div>
        </div>

        {/* 上传按钮 */}
        <Button onClick={() => fileInputRef.current?.click()} className="gap-2 cursor-pointer">
          <Upload className="h-4 w-4" />
          上传文件
        </Button>
        <input
          ref={fileInputRef}
          type="file"
          className="hidden"
          multiple
          accept=".pdf,.docx,.xlsx,.pptx,.txt,.md"
          onChange={(e) => handleFileSelect(e.target.files)}
        />
      </div>

      {/* 文件网格 - 访达风格 */}
      {isLoading ? (
        <div className="flex items-center justify-center py-20">
          <div className="text-center">
            <div className="h-8 w-8 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto mb-3" />
            <p className="text-sm text-muted-foreground">加载中...</p>
          </div>
        </div>
      ) : allFiles.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20">
          <div className="w-20 h-20 rounded-2xl bg-muted/40 flex items-center justify-center mb-4">
            <FileText className="h-10 w-10 text-muted-foreground/40" />
          </div>
          <p className="text-muted-foreground mb-1">暂无文档</p>
          <p className="text-sm text-muted-foreground/70 mb-4">拖拽文件到此处或点击上传按钮</p>
          <Button variant="outline" onClick={() => fileInputRef.current?.click()} className="gap-2 cursor-pointer">
            <Upload className="h-4 w-4" />
            选择文件
          </Button>
        </div>
      ) : (
        <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6 xl:grid-cols-7 2xl:grid-cols-8 gap-3">
          {allFiles.map((doc) => {
            // 上传中的文件不需要右键菜单
            if (doc.isLocal) {
              return (
                <div key={doc.id}>
                  <FileItem doc={doc} />
                </div>
              )
            }

            return (
              <ContextMenu key={doc.id}>
                <ContextMenuTrigger>
                  <FileItem doc={doc} />
                </ContextMenuTrigger>
                <ContextMenuContent className="w-48">
                  <ContextMenuItem
                    disabled={doc.status !== 'completed'}
                    onClick={() => setViewingChunks(doc.id)}
                  >
                    <Eye className="h-4 w-4 mr-2" />
                    查看切片
                  </ContextMenuItem>
                  <ContextMenuItem
                    onClick={() => navigator.clipboard.writeText(doc.filename)}
                  >
                    <Copy className="h-4 w-4 mr-2" />
                    复制文件名
                  </ContextMenuItem>
                  <ContextMenuSeparator />
                  <ContextMenuItem
                    destructive
                    onClick={() => deleteMutation.mutate(doc.id)}
                  >
                    <Trash2 className="h-4 w-4 mr-2" />
                    删除文件
                  </ContextMenuItem>
                </ContextMenuContent>
              </ContextMenu>
            )
          })}
        </div>
      )}

      {/* 切片查看对话框 */}
      <Dialog open={!!viewingChunks} onOpenChange={() => setViewingChunks(null)}>
        <DialogContent className="max-w-2xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>文档切片</DialogTitle>
          </DialogHeader>
          <div className="flex-1 overflow-auto space-y-3">
            {chunks.length === 0 ? (
              <p className="text-muted-foreground text-center py-4">暂无切片数据</p>
            ) : (
              chunks.map((chunk, idx) => (
                <div key={chunk.id || idx} className="border rounded-lg p-3">
                  <div className="text-xs text-muted-foreground mb-1.5 font-medium">
                    切片 #{chunk.chunk_index ?? idx + 1}
                  </div>
                  <div className="text-sm leading-relaxed prose prose-sm max-w-none dark:prose-invert [&>p]:mb-2 [&>p:last-child]:mb-0 [&_table]:text-xs [&_table]:border-collapse [&_td]:border [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:px-2 [&_th]:py-1">
                    <ReactMarkdown>{chunk.content}</ReactMarkdown>
                  </div>
                </div>
              ))
            )}
          </div>
          <div className="flex justify-end pt-4 border-t">
            <Button variant="outline" onClick={() => setViewingChunks(null)}>
              关闭
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default Documents
