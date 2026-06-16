// 前端鉴权基础设施（tenant-auth）
// - JWT 存取（内存 + localStorage 持久化）
// - 统一注入 Authorization 头
// - 401/403 处理钩子（由 AuthProvider 注册）
//
// 注意：前端是展示层防御，不是安全边界——真正鉴权由后端 Guard 强制。
//
// 存储选型：使用 localStorage（单点登录）。
// 同一浏览器内所有标签页共享同一登录态——新开标签页 / 粘贴链接打开都免重新登录，
// 跨标签 storage 事件可使一处登出全局同步。代价是同一浏览器无法并行登录多账号
// （多角色测试请用不同浏览器或隐身窗口）。

export const TOKEN_KEY = 'artoo.jwt'

let _token: string | null = null

// 401 处理回调（登出并跳登录页）。由 AuthProvider 注册，避免与 React 路由耦合。
let _onUnauthorized: (() => void) | null = null

export function loadToken(): string | null {
  if (_token === null) {
    try {
      _token = localStorage.getItem(TOKEN_KEY)
    } catch {
      _token = null
    }
  }
  return _token
}

export function setToken(token: string | null): void {
  _token = token
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* localStorage 不可用时仅保留内存态 */
  }
}

export function clearToken(): void {
  setToken(null)
}

/** 返回带 Authorization 的请求头（无 token 时不附加）。 */
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) }
  const t = loadToken()
  if (t) headers['Authorization'] = `Bearer ${t}`
  return headers
}

export function registerUnauthorizedHandler(fn: () => void): void {
  _onUnauthorized = fn
}

/** 由请求层在收到 401 时调用：清除登录态并触发跳转登录。 */
export function handleUnauthorized(): void {
  clearToken()
  if (_onUnauthorized) _onUnauthorized()
}
