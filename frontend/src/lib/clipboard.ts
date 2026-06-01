/**
 * 跨环境剪贴板复制（兼容 HTTP 部署）。
 *
 * navigator.clipboard.writeText 仅在 HTTPS 或 localhost 下可用；
 * 非安全上下文（如 http://IP:port）会静默失败。
 * 本函数先尝试现代 API，失败后降级为 execCommand('copy')。
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  // 现代 API（HTTPS / localhost）
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 静默失败（非安全上下文），走降级
    }
  }
  // 降级：创建临时 textarea + execCommand
  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.style.position = 'fixed'
    textarea.style.left = '-9999px'
    textarea.style.top = '-9999px'
    document.body.appendChild(textarea)
    textarea.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(textarea)
    return ok
  } catch {
    return false
  }
}
