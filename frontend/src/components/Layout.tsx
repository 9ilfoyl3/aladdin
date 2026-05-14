import { NavLink, Outlet } from 'react-router-dom'
import {
  Database,
  MessageSquare,
  Search,
  Key,
  Settings,
  Cpu,
} from 'lucide-react'
import { cn } from '@/lib/utils'

// 侧边栏导航项配置
const navItems = [
  { to: '/knowledge-bases', label: '知识库', icon: Database },
  { to: '/chat', label: '对话', icon: MessageSquare },
  { to: '/retrieval', label: '检索测试', icon: Search },
  { to: '/models', label: '模型管理', icon: Cpu },
  { to: '/api-keys', label: 'API Key', icon: Key },
  { to: '/settings', label: '系统配置', icon: Settings },
]

// 布局组件：侧边栏 + 主内容区
function Layout() {
  return (
    <div className="flex h-screen">
      {/* 侧边栏 */}
      <aside className="w-60 border-r border-sidebar-border bg-sidebar flex flex-col">
        <div className="p-4 border-b border-sidebar-border">
          <h1 className="text-lg font-semibold text-sidebar-foreground">Agentic RAG</h1>
        </div>
        <nav className="flex-1 p-2 space-y-1">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors',
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
        </nav>
      </aside>

      {/* 主内容区 */}
      <main className="flex-1 overflow-auto p-6 bg-background">
        <Outlet />
      </main>
    </div>
  )
}

export default Layout
