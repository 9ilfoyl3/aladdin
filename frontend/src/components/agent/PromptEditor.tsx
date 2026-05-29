import { forwardRef, useImperativeHandle, useRef, type CSSProperties } from 'react'
import { cn } from '@/lib/utils'

export interface PromptEditorHandle {
  /** 在当前光标处插入文本（保留原生撤销栈） */
  insertAtCursor: (text: string) => void
  /** 替换全部内容（保留原生撤销栈） */
  replaceAll: (text: string) => void
  /** 聚焦编辑器 */
  focus: () => void
}

interface PromptEditorProps {
  value: string
  onChange: (value: string) => void
  /** 已知变量名集合（不含花括号），仅这些 {name} 会被高亮 */
  variables: string[]
  placeholder?: string
  rows?: number
  className?: string
}

// 与 textarea 完全一致的排版，保证镜像层与输入层字符严格对齐
const SHARED_TYPO = 'font-mono text-xs leading-relaxed whitespace-pre-wrap break-words'
const PADDING: CSSProperties = { padding: '0.5rem 0.75rem' }

/**
 * 带变量高亮的提示词编辑器。
 *
 * 实现：透明 textarea 叠在等排版的镜像层之上。镜像层把已知的 {variable} 渲染为
 * 高亮 span，textarea 文字透明、光标/选区可见。滚动时命令式同步镜像层的
 * scrollTop/Left（不使用 transform，避免裁剪边界一起平移导致内容溢出）。
 *
 * 编辑保持原生体验：普通输入走浏览器原生撤销栈；程序化修改（插入变量/模板/清空）
 * 通过 document.execCommand('insertText') 走原生编辑管线，使 Win(Ctrl+Z)/
 * Mac(Cmd+Z) 的撤销/重做同样生效。
 */
const PromptEditor = forwardRef<PromptEditorHandle, PromptEditorProps>(
  ({ value, onChange, variables, placeholder, rows = 14, className }, ref) => {
    const textareaRef = useRef<HTMLTextAreaElement>(null)
    const backdropRef = useRef<HTMLDivElement>(null)

    function syncScroll() {
      const ta = textareaRef.current
      const bd = backdropRef.current
      if (!ta || !bd) return
      bd.scrollTop = ta.scrollTop
      bd.scrollLeft = ta.scrollLeft
    }

    // 通过原生编辑管线修改内容，保留撤销栈
    function execInsert(text: string) {
      const ta = textareaRef.current
      if (!ta) return
      ta.focus()
      // execCommand 已废弃但仍是保留原生 undo 栈的通用方案
      const ok = document.execCommand('insertText', false, text)
      if (!ok) {
        // 极端环境 fallback：直接受控更新（撤销栈可能不连续）
        const start = ta.selectionStart ?? ta.value.length
        const end = ta.selectionEnd ?? ta.value.length
        onChange(value.slice(0, start) + text + value.slice(end))
      }
    }

    useImperativeHandle(ref, () => ({
      insertAtCursor: (text: string) => {
        execInsert(text)
      },
      replaceAll: (text: string) => {
        const ta = textareaRef.current
        if (!ta) return
        ta.focus()
        ta.select()
        if (text === '') {
          const ok = document.execCommand('delete', false)
          if (!ok) onChange('')
        } else {
          const ok = document.execCommand('insertText', false, text)
          if (!ok) onChange(text)
        }
      },
      focus: () => textareaRef.current?.focus(),
    }))

    const known = new Set(variables)
    const segments = tokenize(value)

    return (
      <div
        className={cn(
          'relative w-full rounded-md border border-input bg-background',
          'focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2 ring-offset-background',
          className,
        )}
      >
        {/* 高亮镜像层：固定裁剪框，命令式同步滚动，不接收交互 */}
        <div
          ref={backdropRef}
          aria-hidden
          className={cn(SHARED_TYPO, 'pointer-events-none absolute inset-0 overflow-hidden text-foreground')}
          style={PADDING}
        >
          {value === '' && placeholder ? (
            <span className="text-muted-foreground/60">{placeholder}</span>
          ) : (
            <>
              {segments.map((seg, i) =>
                seg.isVar && known.has(seg.name!) ? (
                  <span
                    key={i}
                    className="rounded bg-primary/15 text-primary ring-1 ring-primary/30 px-0.5"
                  >
                    {seg.text}
                  </span>
                ) : (
                  <span key={i}>{seg.text}</span>
                ),
              )}
              {/* 末尾换行兜底，保证最后一行高度与 textarea 对齐 */}
              {'\n'}
            </>
          )}
        </div>

        {/* 透明输入层：真实交互，原生滚动/撤销 */}
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onScroll={syncScroll}
          rows={rows}
          spellCheck={false}
          placeholder={placeholder}
          className={cn(
            SHARED_TYPO,
            'relative block w-full resize-y bg-transparent text-transparent caret-foreground',
            'outline-none overflow-auto placeholder:text-transparent',
          )}
          style={PADDING}
        />
      </div>
    )
  },
)
PromptEditor.displayName = 'PromptEditor'

interface Segment {
  text: string
  isVar: boolean
  name?: string
}

/** 把文本切成普通片段与 {var} 片段；{var} 仅匹配合法标识符，避免误吞 JSON */
function tokenize(text: string): Segment[] {
  const segments: Segment[] = []
  const re = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g
  let last = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) {
      segments.push({ text: text.slice(last, m.index), isVar: false })
    }
    segments.push({ text: m[0], isVar: true, name: m[1] })
    last = m.index + m[0].length
  }
  if (last < text.length) {
    segments.push({ text: text.slice(last), isVar: false })
  }
  return segments
}

export default PromptEditor
