import { Routes, Route, Navigate } from 'react-router-dom'
import Layout from './components/Layout'
import KnowledgeBase from './pages/KnowledgeBase'
import Documents from './pages/Documents'
import Chat from './pages/Chat'
import Retrieval from './pages/Retrieval'
import Settings from './pages/Settings'
import ApiKeys from './pages/ApiKeys'
import Models from './pages/Models'

// 应用根组件：路由配置
function App() {
  return (
    <Routes>
      <Route path="/" element={<Layout />}>
        <Route index element={<Navigate to="/knowledge-bases" replace />} />
        <Route path="knowledge-bases" element={<KnowledgeBase />} />
        <Route path="knowledge-bases/:id" element={<Documents />} />
        <Route path="chat" element={<Chat />} />
        <Route path="retrieval" element={<Retrieval />} />
        <Route path="models" element={<Models />} />
        <Route path="api-keys" element={<ApiKeys />} />
        <Route path="settings" element={<Settings />} />
      </Route>
    </Routes>
  )
}

export default App
