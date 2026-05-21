import { useState, useCallback, useRef } from 'react'
import { useParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Upload,
  FileText,
  ArrowLeft,
  Eye,
  Trash2,
  Copy,
  FolderPlus,
  Pencil,
  FolderInput,
  FolderUp,
  LayoutGrid,
  List,
  RotateCcw,
} from 'lucide-react'
import { documentApi, knowledgeBaseApi, folderApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
} from '@/components/ui/context-menu'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from '@/components/ui/dialog'
import FileItem from '@/components/documents/FileItem'
import FolderItem from '@/components/documents/FolderItem'
import FolderBreadcrumb from '@/components/documents/FolderBreadcrumb'
import NewFolderDialog from '@/components/documents/NewFolderDialog'
import RenameDialog from '@/components/documents/RenameDialog'
import ChunkViewer from '@/components/documents/ChunkViewer'

import type { DocumentItem, UploadingFile, MergedFile } from '@/components/documents/FileItem'
import { formatSize, statusLabel, statusColor } from '@/components/documents/FileItem'
import type { FolderData } from '@/components/documents/FolderItem'

// 知识库数据类型
interface KnowledgeBaseItem {
  id: string
  name: string
}

// 面包屑项
interface BreadcrumbItem {
  id: string | null
  name: string
}

