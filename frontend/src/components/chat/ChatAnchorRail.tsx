import { useEffect, useState } from 'react'
import { ChevronUp, ChevronDown } from 'lucide-react'
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

// 单个锚点：对应一轮用户提问（messages 中的下标 + 问题文本）
export interface QueryAnchor {
  index: number
  text: string
}

interface ChatAnchorRailProps {
  /** 全部用户提问锚点（按对话顺序） */
  anchors: QueryAnchor[]
  /** 当前视口内激活的锚点 message 下标 */
  activeIndex: number
  /** 点击锚点：滚动定位到对应提问 */
  onJump: (index: number) => void
}

// 一屏最多展示的圆点数；超出用上下箭头 / 滚轮在区域内查看
const MAX_DOTS = 10

/**
 * 侧边锚点导航（GLM 风格）：以每轮用户提问作为一个圆点，竖直排列在对话区右侧。
 * - 最多展示 10 个，超出用上/下箭头或在圆点区域滚轮滚动查看；
 * - 当前所在轮自动高亮并保持在可视窗口内；
 * - 悬浮显示该轮 query 的 tooltip，点击定位到对应提问。
 *
 * 仅负责「展示锚点 + 派发跳转」，滚动状态与定位逻辑由 Chat 页统一持有，保持数据流单向清晰。
 */
function ChatAnchorRail({ anchors, activeIndex, onJump }: ChatAnchorRailProps) {
  const total = anchors.length
  const hasOverflow = total > MAX_DOTS
  // 可视窗口起点（仅在溢出时生效）
  const [windowStart, setWindowStart] = useState(0)

  // 激活项变化时，确保其落在可视窗口内（窗口随当前轮滚动）
  useEffect(() => {
    if (!hasOverflow) return
    const pos = anchors.findIndex((a) => a.index === activeIndex)
    if (pos < 0) return
    setWindowStart((prev) => {
      if (pos < prev) return pos
      if (pos >= prev + MAX_DOTS) return pos - MAX_DOTS + 1
      return prev
    })
  }, [activeIndex, anchors, hasOverflow])

  // 锚点总数变化时收敛窗口起点，避免越界留白
  useEffect(() => {
    setWindowStart((prev) => Math.max(0, Math.min(prev, Math.max(0, total - MAX_DOTS))))
  }, [total])

  // 少于 2 轮无需导航
  if (total < 2) return null

  const maxStart = Math.max(0, total - MAX_DOTS)
  const visible = hasOverflow ? anchors.slice(windowStart, windowStart + MAX_DOTS) : anchors

  const shiftWindow = (delta: number) => {
    setWindowStart((prev) => Math.max(0, Math.min(maxStart, prev + delta)))
  }

  return (
    <TooltipProvider delayDuration={150}>
      <div
        className="pointer-events-auto flex flex-col items-center gap-1 rounded-full bg-background/40 px-1.5 py-2 backdrop-blur-sm transition-opacity duration-300"
        onWheel={(e) => {
          if (!hasOverflow) return
          e.preventDefault()
          shiftWindow(e.deltaY > 0 ? 1 : -1)
        }}
      >
          {hasOverflow && (
            <button
              type="button"
              aria-label="上一组"
              disabled={windowStart === 0}
              onClick={() => shiftWindow(-1)}
              className="flex h-5 w-5 items-center justify-center rounded-full text-muted-foreground/60 transition-all duration-200 hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-30"
            >
              <ChevronUp className="h-3.5 w-3.5" />
            </button>
          )}

          {visible.map((a) => {
            const isActive = a.index === activeIndex
            return (
              <Tooltip key={a.index}>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    aria-label={a.text}
                    onClick={() => onJump(a.index)}
                    className="group flex h-3 w-3 items-center justify-center"
                  >
                    <span
                      className={cn(
                        'rounded-full transition-all duration-300 ease-out',
                        isActive
                          ? 'h-4 w-1.5 bg-primary'
                          : 'h-1.5 w-1.5 bg-muted-foreground/30 group-hover:h-2 group-hover:w-2 group-hover:bg-muted-foreground/70'
                      )}
                    />
                  </button>
                </TooltipTrigger>
                <TooltipContent side="left" className="max-w-xs">
                  <span className="line-clamp-2 text-xs">{a.text}</span>
                </TooltipContent>
              </Tooltip>
            )
          })}

          {hasOverflow && (
            <button
              type="button"
              aria-label="下一组"
              disabled={windowStart >= maxStart}
              onClick={() => shiftWindow(1)}
              className="flex h-5 w-5 items-center justify-center rounded-full text-muted-foreground/60 transition-all duration-200 hover:bg-muted hover:text-foreground disabled:pointer-events-none disabled:opacity-30"
            >
              <ChevronDown className="h-3.5 w-3.5" />
            </button>
          )}
      </div>
    </TooltipProvider>
  )
}

export default ChatAnchorRail
