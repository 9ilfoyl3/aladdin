import { useState, useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { ChevronDown, ChevronUp, Bot, FileText, Loader2, CheckCircle2, XCircle, Lightbulb, Monitor, Sparkles, AlertTriangle, Image as ImageIcon, BookOpen, Copy, Check, ThumbsUp, ThumbsDown, RotateCcw } from 'lucide-react'
import { Streamdown } from 'streamdown'
import { cjk } from '@streamdown/cjk'
import { copyToClipboard } from '@/lib/clipboard'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from '@/components/ui/tooltip'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { isImageFilename } from '@/components/chat/SessionFileList'
import type { MessageAttachment } from '@/lib/api'

// 流式渲染动画配置：模糊渐入、按字符、放慢节奏
const STREAM_ANIMATION = {
  animation: 'blurIn',
  sep: 'word',
  duration: 300,
  stagger: 20,
  easing: 'ease-out',
} as const

// 内容段落类型：思考、回答、工具调用、工具结果按 SSE 顺序交错排列
export interface ContentSegment {
  type: 'thought' | 'answer' | 'tool_call' | 'tool_result'
  content: string  // thought/answer: 文本内容; tool_call/tool_result: 工具名
  toolCallId?: string
  toolName?: string
  // 工具调用参数（如 read_skill 的 skill_name、检索的 query），用于在步骤行展示更具体的信息
  toolArgs?: Record<string, unknown>
  success?: boolean
  durationMs?: number
}

// 消息类型
export interface Message {
  role: 'user' | 'assistant'
  content: string
  // 已落库消息的 DB ID（assistant 消息流式结束后由 message_saved 事件回填；历史消息加载时带上）。
  // 用于反馈（点赞/踩）定位；为空表示尚未落库（如流式进行中或保存失败）。
  id?: string
  // 用户对本条 AI 回答的反馈：'like' | 'dislike' | null
  feedback?: 'like' | 'dislike' | null
  // 标记本条 assistant 消息为错误结果（请求异常）：动作栏仅显示重试
  isError?: boolean
  references?: Reference[]
  agentSteps?: AgentStep[]
  // 用户消息绑定的会话文件附件（发送时从已上传文件中选取，随消息进入历史）
  attachments?: MessageAttachment[]
  // 新格式：交错流式段落
  segments?: ContentSegment[]
  // Agent 执行整体耗时（毫秒），来自 complete 事件
  totalDurationMs?: number
  // 旧格式兼容
  thoughts?: string[]
  toolCalls?: ToolCallStatus[]
  // 检索降级提示（session-file-upload Req 2.x）：区分会话文件源 / 知识库源失败。
  // 来自 SSE meta 事件的 metadata，渲染"部分来源检索失败、结果可能不完整"的分类提示。
  sessionSourceFailed?: boolean
  kbSourceFailed?: boolean
}

// 旧格式兼容
export interface AgentStep {
  step: string
  detail: string
}

// 新格式：工具调用状态
export interface ToolCallStatus {
  tool_call_id: string
  tool_name: string
  status: 'calling' | 'success' | 'failed'
  duration_ms?: number
}

export interface Reference {
  doc_id: string
  chunk_id: string
  filename: string
  content: string
  child_content: string
  score: number
}

interface MessageBubbleProps {
  message: Message
  index: number
  isStreaming: boolean
  isLast: boolean
  expandedRefs: Set<number>
  expandedRefDetails: Set<string>
  onToggleRef: (index: number) => void
  onToggleRefDetail: (key: string) => void
  /** 文件名 → 图片预览 URL（本会话内上传的图片，用于附件 chip 缩略图/放大预览） */
  imagePreviewUrls?: Record<string, string>
  /** 设置/取消反馈（点赞/踩）。仅 assistant 且已落库（有 id）时可用。 */
  onFeedback?: (message: Message, feedback: 'like' | 'dislike' | null) => void
  /** 重试本轮对话。仅最新一条 assistant 消息可用。 */
  onRetry?: () => void
  /** 本条是否为最新一条 assistant 消息（决定是否显示重试按钮）。 */
  isLastAssistant?: boolean
}

function MessageBubble({
  message: msg,
  index: idx,
  isStreaming,
  isLast,
  expandedRefs,
  expandedRefDetails,
  onToggleRef,
  onToggleRefDetail,
  imagePreviewUrls = {},
  onFeedback,
  onRetry,
  isLastAssistant = false,
}: MessageBubbleProps) {
  // 在父块内容中高亮子块命中部分
  function highlightChild(parentContent: string, childContent: string) {
    if (!childContent || !parentContent.includes(childContent)) {
      return <span>{parentContent}</span>
    }
    const i = parentContent.indexOf(childContent)
    const before = parentContent.slice(0, i)
    const match = parentContent.slice(i, i + childContent.length)
    const after = parentContent.slice(i + childContent.length)
    return (
      <>
        {before && <span>{before}</span>}
        <mark className="bg-primary/15 text-foreground font-medium rounded-sm px-0.5">{match}</mark>
        {after && <span>{after}</span>}
      </>
    )
  }

  if (msg.role === 'user') {
    return (
      <div className="flex flex-col items-end gap-1.5 animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
        {msg.attachments && msg.attachments.length > 0 && (
          <MessageAttachments attachments={msg.attachments} imagePreviewUrls={imagePreviewUrls} />
        )}
        <div className="max-w-[75%]">
          <div className="rounded-2xl rounded-br-md bg-primary text-primary-foreground px-4 py-3 text-sm leading-relaxed shadow-sm">
            <p className="whitespace-pre-wrap">{msg.content}</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="group animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
      <div className="flex-1 min-w-0 space-y-2">
        {/* 新格式：交错流式段落渲染 */}
        {msg.segments && msg.segments.length > 0 ? (
          <AgentStreamContent
            segments={msg.segments}
            isStreaming={isStreaming}
            isLast={isLast}
            totalDurationMs={msg.totalDurationMs}
          />
        ) : (
          <>
            {/* 旧格式兼容：思考过程折叠面板 */}
            {msg.thoughts && msg.thoughts.length > 0 && (
              <ThinkingPanel thoughts={msg.thoughts} />
            )}

            {/* 旧格式兼容：工具调用状态 */}
            {msg.toolCalls && msg.toolCalls.length > 0 && (
              <ToolCallsBlock toolCalls={msg.toolCalls} />
            )}

            {/* 旧格式兼容：Agent 思考过程 */}
            {msg.agentSteps && msg.agentSteps.length > 0 && (
              <AgentStepsBlock
                steps={msg.agentSteps}
                hasContent={!!msg.content}
                index={idx}
                expandedRefs={expandedRefs}
                onToggleRef={onToggleRef}
              />
            )}

            {/* 回答内容（非 segments 模式） */}
            {msg.content ? (
              <div className="px-4 py-3 text-sm leading-relaxed">
                <div className="prose prose-sm max-w-none dark:prose-invert [&>p]:mb-2 [&>p:last-child]:mb-0">
                  <Streamdown
                    plugins={{ cjk: cjk }}
                    isAnimating={isStreaming && isLast}
                    animated={STREAM_ANIMATION}
                  >
                    {msg.content}
                  </Streamdown>
                </div>
              </div>
            ) : (
              isStreaming && isLast && (
                <div className="px-4 py-3">
                  <div className="flex items-center gap-2 py-1">
                    <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_infinite]" />
                    <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_0.2s_infinite]" />
                    <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_0.4s_infinite]" />
                  </div>
                </div>
              )
            )}
          </>
        )}

        {/* 检索降级提示（session-file-upload Req 2.x）：区分会话文件源 / 知识库源失败。
            会话源与知识库源可能其一失败、其一成功，分类提示让用户知道结果可能不完整及缺失来源。 */}
        {(msg.sessionSourceFailed || msg.kbSourceFailed) && (
          <div className="mx-1 flex items-start gap-2 rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:border-amber-700/50 dark:bg-amber-950/30 dark:text-amber-400">
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 mt-0.5" />
            <span>
              {msg.sessionSourceFailed && msg.kbSourceFailed
                ? '会话文件与知识库检索均部分失败，本次回答可能不完整。'
                : msg.sessionSourceFailed
                  ? '会话文件检索失败，本次回答未纳入本会话上传文件的内容。'
                  : '知识库检索部分失败，本次回答可能遗漏部分知识库内容。'}
            </span>
          </div>
        )}

        {/* 引用来源 */}
        {msg.references && msg.references.length > 0 && (
          <ReferencesBlock
            references={msg.references}
            index={idx}
            expandedRefs={expandedRefs}
            expandedRefDetails={expandedRefDetails}
            onToggleRef={onToggleRef}
            onToggleRefDetail={onToggleRefDetail}
            highlightChild={highlightChild}
          />
        )}

        {/* 动作栏：复制 / 赞 / 踩 / 重试。流式进行中不显示；错误消息仅显示重试。 */}
        {!(isStreaming && isLast) && (msg.content || msg.isError) && (
          <MessageActions
            message={msg}
            isLastAssistant={isLastAssistant}
            onFeedback={onFeedback}
            onRetry={onRetry}
          />
        )}
      </div>
    </div>
  )
}

