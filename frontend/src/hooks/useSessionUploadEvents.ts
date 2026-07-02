import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { loadToken } from '../lib/auth'

// 会话文件上传实时状态 Hook（session-upload-async-ws Task 10.2 / Design C10）
//
// 连接 WS /api/sessions/{sid}/files/events?access_token=<jwt>，把后端推送的建索引
// 状态事件（queued/processing/progress/completed/failed/removed）实时映射为每个文件
// 的 chip 状态，供 Chat UI 渲染进度。`completed`/`removed` 时 invalidate
// ['session-files', sid] query 触发列表刷新与对账。
//
// 断线重连使用指数退避（带 jitter）；auth/limit 类关闭码（4401/4400/4403/4404/4429）
// 为永久失败，不重连。重连后依赖服务端首帧 snapshot 做对账。
//
// 遵循项目规范：顶级 import、不使用动态引入、数据流清晰、不过度封装。

/** 单个文件的实时状态（对应后端 SessionUploadEvent 的可渲染子集）。 */
export interface SessionFileLiveState {
  status: string
  progress: number
  progress_message: string | null
  error_message: string | null
  filename: string | null
  chunk_count: number | null
}

/** WS 事件帧（与后端 SessionUploadEvent / snapshot 帧对齐）。 */
interface UploadEventFrame {
  type: 'snapshot' | 'queued' | 'processing' | 'progress' | 'completed' | 'failed' | 'removed' | 'ping'
  session_id?: string
  file_id?: string
  filename?: string | null
  status?: string | null
  progress?: number | null
  progress_message?: string | null
  message?: string | null
  error?: string | null
  error_message?: string | null
  chunk_count?: number | null
  // snapshot 帧携带该会话所有文件的当前状态
  files?: Array<{
    file_id: string
    filename: string | null
    status: string
    progress: number | null
    progress_message: string | null
    error_message: string | null
    chunk_count: number | null
  }>
  ts?: number
}

// 永久失败的关闭码：命中则停止重连（鉴权/限流类，重试无意义）。
const PERMANENT_CLOSE_CODES = new Set([4401, 4400, 4403, 4404, 4429])

// 指数退避参数（单位 ms）。
const BACKOFF_BASE_MS = 1000
const BACKOFF_CAP_MS = 30000

/**
 * 订阅某会话的文件上传状态事件。
 *
 * @param sessionId 会话 ID；为空/无 token 时不建立连接。
 * @returns fileStates 每个 file_id 的最新实时状态，供 UI 渲染 chip 进度。
 */
