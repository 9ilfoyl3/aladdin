import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import Layout from './components/Layout'
import KnowledgeBase from './pages/KnowledgeBase'
import Documents from './pages/Documents'
import Chat from './pages/Chat'
import Retrieval from './pages/Retrieval'
import Settings from './pages/Settings'
import ApiKeys from './pages/ApiKeys'
import Models from './pages/Models'
import EmbedConfig from './pages/EmbedConfig'
import OcrServices from './pages/OcrServices'
import AgentConfig from './pages/AgentConfig'
import Login from './pages/Login'
import Register from './pages/Register'
import ChangePassword from './pages/ChangePassword'
import Profile from './pages/Profile'
import Tenants from './pages/Tenants'
import Users from './pages/Users'
import AuditLogs from './pages/AuditLogs'
import Invitations from './pages/Invitations'
import InviteAccept from './pages/InviteAccept'
import { useAuth } from './lib/auth-context'

// 路由守卫：未登录跳登录页；强制改密时跳改密页（仅放行改密页本身）。
function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated, mustChangePassword, ready } = useAuth()
  const location = useLocation()

  // 初始权限/登录态加载中：避免闪烁，渲染空白
  if (!ready) return null

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />
  }
  if (mustChangePassword && location.pathname !== '/change-password') {
    return <Navigate to="/change-password" replace />
  }
  return <>{children}</>
}

// 登录后默认落地页：超管为纯平台管理身份（无知识库/对话），落到"租户管理"；
// 其余身份落到知识库。避免超管落到一个其菜单里并不存在的知识库页（右侧内容与左侧菜单不一致）。
function DefaultLanding() {
  const { isSuperAdmin } = useAuth()
  return <Navigate to={isSuperAdmin ? '/tenants' : '/knowledge-bases'} replace />
}

// 应用根组件：路由配置
function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      <Route path="/change-password" element={<ChangePassword />} />
      <Route path="/invite/:token" element={<InviteAccept />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<DefaultLanding />} />
        <Route path="profile" element={<Profile />} />
        <Route path="knowledge-bases" element={<KnowledgeBase />} />
        <Route path="knowledge-bases/:id" element={<Documents />} />
        <Route path="chat" element={<Chat />} />
        <Route path="retrieval" element={<Retrieval />} />
        <Route path="models" element={<Models />} />
        <Route path="agent-config" element={<AgentConfig />} />
        <Route path="embed-config" element={<EmbedConfig />} />
        <Route path="ocr-services" element={<OcrServices />} />
        <Route path="api-keys" element={<ApiKeys />} />
        <Route path="tenants" element={<Tenants />} />
        <Route path="users" element={<Users />} />
        <Route path="invitations" element={<Invitations />} />
        <Route path="audit-logs" element={<AuditLogs />} />
        <Route path="settings" element={<Settings />} />
      </Route>
    </Routes>
  )
}

export default App
