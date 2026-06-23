// AuthProvider：登录态 + 当前用户固定角色（admin/member）+ 401 跳转登录（tenant-rbac-refactor）
//
// 前端是展示层防御：依据当前用户的固定角色（admin/member）与「是否资源所有者」推导
// 菜单/按钮显隐，不再依赖后端下发的权限点集合；真正鉴权由后端 Guard 强制，
// 前端隐藏 != 后端放行。

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
import { authApi, type MeProfile } from './api'
import {
  TOKEN_KEY,
  clearToken,
  loadToken,
  registerUnauthorizedHandler,
  setToken,
} from './auth'

interface AuthState {
  isAuthenticated: boolean
  isSuperAdmin: boolean
  mustChangePassword: boolean
  // 固定角色：admin（租户管理员）/ member（普通成员）；Super_Admin 为 null（无租户角色）
  role: 'admin' | 'member' | null
  userId: string | null
  profile: MeProfile | null
  ready: boolean // 是否已完成初始身份加载
}

interface AuthContextValue extends AuthState {
  // 当前用户是否为租户管理员（role === 'admin'）
  isAdmin: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => void
  refreshIdentity: () => Promise<void>
  refreshProfile: () => Promise<void>
  clearMustChangePassword: () => void
  // 归属轴：当前用户是否为某资源的所有者（ownerUserId 等于当前行事主体）
  isOwner: (ownerUserId: string | null) => boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

const initialState: AuthState = {
  isAuthenticated: false,
  isSuperAdmin: false,
  mustChangePassword: false,
  role: null,
  userId: null,
  profile: null,
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

  const refreshIdentity = useCallback(async () => {
    if (!loadToken()) {
      setState({ ...initialState, ready: true })
      return
    }
    try {
      const me = await authApi.me()
      // 资料拉取失败不致命（如刚登录权限受限）；尽力而为
      let profile: MeProfile | null = null
      try {
        profile = await authApi.myProfile()
      } catch {
        profile = null
      }
      setState((prev) => ({
        ...prev,
        isAuthenticated: true,
        isSuperAdmin: me.is_super_admin,
        role: me.role,
        userId: me.user_id || null,
        profile,
        ready: true,
      }))
    } catch {
      // 拉取失败（如 token 失效）：交由 401 处理；这里兜底为未登录
      setState({ ...initialState, ready: true })
    }
  }, [])

  const refreshProfile = useCallback(async () => {
    if (!loadToken()) return
    try {
      const profile = await authApi.myProfile()
      setState((prev) => ({ ...prev, profile }))
    } catch {
      /* 忽略：资料拉取失败不影响主流程 */
    }
  }, [])

  // 首次挂载：若有持久化 token，加载身份
  useEffect(() => {
    void refreshIdentity()
  }, [refreshIdentity])

  // 跨标签登录态同步（单点登录）：localStorage 的 token 在其它标签页变化时，
  // 本标签页随之同步——他处登录则本页加载身份，他处登出则本页转未登录并跳登录页。
  useEffect(() => {
    function onStorage(e: StorageEvent) {
      // 仅响应 token 键（key 为 null 表示 clear()，也需处理）
      if (e.key !== null && e.key !== TOKEN_KEY) return
      const current = localStorage.getItem(TOKEN_KEY)
      // 同步内存缓存到最新值
      setToken(current)
      if (current) {
        void refreshIdentity()
      } else {
        setState({ ...initialState, ready: true })
        navigate('/login', { replace: true })
      }
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [refreshIdentity, navigate])

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
      // 登录后拉身份（若需强制改密，身份拉取可能受限，由路由守卫处理）
      if (!res.must_change_password) {
        await refreshIdentity()
      }
    },
    [refreshIdentity],
  )

  const logout = useCallback(() => {
    clearToken()
    setState({ ...initialState, ready: true })
    navigate('/login', { replace: true })
  }, [navigate])

  const clearMustChangePassword = useCallback(() => {
    setState((prev) => ({ ...prev, mustChangePassword: false }))
  }, [])

  // 归属轴判定：ownerUserId 非空且等于当前用户 id
  const isOwner = useCallback(
    (ownerUserId: string | null) => ownerUserId !== null && ownerUserId === state.userId,
    [state.userId],
  )

  const isAdmin = useMemo(() => state.role === 'admin', [state.role])

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      isAdmin,
      login,
      logout,
      refreshIdentity,
      refreshProfile,
      clearMustChangePassword,
      isOwner,
    }),
    [state, isAdmin, login, logout, refreshIdentity, refreshProfile, clearMustChangePassword, isOwner],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用')
  return ctx
}
