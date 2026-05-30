import { Loader2 } from "lucide-react"
import { cn } from "@/lib/utils"

interface SpinnerProps {
  className?: string
  /** 尺寸预设，默认 md */
  size?: "sm" | "md" | "lg"
}

const sizeMap = {
  sm: "h-3.5 w-3.5",
  md: "h-4 w-4",
  lg: "h-8 w-8",
} as const

/**
 * 统一的旋转加载图标。用于按钮内联 loading、局部小范围加载等场景，
 * 保持全站「转圈」样式一致（统一用 Loader2 + primary 色）。
 */
function Spinner({ className, size = "md" }: SpinnerProps) {
  return (
    <Loader2
      className={cn("animate-spin text-primary", sizeMap[size], className)}
    />
  )
}

interface LoadingStateProps {
  /** 提示文案，默认「加载中...」 */
  label?: string
  className?: string
}

/**
 * 居中的整块加载态（图标 + 文案）。
 * 用于无法用骨架屏精确还原结构、或加载极快的次要区域。
 * 列表/卡片类页面优先使用对应的骨架屏组件。
 */
function LoadingState({ label = "加载中...", className }: LoadingStateProps) {
  return (
    <div
      className={cn(
        "flex items-center justify-center py-20 animate-in fade-in-0 duration-300",
        className
      )}
    >
      <div className="text-center">
        <Spinner size="lg" className="mx-auto mb-3" />
        <p className="text-sm text-muted-foreground">{label}</p>
      </div>
    </div>
  )
}

export { Spinner, LoadingState }
