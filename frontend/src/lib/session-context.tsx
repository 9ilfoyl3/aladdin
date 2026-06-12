import { createContext, useContext, useState, useCallback, useMemo, useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { sessionApi } from '@/lib/api'
import type { SessionItem } from '@/lib/api'
import { useAuth } from '@/lib/auth-context'

interface SessionContextValue {
  sessions: SessionItem[]
  currentSessionId: string | null
  setCurrentSessionId: (id: string | null) => void
  handleNewSession: () => void
  /** 「新对话」点击计数：每次点击自增，供页面强制重置本地状态（即使已处于空会话）。 */
  newSessionNonce: number
  handleDeleteSession: (sessionId: string, e: React.MouseEvent) => void
  refreshSessions: () => void
  addOptimisticSession: (session: SessionItem) => void
  replaceOptimisticSession: (tempId: string, session: SessionItem) => void
  removeOptimisticSession: (tempId: string) => void
}

const SessionContext = createContext<SessionContextValue | null>(null)

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)
  // 「新对话」点击计数：即使已处于空会话（currentSessionId 仍为 null），自增也能驱动
  // 页面重置输入框/暂存文件等本地状态。
  const [newSessionNonce, setNewSessionNonce] = useState(0)
  const queryClient = useQueryClient()
  // 仅在已登录、且非"必须改密"态时拉取会话列表。
  // 否则在公开页（如 /invite/:token、/login）会以无 token 调用受保护的 /sessions，
  // 触发 401 -> 全局 handleUnauthorized -> 跳登录页（即"邀请链接打开后自动跳登录"的根因）。
  const { isAuthenticated, mustChangePassword, isSuperAdmin } = useAuth()
  const sessionsEnabled = isAuthenticated && !mustChangePassword && !isSuperAdmin

  const { data: serverSessions = [] } = useQuery({
    queryKey: ['sessions'],
    queryFn: () => sessionApi.list(),
    enabled: sessionsEnabled,
  })

  // 乐观会话：新建会话时本地立即插入，与服务端列表分开存放（不写 query 缓存，
  // 避免被 refreshSessions 的 refetch 整体覆盖）。后端用户消息入库后服务端列表
  // 会返回同 id 的会话，届时该乐观项被自动去重移除（见下方 merge 逻辑）。
  const [optimisticSessions, setOptimisticSessions] = useState<SessionItem[]>([])

  // 合并：服务端列表为准，乐观项里"服务端尚未返回"的补在最前（最新）。
  // 服务端一旦返回同 id（用户消息已入库），对应乐观项自然被过滤掉，无缝切换。
  const sessions = useMemo(() => {
    if (optimisticSessions.length === 0) return serverSessions
    const serverIds = new Set(serverSessions.map((s) => s.id))
    const pending = optimisticSessions.filter((s) => !serverIds.has(s.id))
    return pending.length > 0 ? [...pending, ...serverSessions] : serverSessions
  }, [serverSessions, optimisticSessions])

  // 服务端列表已包含的乐观项及时清除，避免 optimisticSessions 无限累积。
  useEffect(() => {
    if (optimisticSessions.length === 0) return
    const serverIds = new Set(serverSessions.map((s) => s.id))
    setOptimisticSessions((prev) => {
      const remaining = prev.filter((s) => !serverIds.has(s.id))
      return remaining.length === prev.length ? prev : remaining
    })
  }, [serverSessions, optimisticSessions.length])

  const handleNewSession = useCallback(() => {
    setCurrentSessionId(null)
    setNewSessionNonce((n) => n + 1)
  }, [])

  const handleDeleteSession = useCallback(async (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await sessionApi.delete(sessionId)
      if (currentSessionId === sessionId) {
        setCurrentSessionId(null)
      }
      setOptimisticSessions((prev) => prev.filter((s) => s.id !== sessionId))
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
    } catch (err) {
      console.error('删除会话失败', err)
    }
  }, [currentSessionId, queryClient])

  const refreshSessions = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['sessions'] })
  }, [queryClient])

  // 新建会话时乐观插入侧栏：写入本地 state（不进 query 缓存），让会话（以用户问题
  // 为标题）立即出现，无需等后端入库 + 首个流式分片。已存在同 id 则跳过。
  const addOptimisticSession = useCallback((session: SessionItem) => {
    setOptimisticSessions((prev) =>
      prev.some((s) => s.id === session.id) ? prev : [session, ...prev]
    )
  }, [])

  // create 返回后用真实会话替换临时项（回填真实 id），保留问题占位标题。
  const replaceOptimisticSession = useCallback((tempId: string, session: SessionItem) => {
    setOptimisticSessions((prev) => {
      const idx = prev.findIndex((s) => s.id === tempId)
      if (idx >= 0) {
        const next = [...prev]
        next[idx] = session
        return next
      }
      return prev.some((s) => s.id === session.id) ? prev : [session, ...prev]
    })
  }, [])

  // 移除乐观项（create 失败回滚）。
  const removeOptimisticSession = useCallback((tempId: string) => {
    setOptimisticSessions((prev) => prev.filter((s) => s.id !== tempId))
  }, [])

  return (
    <SessionContext.Provider value={{
      sessions,
      currentSessionId,
      setCurrentSessionId,
      handleNewSession,
      newSessionNonce,
      handleDeleteSession,
      refreshSessions,
      addOptimisticSession,
      replaceOptimisticSession,
      removeOptimisticSession,
    }}>
      {children}
    </SessionContext.Provider>
  )
}

export function useSession() {
  const ctx = useContext(SessionContext)
  if (!ctx) throw new Error('useSession must be used within SessionProvider')
  return ctx
}
