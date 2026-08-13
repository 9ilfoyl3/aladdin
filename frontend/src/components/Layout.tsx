import { useState, useEffect } from 'react'
import { NavLink, Outlet, useLocation, useNavigate, Navigate } from 'react-router-dom'
import {
  Database,
  MessageSquare,
  Search,
  Key,
  Settings,
  Cpu,
  ScanText,
  AudioLines,
  Plug,
  Layers,
  Bot,
  Sparkles,
  SquarePen,
  Trash2,
  PanelLeft,
  LogOut,
  KeyRound,
  Building2,
  Users as UsersIcon,
  Mail,
  ScrollText,
  ChevronUp,
  UserCircle,
} from 'lucide-react'
import { cn } from '@/lib/utils'
import { useSession } from '@/lib/session-context'
import { useConfirm } from '@/lib/confirm-context'
import { useAuth } from '@/lib/auth-context'
import ProfileDialog from '@/components/ProfileDialog'
import SettingsDialog from '@/components/SettingsDialog'
import ArtifactPanel from '@/components/artifact/ArtifactPanel'
import { useArtifactStore } from '@/stores/artifactStore'

// 导航项配置。固定角色模型下不再用权限点驱动可见性，而是按 group + 角色推导：
// - content：内容菜单，member/admin 均可见；
// - manage：租户管理菜单（管人/管资产），仅 admin 可见；
// - capability：平台能力配置菜单（模型/Embedding/OCR/检索测试/API Key），属平台底座，
//   全平台一份，仅 Super_Admin 可见可改（capability-config-to-platform）；
// - platform：平台菜单（租户管理），仅 Super_Admin 可见。
// 审计日志归 manage（admin 可见），但 Super_Admin 经下方 SUPER_ADMIN_MENUS 单独放行。
const navItems = [
  { to: '/knowledge-bases', label: '知识库', icon: Database, group: 'content' },
  { to: '/agent-config', label: '智能体', icon: Bot, group: 'content' },
  { to: '/skills', label: '技能', icon: Sparkles, group: 'content' },
  { to: '/retrieval', label: '检索测试', icon: Search, group: 'capability' },
  { to: '/models', label: '模型管理', icon: Cpu, group: 'capability' },
  { to: '/embed-config', label: 'Embedding', icon: Layers, group: 'capability' },
  { to: '/ocr-services', label: 'OCR 服务', icon: ScanText, group: 'capability' },
  { to: '/asr-services', label: 'ASR 服务', icon: AudioLines, group: 'capability' },
  { to: '/mcp-servers', label: 'MCP 服务', icon: Plug, group: 'capability' },
  { to: '/api-keys', label: 'API Key', icon: Key, group: 'capability' },
  { to: '/tenants', label: '租户管理', icon: Building2, group: 'platform' },
  { to: '/users', label: '用户管理', icon: UsersIcon, group: 'manage' },
  { to: '/invitations', label: '邀请链接', icon: Mail, group: 'manage' },
  { to: '/audit-logs', label: '审计日志', icon: ScrollText, group: 'manage' },
] as const

