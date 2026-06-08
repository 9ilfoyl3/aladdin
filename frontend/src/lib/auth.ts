// 前端鉴权基础设施（tenant-auth）
// - JWT 存取（内存 + sessionStorage 持久化）
// - 统一注入 Authorization 头
// - 401/403 处理钩子（由 AuthProvider 注册）
//
// 注意：前端是展示层防御，不是安全边界——真正鉴权由后端 Guard 强制。
//
// 存储选型：使用 sessionStorage 而非 localStorage。
// 原因：sessionStorage 按"浏览器标签页"隔离，使得同一浏览器可在不同标签页
// 分别登录不同用户（超管 / 租户管理员 / 普通用户），便于多角色并行测试与使用，
// 且避免多标签共享同一 token 互相覆盖导致的登录态错乱。
// 代价：新开一个空白标签页需重新登录（同标签页内刷新仍保持登录）。

const TOKEN_KEY = 'artoo.jwt'

let _token: string | null = null

// 401 处理回调（登出并跳登录页）。由 AuthProvider 注册，避免与 React 路由耦合。
let _onUnauthorized: (() => void) | null = null

export function loadToken(): string | null {
  if (_token === null) {
    try {
      _token = sessionStorage.getItem(TOKEN_KEY)
    } catch {
      _token = null
    }
  }
  return _token
}

export function setToken(token: string | null): void {
  _token = token
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    /* sessionStorage 不可用时仅保留内存态 */
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