// 文档管理页面 - Finder 风格
function Documents() {
  const { id: kbId } = useParams<{ id: string }>()
  const queryClient = useQueryClient()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const folderInputRef = useRef<HTMLInputElement>(null)

  // 导航状态
  const [currentFolderId, setCurrentFolderId] = useState<string | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  // 对话框状态
  const [showNewFolder, setShowNewFolder] = useState(false)
  const [renamingFolder, setRenamingFolder] = useState<FolderData | null>(null)
  const [viewingChunks, setViewingChunks] = useState<string | null>(null)
  const [viewMode, setViewMode] = useState<'grid' | 'list'>('grid')

  // 上传状态
  const [uploadingFiles, setUploadingFiles] = useState<UploadingFile[]>([])

  // 文件夹上传状态
  const [folderUploadDialog, setFolderUploadDialog] = useState(false)
  const [folderValidation, setFolderValidation] = useState<{
    supported: { relative_path: string; filename: string; file_type: string }[]
    unsupported: { relative_path: string; filename: string; file_type: string; reason?: string }[]
    folders: string[]
    files: File[]
    paths: string[]
  } | null>(null)
  const [folderUploading, setFolderUploading] = useState(false)

  // ============================================================
  // 数据查询
  // ============================================================

  // 获取知识库信息
  const { data: kb } = useQuery({
    queryKey: ['knowledge-base', kbId],
    queryFn: () => knowledgeBaseApi.get(kbId!) as Promise<KnowledgeBaseItem>,
    enabled: !!kbId,
  })

  // 获取当前目录下的文件夹
  const { data: folders = [] } = useQuery({
    queryKey: ['folders', kbId, currentFolderId],
    queryFn: () => folderApi.list(kbId!, currentFolderId) as Promise<FolderData[]>,
    enabled: !!kbId,
  })

  // 获取当前目录下的文档
  const { data: documents = [], isLoading } = useQuery({
    queryKey: ['documents', kbId, currentFolderId],
    queryFn: () => documentApi.list(kbId!, currentFolderId) as Promise<DocumentItem[]>,
    enabled: !!kbId,
    refetchInterval: 5000,
  })

  // 获取面包屑
  const { data: breadcrumb = [] } = useQuery({
    queryKey: ['breadcrumb', kbId, currentFolderId],
    queryFn: () => folderApi.breadcrumb(kbId!, currentFolderId!) as Promise<BreadcrumbItem[]>,
    enabled: !!kbId && !!currentFolderId,
  })

  // ============================================================
  // 变更操作
  // ============================================================

  // 创建文件夹
  const createFolderMutation = useMutation({
    mutationFn: (name: string) => folderApi.create(kbId!, { name, parent_id: currentFolderId }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders', kbId, currentFolderId] })
      setShowNewFolder(false)
    },
  })

  // 重命名文件夹
  const renameFolderMutation = useMutation({
    mutationFn: ({ folderId, name }: { folderId: string; name: string }) =>
      folderApi.update(folderId, { name }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders', kbId, currentFolderId] })
      setRenamingFolder(null)
    },
  })

  // 删除文件夹
  const deleteFolderMutation = useMutation({
    mutationFn: (folderId: string) => folderApi.delete(folderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['folders', kbId, currentFolderId] })
    },
  })

  // 上传文件
  const uploadMutation = useMutation({
    mutationFn: ({ file, localId }: { file: File; localId: string }) => {
      return documentApi.upload(kbId!, file, currentFolderId).then((res) => ({ res, localId }))
    },
    onSuccess: ({ res, localId }) => {
      setUploadingFiles((prev) => prev.filter((f) => f.id !== localId))
      if (res?.status === 'duplicate') {
        alert(res.error_message || '文件已存在（内容重复）')
      }
      queryClient.invalidateQueries({ queryKey: ['documents', kbId, currentFolderId] })
    },
    onError: (_err, { localId }) => {
      setUploadingFiles((prev) => prev.filter((f) => f.id !== localId))
    },
  })

  // 删除文档
  const deleteMutation = useMutation({
    mutationFn: (id: string) => documentApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents', kbId, currentFolderId] })
    },
  })

  // 重试失败文档
  const retryMutation = useMutation({
    mutationFn: (id: string) => documentApi.retry(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents', kbId, currentFolderId] })
    },
  })

  // ============================================================
  // 事件处理
  // ============================================================

  // 导航到文件夹
  function navigateToFolder(folderId: string | null) {
    setCurrentFolderId(folderId)
    setSelectedId(null)
  }

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

  // 处理文件夹选择
  async function handleFolderSelect(files: FileList | null) {
    if (!files || files.length === 0) return

    const fileArray = Array.from(files)
    const paths = fileArray.map((f) => f.webkitRelativePath)

    // 调用校验接口
    try {
      const validation = await documentApi.validateFolder(kbId!, paths)
      setFolderValidation({
        supported: validation.supported_files,
        unsupported: validation.unsupported_files,
        folders: validation.folder_structure,
        files: fileArray,
        paths,
      })
      setFolderUploadDialog(true)
    } catch (err) {
      console.error('文件夹校验失败:', err)
    }
  }

  // 确认文件夹上传
  async function handleFolderUploadConfirm() {
    if (!folderValidation || !kbId) return

    setFolderUploading(true)

    // 只上传支持的文件
    const supportedPaths = new Set(folderValidation.supported.map((f) => f.relative_path))
    const filesToUpload: File[] = []
    const pathsToUpload: string[] = []

    for (let i = 0; i < folderValidation.files.length; i++) {
      const path = folderValidation.paths[i]
      if (supportedPaths.has(path)) {
        filesToUpload.push(folderValidation.files[i])
        pathsToUpload.push(path)
      }
    }

    try {
      await documentApi.uploadFolder(kbId, filesToUpload, pathsToUpload, currentFolderId)
      queryClient.invalidateQueries({ queryKey: ['folders', kbId, currentFolderId] })
      queryClient.invalidateQueries({ queryKey: ['documents', kbId, currentFolderId] })
      setFolderUploadDialog(false)
      setFolderValidation(null)
    } catch (err) {
      console.error('文件夹上传失败:', err)
    } finally {
      setFolderUploading(false)
    }
  }

  // 拖拽事件
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
  }, [currentFolderId, kbId])

  // 选中项
  function handleSelectFolder(id: string) {
    setSelectedId(id)
  }

  function handleSelectFile(id: string) {
    setSelectedId(id)
  }

  // 点击空白取消选中
  function handleBackgroundClick() {
    setSelectedId(null)
  }

  // ============================================================
  // 合并列表
  // ============================================================

  const allFiles: MergedFile[] = [
    ...uploadingFiles.map((f) => ({
      id: f.id,
      filename: f.filename,
      file_size: f.file_size,
      status: f.status,
      error_message: null,
      chunk_count: 0,
      isLocal: true,
    })),
    ...documents.map((doc) => ({
      id: doc.id,
      filename: doc.filename,
      file_size: doc.file_size,
      status: doc.status,
      error_message: doc.error_message,
      chunk_count: doc.chunk_count,
      isLocal: false,
    })),
  ]

  const totalItems = folders.length + allFiles.length

  // ============================================================
  // 渲染
  // ============================================================

  return (
    <div
      className="relative h-full flex flex-col"
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
            <p className="text-sm text-muted-foreground mt-1">
              上传到{currentFolderId ? '当前文件夹' : '根目录'}
            </p>
          </div>
        </div>
      )}

      {/* 页面头部 */}
      <div className="flex items-center justify-between mb-4 shrink-0">
        <div className="flex items-center gap-3">
          <Link to="/knowledge-bases">
            <button className="h-8 w-8 rounded-lg flex items-center justify-center hover:bg-muted transition-colors cursor-pointer">
              <ArrowLeft className="h-4 w-4 text-muted-foreground" />
            </button>
          </Link>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">{kb?.name || '文档管理'}</h1>
          </div>
        </div>

        {/* 操作按钮 */}
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={(e) => { e.stopPropagation(); setShowNewFolder(true) }}
            className="gap-1.5 cursor-pointer"
          >
            <FolderPlus className="h-4 w-4" />
            新建文件夹
          </Button>
          <Button
            size="sm"
            onClick={(e) => { e.stopPropagation(); fileInputRef.current?.click() }}
            className="gap-1.5 cursor-pointer"
          >
            <Upload className="h-4 w-4" />
            上传文件
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={(e) => { e.stopPropagation(); folderInputRef.current?.click() }}
            className="gap-1.5 cursor-pointer"
          >
            <FolderUp className="h-4 w-4" />
            上传文件夹
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            multiple
            accept=".pdf,.docx,.xlsx,.pptx,.csv,.txt,.md,.jpg,.jpeg,.png"
            onChange={(e) => handleFileSelect(e.target.files)}
          />
          <input
            ref={folderInputRef}
            type="file"
            className="hidden"
            {...({ webkitdirectory: '', directory: '' } as React.InputHTMLAttributes<HTMLInputElement>)}
            onChange={(e) => handleFolderSelect(e.target.files)}
          />
        </div>
      </div>

      {/* 面包屑导航 */}
      <div className="flex items-center justify-between mb-4 shrink-0">
        <FolderBreadcrumb items={breadcrumb} onNavigate={navigateToFolder} />
        <div className="flex items-center border border-border rounded-lg p-0.5 shrink-0">
          <button
            onClick={(e) => { e.stopPropagation(); setViewMode('grid') }}
            className={`p-1.5 rounded-md cursor-pointer transition-colors ${viewMode === 'grid' ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'}`}
          >
            <LayoutGrid className="h-4 w-4" />
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); setViewMode('list') }}
            className={`p-1.5 rounded-md cursor-pointer transition-colors ${viewMode === 'list' ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground'}`}
          >
            <List className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* 内容区域 */}
      <div className="flex-1 min-h-0 overflow-auto">
        {isLoading ? (
          <div className="flex items-center justify-center py-20">
            <div className="text-center">
              <div className="h-8 w-8 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto mb-3" />
              <p className="text-sm text-muted-foreground">加载中...</p>
            </div>
          </div>
        ) : totalItems === 0 ? (
          <div className="flex flex-col items-center justify-center py-20">
            <div className="w-20 h-20 rounded-2xl bg-muted/40 flex items-center justify-center mb-4">
              <FileText className="h-10 w-10 text-muted-foreground/40" />
            </div>
            <p className="text-muted-foreground mb-1">
              {currentFolderId ? '此文件夹为空' : '暂无文档'}
            </p>
            <p className="text-sm text-muted-foreground/70 mb-4">拖拽文件到此处或点击上传按钮</p>
            <div className="flex gap-2">
              <Button
                variant="outline"
                onClick={(e) => { e.stopPropagation(); setShowNewFolder(true) }}
                className="gap-2 cursor-pointer"
              >
                <FolderPlus className="h-4 w-4" />
                新建文件夹
              </Button>
              <Button
                variant="outline"
                onClick={(e) => { e.stopPropagation(); fileInputRef.current?.click() }}
                className="gap-2 cursor-pointer"
              >
                <Upload className="h-4 w-4" />
                选择文件
              </Button>
            </div>
          </div>
        ) : viewMode === 'grid' ? (
          <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-9 2xl:grid-cols-10 gap-2 p-2">
            {/* 文件夹列表 */}
            {folders.map((folder) => (
              <ContextMenu key={folder.id}>
                <ContextMenuTrigger>
                  <FolderItem
                    folder={folder}
                    isSelected={selectedId === folder.id}
                    onSelect={handleSelectFolder}
                    onOpen={navigateToFolder}
                  />
                </ContextMenuTrigger>
                <ContextMenuContent className="w-48">
                  <ContextMenuItem onClick={() => navigateToFolder(folder.id)}>
                    <FolderInput className="h-4 w-4 mr-2" />
                    打开
                  </ContextMenuItem>
                  <ContextMenuItem onClick={() => setRenamingFolder(folder)}>
                    <Pencil className="h-4 w-4 mr-2" />
                    重命名
                  </ContextMenuItem>
                  <ContextMenuSeparator />
                  <ContextMenuItem
                    destructive
                    onClick={() => deleteFolderMutation.mutate(folder.id)}
                  >
                    <Trash2 className="h-4 w-4 mr-2" />
                    删除文件夹
                  </ContextMenuItem>
                </ContextMenuContent>
              </ContextMenu>
            ))}

            {/* 文件列表 */}
            {allFiles.map((doc) => {
              if (doc.isLocal) {
                return (
                  <div key={doc.id}>
                    <FileItem
                      doc={doc}
                      isSelected={selectedId === doc.id}
                      onSelect={handleSelectFile}
                      onRetry={(id) => retryMutation.mutate(id)}
                    />
                  </div>
                )
              }

              return (
                <ContextMenu key={doc.id}>
                  <ContextMenuTrigger>
                    <FileItem
                      doc={doc}
                      isSelected={selectedId === doc.id}
                      onSelect={handleSelectFile}
                      onRetry={(id) => retryMutation.mutate(id)}
                    />
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
                    {doc.status !== 'processing' && (
                      <ContextMenuItem
                        onClick={() => retryMutation.mutate(doc.id)}
                      >
                        <RotateCcw className="h-4 w-4 mr-2" />
                        重新识别
                      </ContextMenuItem>
                    )}
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
        ) : (
          /* 列表视图 */
          <div className="border border-border rounded-xl overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-muted/80 border-b border-border">
                <tr>
                  <th className="text-left font-medium px-4 py-2.5 text-muted-foreground">名称</th>
                  <th className="text-left font-medium px-4 py-2.5 text-muted-foreground hidden md:table-cell">大小</th>
                  <th className="text-left font-medium px-4 py-2.5 text-muted-foreground hidden lg:table-cell">状态</th>
                  <th className="text-left font-medium px-4 py-2.5 text-muted-foreground hidden lg:table-cell">切片</th>
                  <th className="text-right font-medium px-4 py-2.5 text-muted-foreground">操作</th>
                </tr>
              </thead>
              <tbody>
                {folders.map((folder) => (
                  <tr
                    key={folder.id}
                    className={`border-b border-border/50 last:border-0 transition-colors cursor-pointer ${
                      selectedId === folder.id ? 'bg-primary/5' : 'hover:bg-muted/30'
                    }`}
                    onClick={(e) => { e.stopPropagation(); handleSelectFolder(folder.id) }}
                    onDoubleClick={(e) => { e.stopPropagation(); navigateToFolder(folder.id) }}
                  >
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <FolderInput className="h-4 w-4 text-blue-400" />
                        <span className="font-medium">{folder.name}</span>
                      </div>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground hidden md:table-cell">—</td>
                    <td className="px-4 py-2.5 text-muted-foreground hidden lg:table-cell">—</td>
                    <td className="px-4 py-2.5 text-muted-foreground hidden lg:table-cell">
                      {folder.doc_count + folder.subfolder_count} 项
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center justify-end gap-1">
                        <Button variant="ghost" size="sm" className="h-7 text-xs gap-1 cursor-pointer" onClick={(e) => { e.stopPropagation(); setRenamingFolder(folder) }}>
                          <Pencil className="h-3 w-3" />
                        </Button>
                        <Button variant="ghost" size="sm" className="h-7 text-xs text-destructive hover:text-destructive cursor-pointer" onClick={(e) => { e.stopPropagation(); deleteFolderMutation.mutate(folder.id) }}>
                          <Trash2 className="h-3 w-3" />
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
                {allFiles.map((doc) => (
                  <tr
                    key={doc.id}
                    className={`border-b border-border/50 last:border-0 transition-colors cursor-default ${
                      selectedId === doc.id ? 'bg-primary/5' : 'hover:bg-muted/30'
                    }`}
                    onClick={(e) => { e.stopPropagation(); handleSelectFile(doc.id) }}
                  >
                    <td className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <FileText className="h-4 w-4 text-muted-foreground" />
                        <span className="font-medium truncate max-w-[200px]">{doc.filename}</span>
                      </div>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground hidden md:table-cell">{formatSize(doc.file_size)}</td>
                    <td className="px-4 py-2.5 hidden lg:table-cell">
                      <span className={`text-xs px-1.5 py-0.5 rounded border ${statusColor(doc.status)}`}>
                        {statusLabel(doc.status)}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground hidden lg:table-cell">
                      {doc.status === 'completed' && doc.chunk_count > 0 ? `${doc.chunk_count} 片` : '—'}
                    </td>
                    <td className="px-4 py-2.5">
                      <div className="flex items-center justify-end gap-1">
                        {!doc.isLocal && (
                          <>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 text-xs gap-1 cursor-pointer"
                              disabled={doc.status !== 'completed'}
                              onClick={(e) => { e.stopPropagation(); setViewingChunks(doc.id) }}
                            >
                              <Eye className="h-3 w-3" />
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 text-xs text-destructive hover:text-destructive cursor-pointer"
                              onClick={(e) => { e.stopPropagation(); deleteMutation.mutate(doc.id) }}
                            >
                              <Trash2 className="h-3 w-3" />
                            </Button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 新建文件夹对话框 */}
      <NewFolderDialog
        open={showNewFolder}
        onOpenChange={setShowNewFolder}
        onConfirm={(name) => createFolderMutation.mutate(name)}
        isLoading={createFolderMutation.isPending}
      />

      {/* 重命名对话框 */}
      <RenameDialog
        open={!!renamingFolder}
        onOpenChange={(open) => { if (!open) setRenamingFolder(null) }}
        currentName={renamingFolder?.name || ''}
        onConfirm={(newName) => {
          if (renamingFolder) {
            renameFolderMutation.mutate({ folderId: renamingFolder.id, name: newName })
          }
        }}
        isLoading={renameFolderMutation.isPending}
      />

      {/* 切片查看器 */}
      <ChunkViewer documentId={viewingChunks} onClose={() => setViewingChunks(null)} />

      {/* 文件夹上传确认对话框 */}
      <Dialog open={folderUploadDialog} onOpenChange={(open) => { if (!open) { setFolderUploadDialog(false); setFolderValidation(null) } }}>
        <DialogContent className="max-w-lg max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle>上传文件夹</DialogTitle>
            <DialogDescription>
              确认要上传的文件夹内容
            </DialogDescription>
          </DialogHeader>

          {folderValidation && (
            <div className="flex-1 min-h-0 overflow-auto space-y-4 py-2">
              {/* 文件夹结构 */}
              {folderValidation.folders.length > 0 && (
                <div>
                  <p className="text-sm font-medium mb-1.5">
                    将创建 {folderValidation.folders.length} 个文件夹
                  </p>
                  <div className="bg-muted/50 rounded-lg p-3 max-h-28 overflow-auto">
                    {folderValidation.folders.map((f) => (
                      <div key={f} className="text-xs text-muted-foreground flex items-center gap-1.5 py-0.5">
                        <FolderInput className="h-3 w-3 text-blue-400 shrink-0" />
                        {f}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 支持的文件 */}
              <div>
                <p className="text-sm font-medium mb-1.5 text-green-600">
                  ✓ 支持的文件（{folderValidation.supported.length} 个）
                </p>
                {folderValidation.supported.length > 0 && (
                  <div className="bg-green-50 dark:bg-green-950/20 rounded-lg p-3 max-h-36 overflow-auto">
                    {folderValidation.supported.map((f) => (
                      <div key={f.relative_path} className="text-xs text-muted-foreground flex items-center gap-1.5 py-0.5">
                        <FileText className="h-3 w-3 shrink-0" />
                        <span className="truncate">{f.relative_path}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* 不支持的文件 */}
              {folderValidation.unsupported.length > 0 && (
                <div>
                  <p className="text-sm font-medium mb-1.5 text-amber-600">
                    ✗ 不支持的文件（{folderValidation.unsupported.length} 个，将跳过）
                  </p>
                  <div className="bg-amber-50 dark:bg-amber-950/20 rounded-lg p-3 max-h-36 overflow-auto">
                    {folderValidation.unsupported.map((f) => (
                      <div key={f.relative_path} className="text-xs text-muted-foreground flex items-center gap-1.5 py-0.5">
                        <FileText className="h-3 w-3 shrink-0 text-amber-500" />
                        <span className="truncate">{f.relative_path}</span>
                        <span className="text-amber-500 shrink-0">({f.file_type})</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}

          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => { setFolderUploadDialog(false); setFolderValidation(null) }}
              disabled={folderUploading}
            >
              取消
            </Button>
            <Button
              onClick={handleFolderUploadConfirm}
              disabled={folderUploading || !folderValidation || folderValidation.supported.length === 0}
              className="gap-1.5"
            >
              {folderUploading ? (
                <>
                  <div className="h-3.5 w-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" />
                  上传中...
                </>
              ) : (
                <>
                  <Upload className="h-4 w-4" />
                  确认上传 {folderValidation?.supported.length || 0} 个文件
                </>
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default Documents
