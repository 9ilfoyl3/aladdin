import { useState, useRef, useEffect, useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { knowledgeBaseApi, llmConfigApi, sessionApi, agentPresetApi, sessionFileApi } from '@/lib/api'
import type { SessionFileResponse, MessageAttachment } from '@/lib/api'
import MessageBubble from '@/components/chat/MessageBubble'
import type { Message, Reference, ContentSegment } from '@/components/chat/MessageBubble'
import ChatInput from '@/components/chat/ChatInput'
import type { PendingSessionFile } from '@/components/chat/SessionFileList'
import { isImageFilename } from '@/components/chat/SessionFileList'
import SuggestedQuestions from '@/components/chat/SuggestedQuestions'
import ChatMessagesSkeleton from '@/components/skeletons/ChatMessagesSkeleton'
import SideRays from '@/components/SideRays'
import { useSession } from '@/lib/session-context'
import { useConfirm } from '@/lib/confirm-context'
import { authHeaders, handleUnauthorized } from '@/lib/auth'

interface KnowledgeBaseItem {
  id: string
  name: string
  // 归属/可见性（用于聊天选择器按「个人 / 共享」分组；后端列表已透出）
  owner_user_id?: string | null
  visibility?: string | null
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
  const [isLoadingMessages, setIsLoadingMessages] = useState(false)
  const [selectedModel, setSelectedModel] = useState('')
  const [selectedPreset, setSelectedPreset] = useState('')
  // 已选知识库（按选中顺序，首个即后端主库 kb_ids[0]，检索权重更高）。
  const [selectedKbIds, setSelectedKbIds] = useState<string[]>([])
  const [expandedRefs, setExpandedRefs] = useState<Set<number>>(new Set())
  const [expandedRefDetails, setExpandedRefDetails] = useState<Set<string>>(new Set())
  const [contextUsage, setContextUsage] = useState<{ current: number; max: number }>({ current: 0, max: 0 })
  // 会话文件本地占位（POST 在飞 / 失败提示），与服务端列表一起展示。
  // 同步上传完成后由 react-query 刷新列表 + 清掉对应占位。
  const [pendingFiles, setPendingFiles] = useState<PendingSessionFile[]>([])
  // 上传中占位的 AbortController：localId → controller，用于中途取消在飞的 POST。
  const uploadControllersRef = useRef<Record<string, AbortController>>({})
  // 本会话内上传的图片：文件名 → object URL（用于 chip 缩略图与放大预览）。
  // 服务端临时文件处理后即删，无法回源取图，故在上传时就地从客户端 blob 生成。
  const [imagePreviewUrls, setImagePreviewUrls] = useState<Record<string, string>>({})
  const imagePreviewUrlsRef = useRef<Record<string, string>>({})
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  // 标记：刚在该会话发起发送（消息已在本地），跳过 loadMessages 避免覆盖
  const pendingSendSessionRef = useRef<string | null>(null)

  const { currentSessionId, setCurrentSessionId, refreshSessions } = useSession()
  const confirm = useConfirm()
  const queryClient = useQueryClient()

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

  // 会话上传文件列表（仅在会话存在时拉取；切会话时随 currentSessionId 重新拉）
  // session-file-upload Task 16 / Req 1.8
  const { data: sessionFiles = [] } = useQuery<SessionFileResponse[]>({
    queryKey: ['session-files', currentSessionId],
    queryFn: () => sessionFileApi.list(currentSessionId!),
    enabled: !!currentSessionId,
  })

  // 切换会话时清掉本地占位（避免 A 会话上传中切到 B 仍显示）。
  // 用 ref 跟踪上一次会话：跳过「null -> 新建会话」首建场景——该场景是新对话首次
  // 上传时由 ensureSessionId() 创建会话触发的，此刻 handleUploadSessionFiles 正要
  // 加上传占位，若在此清空会把刚加的「处理中」占位冲掉，导致看不到上传进度。
  const prevSessionForPendingRef = useRef<string | null>(currentSessionId)
  useEffect(() => {
    const prev = prevSessionForPendingRef.current
    prevSessionForPendingRef.current = currentSessionId
    // 仅在「从一个已存在会话切到另一个会话」或「切回无会话」时清空；
    // 「null -> 新建会话」不清（保留上传发起时刚加的占位）。
    if (prev !== null) {
      setPendingFiles([])
      // 释放上一会话的图片预览 object URL，避免内存泄漏
      Object.values(imagePreviewUrlsRef.current).forEach((url) => URL.revokeObjectURL(url))
      imagePreviewUrlsRef.current = {}
      setImagePreviewUrls({})
    }
  }, [currentSessionId])

  // 组件卸载时统一释放所有图片预览 object URL
  useEffect(() => {
    return () => {
      Object.values(imagePreviewUrlsRef.current).forEach((url) => URL.revokeObjectURL(url))
    }
  }, [])

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
      setIsLoadingMessages(true)
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
            attachments: m.attachments || undefined,
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
                    toolArgs: (step.arguments as Record<string, unknown>) || undefined,
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
                } else if (step.type === 'final_answer') {
                  if (step.content) {
                    // 合并连续的 answer 段落
                    const lastSeg = segments[segments.length - 1]
                    if (lastSeg && lastSeg.type === 'answer') {
                      lastSeg.content += String(step.content)
                    } else {
                      segments.push({ type: 'answer', content: String(step.content) })
                    }
                  } else if ((step as Record<string, unknown>).done) {
                    // done=true 且无 content：natural_stop / stuck_loop 场景。
                    // 答案已作为最后一个 thought 段落流式发出（弱 function-calling 模型
                    // 闲聊时把答案当普通 content 输出），需与流式渲染逻辑一致，把它转为
                    // answer。否则末尾兜底会用 base.content 再补一个 answer 段落，导致
                    // 历史恢复时「思考面板 + 正文」双份显示同一内容。
                    const lastSeg = segments[segments.length - 1]
                    if (lastSeg && lastSeg.type === 'thought') {
                      lastSeg.type = 'answer'
                    }
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

        // 恢复该会话最近一次使用的知识库选择：从最后一条带 kb 信息的消息读取。
        // kb_ids（多选，有序，首个为主库）优先；否则回退单选 kb_id。
        for (let i = msgs.length - 1; i >= 0; i--) {
          const m = msgs[i]
          if (m.kb_ids && m.kb_ids.length > 0) {
            setSelectedKbIds(m.kb_ids.filter(Boolean))
            break
          }
          if (m.kb_id) {
            setSelectedKbIds([m.kb_id])
            break
          }
        }
      } catch (e) {
        console.error('加载会话消息失败', e)
        setMessages([])
      } finally {
        setIsLoadingMessages(false)
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
        // 不在此刷新侧栏：此刻会话尚无消息（被空会话过滤隐藏）。
        // 待首个流式分片到达（后端已入库用户消息+播种标题）时再刷新。
      } catch (e) {
        console.error('自动创建会话失败', e)
      }
    } else {
      // 已有会话内发送：同样标记，防止其它原因触发的重载覆盖流式消息
      pendingSendSessionRef.current = sessionId
    }

    const userMessage: Message = { role: 'user', content: query }
    // 绑定当前暂存的会话文件为本条用户消息的附件（已建索引完成的；上传中的不绑）。
    const boundAttachments: MessageAttachment[] = stagedFiles.map((f) => ({
      file_id: f.id,
      filename: f.filename,
      file_size: f.file_size,
      file_type: f.file_type,
    }))
    if (boundAttachments.length > 0) userMessage.attachments = boundAttachments
    setMessages((prev) => [...prev, userMessage])
    setInput('')
    setIsStreaming(true)

    const assistantMessage: Message = { role: 'assistant', content: '', references: [] }
    setMessages((prev) => [...prev, assistantMessage])

    try {
      // 合并知识库选择映射到后端契约（保持既有路由语义不变）：
      // - 0 个库 → 都不传；1 个库 → 仅 knowledge_base_id（走 SINGLE_KB，保留单库查询理解链路）；
      // - 2+ 个库 → kb_ids（走 MULTI_KB），数组首个即权重 1.0 的主库。
      const primaryKb = selectedKbIds[0] || undefined
      const multiKbIds = selectedKbIds.length > 1 ? selectedKbIds : undefined

      const response = await fetch('/api/chat/completions', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          model: 'rag',
          messages: [{ role: 'user', content: query }],
          stream: true,
          knowledge_base_id: primaryKb,
          model_config_id: selectedModel || undefined,
          agent_preset_id: selectedPreset || undefined,
          kb_ids: multiKbIds,
          session_id: sessionId || undefined,
          attachments: boundAttachments.length > 0 ? boundAttachments : undefined,
        }),
      })

      if (response.status === 401) {
        handleUnauthorized()
        throw new Error('登录态已失效，请重新登录')
      }
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
      // 检索降级标志（来自 meta 事件 metadata）：区分会话文件源 / 知识库源失败（Req 2.x）
      let sessionSourceFailed = false
      let kbSourceFailed = false
      // 后端在生成回答前已入库用户消息并播种标题；首个流式分片到达时刷新侧栏，
      // 让新会话（及其问题标题）立即出现，无需等 AI 答完。
      let sidebarRefreshed = false

      if (reader) {
        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          if (!sidebarRefreshed) {
            sidebarRefreshed = true
            refreshSessions()
          }

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            const data = line.slice(6).trim()
            if (data === '[DONE]') continue

            try {
              const parsed = JSON.parse(data)

              // 检索降级元数据（meta 事件携带 metadata；区分会话文件源 / 知识库源失败，Req 2.x）。
              // 任何带 metadata 的事件都提取，挂到当前 assistant 消息以渲染分类提示。
              if (parsed.metadata && typeof parsed.metadata === 'object') {
                if (parsed.metadata.session_source_failed) sessionSourceFailed = true
                if (parsed.metadata.kb_source_failed) kbSourceFailed = true
                if (sessionSourceFailed || kbSourceFailed) {
                  setMessages((prev) => {
                    const updated = [...prev]
                    const last = updated[updated.length - 1]
                    if (last && last.role === 'assistant') {
                      updated[updated.length - 1] = { ...last, sessionSourceFailed, kbSourceFailed }
                    }
                    return updated
                  })
                }
              }

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
                      toolArgs: parsed.arguments,
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
            sessionSourceFailed,
            kbSourceFailed,
          }
          return updated
        })
      } else {
        setMessages((prev) => {
          const updated = [...prev]
          updated[updated.length - 1] = { role: 'assistant', content: fullContent, references, agentSteps, sessionSourceFailed, kbSourceFailed }
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

  function toggleKb(kbId: string) {
    setSelectedKbIds((prev) =>
      prev.includes(kbId) ? prev.filter((id) => id !== kbId) : [...prev, kbId]
    )
  }

  // ==========================================================================
  // 会话文件上传 / 移除（session-file-upload Task 16）
  //
  // 设计：上传时若用户尚未开会话，先调 POST /sessions 建会话，再 POST 文件；
  // 同步建索引完成后由后端返回 SessionFileResponse，react-query 刷新列表 +
  // 清掉本地占位；并发上传按文件逐个 await 排队，避免同时打多份解析占用 API
  // 进程的 embed 信号量。失败则将占位状态置 failed + toast 后端友好中文 detail。
  // ==========================================================================

  async function ensureSessionId(): Promise<string | null> {
    if (currentSessionId) return currentSessionId
    try {
      const session = await sessionApi.create({ title: '新对话' })
      pendingSendSessionRef.current = session.id
      setCurrentSessionId(session.id)
      refreshSessions()
      return session.id
    } catch (err) {
      console.error('自动创建会话失败', err)
      toast.error(err instanceof Error ? err.message : '创建会话失败')
      return null
    }
  }

  async function handleUploadSessionFiles(files: FileList) {
    // 关键：在任何 await 之前同步拷贝 FileList。files 是 input.files 的实时引用，
    // ChatInput 在调用本函数后会立即执行 e.target.value=''（为支持重复选同名文件），
    // 该操作会清空这个 FileList。若等到 await ensureSessionId() 之后再读，files 已为空，
    // 导致上传循环不执行（表现为：会话建了、列表查了，但上传 POST 永不发出）。
    const fileArr = Array.from(files)
    if (fileArr.length === 0) return

    const sessionId = await ensureSessionId()
    if (!sessionId) return

    // 逐个串行上传（同步建索引耗时较长，避免并发打爆 API 进程的 embed 信号量）
    for (const file of fileArr) {
      const localId = `local_${Date.now()}_${Math.random().toString(36).slice(2)}`
      // 图片：就地生成预览 object URL（服务端临时文件处理后即删，事后无法回源取图）
      if (isImageFilename(file.name)) {
        const url = URL.createObjectURL(file)
        // 同名旧预览先释放再覆盖
        const old = imagePreviewUrlsRef.current[file.name]
        if (old) URL.revokeObjectURL(old)
        imagePreviewUrlsRef.current = { ...imagePreviewUrlsRef.current, [file.name]: url }
        setImagePreviewUrls((prev) => ({ ...prev, [file.name]: url }))
      }
      setPendingFiles((prev) => [
        ...prev,
        { localId, filename: file.name, size: file.size, status: 'uploading' },
      ])
      // 该次上传的中止句柄：用户在上传中点取消时 abort
      const controller = new AbortController()
      uploadControllersRef.current[localId] = controller
      try {
        await sessionFileApi.upload(sessionId, file, controller.signal)
        // 成功：刷新服务端列表 + 清占位（让服务端条目无缝替换）
        await queryClient.invalidateQueries({ queryKey: ['session-files', sessionId] })
        setPendingFiles((prev) => prev.filter((p) => p.localId !== localId))
      } catch (err) {
        // 用户主动取消：静默清掉占位，不报错（占位已在取消处理器中移除）
        if (err instanceof DOMException && err.name === 'AbortError') {
          setPendingFiles((prev) => prev.filter((p) => p.localId !== localId))
        } else {
          const msg = err instanceof Error ? err.message : '上传失败'
          toast.error(msg)
          // 失败的占位保留并标红，让用户能看到失败原因；点击取消才本地清掉
          setPendingFiles((prev) =>
            prev.map((p) =>
              p.localId === localId ? { ...p, status: 'failed', errorMessage: msg } : p
            )
          )
        }
      } finally {
        delete uploadControllersRef.current[localId]
      }
    }
  }

  async function handleRemoveSessionFile(fileId: string) {
    if (!currentSessionId) return
    const target = sessionFiles.find((f) => f.id === fileId)
    const ok = await confirm({
      title: '移除会话文件',
      description: (
        <>
          确定要从本会话移除文件{target ? `「${target.filename}」` : ''}吗？该文件的索引将被删除，后续问答不会再命中其内容。
        </>
      ),
      confirmText: '移除',
    })
    if (!ok) return
    try {
      await sessionFileApi.remove(currentSessionId, fileId)
      await queryClient.invalidateQueries({ queryKey: ['session-files', currentSessionId] })
      // 释放该文件对应的图片预览 object URL（若有）
      if (target && imagePreviewUrlsRef.current[target.filename]) {
        URL.revokeObjectURL(imagePreviewUrlsRef.current[target.filename])
        const { [target.filename]: _removed, ...rest } = imagePreviewUrlsRef.current
        imagePreviewUrlsRef.current = rest
        setImagePreviewUrls(rest)
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '移除失败')
    }
  }

  function handleDismissPendingSessionFile(localId: string) {
    setPendingFiles((prev) => prev.filter((p) => p.localId !== localId))
  }

  // 取消一个上传中的占位：中止在飞的 POST，并立即清掉占位。
  // 占位清理也由 upload 的 AbortError 分支兜底，这里先行移除让 UI 即时响应。
  function handleCancelPendingSessionFile(localId: string) {
    const controller = uploadControllersRef.current[localId]
    if (controller) {
      controller.abort()
      delete uploadControllersRef.current[localId]
    }
    setPendingFiles((prev) => prev.filter((p) => p.localId !== localId))
  }

  const selectedModelName = llmConfigs.find((c) => c.id === selectedModel)?.name || ''

  // 已被某条用户消息"消费"的会话文件 ID（发送时绑定为附件后，从输入区清出）。
  // 单一数据源：从 messages 的 attachments 派生，刷新历史后同样成立。
  const consumedFileIds = useMemo(() => {
    const ids = new Set<string>()
    for (const m of messages) {
      if (m.attachments) for (const a of m.attachments) ids.add(a.file_id)
    }
    return ids
  }, [messages])

  // 输入区仅展示"尚未随消息发出"的会话文件（已发送的随气泡上移）。
  const stagedFiles = useMemo(
    () => sessionFiles.filter((f) => !consumedFileIds.has(f.id)),
    [sessionFiles, consumedFileIds]
  )

  const isEmpty = messages.length === 0
  // 共用的输入框 props
  const chatInputProps = {
    input,
    isStreaming,
    selectedKbIds,
    selectedModel,
    selectedModelName,
    selectedPreset,
    contextUsage,
    knowledgeBases,
    llmConfigs,
    agentPresets,
    onInputChange: setInput,
    onSend: handleSend,
    onToggleKb: toggleKb,
    onModelChange: setSelectedModel,
    onPresetChange: setSelectedPreset,
    // 会话文件上传：始终可用（即使未选 KB，Req 1.4），仅在流式或建索引时禁用由组件内判断
    // 输入区只展示"尚未随消息发出"的暂存文件；已发送的随用户气泡上移。
    sessionFiles: stagedFiles,
    pendingSessionFiles: pendingFiles,
    canUploadSessionFile: !isStreaming,
    onUploadSessionFiles: handleUploadSessionFiles,
    onRemoveSessionFile: handleRemoveSessionFile,
    onCancelPendingSessionFile: handleCancelPendingSessionFile,
    onDismissPendingSessionFile: handleDismissPendingSessionFile,
    sessionImagePreviewUrls: imagePreviewUrls,
  }

  // 空态：标题 + 提问示例气泡 + 居中输入框
  // 加载历史消息期间不显示空态，避免「Artoo 欢迎页」闪现
  if (isEmpty && !isLoadingMessages) {
    return (
      <div className="relative h-full flex flex-col items-center justify-center px-4 overflow-hidden">
        {/* 新对话页背景：Side Rays 动态光线（reactbits）。仅暗色模式显示，浅色模式会蒙灰故隐藏 */}
        <div className="pointer-events-none absolute inset-0 z-0 hidden dark:block">
          <SideRays />
        </div>
        <div className="relative z-10 w-full max-w-3xl flex flex-col items-center -mt-12">
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
        {isLoadingMessages ? (
          <ChatMessagesSkeleton />
        ) : (
          <div className="max-w-3xl mx-auto py-6 px-4 space-y-5 animate-in fade-in-0 duration-500">
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
                imagePreviewUrls={imagePreviewUrls}
              />
            ))}
          </div>
        )}
      </div>

      {/* 输入区域 */}
      <ChatInput {...chatInputProps} />
    </div>
  )
}

export default Chat
