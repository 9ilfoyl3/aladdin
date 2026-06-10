import { useEffect, useState } from 'react'
import { X, Download, FileText } from 'lucide-react'
import { cn } from '@/lib/utils'
import { documentApi, sessionFileApi } from '@/lib/api'
import { useArtifactStore, type ArtifactTarget } from '@/stores/artifactStore'
import PdfPreview from './previews/PdfPreview'

/**
 * Artifact 公用预览面板。
 *
 * 设计要点（满足需求）：
 * - 从右侧滑入，作为 flex 兄弟节点「占用空间」（非浮层）：宽度 0 ↔ 固定宽度过渡，
 *   主内容区被自然挤压，配合 transition 形成流畅推拉动画。
 * - 承载不同类型的预览器（registry 按 fileType 分发），当前支持 PDF，后续可扩展
 *   docx / xlsx / image 等，只需在 renderPreview 增加分支。
 * - 原件字节由本组件统一按来源（document / session-file）拉取为 blob objectURL，
 *   并在切换 / 卸载时 revoke，避免内存泄漏。数据流单向：store(target) → fetch → previewer。
 */

const PANEL_WIDTH = 'w-[clamp(420px,42vw,860px)]'

function ArtifactPanel() {
  const { open, target, closeArtifact } = useArtifactStore()

  // 关闭动画期间保留内容，动画结束后再清空，避免内容在滑出过程中闪烁/塌缩。
  const [mountedTarget, setMountedTarget] = useState<ArtifactTarget | null>(target)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  // 同步 target：打开或切换文件时立即更新挂载内容
  useEffect(() => {
    if (target) setMountedTarget(target)
  }, [target])

  // 拉取原件 blob，按来源选择接口；切换/卸载时 revoke 旧 URL
  useEffect(() => {
    if (!open || !target) return
    let revoked = false
    let createdUrl: string | null = null
    setObjectUrl(null)
    setError(null)

    const fetchRaw =
      target.source === 'session-file' && target.sessionId
        ? sessionFileApi.rawFile(target.sessionId, target.id)
        : documentApi.rawFile(target.id)

    fetchRaw
      .then((url) => {
        if (revoked) {
          URL.revokeObjectURL(url)
          return
        }
        createdUrl = url
        setObjectUrl(url)
      })
      .catch((e) => {
        if (!revoked) setError(e instanceof Error ? e.message : '加载失败')
      })

    return () => {
      revoked = true
      if (createdUrl) URL.revokeObjectURL(createdUrl)
    }
  }, [open, target])

  // 关闭动画结束后清理挂载内容（与 transition 时长一致）
  function handleTransitionEnd() {
    if (!open) {
      setMountedTarget(null)
      setObjectUrl(null)
      setError(null)
    }
  }

  function renderPreview() {
    if (!mountedTarget) return null
    const type = mountedTarget.fileType.toLowerCase()
    switch (type) {
      case 'pdf':
        return <PdfPreview objectUrl={objectUrl} error={error} />
      default:
        return (
          <div className="h-full flex flex-col items-center justify-center gap-3 text-muted-foreground">
            <FileText className="h-10 w-10 opacity-40" />
            <p className="text-sm">暂不支持预览 .{type} 文件</p>
          </div>
        )
    }
  }

  return (
    <div
      className={cn(
        'shrink-0 h-full overflow-hidden border-l border-border bg-card',
        'transition-[width] duration-300 ease-in-out',
        open ? PANEL_WIDTH : 'w-0'
      )}
      onTransitionEnd={handleTransitionEnd}
      aria-hidden={!open}
    >
      {/* 固定宽度内层：面板收起时不被压缩内容，保证滑动平滑 */}
      <div className={cn('h-full flex flex-col', PANEL_WIDTH)}>
        {/* 头部 */}
        <div className="flex items-center gap-2 px-4 h-12 border-b border-border shrink-0">
          <FileText className="h-4 w-4 text-muted-foreground shrink-0" />
          <span className="flex-1 truncate text-sm font-medium" title={mountedTarget?.filename}>
            {mountedTarget?.filename ?? '预览'}
          </span>
          {objectUrl && mountedTarget && (
            <a
              href={objectUrl}
              download={mountedTarget.filename}
              className="h-7 w-7 rounded-md flex items-center justify-center text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
              title="下载原件"
            >
              <Download className="h-4 w-4" />
            </a>
          )}
          <button
            onClick={closeArtifact}
            className="h-7 w-7 rounded-md flex items-center justify-center text-muted-foreground hover:bg-muted hover:text-foreground transition-colors cursor-pointer"
            title="关闭预览"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* 预览内容区 */}
        <div className="flex-1 min-h-0">{renderPreview()}</div>
      </div>
    </div>
  )
}

export default ArtifactPanel
