import { useState, useRef, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { knowledgeBaseApi, llmConfigApi, sessionApi, agentPresetApi } from '@/lib/api'
import MessageBubble from '@/components/chat/MessageBubble'
import type { Message, Reference, ContentSegment } from '@/components/chat/MessageBubble'
import ChatInput from '@/components/chat/ChatInput'
import SuggestedQuestions from '@/components/chat/SuggestedQuestions'
import { useSession } from '@/lib/session-context'

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
  const [selectedPreset, setSelectedPreset] = useState('')
  const [auxiliaryKbIds, setAuxiliaryKbIds] = useState<string[]>([])
  const [expandedRefs, setExpandedRefs] = useState<Set<number>>(new Set())
  const [expandedRefDetails, setExpandedRefDetails] = useState<Set<string>>(new Set())
  const [contextUsage, setContextUsage] = useState<{ current: number; max: number }>({ current: 0, max: 0 })
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  // 标记：刚在该会话发起发送（消息已在本地），跳过 loadMessages 避免覆盖
  const pendingSendSessionRef = useRef<string | null>(null)

  const { currentSessionId, setCurrentSessionId, refreshSessions } = useSession()

  const { data: knowledgeBases = [] } = useQuery({
    queryKey: ['knowledge-bases'],
    queryFn: () =>
      knowledgeBaseApi.list({ page_size: 100 }).then((res) => res.items as KnowledgeBaseItem[]),
  })

  const { data: llmConfigs = [] } = useQuery({
    queryKey: ['llm-configs', 'chat-visible'],
    queryFn: () => llmConfigApi.list(true) as Promise<LLMConfigItem[]>,
  })

  const { data: agentPresets = [] } = useQuery({
    queryKey: ['agent-presets'],
    queryFn: () => agentPresetApi.list(),
  })

  // 默认选中 is_default 的 Agent 预设
  useEffect(() => {
    if (agentPresets.length > 0 && !selectedPreset) {
      const def = agentPresets.find((p) => p.is_default) ?? agentPresets[0]
      if (def) setSelectedPreset(def.id)
    }
  }, [agentPresets, selectedPreset])

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

  // 会话切换时加载消息
  useEffect(() => {
    if (currentSessionId === null) {
      setMessages([])
      setExpandedRefs(new Set())
      setExpandedRefDetails(new Set())
      setContextUsage({ current: 0, max: 0 })
      return
    }
    // 若该会话是刚在本地发起发送的（消息已在本地、可能正在流式），跳过加载避免覆盖
    if (pendingSendSessionRef.current === currentSessionId) {
      return
    }
    // 加载会话消息
    async function loadMessages() {
      setExpandedRefs(new Set())
      setExpandedRefDetails(new Set())
      setContextUsage({ current: 0, max: 0 })
      try {
        const msgs = await sessionApi.getMessages(currentSessionId!)
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
                } else if (step.type === 'complete' && typeof (step as Record<string, unknown>).total_duration_ms === 'number') {
                  // 恢复整体耗时
                  base.totalDurationMs = (step as Record<string, unknown>).total_duration_ms as number
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

        // 从历史消息中恢复上下文用量圆环：取最后一条带 token_usage 步骤的记录
        // token_usage 事件在流式时已随其他 SSE 事件一并存入 agent_steps（后端 _stream_response）
        let restoredUsage: { current: number; max: number } | null = null
        for (const m of msgs) {
          if (!m.agent_steps || !Array.isArray(m.agent_steps)) continue
          for (const step of m.agent_steps) {
            if (step.type === 'token_usage' && typeof step.max_context_tokens === 'number') {
              restoredUsage = {
                current: step.current_context_tokens || 0,
                max: step.max_context_tokens,
              }
            }
          }
        }
        if (restoredUsage) setContextUsage(restoredUsage)

        // 恢复该会话最近一次使用的知识库选择：从最后一条带 kb 信息的消息读取
        for (let i = msgs.length - 1; i >= 0; i--) {
          const m = msgs[i]
          if (m.kb_id || (m.kb_ids && m.kb_ids.length > 0)) {
            setSelectedKb(m.kb_id || '')
            setAuxiliaryKbIds(
              (m.kb_ids || []).filter((id) => id && id !== m.kb_id)
            )
            break
          }
        }
      } catch (e) {
        console.error('加载会话消息失败', e)
        setMessages([])
      }
    }
    loadMessages()
  }, [currentSessionId])

  // 发送消息
  async function handleSend(overrideQuery?: string) {
    const query = (overrideQuery ?? input).trim()
    if (!query || isStreaming) return

    let sessionId = currentSessionId
    if (!sessionId) {
      try {
        const session = await sessionApi.create({ title: '新对话' })
        sessionId = session.id
        // 标记该会话为本地发起发送，避免 setCurrentSessionId 触发的 loadMessages 覆盖本地消息
        pendingSendSessionRef.current = session.id
        setCurrentSessionId(session.id)
        refreshSessions()
      } catch (e) {
        console.error('自动创建会话失败', e)
      }
    } else {
      // 已有会话内发送：同样标记，防止其它原因触发的重载覆盖流式消息
      pendingSendSessionRef.current = sessionId
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
          agent_preset_id: selectedPreset || undefined,
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
      let totalDurationMs: number | undefined = undefined

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
                    if (typeof parsed.total_duration_ms === 'number') {
                      totalDurationMs = parsed.total_duration_ms
                      setMessages((prev) => {
                        const updated = [...prev]
                        updated[updated.length - 1] = {
                          role: 'assistant',
                          content: fullContent,
                          references,
                          segments,
                          totalDurationMs,
                        }
                        return updated
                      })
                    }
                    break
                  }

                  // 上下文 token 用量：更新进度圆环
                  case 'token_usage': {
                    setContextUsage({
                      current: parsed.current_context_tokens,
                      max: parsed.max_context_tokens,
                    })
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
            totalDurationMs,
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
      refreshSessions()
      // 清除本地发送标记：之后再切回该会话时正常从服务端加载
      pendingSendSessionRef.current = null
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

  const isEmpty = messages.length === 0

  // 共用的输入框 props
  const chatInputProps = {
    input,
    isStreaming,
    selectedKb,
    selectedModel,
    selectedModelName,
    selectedPreset,
    auxiliaryKbIds,
    contextUsage,
    knowledgeBases,
    llmConfigs,
    agentPresets,
    onInputChange: setInput,
    onSend: handleSend,
    onKbChange: setSelectedKb,
    onModelChange: setSelectedModel,
    onPresetChange: setSelectedPreset,
    onToggleAuxiliaryKb: toggleAuxiliaryKb,
  }

  // 空态：标题 + 提问示例气泡 + 居中输入框（参考 WeKnora 新对话布局）
  if (isEmpty) {
    return (
      <div className="h-full flex flex-col items-center justify-center px-4">
        <div className="w-full max-w-3xl flex flex-col items-center -mt-12">
          <h1 className="text-3xl font-semibold text-foreground text-center mb-8">
            我是 <span className="font-serif">Artoo</span>，你的知识库问答助手
          </h1>

          <div className="mb-8 w-full">
            <SuggestedQuestions onSelect={(q) => setInput(q)} />
          </div>

          <ChatInput {...chatInputProps} centered />
        </div>
      </div>
    )
  }

  return (
    <div className="h-full flex flex-col relative">
      {/* 消息列表 */}
      <div ref={scrollContainerRef} className="flex-1 overflow-auto pb-36">
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
      </div>

      {/* 输入区域 */}
      <ChatInput {...chatInputProps} />
    </div>
  )
}

export default Chat