// 消息动作栏：复制 / 点赞 / 踩 / 重试。
// - 复制：始终可用；
// - 点赞/踩：仅 assistant 且已落库（有 id）且非错误消息时可用，互斥可取消；
// - 重试：仅最新一条 assistant 消息显示；错误消息仅显示重试。
function MessageActions({
  message,
  isLastAssistant,
  onFeedback,
  onRetry,
}: {
  message: Message
  isLastAssistant: boolean
  onFeedback?: (message: Message, feedback: 'like' | 'dislike' | null) => void
  onRetry?: () => void
}) {
  const [copied, setCopied] = useState(false)
  // 触发图标弹跳动画的计数键：变化即重播动画（复制成功 / 点赞 / 点踩 各自独立）
  const [popKey, setPopKey] = useState({ copy: 0, like: 0, dislike: 0 })
  const isError = !!message.isError
  const canFeedback = !isError && !!message.id && !!onFeedback
  const showRetry = isLastAssistant && !!onRetry

  async function handleCopy() {
    const ok = await copyToClipboard(message.content)
    if (ok) {
      setCopied(true)
      setPopKey((p) => ({ ...p, copy: p.copy + 1 }))
      setTimeout(() => setCopied(false), 1500)
    }
  }

  function toggleFeedback(next: 'like' | 'dislike') {
    if (!canFeedback) return
    // 仅在「激活」（非取消）时弹跳，取消则不弹
    if (message.feedback !== next) {
      setPopKey((p) => ({ ...p, [next]: p[next] + 1 }))
    }
    onFeedback?.(message, message.feedback === next ? null : next)
  }

  return (
    <div className="flex items-center gap-1 px-1 pt-0.5 opacity-0 transition-opacity duration-200 group-hover:opacity-100 focus-within:opacity-100">
      {/* 错误消息不展示复制/反馈，仅重试 */}
      {!isError && (
        <>
          <ActionButton label={copied ? '已复制' : '复制'} onClick={handleCopy}>
            {copied ? (
              <Check key={`copy-${popKey.copy}`} className="h-3.5 w-3.5 text-green-500 animate-icon-pop" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
          </ActionButton>
          {canFeedback && (
            <>
              <ActionButton
                label="赞"
                active={message.feedback === 'like'}
                onClick={() => toggleFeedback('like')}
              >
                <ThumbsUp
                  key={`like-${popKey.like}`}
                  className={`h-3.5 w-3.5 ${message.feedback === 'like' ? 'text-primary fill-primary/20 animate-icon-pop' : ''}`}
                />
              </ActionButton>
              <ActionButton
                label="踩"
                active={message.feedback === 'dislike'}
                onClick={() => toggleFeedback('dislike')}
              >
                <ThumbsDown
                  key={`dislike-${popKey.dislike}`}
                  className={`h-3.5 w-3.5 ${message.feedback === 'dislike' ? 'text-destructive fill-destructive/20 animate-icon-pop' : ''}`}
                />
              </ActionButton>
            </>
          )}
        </>
      )}
      {showRetry && (
        <ActionButton label="重试" onClick={onRetry}>
          <RotateCcw className="h-3.5 w-3.5 transition-transform duration-300 group-active/btn:-rotate-180" />
        </ActionButton>
      )}
    </div>
  )
}

// 动作栏单个图标按钮（带 tooltip + 选中态高亮 + 按下回弹）
function ActionButton({
  label,
  active,
  onClick,
  children,
}: {
  label: string
  active?: boolean
  onClick?: () => void
  children: ReactNode
}) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            onClick={onClick}
            className={`group/btn p-1.5 rounded-md transition-all duration-150 cursor-pointer text-muted-foreground hover:text-foreground hover:bg-muted/60 active:scale-90 ${active ? 'text-foreground' : ''}`}
          >
            {children}
          </button>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

// 新格式：交错流式段落渲染（思考 + 回答 + 工具调用按 SSE 顺序排列）
function AgentStreamContent({
  segments,
  isStreaming,
  isLast,
  totalDurationMs,
}: {
  segments: ContentSegment[]
  isStreaming: boolean
  isLast: boolean
  totalDurationMs?: number
}) {
  // 过程步骤（思考 + 工具调用）与回答分离：过程步骤汇总到顶部统计面板，回答正常渲染
  const processSegments = segments.filter(
    (s) => s.type === 'thought' || s.type === 'tool_call'
  )
  const answerSegments = segments.filter((s) => s.type === 'answer')

  return (
    <div className="px-4 py-3 space-y-3">
      {/* 步骤统计面板 */}
      {processSegments.length > 0 && (
        <StepSummaryPanel
          steps={processSegments}
          isStreaming={isStreaming}
          isLast={isLast}
          totalDurationMs={totalDurationMs}
          answerStarted={answerSegments.length > 0}
        />
      )}

      {/* 回答内容 */}
      {answerSegments.map((seg, i) => {
        const isLastSegment =
          i === answerSegments.length - 1 && isStreaming && isLast
        return (
          <div key={`ans-${i}`} className="text-sm leading-relaxed">
            <div className="prose prose-sm max-w-none dark:prose-invert [&>p]:mb-2 [&>p:last-child]:mb-0">
              <Streamdown plugins={{ cjk: cjk }} isAnimating={isLastSegment} animated={STREAM_ANIMATION}>
                {seg.content}
              </Streamdown>
            </div>
          </div>
        )
      })}

      {/* 流式中但还没有任何段落内容时显示加载动画 */}
      {segments.length === 0 && isStreaming && isLast && (
        <div className="flex items-center gap-2 py-1">
          <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_infinite]" />
          <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_0.2s_infinite]" />
          <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_0.4s_infinite]" />
        </div>
      )}
    </div>
  )
}

