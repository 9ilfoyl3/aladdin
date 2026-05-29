import { useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import {
  Database,
  MessageSquare,
  Search,
  Key,
  Settings,
  Cpu,
  ScanText,
  Layers,
  Bot,
  SquarePen,
  Trash2,
  PanelLeft,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useSession } from '@/lib/session-context'

// 导航项配置
const navItems = [
  { to: '/knowledge-bases', label: '知识库', icon: Database },
  { to: '/retrieval', label: '检索测试', icon: Search },
  { to: '/models', label: '模型管理', icon: Cpu },
  { to: '/agent-config', label: 'Agent 配置', icon: Bot },
  { to: '/embed-config', label: 'Embedding', icon: Layers },
  { to: '/ocr-services', label: 'OCR 服务', icon: ScanText },
  { to: '/api-keys', label: 'API Key', icon: Key },
  { to: '/settings', label: '系统配置', icon: Settings },
]

// 布局组件：侧边栏 + 主内容区
function Layout() {
  const location = useLocation()
  const navigate = useNavigate()
  const isChat = location.pathname === '/chat'
  const [sidebarOpen, setSidebarOpen] = useState(true)

  const {
    sessions,
    currentSessionId,
    setCurrentSessionId,
    handleNewSession,
    handleDeleteSession,
  } = useSession()

  // 点击新对话：跳转到 chat 页面并重置会话
  function onNewSession() {
    handleNewSession()
    navigate('/chat')
  }

  // 点击历史会话：跳转到 chat 页面并切换会话
  function onSwitchSession(sessionId: string) {
    setCurrentSessionId(sessionId)
    navigate('/chat')
  }

  return (
    <div className="flex h-screen">
      {/* 侧边栏 */}
      <aside
        className={cn(
          'shrink-0 border-r border-sidebar-border bg-sidebar flex flex-col transition-[width] duration-200 ease-in-out overflow-hidden',
          sidebarOpen ? 'w-60' : 'w-12'
        )}
      >
        {/* 展开状态内容 */}
        <div
          className={cn(
            'w-60 h-full flex flex-col transition-opacity duration-200',
            sidebarOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'
          )}
        >
          {/* 顶部：标题 + toggle */}
          <div className="flex items-center justify-between px-4 py-3">
            <h1 className="text-lg font-semibold text-sidebar-foreground">Aladdin</h1>
            <button
              className="h-7 w-7 flex items-center justify-center rounded-md text-sidebar-foreground/60 hover:bg-sidebar-accent hover:text-sidebar-foreground transition-colors cursor-pointer"
              onClick={() => setSidebarOpen(false)}
              title="收起侧边栏"
            >
              <PanelLeft className="h-4 w-4" />
            </button>
          </div>

          {/* 常驻按钮区：新对话 + 导航 */}
          <div className="px-3 pt-3 pb-2 space-y-1">
            <button
              onClick={onNewSession}
              className={cn(
                'w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-sm font-medium cursor-pointer transition-colors',
                isChat && (currentSessionId === null)
                  ? 'bg-sidebar-primary text-sidebar-primary-foreground'
                  : 'text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground'
              )}
            >
              <SquarePen className="h-4 w-4" />
              <span>新对话</span>
            </button>
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  cn(
                    'flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-sidebar-primary text-sidebar-primary-foreground'
                      : 'text-sidebar-foreground hover:bg-sidebar-accent hover:text-sidebar-accent-foreground'
                  )
                }
              >
                <item.icon className="h-4 w-4" />
                {item.label}
              </NavLink>
            ))}
          </div>

          {/* 历史对话列表 */}
          <div className="flex-1 overflow-auto px-2 pt-2 pb-2 space-y-0.5">
            <p className="px-3 pt-2 pb-1 text-xs text-sidebar-foreground/85 font-medium">历史对话</p>
            {sessions.map((session) => (
              <div
                key={session.id}
                onClick={() => onSwitchSession(session.id)}
                className={cn(
                  'group flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer transition-colors text-[13px]',
                  currentSessionId === session.id && isChat
                    ? 'bg-sidebar-primary text-sidebar-primary-foreground font-medium'
                    : 'text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground'
                )}
              >
                <span className="flex-1 truncate leading-snug">{session.title}</span>
                <button
                  onClick={(e) => handleDeleteSession(session.id, e)}
                  className={cn(
                    'opacity-0 group-hover:opacity-100 h-5 w-5 rounded flex items-center justify-center transition-opacity cursor-pointer',
                    currentSessionId === session.id && isChat
                      ? 'hover:bg-black/10'
                      : 'hover:bg-destructive/10'
                  )}
                >
                  <Trash2 className={cn(
                    'h-3 w-3',
                    currentSessionId === session.id && isChat
                      ? 'text-sidebar-primary-foreground/70 hover:text-sidebar-primary-foreground'
                      : 'text-muted-foreground hover:text-destructive'
                  )} />
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

        {/* 收起状态内容 */}
        <div
          className={cn(
            'absolute inset-0 w-12 h-full flex flex-col items-center transition-opacity duration-200',
            sidebarOpen ? 'opacity-0 pointer-events-none' : 'opacity-100'
          )}
        >
          {/* 标题缩写 + hover 显示展开图标 */}
          <div
            className="h-[52px] w-full flex items-center justify-center cursor-pointer"
            onClick={() => setSidebarOpen(true)}
            title="展开侧边栏"
          >
            <div className="group h-8 w-8 flex items-center justify-center rounded-md hover:bg-sidebar-accent transition-colors">
              <span className="text-lg font-semibold text-sidebar-foreground group-hover:hidden">Al</span>
              <PanelLeft className="h-4 w-4 text-sidebar-foreground hidden group-hover:block" />
            </div>
          </div>

          {/* 导航图标 */}
          <div className="flex flex-col items-center pt-3 px-1">
            <button
              onClick={onNewSession}
              className="h-10 w-full flex items-center justify-center cursor-pointer"
              title="新对话"
            >
              <div className={cn(
                'h-8 w-8 flex items-center justify-center rounded-md transition-colors',
                isChat && (currentSessionId === null)
                  ? 'bg-sidebar-primary text-sidebar-primary-foreground'
                  : 'text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-foreground'
              )}>
                <SquarePen className="h-4 w-4" />
              </div>
            </button>
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className="h-10 w-full flex items-center justify-center"
                title={item.label}
              >
                {({ isActive }) => (
                  <div className={cn(
                    'h-8 w-8 flex items-center justify-center rounded-md transition-colors',
                    isActive
                      ? 'bg-sidebar-primary text-sidebar-primary-foreground'
                      : 'text-sidebar-foreground/70 hover:bg-sidebar-accent hover:text-sidebar-foreground'
                  )}>
                    <item.icon className="h-4 w-4" />
                  </div>
                )}
              </NavLink>
            ))}
          </div>
        </div>
      </aside>

      {/* 主内容区 */}
      <main className={cn('flex-1 overflow-auto bg-background', !isChat && 'p-6')}>
        <Outlet />
      </main>
    </div>
  )
}

export default Layout
