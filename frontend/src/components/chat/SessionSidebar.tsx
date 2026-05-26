import { SquarePen, MessageSquare, Trash2 } from 'lucide-react'
import type { SessionItem } from '@/lib/api'

interface SessionSidebarProps {
  sessions: SessionItem[]
  currentSessionId: string | null
  isNewChat: boolean
  onNewSession: () => void
  onSwitchSession: (id: string) => void
  onDeleteSession: (id: string, e: React.MouseEvent) => void
}

function SessionSidebar({
  sessions,
  currentSessionId,
  isNewChat,
  onNewSession,
  onSwitchSession,
  onDeleteSession,
}: SessionSidebarProps) {
  return (
    <div className="h-full flex flex-col">
      {/* 新对话按钮 - 常驻顶部 */}
      <div className="px-3 pt-3 pb-1">
        <button
          onClick={onNewSession}
          className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-sm font-medium cursor-pointer transition-colors ${
            isNewChat
              ? 'bg-primary/15 text-primary'
              : 'hover:bg-accent text-foreground'
          }`}
        >
          <SquarePen className="h-4 w-4" />
          <span>新对话</span>
        </button>
      </div>

      {/* 历史会话列表 */}
      <div className="flex-1 overflow-auto px-2 pt-2 pb-2 space-y-0.5">
        {sessions.map((session) => (
          <div
            key={session.id}
            onClick={() => onSwitchSession(session.id)}
            className={`group flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer transition-colors text-[13px] ${
              currentSessionId === session.id && !isNewChat
                ? 'bg-primary/15 text-primary font-medium'
                : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground'
            }`}
          >
            <span className="flex-1 truncate leading-snug">{session.title}</span>
            <button
              onClick={(e) => onDeleteSession(session.id, e)}
              className="opacity-0 group-hover:opacity-100 h-5 w-5 rounded flex items-center justify-center hover:bg-destructive/10 transition-opacity cursor-pointer"
            >
              <Trash2 className="h-3 w-3 text-muted-foreground hover:text-destructive" />
            </button>
          </div>
        ))}
        {sessions.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16 text-muted-foreground">
            <MessageSquare className="h-6 w-6 opacity-20 mb-2" />
            <p className="text-xs">暂无对话记录</p>
          </div>
        )}
      </div>
    </div>
  )
}

export default SessionSidebar
