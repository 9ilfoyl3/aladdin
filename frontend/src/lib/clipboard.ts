/**
 * 跨环境剪贴板复制（兼容 HTTP 部署）。
 *
 * navigator.clipboard.writeText 仅在安全上下文（HTTPS / localhost）下可用；
 * 非安全上下文（如 http://IP:port）下调用会 reject 或不存在。
 *
 * 关键点：浏览器要求 execCommand('copy') 必须在用户手势（点击）的同步调用栈内执行。
 * 若先 `await` 一个会 reject 的 clipboard.writeText，再降级 execCommand，
 * 此时用户手势上下文已失效，降级同样静默失败。
 * 因此这里在“非安全上下文”下直接走同步的 execCommand 降级，不经过 await。
 */
function legacyCopy(text: string): boolean {
  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    // 避免页面滚动 / 闪烁，同时保证元素可被选中（不能用 display:none）
    textarea.style.position = 'fixed'
    textarea.style.left = '-9999px'
    textarea.style.top = '0'
    textarea.style.opacity = '0'
    textarea.setAttribute('readonly', '')
    document.body.appendChild(textarea)
    textarea.focus()
    textarea.select()
    // iOS Safari 需要显式设置选区
    textarea.setSelectionRange(0, text.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(textarea)
    return ok
  } catch {
    return false
  }
}

export async function copyToClipboard(text: string): Promise<boolean> {
  // 仅在安全上下文下使用现代异步 API；否则直接走同步降级，保住用户手势上下文。
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 安全上下文下仍可能因权限失败，继续尝试降级
    }
  }
  return legacyCopy(text)
}
