import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, PanelLeftClose, PanelLeft } from 'lucide-react'
import { knowledgeBaseApi, llmConfigApi, sessionApi } from '@/lib/api'
import type { SessionItem } from '@/lib/api'
import { Button } from '@/components/ui/button'
import SessionSidebar from '@/components/chat/SessionSidebar'
import MessageBubble from '@/components/chat/MessageBubble'
import type { Message, Reference } from '@/components/chat/MessageBubble'
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
    // 点击"新对话"只是进入空白状态，不创建 session
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
      setMessages(msgs.map((m) => ({
        role: m.role as 'user' | 'assistant',
        content: m.content,
        references: (m.references as Reference[]) || undefined,
        agentSteps: m.agent_steps || undefined,
      })))
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
      let buffer = ''

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

              if (parsed.type === 'agent_progress') {
                agentSteps = [...(agentSteps || []), { step: parsed.step, detail: parsed.detail }]
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
            } catch { /* 忽略解析错误 */ }
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
