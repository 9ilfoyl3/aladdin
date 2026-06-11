import { createContext, useContext, useState, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { sessionApi } from '@/lib/api'
import type { SessionItem } from '@/lib/api'
import { useAuth } from '@/lib/auth-context'

interface SessionContextValue {
  sessions: SessionItem[]
  currentSessionId: string | null
  setCurrentSessionId: (id: string | null) => void
  handleNewSession: () => void
  handleDeleteSession: (sessionId: string, e: React.MouseEvent) => void
  refreshSessions: () => void
  addOptimisticSession: (session: SessionItem) => void
}

const SessionContext = createContext<SessionContextValue | null>(null)

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null)
  const queryClient = useQueryClient()
  // 仅在已登录、且非"必须改密"态时拉取会话列表。
  // 否则在公开页（如 /invite/:token、/login）会以无 token 调用受保护的 /sessions，
  // 触发 401 -> 全局 handleUnauthorized -> 跳登录页（即"邀请链接打开后自动跳登录"的根因）。
  const { isAuthenticated, mustChangePassword, isSuperAdmin } = useAuth()
  const sessionsEnabled = isAuthenticated && !mustChangePassword && !isSuperAdmin

  const { data: sessions = [] } = useQuery({
    queryKey: ['sessions'],
    queryFn: () => sessionApi.list(),
    enabled: sessionsEnabled,
  })

  const handleNewSession = useCallback(() => {
    setCurrentSessionId(null)
  }, [])

  const handleDeleteSession = useCallback(async (sessionId: string, e: React.MouseEvent) => {
    e.stopPropagation()
    try {
      await sessionApi.delete(sessionId)
      if (currentSessionId === sessionId) {
        setCurrentSessionId(null)
      }
      queryClient.invalidateQueries({ queryKey: ['sessions'] })
    } catch (err) {
      console.error('删除会话失败', err)
    }
  }, [currentSessionId, queryClient])

  const refreshSessions = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['sessions'] })
  }, [queryClient])

  // 新建会话时乐观插入侧栏：直接写入 react-query 缓存，让会话（以用户问题为标题）
  // 立即出现，无需等后端入库用户消息 + 首个流式分片返回。后续 refreshSessions 会用
  // 服务端真实数据（含 LLM 精炼后的标题）覆盖。已存在同 id 则跳过（避免重复）。
  const addOptimisticSession = useCallback((session: SessionItem) => {
    queryClient.setQueryData<SessionItem[]>(['sessions'], (prev) => {
      const list = prev ?? []
      if (list.some((s) => s.id === session.id)) return list
      return [session, ...list]
    })
  }, [queryClient])

  return (
    <SessionContext.Provider value={{
      sessions,
      currentSessionId,
      setCurrentSessionId,
      handleNewSession,
      handleDeleteSession,
      refreshSessions,
      addOptimisticSession,
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