// 布局组件：侧边栏 + 主内容区
function Layout() {
  const location = useLocation()
  const navigate = useNavigate()
  const isChat = location.pathname === '/chat'
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [accountMenuOpen, setAccountMenuOpen] = useState(false)

  // 路由切换时关闭 Artifact 预览面板：预览内容（会话附件/知识库文档原件）与具体页面绑定，
  // 离开页面后悬浮的预览已失去上下文，应随之收起。
  const closeArtifact = useArtifactStore((s) => s.closeArtifact)
  useEffect(() => {
    closeArtifact()
  }, [location.pathname, closeArtifact])
  const [profileOpen, setProfileOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const confirm = useConfirm()
  const { isSuperAdmin, isAdmin, logout, profile } = useAuth()

  // 菜单可见性（固定角色模型，取代权限点）：
  // - Super_Admin（平台级）：平台菜单（租户管理）+ 平台能力配置（capability）+ 审计日志。
  //   不显示租户级管理（用户/邀请）与内容菜单（超管无租户上下文、不参与内容）。
  // - admin（租户管理员）：租户管理菜单（manage，管人/管资产）+ 内容菜单（content）。
  //   不再显示能力配置（capability，已上收平台）。
  // - member（普通成员）：仅内容菜单（content）。
  const SUPER_ADMIN_MENUS = new Set([
    '/tenants',
    '/audit-logs',
    '/models',
    '/embed-config',
    '/ocr-services',
    '/asr-services',
    '/mcp-servers',
    '/retrieval',
    '/api-keys',
  ])
  const visibleNavItems = navItems.filter((item) => {
    if (isSuperAdmin) return SUPER_ADMIN_MENUS.has(item.to)
    if (item.group === 'platform') return false // 平台菜单仅 Super_Admin
    if (item.group === 'capability') return false // 能力配置仅 Super_Admin（已上收平台）
    if (item.group === 'content') return true // 内容菜单 admin/member 均可见
    return isAdmin // manage 菜单仅 admin
  })

  const {
    sessions,
    currentSessionId,
    setCurrentSessionId,
    handleNewSession,
    handleDeleteSession,
  } = useSession()

  // 内容与菜单一致性守卫：超管为纯平台管理身份，仅允许访问其菜单内的页面
  // （租户管理 / 审计日志）与账号自助页（改密）。系统设置/个人资料已改为账号
  // 菜单弹窗（非路由），不在此列。直接命中知识库/对话等页面时，重定向回
  // "租户管理"，避免出现"左侧无此菜单、右侧却是知识库内容"的错位。
  // 注意：置于所有 hook 调用之后，避免条件式调用 hook。
  // 超管可访问：平台菜单（租户管理/审计日志）、平台能力配置（模型/Embedding/OCR/检索测试/
  // API Key，capability-config-to-platform）、账号自助页（改密）。
  const SUPER_ADMIN_ALLOWED_PATHS = new Set([
    '/tenants',
    '/audit-logs',
    '/change-password',
    '/models',
    '/embed-config',
    '/ocr-services',
    '/asr-services',
    '/mcp-servers',
    '/retrieval',
    '/api-keys',
  ])
  if (isSuperAdmin && !SUPER_ADMIN_ALLOWED_PATHS.has(location.pathname)) {
    return <Navigate to="/tenants" replace />
  }

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

  // 删除会话（统一确认交互）
  async function onDeleteSession(session: { id: string; title: string }, e: React.MouseEvent) {
    e.stopPropagation()
    const ok = await confirm({
      title: '删除对话',
      description: <>确定要删除对话「{session.title}」吗？此操作不可撤销。</>,
    })
    if (ok) handleDeleteSession(session.id, e)
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
            <h1 className="text-lg font-semibold text-sidebar-foreground font-serif">Artoo</h1>
            <button
              className="h-7 w-7 flex items-center justify-center rounded-md text-sidebar-foreground/60 hover:bg-sidebar-accent hover:text-sidebar-foreground transition-colors cursor-pointer"
              onClick={() => setSidebarOpen(false)}
              title="收起侧边栏"
            >
              <PanelLeft className="h-4 w-4" />
            </button>
          </div>

          {/* 常驻按钮区：新对话 + 导航。超管为纯平台管理身份，不使用对话/知识库功能，隐藏新对话。 */}
          <div className="px-3 pt-3 pb-2 space-y-1">
            {!isSuperAdmin && (
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
            )}
            {visibleNavItems.map((item) => (
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

          {/* 历史对话列表（超管不显示，纯平台管理身份不参与对话） */}
          {!isSuperAdmin ? (
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
                  onClick={(e) => onDeleteSession(session, e)}
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
          ) : (
            <div className="flex-1" />
          )}

          {/* 底部：当前登录者（紧凑一栏：头像+用户名+身份）。点击展开账号操作。 */}
          <div className="border-t border-sidebar-border px-3 py-2 relative">
            {/* 展开的操作菜单（个人资料 / 系统设置 / 修改密码 / 退出登录） */}
            {accountMenuOpen && (
              <>
                {/* 点击空白处关闭 */}
                <div className="fixed inset-0 z-10" onClick={() => setAccountMenuOpen(false)} />
                <div className="absolute bottom-full left-3 right-3 mb-1 z-20 rounded-lg border border-sidebar-border bg-sidebar shadow-lg p-1 space-y-0.5">
                  <button
                    onClick={() => { setAccountMenuOpen(false); setProfileOpen(true) }}
                    className="w-full flex items-center gap-2.5 px-3 py-2 rounded-md text-sm text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-foreground transition-colors cursor-pointer"
                  >
                    <UserCircle className="h-4 w-4" />
                    <span>个人资料</span>
                  </button>
                  <button
                    onClick={() => { setAccountMenuOpen(false); setSettingsOpen(true) }}
                    className="w-full flex items-center gap-2.5 px-3 py-2 rounded-md text-sm text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-foreground transition-colors cursor-pointer"
                  >
                    <Settings className="h-4 w-4" />
                    <span>系统设置</span>
                  </button>
                  <button
                    onClick={() => { setAccountMenuOpen(false); navigate('/change-password') }}
                    className="w-full flex items-center gap-2.5 px-3 py-2 rounded-md text-sm text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-foreground transition-colors cursor-pointer"
                  >
                    <KeyRound className="h-4 w-4" />
                    <span>修改密码</span>
                  </button>
                  <button
                    onClick={() => { setAccountMenuOpen(false); logout() }}
                    className="w-full flex items-center gap-2.5 px-3 py-2 rounded-md text-sm text-sidebar-foreground/80 hover:bg-destructive/10 hover:text-destructive transition-colors cursor-pointer"
                  >
                    <LogOut className="h-4 w-4" />
                    <span>退出登录</span>
                  </button>
                </div>
              </>
            )}
            {/* 紧凑一栏 */}
            <button
              onClick={() => setAccountMenuOpen((v) => !v)}
              className="w-full flex items-center gap-2.5 px-2 py-2 rounded-lg hover:bg-sidebar-accent transition-colors cursor-pointer text-left"
              title="账号"
            >
              {profile?.avatar ? (
                <img src={profile.avatar} alt="" className="h-8 w-8 rounded-full object-cover shrink-0" />
              ) : (
                <div className="h-8 w-8 rounded-full bg-sidebar-primary/15 flex items-center justify-center shrink-0 text-sm font-medium text-sidebar-foreground">
                  {(profile?.username ?? '?').slice(0, 1).toUpperCase()}
                </div>
              )}
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-sidebar-foreground">{profile?.username ?? '—'}</div>
                <div className="truncate text-xs text-sidebar-foreground/60">
                  {profile?.role_label ?? '用户'}
                </div>
              </div>
              <ChevronUp className={cn('h-4 w-4 text-sidebar-foreground/50 shrink-0 transition-transform', accountMenuOpen ? '' : 'rotate-180')} />
            </button>
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
              <span className="text-lg font-semibold text-sidebar-foreground font-serif group-hover:hidden">Ar</span>
              <PanelLeft className="h-4 w-4 text-sidebar-foreground hidden group-hover:block" />
            </div>
          </div>

          {/* 导航图标 */}
          <div className="flex flex-col items-center pt-3 px-1">
            {!isSuperAdmin && (
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
            )}
            {visibleNavItems.map((item) => (
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

      {/* 主内容区 + Artifact 预览面板（flex 行：面板占用空间、从右滑入推挤内容） */}
      <main className="flex-1 min-w-0 flex overflow-hidden">
        <div className={cn('flex-1 min-w-0 overflow-auto bg-background', !isChat && 'p-6')}>
          <Outlet />
        </div>
        <ArtifactPanel />
      </main>

      {/* 个人资料弹窗 */}
      <ProfileDialog open={profileOpen} onOpenChange={setProfileOpen} />

      {/* 系统设置弹窗（账号菜单打开）：外观分项所有人可见；切片/检索/平台配置为平台
          能力配置，仅超级管理员可见可改（capability-config-to-platform）。 */}
      <SettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        canManageChunk={isSuperAdmin}
        isSuperAdmin={isSuperAdmin}
      />
    </div>
  )
}

export default Layout
