/**
 * 复制文本到剪贴板的公用方法。
 *
 * 兼容性说明：
 * - `navigator.clipboard` 仅在安全上下文（HTTPS 或 localhost）下可用。
 *   在 HTTP 部署环境、旧版 Chrome（如 86）、火狐等浏览器下，
 *   `navigator.clipboard` 可能为 undefined，直接调用会抛错或静默失败。
 * - 因此这里优先使用现代 Clipboard API，失败时降级到 `document.execCommand("copy")`。
 *
 * @param text 待复制的文本
 * @returns 是否复制成功
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  // 优先使用现代 Clipboard API（仅安全上下文可用）
  if (
    typeof navigator !== "undefined" &&
    navigator.clipboard &&
    window.isSecureContext
  ) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 失败则继续走降级方案
    }
  }

  // 降级方案：临时 textarea + execCommand("copy")
  return fallbackCopy(text)
}

/**
 * 降级复制方案：临时元素 + Range/Selection + execCommand("copy")。
 *
 * 为什么不用 textarea.select()：
 * - textarea/input 的 select() 依赖元素自身处于聚焦状态。当复制发生在 Dialog/Modal
 *   内时，reka-ui / Radix 的 Focus Trap 会在 focusin 时把焦点抢回弹窗，
 *   导致 textarea 的选区被清空。此时 execCommand("copy") 仍会返回 true
 *   （命令本身可用），但实际剪贴板内容为空，表现为“复制成功却没复制到东西”。
 *
 * 解决方式：
 * - 改用 Range + window.getSelection() 选中一个普通（非表单、不可聚焦）元素的文本。
 *   文档级 Selection 不依赖元素焦点，Focus Trap 抢焦点不会清空它。
 * - 元素挂载到当前打开的弹窗容器内（若存在），进一步避免被 Focus Trap 干扰。
 * - 复制后通过读取 selection 是否仍有内容来辅助判断，execCommand 返回值不可靠。
 */
function fallbackCopy(text: string): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    if (typeof document === "undefined") {
      resolve(false)
      return
    }
    if (typeof document.execCommand !== "function") {
      resolve(false)
      return
    }

    // 优先挂载到当前打开的弹窗内，避免 Focus Trap 把焦点/选区限制在弹窗内时丢失选区
    const host =
      (document.querySelector(
        '[role="dialog"][data-state="open"]',
      ) as HTMLElement | null) ?? document.body

    const span = document.createElement("span")
    span.textContent = text
    // 保留换行与空白，避免多行文本被折叠
    span.style.whiteSpace = "pre"
    // 视觉上隐藏但仍可被 Range 选中（不能用 display:none / visibility:hidden）
    span.style.position = "fixed"
    span.style.top = "0"
    span.style.left = "0"
    span.style.opacity = "0"
    span.style.pointerEvents = "none"
    // iOS Safari：font-size < 16px 触发自动缩放
    span.style.fontSize = "16px"
    host.appendChild(span)

    const selection = window.getSelection()
    const previousRange =
      selection && selection.rangeCount > 0
        ? selection.getRangeAt(0).cloneRange()
        : null

    const range = document.createRange()
    range.selectNodeContents(span)
    selection?.removeAllRanges()
    selection?.addRange(range)

    let ok = false
    try {
      ok = document.execCommand("copy")
    } catch {
      ok = false
    }

    // 清理：移除临时元素并恢复用户原有选区
    span.remove()
    selection?.removeAllRanges()
    if (previousRange) {
      selection?.addRange(previousRange)
    }

    resolve(ok)
  })
}