export function useSessionUploadEvents(sessionId: string | null | undefined): {
  fileStates: Record<string, SessionFileLiveState>
} {
  const queryClient = useQueryClient()
  const [fileStates, setFileStates] = useState<Record<string, SessionFileLiveState>>({})

  // 用 ref 持有 socket / 重连定时器 / 退避计数 / 关闭意图，避免 re-render 触发重连。
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const backoffAttemptRef = useRef(0)
  const closedByUnmountRef = useRef(false)

  useEffect(() => {
    // 重置本轮 effect 的状态
    closedByUnmountRef.current = false
    setFileStates({})

    const token = loadToken()
    if (!sessionId || !token) {
      return
    }

    const invalidateList = () => {
      queryClient.invalidateQueries({ queryKey: ['session-files', sessionId] })
    }

    const buildUrl = (): string => {
      const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
      const host = window.location.host
      const path = `/api/sessions/${sessionId}/files/events`
      return `${scheme}//${host}${path}?access_token=${encodeURIComponent(token)}`
    }

    const applyEvent = (frame: UploadEventFrame) => {
      switch (frame.type) {
        case 'ping':
          // 服务端保活帧，忽略。
          return

        case 'snapshot': {
          // 首帧/重连对账：用快照整体替换本地状态。
          const next: Record<string, SessionFileLiveState> = {}
          for (const f of frame.files ?? []) {
            next[f.file_id] = {
              status: f.status,
              progress: f.progress ?? 0,
              progress_message: f.progress_message ?? null,
              error_message: f.error_message ?? null,
              filename: f.filename ?? null,
              chunk_count: f.chunk_count ?? null,
            }
          }
          setFileStates(next)
          return
        }

        case 'removed': {
          if (!frame.file_id) return
          const removedId = frame.file_id
          setFileStates((prev) => {
            if (!(removedId in prev)) return prev
            const next = { ...prev }
            delete next[removedId]
            return next
          })
          invalidateList()
          return
        }

        case 'completed': {
          if (!frame.file_id) return
          const fileId = frame.file_id
          setFileStates((prev) => ({
            ...prev,
            [fileId]: {
              status: 'completed',
              progress: 100,
              progress_message: prev[fileId]?.progress_message ?? null,
              error_message: null,
              filename: frame.filename ?? prev[fileId]?.filename ?? null,
              chunk_count: frame.chunk_count ?? prev[fileId]?.chunk_count ?? null,
            },
          }))
          // 建索引完成：刷新服务端列表，让 completed 条目携带真实 chunk_count 等落地。
          invalidateList()
          return
        }

        // queued / processing / progress / failed：增量更新对应文件状态。
        case 'queued':
        case 'processing':
        case 'progress':
        case 'failed': {
          if (!frame.file_id) return
          const fileId = frame.file_id
          setFileStates((prev) => {
            const cur = prev[fileId]
            return {
              ...prev,
              [fileId]: {
                status: frame.status ?? frame.type,
                progress: frame.progress ?? cur?.progress ?? 0,
                progress_message: frame.progress_message ?? frame.message ?? cur?.progress_message ?? null,
                error_message: frame.error ?? frame.error_message ?? (frame.type === 'failed' ? cur?.error_message ?? null : null),
                filename: frame.filename ?? cur?.filename ?? null,
                chunk_count: frame.chunk_count ?? cur?.chunk_count ?? null,
              },
            }
          })
          return
        }

        default:
          return
      }
    }

    const scheduleReconnect = () => {
      if (closedByUnmountRef.current) return
      const attempt = backoffAttemptRef.current
      backoffAttemptRef.current = attempt + 1
      // 指数退避 + jitter，封顶 BACKOFF_CAP_MS。
      const expo = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * 2 ** attempt)
      const delay = expo / 2 + Math.random() * (expo / 2)
      reconnectTimerRef.current = setTimeout(connect, delay)
    }

    const connect = () => {
      if (closedByUnmountRef.current) return
      const ws = new WebSocket(buildUrl())
      wsRef.current = ws

      ws.onopen = () => {
        // 连接成功：重置退避计数；服务端会主动推 snapshot 帧做对账。
        backoffAttemptRef.current = 0
      }

      ws.onmessage = (event) => {
        try {
          const frame = JSON.parse(event.data as string) as UploadEventFrame
          applyEvent(frame)
        } catch {
          // 非法帧忽略，不影响连接。
        }
      }

      ws.onclose = (event) => {
        if (wsRef.current === ws) wsRef.current = null
        if (closedByUnmountRef.current) return
        if (PERMANENT_CLOSE_CODES.has(event.code)) {
          // 鉴权/限流类永久失败：停止重连。
          console.warn(`[useSessionUploadEvents] 连接被拒绝（code=${event.code}），停止重连`)
          return
        }
        // 瞬时/网络类关闭：指数退避重连。
        scheduleReconnect()
      }

      ws.onerror = () => {
        // 错误后紧跟 onclose，由 onclose 统一决定是否重连；此处仅确保 socket 关闭。
        try {
          ws.close()
        } catch {
          /* 忽略重复关闭 */
        }
      }
    }

    connect()

    return () => {
      // 卸载 / sessionId 变化：停止重连并关闭连接。
      closedByUnmountRef.current = true
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }
      backoffAttemptRef.current = 0
      const ws = wsRef.current
      wsRef.current = null
      if (ws) {
        ws.onopen = null
        ws.onmessage = null
        ws.onclose = null
        ws.onerror = null
        try {
          ws.close()
        } catch {
          /* 忽略关闭异常 */
        }
      }
    }
  }, [sessionId, queryClient])

  return { fileStates }
}
