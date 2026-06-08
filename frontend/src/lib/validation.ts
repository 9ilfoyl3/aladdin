// 前端输入校验（与后端 app/auth/validators.py 规则保持一致）。
// 前端是体验防御，真正校验仍由后端强制；此处提前拦截明显错误，减少无谓请求。

// 用户名：首字符为中文/字母/数字，整体 1–32，正文允许中文、字母数字与 _ . -
const USERNAME_RE = /^[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9_.\-\u4e00-\u9fff]{0,31}$/

export function validateUsername(username: string): string | null {
  const name = username.trim()
  if (name.length < 1 || name.length > 32) return '用户名长度需为 1–32 个字符'
  if (!USERNAME_RE.test(name)) return '用户名只能含中文、字母、数字、下划线、点、连字符，且不能以点/下划线/连字符开头'
  return null
}

export function validatePassword(password: string): string | null {
  if (password.length < 8 || password.length > 64) return '密码长度需为 8–64 个字符'
  // UTF-8 字节数（bcrypt 上限 72）
  if (new TextEncoder().encode(password).length > 72) return '密码过长（编码后不得超过 72 字节）'
  if (/[\x00-\x1F\x7F]/.test(password)) return '密码不能包含控制字符'
  const hasAlpha = /[A-Za-z]/.test(password)
  const hasDigit = /\d/.test(password)
  if (!hasAlpha || !hasDigit) return '密码需至少同时包含字母与数字'
  return null
}

export function validateTenantName(name: string): string | null {
  const n = name.trim()
  if (n.length < 1 || n.length > 64) return '租户名长度需为 1–64 个字符'
  return null
}
