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
import ChangePassword from './pages/ChangePassword'
import Tenants from './pages/Tenants'
import Users from './pages/Users'
import Roles from './pages/Roles'
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

// 应用根组件：路由配置
function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
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
        <Route index element={<Navigate to="/knowledge-bases" replace />} />
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
        <Route path="roles" element={<Roles />} />
        <Route path="invitations" element={<Invitations />} />
        <Route path="audit-logs" element={<AuditLogs />} />
        <Route path="settings" element={<Settings />} />
      </Route>
    </Routes>
  )
}

export default App
