import { createContext, useContext, useState, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { sessionApi } from '@/lib/api'
import type { SessionItem } from '@/lib/api'

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

  const { data: sessions = [] } = useQuery({
    queryKey: ['sessions'],
    queryFn: () => sessionApi.list(),
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
