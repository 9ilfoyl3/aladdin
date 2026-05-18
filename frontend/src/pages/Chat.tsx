import { useState, useRef, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Send, ChevronDown, Bot, Database, Cpu, FileText } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import { knowledgeBaseApi, llmConfigApi } from '@/lib/api'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { Badge } from '@/components/ui/badge'

// 消息类型
interface Message {
  role: 'user' | 'assistant'
  content: string
  references?: Reference[]
  agentSteps?: AgentStep[]
}

// Agent 进度步骤
interface AgentStep {
  step: string
  detail: string
}

// 引用来源类型
interface Reference {
  doc_id: string
  chunk_id: string
  filename: string
  content: string
  child_content: string
  score: number
}

// 知识库类型
interface KnowledgeBaseItem {
  id: string
  name: string
}

// LLM 模型配置类型
interface LLMConfigItem {
  id: string
  name: string
  provider: string
  base_url: string
  model: string
  is_default: boolean
}

// 对话界面
function Chat() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [selectedKb, setSelectedKb] = useState('')
  const [selectedModel, setSelectedModel] = useState('')
  const [expandedRefs, setExpandedRefs] = useState<Set<number>>(new Set())
  const [expandedRefDetails, setExpandedRefDetails] = useState<Set<string>>(new Set())
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // 获取知识库列表
  const { data: knowledgeBases = [] } = useQuery({
    queryKey: ['knowledge-bases'],
    queryFn: () => knowledgeBaseApi.list() as Promise<KnowledgeBaseItem[]>,
  })

  // 获取 LLM 模型列表（仅对话可见模型）
  const { data: llmConfigs = [] } = useQuery({
    queryKey: ['llm-configs', 'chat-visible'],
    queryFn: () => llmConfigApi.list(true) as Promise<LLMConfigItem[]>,
  })

  // 默认选中 is_default 的模型
  useEffect(() => {
    if (llmConfigs.length > 0 && !selectedModel) {
      const defaultConfig = llmConfigs.find((c) => c.is_default)
      if (defaultConfig) {
        setSelectedModel(defaultConfig.id)
      }
    }
  }, [llmConfigs, selectedModel])

  // 平滑滚动到底部
  function scrollToBottom() {
    if (scrollContainerRef.current) {
      scrollContainerRef.current.scrollTo({
        top: scrollContainerRef.current.scrollHeight,
        behavior: 'smooth',
      })
    }
  }

  // 消息变化时滚动
  useEffect(() => {
    scrollToBottom()
  }, [messages])

  // 自动调整 textarea 高度
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
      textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 120) + 'px'
    }
  }, [input])

  // 发送消息
  async function handleSend() {
    const query = input.trim()
    if (!query || isStreaming) return

    const userMessage: Message = { role: 'user', content: query }
    setMessages((prev) => [...prev, userMessage])
    setInput('')
    setIsStreaming(true)

    const assistantMessage: Message = { role: 'assistant', content: '', references: [] }
    setMessages((prev) => [...prev, assistantMessage])

    try {
      const response = await fetch('/api/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: 'rag',
          messages: [{ role: 'user', content: query }],
          stream: true,
          knowledge_base_id: selectedKb || undefined,
          model_config_id: selectedModel || undefined,
        }),
      })

      if (!response.ok) {
        throw new Error(`请求失败: ${response.status}`)
      }

      const reader = response.body?.getReader()
      const decoder = new TextDecoder()
      let fullContent = ''
      let references: Reference[] = []
      let agentSteps: AgentStep[] = []
      let buffer = ''

      if (reader) {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          // 保留最后一个可能不完整的行
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            const data = line.slice(6).trim()
            if (data === '[DONE]') continue

            try {
              const parsed = JSON.parse(data)

              // Agent 进度事件
              if (parsed.type === 'agent_progress') {
                agentSteps = [...agentSteps, { step: parsed.step, detail: parsed.detail }]
                setMessages((prev) => {
                  const updated = [...prev]
                  updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps }
                  return updated
                })
                continue
              }

              const delta = parsed.choices?.[0]?.delta?.content
              if (delta) {
                fullContent += delta
                setMessages((prev) => {
                  const updated = [...prev]
                  updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps }
                  return updated
                })
              }
              if (parsed.references) {
                references = parsed.references
                setMessages((prev) => {
                  const updated = [...prev]
                  updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps }
                  return updated
                })
              }
            } catch {
              // 忽略解析错误
            }
          }
        }
      }

      setMessages((prev) => {
        const updated = [...prev]
        updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps }
        return updated
      })
    } catch (error) {
      const errMsg = error instanceof Error ? error.message : '请求失败'
      setMessages((prev) => {
        const updated = [...prev]
        updated[updated.length - 1] = { role: 'assistant', content: `⚠️ ${errMsg}` }
        return updated
      })
    } finally {
      setIsStreaming(false)
    }
  }

  // 在父块内容中高亮子块命中部分
  function highlightChild(parentContent: string, childContent: string) {
    if (!childContent || !parentContent.includes(childContent)) {
      return <span>{parentContent}</span>
    }
    const idx = parentContent.indexOf(childContent)
    const before = parentContent.slice(0, idx)
    const match = parentContent.slice(idx, idx + childContent.length)
    const after = parentContent.slice(idx + childContent.length)
    return (
      <>
        {before && <span>{before}</span>}
        <mark className="bg-primary/15 text-foreground font-medium rounded-sm px-0.5">{match}</mark>
        {after && <span>{after}</span>}
      </>
    )
  }

  // 切换引用展开
  function toggleRef(index: number) {
    setExpandedRefs((prev) => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  // 键盘事件
  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  // 获取当前选中的模型名称
  const selectedModelName = llmConfigs.find((c) => c.id === selectedModel)?.name || ''

  return (
    <div className="relative h-[calc(100vh-3rem)] flex flex-col">
      {/* 消息列表 */}
      <div
        ref={scrollContainerRef}
        className="flex-1 overflow-auto pb-36"
      >
        {messages.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-center">
            <div className="w-14 h-14 rounded-2xl bg-muted/50 flex items-center justify-center mb-4">
              <Bot className="h-7 w-7 text-muted-foreground/50" />
            </div>
            <p className="text-muted-foreground">开始对话，向知识库提问</p>
          </div>
        ) : (
          <div className="max-w-3xl mx-auto py-6 px-4 space-y-5">
            {messages.map((msg, idx) => (
              <div
                key={idx}
                className={`animate-in fade-in-0 slide-in-from-bottom-2 duration-300 ${msg.role === 'user' ? 'flex justify-end' : ''}`}
              >
                {msg.role === 'user' ? (
                  /* 用户消息 */
                  <div className="max-w-[75%]">
                    <div className="rounded-2xl rounded-br-md bg-primary text-primary-foreground px-4 py-3 text-sm leading-relaxed">
                      <p className="whitespace-pre-wrap">{msg.content}</p>
                    </div>
                  </div>
                ) : (
                  /* AI 消息 */
                  <div className="flex gap-3 items-start">
                    <div className="w-8 h-8 rounded-full bg-primary/10 flex items-center justify-center shrink-0 mt-1">
                      <Bot className="h-4 w-4 text-primary" />
                    </div>
                    <div className="flex-1 min-w-0 space-y-2">
                      {/* 思考过程气泡 */}
                      {msg.agentSteps && msg.agentSteps.length > 0 && (
                        <div className="rounded-2xl rounded-bl-md bg-muted/40 border border-border/50 overflow-hidden">
                          {/* 思考中：展开显示步骤 */}
                          {!msg.content && (
                            <div className="px-4 py-3 space-y-2">
                              {msg.agentSteps.map((step, stepIdx) => {
                                const isLatest = stepIdx === msg.agentSteps!.length - 1
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

                          {/* 思考完成：可折叠 */}
                          {msg.content && (
                            <>
                              <button
                                onClick={() => toggleRef(-idx - 100)}
                                className="w-full flex items-center gap-2 px-4 py-2.5 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors cursor-pointer"
                              >
                                <Cpu className="h-3.5 w-3.5 shrink-0" />
                                <span>深度检索 · {msg.agentSteps.length} 步完成</span>
                                <ChevronDown
                                  className="h-3.5 w-3.5 ml-auto transition-transform duration-200"
                                  style={{ transform: expandedRefs.has(-idx - 100) ? 'rotate(0deg)' : 'rotate(-90deg)' }}
                                />
                              </button>
                              <div
                                className="grid transition-all duration-300 ease-in-out"
                                style={{
                                  gridTemplateRows: expandedRefs.has(-idx - 100) ? '1fr' : '0fr',
                                  opacity: expandedRefs.has(-idx - 100) ? 1 : 0,
                                }}
                              >
                                <div className="overflow-hidden">
                                  <div className="px-4 pb-3 pt-1 space-y-1.5 border-t border-border/40">
                                    {msg.agentSteps.map((step, stepIdx) => (
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
                      )}

                      {/* 回答内容气泡 */}
                      {msg.content ? (
                        <div className="px-4 py-3 text-sm leading-relaxed">
                          <div className="prose prose-sm max-w-none dark:prose-invert [&>p]:mb-2 [&>p:last-child]:mb-0">
                            <ReactMarkdown>{msg.content}</ReactMarkdown>
                          </div>
                        </div>
                      ) : (
                        (!msg.agentSteps?.length || msg.agentSteps[msg.agentSteps.length - 1]?.step === 'done') && (
                          <div className="px-4 py-3">
                            <div className="flex items-center gap-2 py-1">
                              <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_infinite]" />
                              <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_0.2s_infinite]" />
                              <span className="w-2 h-2 rounded-full bg-primary/70 animate-[bounce_1.4s_ease-in-out_0.4s_infinite]" />
                            </div>
                          </div>
                        )
                      )}

                      {/* 引用来源 */}
                      {msg.references && msg.references.length > 0 && (
                        <div className="mt-3">
                          <button
                            onClick={() => toggleRef(idx)}
                            className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground cursor-pointer transition-colors"
                          >
                            <span className="transition-transform duration-200" style={{ transform: expandedRefs.has(idx) ? 'rotate(0deg)' : 'rotate(-90deg)' }}>
                              <ChevronDown className="h-3.5 w-3.5" />
                            </span>
                            <span>{msg.references.length} 个引用来源</span>
                          </button>

                          <div
                            className="grid transition-all duration-300 ease-in-out"
                            style={{
                              gridTemplateRows: expandedRefs.has(idx) ? '1fr' : '0fr',
                              opacity: expandedRefs.has(idx) ? 1 : 0,
                            }}
                          >
                            <div className="overflow-hidden">
                              <div className="mt-2 space-y-2">
                                {msg.references.map((ref, refIdx) => {
                                  const detailKey = `${idx}-${refIdx}`
                                  const isDetailExpanded = expandedRefDetails.has(detailKey)
                                  return (
                                    <div
                                      key={refIdx}
                                      className="rounded-xl border border-border bg-card p-3.5 transition-all duration-200 hover:border-primary/20"
                                    >
                                      {/* 头部：文件名 + 分数 */}
                                      <div className="flex items-center justify-between mb-2">
                                        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                                          <FileText className="h-3 w-3 shrink-0" />
                                          <span className="truncate max-w-[220px]">{ref.filename || ref.doc_id?.slice(0, 8)}</span>
                                        </div>
                                        <Badge variant="outline" className="text-[10px] font-mono tabular-nums px-1.5 py-0">
                                          {ref.score?.toFixed(3)}
                                        </Badge>
                                      </div>

                                      {/* 内容 */}
                                      <p className="text-xs leading-relaxed text-foreground/80 line-clamp-3">
                                        {ref.child_content || ref.content}
                                      </p>

                                      {/* 展开完整上下文 */}
                                      {ref.content && ref.child_content && ref.content !== ref.child_content && (
                                        <>
                                          <button
                                            onClick={() => {
                                              setExpandedRefDetails((prev) => {
                                                const next = new Set(prev)
                                                if (next.has(detailKey)) next.delete(detailKey)
                                                else next.add(detailKey)
                                                return next
                                              })
                                            }}
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
                      )}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 底部输入区域 - 悬浮定位 */}
      <div className="absolute bottom-0 left-0 right-0 p-4 bg-gradient-to-t from-background via-background to-transparent pt-10 pointer-events-none">
        <div className="max-w-3xl mx-auto pointer-events-auto">
          <div className="rounded-2xl border border-border bg-card shadow-lg overflow-hidden">
            {/* 输入框 */}
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="输入问题，按 Enter 发送..."
              className="w-full px-4 pt-4 pb-2 text-sm bg-transparent border-none outline-none resize-none placeholder:text-muted-foreground/60 min-h-[44px] max-h-[120px]"
              rows={1}
              disabled={isStreaming}
            />

            {/* 底部工具栏 */}
            <div className="flex items-center justify-between px-3 pb-3">
              {/* 左侧：知识库选择 */}
              <div className="flex items-center gap-2">
                <Select value={selectedKb} onValueChange={setSelectedKb}>
                  <SelectTrigger className="h-7 border-none bg-muted/50 hover:bg-muted rounded-lg px-2.5 gap-1.5 text-xs text-muted-foreground shadow-none focus:ring-0">
                    <Database className="h-3 w-3 shrink-0" />
                    <SelectValue placeholder="全部知识库" />
                  </SelectTrigger>
                  <SelectContent>
                    {knowledgeBases.map((kb) => (
                      <SelectItem key={kb.id} value={kb.id}>{kb.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              {/* 右侧：模型选择 + 发送 */}
              <div className="flex items-center gap-2">
                <Select value={selectedModel} onValueChange={setSelectedModel}>
                  <SelectTrigger className="h-7 border-none bg-transparent hover:bg-muted/50 rounded-lg px-2 gap-1 text-xs text-muted-foreground shadow-none focus:ring-0">
                    <Cpu className="h-3 w-3 shrink-0" />
                    <span className="max-w-[80px] truncate">{selectedModelName || '模型'}</span>
                  </SelectTrigger>
                  <SelectContent>
                    {llmConfigs.length === 0 ? (
                      <div className="px-3 py-2 text-xs text-muted-foreground">暂无可用的对话模型，请先在模型管理中添加</div>
                    ) : (
                      llmConfigs.map((config) => (
                        <SelectItem key={config.id} value={config.id}>{config.name}</SelectItem>
                      ))
                    )}
                  </SelectContent>
                </Select>

                <button
                  onClick={handleSend}
                  disabled={!input.trim() || isStreaming}
                  className="h-8 w-8 rounded-full bg-primary text-primary-foreground flex items-center justify-center disabled:opacity-30 disabled:cursor-not-allowed hover:bg-primary/90 transition-colors cursor-pointer shrink-0"
                >
                  <Send className="h-4 w-4" />
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default Chat
