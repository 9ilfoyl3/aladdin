import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, PanelLeftClose, PanelLeft } from 'lucide-react'
import { knowledgeBaseApi, llmConfigApi, sessionApi } from '@/lib/api'
import type { SessionItem } from '@/lib/api'
import { Button } from '@/components/ui/button'
import SessionSidebar from '@/components/chat/SessionSidebar'
import MessageBubble from '@/components/chat/MessageBubble'
import type { Message, Reference, ContentSegment } from '@/components/chat/MessageBubble'
import ChatInput from '@/components/chat/ChatInput'

interface KnowledgeBaseItem {
  id: string
  name: string
}

interface LLMConfigItem {
  id: string
  name: string
  provider: string
  base_url: string
  model: string
  is_default: boolean
}

function Chat() {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [selectedKb, setSelectedKb] = useState('')
  const [selectedModel, setSelectedModel] = useState('')
  const [retrievalMode, setRetrievalMode] = useState('auto')
  const [auxiliaryKbIds, setAuxiliaryKbIds] = useState<string[]>([])
  const [expandedRefs, setExpandedRefs] = useState<Set<number>>(new Set())
  const [expandedRefDetails, setExpandedRefDetails] = useState<Set<string>>(new Set())
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const queryClient = useQueryClient()

  // 数据查询
  const { data: sessions = [] } = useQuery({
    queryKey: ['sessions'],
    queryFn: () => sessionApi.list(),
  })

  const { data: knowledgeBases = [] } = useQuery({
    queryKey: ['knowledge-bases'],
    queryFn: () => knowledgeBaseApi.list() as Promise<KnowledgeBaseItem[]>,
  })

  const { data: llmConfigs = [] } = useQuery({
    queryKey: ['llm-configs', 'chat-visible'],
    queryFn: () => llmConfigApi.list(true) as Promise<LLMConfigItem[]>,
  })

  // 默认选中 is_default 的模型
  useEffect(() => {
    if (llmConfigs.length > 0 && !selectedModel) {
      const defaultConfig = llmConfigs.find((c) => c.is_default)
      if (defaultConfig) setSelectedModel(defaultConfig.id)
    }
  }, [llmConfigs, selectedModel])

  // 滚动到底部
  useEffect(() => {
    if (scrollContainerRef.current) {
      scrollContainerRef.current.scrollTo({
        top: scrollContainerRef.current.scrollHeight,
        behavior: 'smooth',
      })
    }
  }, [messages])

  // 会话操作
  const handleNewSession = useCallback(async () => {
    setCurrentSessionId(null)
    setMessages([])
    setExpandedRefs(new Set())
    setExpandedRefDetails(new Set())
  }, [])

  const handleSwitchSession = useCallback(async (sessionId: string) => {
    if (sessionId === currentSessionId) return
    setCurrentSessionId(sessionId)
    setExpandedRefs(new Set())
    setExpandedRefDetails(new Set())
    try {
      const msgs = await sessionApi.getMessages(sessionId)
      setMessages(msgs.map((m) => {
        const base: Message = {
          role: m.role as 'user' | 'assistant',
          content: m.content,
          references: (m.references as Reference[]) || undefined,
        }
        // 解析 agent_steps：区分新格式（有 type 字段）和旧格式（有 step/detail 字段）
        if (m.agent_steps && Array.isArray(m.agent_steps)) {
          const hasNewFormat = m.agent_steps.some((s: Record<string, unknown>) => s.type)
          if (hasNewFormat) {
            // 新格式：构建 segments 数组（按存储顺序还原交错段落）
            const segments: ContentSegment[] = []
            for (const step of m.agent_steps) {
              if (step.type === 'thought' && step.content) {
                // 合并连续的 thought 段落
                const lastSeg = segments[segments.length - 1]
                if (lastSeg && lastSeg.type === 'thought') {
                  lastSeg.content += String(step.content)
                } else {
                  segments.push({ type: 'thought', content: String(step.content) })
                }
              } else if (step.type === 'tool_call') {
                segments.push({
                  type: 'tool_call',
                  content: String(step.tool_name || ''),
                  toolCallId: String(step.tool_call_id || ''),
                  toolName: String(step.tool_name || ''),
                  success: undefined,
                })
              } else if (step.type === 'tool_result') {
                // 更新匹配的 tool_call 段落
                const existing = segments.find(
                  (seg) => seg.type === 'tool_call' && seg.toolCallId === step.tool_call_id
                )
                if (existing) {
                  existing.success = step.success as boolean
                  existing.durationMs = step.duration_ms as number | undefined
                }
              } else if (step.type === 'final_answer' && step.content) {
                // 合并连续的 answer 段落
                const lastSeg = segments[segments.length - 1]
                if (lastSeg && lastSeg.type === 'answer') {
                  lastSeg.content += String(step.content)
                } else {
                  segments.push({ type: 'answer', content: String(step.content) })
                }
              }
            }
            // 如果没有从 steps 中还原出 answer 段落，但有 content，补一个
            if (base.content && !segments.some((s) => s.type === 'answer')) {
              segments.push({ type: 'answer', content: base.content })
            }
            if (segments.length > 0) base.segments = segments
          } else {
            // 旧格式：直接作为 agentSteps
            base.agentSteps = m.agent_steps
          }
        }
        return base
      }))
    } catch (e) {
      console.error('加载会话消息失败', e)
      setMessages([])
    }
  }, [currentSessionId])

  const handleDeleteSession = useCallback(async (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await sessionApi.delete(sessionId)
      if (currentSessionId === sessionId) {
        setCurrentSessionId(null)
        setMessages([])
      }
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
    } catch (err) {
      console.error('删除会话失败', err)
    }
  }, [currentSessionId, queryClient])

  // 发送消息
  async function handleSend() {
    const query = input.trim()
    if (!query || isStreaming) return

    let sessionId = currentSessionId
    if (!sessionId) {
      try {
        const session = await sessionApi.create({ title: '新对话' })
        sessionId = session.id
        setCurrentSessionId(session.id)
        queryClient.invalidateQueries({ queryKey: ['sessions'] })
      } catch (e) {
        console.error('自动创建会话失败', e)
      }
    }

    const userMessage: Message = { role: 'user', content: query }
    setMessages((prev) => [...prev, userMessage])
    setInput('')
    setIsStreaming(true)

    const assistantMessage: Message = { role: 'assistant', content: '', references: [] }
    setMessages((prev) => [...prev, assistantMessage])

    try {
      let kbIds: string[] | undefined = undefined
      if (selectedKb && auxiliaryKbIds.length > 0) {
        kbIds = [selectedKb, ...auxiliaryKbIds.filter((id) => id !== selectedKb)]
      } else if (!selectedKb && auxiliaryKbIds.length > 0) {
        kbIds = auxiliaryKbIds
      }

      const response = await fetch('/api/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: 'rag',
          messages: [{ role: 'user', content: query }],
          stream: true,
          knowledge_base_id: selectedKb || undefined,
          model_config_id: selectedModel || undefined,
          retrieval_mode: retrievalMode === 'auto' ? undefined : retrievalMode,
          kb_ids: kbIds,
          session_id: sessionId || undefined,
        }),
      })

      if (!response.ok) throw new Error(`请求失败: ${response.status}`)

      const reader = response.body?.getReader()
      const decoder = new TextDecoder()
      let fullContent = ''
      let references: Reference[] = []
      let agentSteps: Message['agentSteps'] = []
      let segments: ContentSegment[] = []
      let buffer = ''
      let isAgentMode = false

      if (reader) {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            const data = line.slice(6).trim()
            if (data === '[DONE]') continue

            try {
              const parsed = JSON.parse(data)

              // Task 10.1: 检测新 Agent SSE 事件格式（有 type 字段）
              if (parsed.type) {
                isAgentMode = true

                switch (parsed.type) {
                  // 思考过程：追加到最后一个 thought 段落或新建
                  case 'thought': {
                    if (parsed.content) {
                      const lastSeg = segments[segments.length - 1]
                      if (lastSeg && lastSeg.type === 'thought') {
                        segments = [...segments.slice(0, -1), { ...lastSeg, content: lastSeg.content + parsed.content }]
                      } else {
                        segments = [...segments, { type: 'thought', content: parsed.content }]
                      }
                    }
                    setMessages((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }

                  // 工具调用开始：插入 tool_call 段落
                  case 'tool_call': {
                    segments = [...segments, {
                      type: 'tool_call',
                      content: parsed.tool_name || '',
                      toolCallId: parsed.tool_call_id,
                      toolName: parsed.tool_name,
                    }]
                    setMessages((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }

                  // 工具调用结果：更新匹配的 tool_call 段落
                  case 'tool_result': {
                    segments = segments.map((seg) =>
                      seg.type === 'tool_call' && seg.toolCallId === parsed.tool_call_id
                        ? {
                            ...seg,
                            success: parsed.success,
                            durationMs: parsed.duration_ms,
                          }
                        : seg
                    )
                    setMessages((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }

                  // 最终答案流式渲染：追加到最后一个 answer 段落或新建
                  case 'final_answer': {
                    if (parsed.content) {
                      fullContent += parsed.content
                      const lastSeg = segments[segments.length - 1]
                      if (lastSeg && lastSeg.type === 'answer') {
                        segments = [...segments.slice(0, -1), { ...lastSeg, content: lastSeg.content + parsed.content }]
                      } else {
                        segments = [...segments, { type: 'answer', content: parsed.content }]
                      }
                    } else if (parsed.done) {
                      // done=true 且无 content：natural_stop 场景
                      // 把最后一个 thought 段落转为 answer（内容已流式发射为 thought）
                      const lastSeg = segments[segments.length - 1]
                      if (lastSeg && lastSeg.type === 'thought') {
                        segments = [...segments.slice(0, -1), { ...lastSeg, type: 'answer' }]
                        fullContent = lastSeg.content
                      }
                    }
                    setMessages((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }

                  // 引用来源
                  case 'references': {
                    if (parsed.references && Array.isArray(parsed.references)) {
                      references = parsed.references
                    }
                    setMessages((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }

                  // 执行完成
                  case 'complete': {
                    break
                  }

                  // 错误事件
                  case 'error': {
                    fullContent = `⚠️ ${parsed.content || '执行出错'}`
                    segments = [...segments, { type: 'answer', content: fullContent }]
                    setMessages((prev) => {
                      const updated = [...prev]
                      updated[updated.length - 1] = {
                        role: 'assistant',
                        content: fullContent,
                        references,
                        segments,
                      }
                      return updated
                    })
                    break
                  }
                }
                continue
              }

              // Agent 模式下的 references 事件（无 type 字段，直接包含 references 数组）
              if (isAgentMode && parsed.references && Array.isArray(parsed.references)) {
                references = parsed.references
                setMessages((prev) => {
                  const updated = [...prev]
                  updated[updated.length - 1] = {
                    role: 'assistant',
                    content: fullContent,
                    references,
                    segments,
                  }
                  return updated
                })
                continue
              }

              // 旧格式兼容：agent_progress 事件（非 agent 模式下保留）
              if (parsed.type === 'agent_progress') {
                agentSteps = [...(agentSteps || []), { step: parsed.step, detail: parsed.detail }]
                setMessages((prev) => {
                  const updated = [...prev]
                  updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps }
                  return updated
                })
                continue
              }

              // 非 agent 模式：ChatCompletionChunk 格式（direct/hybrid 模式）
              if (!isAgentMode) {
                const delta = parsed.choices?.[0]?.delta?.content
                if (delta) {
                  fullContent += delta
                  setMessages((prev) => {
                    const updated = [...prev]
                    updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps }
                    return updated
                  })
                }
                // 非 agent 模式下的 references（旧格式，直接在 JSON 中）
                if (parsed.references) {
                  references = parsed.references
                  setMessages((prev) => {
                    const updated = [...prev]
                    updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps }
                    return updated
                  })
                }
              }
            } catch { /* 忽略解析错误 */ }
          }
        }
      }

      // 最终状态更新
      if (isAgentMode) {
        setMessages((prev) => {
          const updated = [...prev]
          updated[updated.length - 1] = {
            role: 'assistant',
            content: fullContent,
            references,
            segments,
          }
          return updated
        })
      } else {
        setMessages((prev) => {
          const updated = [...prev]
          updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps }
          return updated
        })
      }
    } catch (error) {
      const errMsg = error instanceof Error ? error.message : '请求失败'
      setMessages((prev) => {
        const updated = [...prev]
        updated[updated.length - 1] = { role: 'assistant', content: `⚠️ ${errMsg}` }
        return updated
      })
    } finally {
      setIsStreaming(false)
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
    }
  }

  // 交互回调
  function toggleRef(index: number) {
    setExpandedRefs((prev) => {
      const next = new Set(prev)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  function toggleRefDetail(key: string) {
    setExpandedRefDetails((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function toggleAuxiliaryKb(kbId: string) {
    setAuxiliaryKbIds((prev) =>
      prev.includes(kbId) ? prev.filter((id) => id !== kbId) : [...prev, kbId]
    )
  }

  const selectedModelName = llmConfigs.find((c) => c.id === selectedModel)?.name || ''

  return (
    <div className="h-full flex">
      {/* 会话侧边栏 */}
      <div
        className={`shrink-0 border-r border-border/60 bg-card/30 transition-[width] duration-200 ease-in-out ${
          sidebarOpen ? 'w-56' : 'w-0 border-r-0'
        }`}
      >
        <div className={`w-56 h-full transition-opacity duration-200 ${sidebarOpen ? 'opacity-100' : 'opacity-0'}`}>
          <SessionSidebar
            sessions={sessions as SessionItem[]}
            currentSessionId={currentSessionId}
            isNewChat={currentSessionId === null || messages.length === 0}
            onNewSession={handleNewSession}
            onSwitchSession={handleSwitchSession}
            onDeleteSession={handleDeleteSession}
          />
        </div>
      </div>

      {/* 主内容区 */}
      <div className="relative flex-1 flex flex-col min-w-0">
        {/* 侧边栏切换 */}
        <Button
          variant="ghost"
          size="icon"
          className="absolute top-2 left-2 z-10 h-8 w-8"
          onClick={() => setSidebarOpen((v) => !v)}
          title={sidebarOpen ? '收起侧边栏' : '展开侧边栏'}
        >
          {sidebarOpen ? <PanelLeftClose className="h-4 w-4" /> : <PanelLeft className="h-4 w-4" />}
        </Button>

        {/* 消息列表 */}
        <div ref={scrollContainerRef} className="flex-1 overflow-auto pb-36">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-center">
              <div className="w-14 h-14 rounded-2xl bg-muted/50 flex items-center justify-center mb-4">
                <Bot className="h-7 w-7 text-muted-foreground/50" />
              </div>
              <p className="text-muted-foreground text-sm">开始对话，向知识库提问</p>
            </div>
          ) : (
            <div className="max-w-3xl mx-auto py-6 px-4 space-y-5">
              {messages.map((msg, idx) => (
                <MessageBubble
                  key={idx}
                  message={msg}
                  index={idx}
                  isStreaming={isStreaming}
                  isLast={idx === messages.length - 1}
                  expandedRefs={expandedRefs}
                  expandedRefDetails={expandedRefDetails}
                  onToggleRef={toggleRef}
                  onToggleRefDetail={toggleRefDetail}
                />
              ))}
            </div>
          )}
        </div>

        {/* 输入区域 */}
        <ChatInput
          input={input}
          isStreaming={isStreaming}
          selectedKb={selectedKb}
          selectedModel={selectedModel}
          selectedModelName={selectedModelName}
          retrievalMode={retrievalMode}
          auxiliaryKbIds={auxiliaryKbIds}
          knowledgeBases={knowledgeBases}
          llmConfigs={llmConfigs}
          onInputChange={setInput}
          onSend={handleSend}
          onKbChange={setSelectedKb}
          onModelChange={setSelectedModel}
          onRetrievalModeChange={setRetrievalMode}
          onToggleAuxiliaryKb={toggleAuxiliaryKb}
        />
      </div>
    </div>
  )
}

export default Chat
