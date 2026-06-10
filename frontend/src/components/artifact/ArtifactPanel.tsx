import { useEffect, useState } from 'react'
import { X, Download, FileText } from 'lucide-react'
import { cn } from '@/lib/utils'
import { documentApi, sessionFileApi } from '@/lib/api'
import { useArtifactStore, type ArtifactTarget } from '@/stores/artifactStore'
import PdfPreview from './previews/PdfPreview'
import ImagePreview from './previews/ImagePreview'
import TextPreview from './previews/TextPreview'
import MarkdownPreview from './previews/MarkdownPreview'
import CsvPreview from './previews/CsvPreview'

/**
 * Artifact 公用预览面板。
 *
 * 设计要点（满足需求）：
 * - 从右侧滑入，作为 flex 兄弟节点「占用空间」（非浮层）：宽度 0 ↔ 固定宽度过渡，
 *   主内容区被自然挤压，配合 transition 形成流畅推拉动画。
 * - 承载不同类型的预览器（registry 按 fileType 分发）：PDF / 图片走 blob objectURL，
 *   txt / md / csv 走纯文本内容。office 暂不支持（需重依赖）。
 * - 原件字节由本组件统一按来源（document / session-file）拉取为 blob，按类别派生
 *   objectURL 或文本，并在切换 / 卸载时 revoke，避免内存泄漏。
 *   数据流单向：store(target) → fetch → previewer。
 */

const PANEL_WIDTH = 'w-[clamp(420px,42vw,860px)]'

// 文本类预览（读取 blob.text()）与二进制类预览（用 objectURL）分流
const TEXT_TYPES = new Set(['txt', 'md', 'csv'])

function ArtifactPanel() {
  const { open, target, closeArtifact } = useArtifactStore()

  // 关闭动画期间保留内容，动画结束后再清空，避免内容在滑出过程中闪烁/塌缩。
  const [mountedTarget, setMountedTarget] = useState<ArtifactTarget | null>(target)
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const [textContent, setTextContent] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 同步 target：打开或切换文件时立即更新挂载内容
  useEffect(() => {
    if (target) setMountedTarget(target)
  }, [target])

  // 拉取原件，按来源选择接口、按类型派生 objectURL / 文本；切换/卸载时 revoke
  useEffect(() => {
    if (!open || !target) return
    let revoked = false
    let createdUrl: string | null = null
    setObjectUrl(null)
    setTextContent(null)
    setError(null)
    setLoading(true)

    const isText = TEXT_TYPES.has(target.fileType.toLowerCase())

    const fetchRaw =
      target.source === 'session-file' && target.sessionId
        ? sessionFileApi.rawFile(target.sessionId, target.id)
        : documentApi.rawFile(target.id)

    fetchRaw
      .then(async (url) => {
        if (revoked) {
          URL.revokeObjectURL(url)
          return
        }
        if (isText) {
          // 文本类：读取内容后即可释放 objectURL（不需要长期持有）
          try {
            const resp = await fetch(url)
            const txt = await resp.text()
            if (!revoked) setTextContent(txt)
          } finally {
            URL.revokeObjectURL(url)
          }
        } else {
          createdUrl = url
          setObjectUrl(url)
        }
      })
      .catch((e) => {
        if (!revoked) setError(e instanceof Error ? e.message : '加载失败')
      })
      .finally(() => {
        if (!revoked) setLoading(false)
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
      setTextContent(null)
      setError(null)
    }
  }

  function renderPreview() {
    if (!mountedTarget) return null
    const type = mountedTarget.fileType.toLowerCase()
    switch (type) {
      case 'pdf':
        return <PdfPreview objectUrl={objectUrl} error={error} />
      case 'jpg':
      case 'jpeg':
      case 'png':
        return <ImagePreview objectUrl={objectUrl} error={error} />
      case 'txt':
        return <TextPreview text={textContent} loading={loading} error={error} />
      case 'md':
        return <MarkdownPreview text={textContent} loading={loading} error={error} />
      case 'csv':
        return <CsvPreview text={textContent} loading={loading} error={error} />
      default:
        return (
          <div className="h-full flex flex-col items-center justify-center gap-3 text-muted-foreground">
            <FileText className="h-10 w-10 opacity-40" />
            <p className="text-sm">暂不支持预览 .{type} 文件</p>
          </div>
        )
    }
  }

  // 下载链接：文本类在拉取后已 revoke objectURL，故用接口直链兜底（带鉴权由浏览器走代理）。
  // 这里统一在有 objectUrl 时提供下载；文本类不展示 objectURL 下载按钮（避免失效链接）。
  const canDownload = !!objectUrl && !!mountedTarget

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
          {canDownload && (
            <a
              href={objectUrl!}
              download={mountedTarget!.filename}
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
