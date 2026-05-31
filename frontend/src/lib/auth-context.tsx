// AuthProvider：登录态 + 当前用户权限点 + 401 跳转登录（tenant-auth）
//
// 前端是展示层防御：依据后端下发的权限点显隐菜单/按钮，不硬编码角色名；
// 真正鉴权由后端 Guard 强制，前端隐藏 != 后端放行。

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { useNavigate } from 'react-router-dom'
import { authApi, type PermissionItem } from './api'
import {
  clearToken,
  loadToken,
  registerUnauthorizedHandler,
  setToken,
} from './auth'

interface AuthState {
  isAuthenticated: boolean
  isSuperAdmin: boolean
  mustChangePassword: boolean
  permissions: Set<string>
  ready: boolean // 是否已完成初始权限加载
}

interface AuthContextValue extends AuthState {
  login: (username: string, password: string) => Promise<void>
  logout: () => void
  refreshPermissions: () => Promise<void>
  clearMustChangePassword: () => void
  hasPermission: (code: string) => boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

const initialState: AuthState = {
  isAuthenticated: false,
  isSuperAdmin: false,
  mustChangePassword: false,
  permissions: new Set(),
  ready: false,
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate()
  const [state, setState] = useState<AuthState>(initialState)

  // 401 处理：清登录态 + 跳登录页
  useEffect(() => {
    registerUnauthorizedHandler(() => {
      setState({ ...initialState, ready: true })
      navigate('/login', { replace: true })
    })
  }, [navigate])

  const refreshPermissions = useCallback(async () => {
    if (!loadToken()) {
      setState({ ...initialState, ready: true })
      return
    }
    try {
      const me = await authApi.myPermissions()
      setState((prev) => ({
        ...prev,
        isAuthenticated: true,
        isSuperAdmin: me.is_super_admin,
        permissions: new Set(me.permissions.map((p: PermissionItem) => p.code)),
        ready: true,
      }))
    } catch {
      // 拉取失败（如 token 失效）：交由 401 处理；这里兜底为未登录
      setState({ ...initialState, ready: true })
    }
  }, [])

  // 首次挂载：若有持久化 token，加载权限
  useEffect(() => {
    void refreshPermissions()
  }, [refreshPermissions])

  const login = useCallback(
    async (username: string, password: string) => {
      const res = await authApi.login(username, password)
      setToken(res.access_token)
      setState((prev) => ({
        ...prev,
        isAuthenticated: true,
        isSuperAdmin: res.is_super_admin,
        mustChangePassword: res.must_change_password,
        ready: true,
      }))
      // 登录后拉权限（若需强制改密，权限拉取可能受限，由路由守卫处理）
      if (!res.must_change_password) {
        await refreshPermissions()
      }
    },
    [refreshPermissions],
  )

  const logout = useCallback(() => {
    clearToken()
    setState({ ...initialState, ready: true })
    navigate('/login', { replace: true })
  }, [navigate])

  const clearMustChangePassword = useCallback(() => {
    setState((prev) => ({ ...prev, mustChangePassword: false }))
  }, [])

  const hasPermission = useCallback(
    (code: string) => state.isSuperAdmin || state.permissions.has(code),
    [state.isSuperAdmin, state.permissions],
  )

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      login,
      logout,
      refreshPermissions,
      clearMustChangePassword,
      hasPermission,
    }),
    [state, login, logout, refreshPermissions, clearMustChangePassword, hasPermission],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用')
  return ctx
}
