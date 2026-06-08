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

  return (
    <SessionContext.Provider value={{
      sessions,
      currentSessionId,
      setCurrentSessionId,
      handleNewSession,
      handleDeleteSession,
      refreshSessions,
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
