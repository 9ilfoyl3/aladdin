import { useState } from 'react'
import { ChevronDown, ChevronUp, Bot, FileText, Loader2, CheckCircle2, XCircle } from 'lucide-react'
import { Streamdown } from 'streamdown'
import { cjk } from '@streamdown/cjk'
import { Badge } from '@/components/ui/badge'

// 内容段落类型：思考、回答、工具调用、工具结果按 SSE 顺序交错排列
export interface ContentSegment {
  type: 'thought' | 'answer' | 'tool_call' | 'tool_result'
  content: string  // thought/answer: 文本内容; tool_call/tool_result: 工具名
  toolCallId?: string
  toolName?: string
  success?: boolean
  durationMs?: number
}

// 消息类型
export interface Message {
  role: 'user' | 'assistant'
  content: string
  references?: Reference[]
  agentSteps?: AgentStep[]
  // 新格式：交错流式段落
  segments?: ContentSegment[]
  // 旧格式兼容
  thoughts?: string[]
  toolCalls?: ToolCallStatus[]
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
      <div className="flex justify-end animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
        <div className="max-w-[75%]">
          <div className="rounded-2xl rounded-br-md bg-primary text-primary-foreground px-4 py-3 text-sm leading-relaxed shadow-sm">
            <p className="whitespace-pre-wrap">{msg.content}</p>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex gap-3 items-start animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
      <div className="w-8 h-8 rounded-full bg-primary/10 flex items-center justify-center shrink-0 mt-1">
        <Bot className="h-4 w-4 text-primary" />
      </div>
      <div className="flex-1 min-w-0 space-y-2">
        {/* 新格式：交错流式段落渲染 */}
        {msg.segments && msg.segments.length > 0 ? (
          <AgentStreamContent
            segments={msg.segments}
            isStreaming={isStreaming}
            isLast={isLast}
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
      </div>
    </div>
  )
}

// 新格式：交错流式段落渲染（思考 + 回答 + 工具调用按 SSE 顺序排列）
function AgentStreamContent({
  segments,
  isStreaming,
  isLast,
}: {
  segments: ContentSegment[]
  isStreaming: boolean
  isLast: boolean
}) {
  // 判断某个 segment 是否是最后一个内容段（用于动画控制）
  const lastContentIdx = segments.length - 1

  return (
    <div className="px-4 py-3 space-y-2">
      {segments.map((seg, i) => {
        const isLastSegment = i === lastContentIdx && isStreaming && isLast

        if (seg.type === 'thought') {
          return (
            <div key={i} className="text-sm leading-relaxed text-muted-foreground/70">
              <div className="prose prose-sm max-w-none dark:prose-invert [&>p]:mb-1 [&>p:last-child]:mb-0 text-muted-foreground/70 **:text-muted-foreground/70">
                <Streamdown
                  plugins={{ cjk: cjk }}
                  isAnimating={isLastSegment}
                >
                  {seg.content}
                </Streamdown>
              </div>
            </div>
          )
        }

        if (seg.type === 'answer') {
          return (
            <div key={i} className="text-sm leading-relaxed">
              <div className="prose prose-sm max-w-none dark:prose-invert [&>p]:mb-2 [&>p:last-child]:mb-0">
                <Streamdown
                  plugins={{ cjk: cjk }}
                  isAnimating={isLastSegment}
                >
                  {seg.content}
                </Streamdown>
              </div>
            </div>
          )
        }

        if (seg.type === 'tool_call' || seg.type === 'tool_result') {
          return (
            <div
              key={i}
              className="flex items-center gap-2 text-xs text-muted-foreground py-1 px-2 rounded-lg bg-muted/20"
            >
              {seg.type === 'tool_call' && !seg.success && seg.durationMs === undefined && (
                <Loader2 className="h-3 w-3 animate-spin text-primary/70 shrink-0" />
              )}
              {seg.success === true && (
                <CheckCircle2 className="h-3 w-3 text-green-500 shrink-0" />
              )}
              {seg.success === false && (
                <XCircle className="h-3 w-3 text-red-500 shrink-0" />
              )}
              <span className="font-mono text-[11px]">{seg.toolName || seg.content}</span>
              {seg.durationMs !== undefined && (
                <span className="text-[10px] text-muted-foreground/70 ml-auto">
                  {seg.durationMs}ms
                </span>
              )}
            </div>
          )
        }

        return null
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

export default MessageBubble