// 耗时格式化：>=1s 显示秒，否则显示毫秒
function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}m${s}s`
}

// 实时耗时计数：running 期间以 100ms 步进累加，结束后返回后端最终耗时
function useLiveDuration(running: boolean, finalMs?: number): number | undefined {
  const [elapsed, setElapsed] = useState(0)
  const startRef = useRef<number | null>(null)

  useEffect(() => {
    if (!running) {
      startRef.current = null
      return
    }
    if (startRef.current === null) startRef.current = Date.now()
    setElapsed(Date.now() - startRef.current)
    const timer = setInterval(() => {
      if (startRef.current !== null) setElapsed(Date.now() - startRef.current)
    }, 100)
    return () => clearInterval(timer)
  }, [running])

  // 结束后优先展示后端返回的精确耗时；running 期间展示本地累加值。
  // 停止但后端值尚未到达时（答案已开始流式但 complete 事件未到），冻结在最后的
  // 本地累加值，避免耗时短暂消失再出现的闪烁。
  if (!running && finalMs !== undefined) return finalMs
  if (running) return elapsed
  return elapsed > 0 ? elapsed : finalMs
}

// 步骤统计面板：顶部汇总（步骤数 + 整体耗时），可折叠展开各步骤
function StepSummaryPanel({
  steps,
  isStreaming,
  isLast,
  totalDurationMs,
  answerStarted,
}: {
  steps: ContentSegment[]
  isStreaming: boolean
  isLast: boolean
  totalDurationMs?: number
  answerStarted?: boolean
}) {
  const [open, setOpen] = useState(true)
  const [expandedSteps, setExpandedSteps] = useState<Set<number>>(new Set())
  // 步骤面板状态：答案一旦开始产出即视为「执行步骤」结束（与后端耗时截止时刻一致），
  // 此时停止本地实时计时，避免把答案流式输出的时间也计入步骤耗时。
  const running = isStreaming && isLast && !answerStarted
  // 实时耗时：执行中持续累加，结束后切换为后端精确值
  const displayDuration = useLiveDuration(running, totalDurationMs)

  function toggleStep(i: number) {
    setExpandedSteps((prev) => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })
  }

  return (
    <div className="rounded-xl border border-border/50 bg-muted/20 overflow-hidden">
      {/* 汇总头部 */}
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-2.5 text-sm hover:bg-muted/40 transition-colors cursor-pointer"
      >
        {running ? (
          <Sparkles className="h-4 w-4 shrink-0 text-primary animate-twinkle" />
        ) : (
          <Sparkles className="h-4 w-4 shrink-0 text-primary" />
        )}
        <span className="text-foreground/80">
          {running ? '正在执行' : '已完成'}
          <span className="text-primary font-medium mx-1">{steps.length}</span>
          个步骤
        </span>
        {displayDuration !== undefined && (
          <span className="text-muted-foreground">
            ，耗时
            <span className="text-primary font-medium ml-1">
              {formatDuration(displayDuration)}
            </span>
          </span>
        )}
        {open ? (
          <ChevronUp className="h-4 w-4 ml-auto text-muted-foreground shrink-0" />
        ) : (
          <ChevronDown className="h-4 w-4 ml-auto text-muted-foreground shrink-0" />
        )}
      </button>

      {/* 步骤列表 */}
      <div
        className="grid transition-all duration-300 ease-in-out"
        style={{
          gridTemplateRows: open ? '1fr' : '0fr',
          opacity: open ? 1 : 0,
        }}
      >
        <div className="overflow-hidden">
          <div className="px-2 pb-2 pt-1 space-y-1.5 border-t border-border/40">
            {steps.map((seg, i) => {
              const isActive = running && i === steps.length - 1
              return (
                <StepRow
                  key={i}
                  seg={seg}
                  // 流式中自动展开当前步骤以实时显示内容；完成后默认折叠
                  expanded={expandedSteps.has(i) || isActive}
                  onToggle={() => toggleStep(i)}
                  animating={isActive}
                />
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}

// 工具名 → 中文标签映射（步骤行展示用，与 AgentConfig 的 ALL_TOOLS 保持一致）
const TOOL_LABELS: Record<string, string> = {
  knowledge_search: '语义检索',
  grep_chunks: '关键词检索',
  list_knowledge_chunks: '分页浏览',
  web_search: '网页搜索',
  thinking: '内部思考',
  read_attachment: '阅读附件',
  read_skill: '加载技能',
  final_answer: '生成答案',
}

// 从工具调用参数中提取一段简短描述，用于在步骤标题后展示「调用了什么」的具体信息
function toolArgSummary(toolName?: string, args?: Record<string, unknown>): string | null {
  if (!args) return null
  if (toolName === 'read_skill') {
    const name = args.skill_name
    return typeof name === 'string' && name ? name : null
  }
  if (toolName === 'read_attachment') {
    const fn = args.filename
    return typeof fn === 'string' && fn ? fn : null
  }
  return null
}

// 单个步骤行：思考显示文本摘要，工具调用显示「调用 xxx」前缀，均可折叠
function StepRow({
  seg,
  expanded,
  onToggle,
  animating,
}: {
  seg: ContentSegment
  expanded: boolean
  onToggle: () => void
  animating: boolean
}) {
  const isTool = seg.type === 'tool_call'
  const isSkill = isTool && seg.toolName === 'read_skill'
  const toolLabel = (seg.toolName && TOOL_LABELS[seg.toolName]) || seg.toolName || seg.content
  const argSummary = toolArgSummary(seg.toolName, seg.toolArgs)

  return (
    <div className="rounded-lg border border-border/40 bg-background/40 overflow-hidden">
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-2 px-2.5 py-2 text-xs hover:bg-muted/40 transition-colors cursor-pointer text-left"
      >
        {isSkill ? (
          <BookOpen className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        ) : isTool ? (
          <Monitor className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        ) : (
          <Lightbulb className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        )}
        {isTool ? (
          <span className="truncate text-foreground/80">
            {toolLabel}
            {argSummary && (
              <span className="font-mono text-primary/80 ml-1">{argSummary}</span>
            )}
          </span>
        ) : (
          <span className="truncate text-muted-foreground">{seg.content}</span>
        )}
        <ChevronDown
          className="h-3.5 w-3.5 ml-auto text-muted-foreground/60 shrink-0 transition-transform duration-200"
          style={{ transform: expanded ? 'rotate(0deg)' : 'rotate(-90deg)' }}
        />
      </button>

      <div
        className="grid transition-all duration-200 ease-in-out"
        style={{
          gridTemplateRows: expanded ? '1fr' : '0fr',
          opacity: expanded ? 1 : 0,
        }}
      >
        <div className="overflow-hidden">
          <div className="px-2.5 pb-2.5 pt-1 border-t border-border/30">
            {isTool ? (
              <p className="text-xs text-muted-foreground">
                {isSkill ? '加载技能' : '调用工具'}{' '}
                <span className="font-mono">{argSummary || toolLabel}</span>
              </p>
            ) : (
              <div className="prose prose-sm max-w-none dark:prose-invert text-xs leading-relaxed **:text-xs [&>p]:mb-1 [&>p:last-child]:mb-0 text-muted-foreground **:text-muted-foreground">
                <Streamdown plugins={{ cjk: cjk }} isAnimating={animating} animated={STREAM_ANIMATION}>
                  {seg.content}
                </Streamdown>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

// 旧格式兼容：思考过程折叠面板（Task 10.2）
function ThinkingPanel({ thoughts }: { thoughts: string[] }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="rounded-xl border border-border/50 bg-muted/30 overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/50 transition-colors cursor-pointer"
      >
        <Bot className="h-3.5 w-3.5 shrink-0 text-primary/70" />
        <span className="font-medium">思考过程</span>
        <Badge variant="outline" className="text-[10px] px-1.5 py-0 ml-1">
          {thoughts.length}
        </Badge>
        {expanded ? (
          <ChevronUp className="h-3.5 w-3.5 ml-auto" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5 ml-auto" />
        )}
      </button>
      <div
        className="grid transition-all duration-300 ease-in-out"
        style={{
          gridTemplateRows: expanded ? '1fr' : '0fr',
          opacity: expanded ? 1 : 0,
        }}
      >
        <div className="overflow-hidden">
          <div className="px-3 pb-3 pt-1 space-y-2 border-t border-border/40">
            {thoughts.map((thought, i) => (
              <div key={i} className="flex items-start gap-2 text-xs text-muted-foreground">
                <span className="w-1.5 h-1.5 rounded-full bg-primary/40 shrink-0 mt-1.5" />
                <span className="leading-relaxed whitespace-pre-wrap">{thought}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

// 新格式：工具调用状态展示（Task 10.3）
function ToolCallsBlock({ toolCalls }: { toolCalls: ToolCallStatus[] }) {
  return (
    <div className="space-y-1.5 px-1">
      {toolCalls.map((tc) => (
        <div
          key={tc.tool_call_id}
          className="flex items-center gap-2 text-xs text-muted-foreground py-1 px-2 rounded-lg bg-muted/20"
        >
          {tc.status === 'calling' && (
            <Loader2 className="h-3 w-3 animate-spin text-primary/70 shrink-0" />
          )}
          {tc.status === 'success' && (
            <CheckCircle2 className="h-3 w-3 text-green-500 shrink-0" />
          )}
          {tc.status === 'failed' && (
            <XCircle className="h-3 w-3 text-red-500 shrink-0" />
          )}
          <span className="font-mono text-[11px]">{tc.tool_name}</span>
          {tc.duration_ms !== undefined && (
            <span className="text-[10px] text-muted-foreground/70 ml-auto">
              {tc.duration_ms}ms
            </span>
          )}
        </div>
      ))}
    </div>
  )
}

// 旧格式兼容：Agent 思考步骤子组件
function AgentStepsBlock({
  steps,
  hasContent,
  index,
  expandedRefs,
  onToggleRef,
}: {
  steps: AgentStep[]
  hasContent: boolean
  index: number
  expandedRefs: Set<number>
  onToggleRef: (index: number) => void
}) {
  const refKey = -index - 100

  return (
    <div className="rounded-2xl rounded-bl-md bg-muted/40 border border-border/50 overflow-hidden">
      {!hasContent && (
        <div className="px-4 py-3 space-y-2">
          {steps.map((step, stepIdx) => {
            const isLatest = stepIdx === steps.length - 1
            return (
              <div key={stepIdx} className={`flex items-start gap-2 text-sm ${isLatest ? 'text-primary font-medium' : 'text-muted-foreground'}`}>
                {isLatest ? (
                  <span className="w-2 h-2 rounded-full bg-primary animate-pulse shrink-0 mt-1.5" />
                ) : (
                  <span className="w-2 h-2 rounded-full bg-primary/30 shrink-0 mt-1.5" />
                )}
                <span className="leading-relaxed">{step.detail}</span>
              </div>
            )
          })}
        </div>
      )}

      {hasContent && (
        <>
          <button
            onClick={() => onToggleRef(refKey)}
            className="w-full flex items-center gap-2 px-4 py-2.5 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors cursor-pointer"
          >
            <Bot className="h-3.5 w-3.5 shrink-0" />
            <span>深度检索 · {steps.length} 步完成</span>
            <ChevronDown
              className="h-3.5 w-3.5 ml-auto transition-transform duration-200"
              style={{ transform: expandedRefs.has(refKey) ? 'rotate(0deg)' : 'rotate(-90deg)' }}
            />
          </button>
          <div
            className="grid transition-all duration-300 ease-in-out"
            style={{
              gridTemplateRows: expandedRefs.has(refKey) ? '1fr' : '0fr',
              opacity: expandedRefs.has(refKey) ? 1 : 0,
            }}
          >
            <div className="overflow-hidden">
              <div className="px-4 pb-3 pt-1 space-y-1.5 border-t border-border/40">
                {steps.map((step, stepIdx) => (
                  <div key={stepIdx} className="flex items-start gap-2 text-xs text-muted-foreground">
                    <span className="w-1.5 h-1.5 rounded-full bg-primary/30 shrink-0 mt-1.5" />
                    <span className="leading-relaxed">{step.detail}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

// 引用来源子组件
function ReferencesBlock({
  references,
  index,
  expandedRefs,
  expandedRefDetails,
  onToggleRef,
  onToggleRefDetail,
  highlightChild,
}: {
  references: Reference[]
  index: number
  expandedRefs: Set<number>
  expandedRefDetails: Set<string>
  onToggleRef: (index: number) => void
  onToggleRefDetail: (key: string) => void
  highlightChild: (parent: string, child: string) => React.ReactNode
}) {
  return (
    <div className="mt-3">
      <button
        onClick={() => onToggleRef(index)}
        className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
      >
        <span className="transition-transform duration-200" style={{ transform: expandedRefs.has(index) ? 'rotate(0deg)' : 'rotate(-90deg)' }}>
          <ChevronDown className="h-3.5 w-3.5" />
        </span>
        <span>{references.length} 个引用来源</span>
      </button>

      <div
        className="grid transition-all duration-300 ease-in-out"
        style={{
          gridTemplateRows: expandedRefs.has(index) ? '1fr' : '0fr',
          opacity: expandedRefs.has(index) ? 1 : 0,
        }}
      >
        <div className="overflow-hidden">
          <div className="mt-2 space-y-2">
            {references.map((ref, refIdx) => {
              const detailKey = `${index}-${refIdx}`
              const isDetailExpanded = expandedRefDetails.has(detailKey)
              return (
                <div
                  key={refIdx}
                  className="rounded-xl border border-border bg-card p-3.5 transition-all duration-200 hover:border-primary/20 hover:shadow-sm"
                >
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      <FileText className="h-3 w-3 shrink-0" />
                      <span className="truncate max-w-[220px]">{ref.filename || ref.doc_id?.slice(0, 8)}</span>
                    </div>
                    <Badge variant="outline" className="text-[10px] font-mono tabular-nums px-1.5 py-0">
                      {ref.score?.toFixed(3)}
                    </Badge>
                  </div>

                  <p className="text-xs leading-relaxed text-foreground/80 line-clamp-3">
                    {ref.child_content || ref.content}
                  </p>

                  {ref.content && ref.child_content && ref.content !== ref.child_content && (
                    <>
                      <button
                        onClick={() => onToggleRefDetail(detailKey)}
                        className="mt-2 flex items-center gap-1 text-[11px] text-primary/70 hover:text-primary cursor-pointer transition-colors"
                      >
                        <span className="transition-transform duration-200" style={{ transform: isDetailExpanded ? 'rotate(0deg)' : 'rotate(-90deg)' }}>
                          <ChevronDown className="h-3 w-3" />
                        </span>
                        {isDetailExpanded ? '收起上下文' : '查看完整上下文'}
                      </button>
                      <div
                        className="grid transition-all duration-200 ease-in-out"
                        style={{
                          gridTemplateRows: isDetailExpanded ? '1fr' : '0fr',
                          opacity: isDetailExpanded ? 1 : 0,
                        }}
                      >
                        <div className="overflow-hidden">
                          <div className="mt-2 pt-2 border-t border-border/60">
                            <p className="text-[11px] leading-relaxed text-muted-foreground whitespace-pre-wrap">
                              {highlightChild(ref.content, ref.child_content)}
                            </p>
                          </div>
                        </div>
                      </div>
                    </>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}

// 用户消息附件 chip 行（发送时绑定的会话文件）。展示在用户气泡上方、右对齐。
// 图片附件若本会话内有预览 URL（客户端 blob）则内联缩略图 + 点击放大；
// 历史回放（刷新后）无 blob，退化为文件图标 + 文件名，悬浮看全称。
function MessageAttachments({
  attachments,
  imagePreviewUrls,
}: {
  attachments: MessageAttachment[]
  imagePreviewUrls: Record<string, string>
}) {
  const [preview, setPreview] = useState<{ url: string; name: string } | null>(null)

  function formatSize(bytes?: number | null): string {
    if (!bytes || bytes <= 0) return ''
    if (bytes < 1024) return `${bytes}B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`
    return `${(bytes / 1024 / 1024).toFixed(1)}MB`
  }

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex flex-wrap justify-end gap-1.5 max-w-[75%]">
        {attachments.map((a) => {
          const isImg = isImageFilename(a.filename)
          const previewUrl = isImg ? imagePreviewUrls[a.filename] : undefined
          const sz = formatSize(a.file_size)
          return (
            <Tooltip key={a.file_id}>
              <TooltipTrigger asChild>
                <div className="inline-flex items-center gap-1.5 h-8 pl-1.5 pr-2 rounded-xl border border-border bg-card text-xs text-foreground max-w-[15em] transition-colors hover:border-primary/40">
                  {previewUrl ? (
                    <button
                      type="button"
                      onClick={() => setPreview({ url: previewUrl, name: a.filename })}
                      className="h-6 w-6 shrink-0 rounded-md overflow-hidden ring-1 ring-border hover:ring-primary/50 transition-all cursor-zoom-in"
                      aria-label={`预览图片 ${a.filename}`}
                    >
                      <img src={previewUrl} alt="" className="h-full w-full object-cover" />
                    </button>
                  ) : (
                    <span className="h-6 w-6 shrink-0 flex items-center justify-center">
                      {isImg ? (
                        <ImageIcon className="h-3.5 w-3.5 text-muted-foreground" />
                      ) : (
                        <FileText className="h-3.5 w-3.5 text-muted-foreground" />
                      )}
                    </span>
                  )}
                  <span className="truncate font-medium">{a.filename}</span>
                </div>
              </TooltipTrigger>
              <TooltipContent side="top" className="max-w-xs p-2">
                {previewUrl && (
                  <img src={previewUrl} alt="" className="mb-1.5 max-h-40 w-auto rounded-md object-contain" />
                )}
                <div className="font-medium break-all leading-snug">{a.filename}</div>
                {sz && <div className="mt-0.5 text-xs text-muted-foreground">{sz}</div>}
              </TooltipContent>
            </Tooltip>
          )
        })}
      </div>

      <Dialog open={!!preview} onOpenChange={(open) => !open && setPreview(null)}>
        <DialogContent className="max-w-3xl p-3 bg-background">
          <DialogTitle className="sr-only">{preview?.name ?? '图片预览'}</DialogTitle>
          {preview && (
            <div className="flex flex-col gap-2">
              <img src={preview.url} alt={preview.name} className="w-full max-h-[78vh] rounded-md object-contain" />
              <p className="text-center text-xs text-muted-foreground break-all">{preview.name}</p>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </TooltipProvider>
  )
}

export default MessageBubble