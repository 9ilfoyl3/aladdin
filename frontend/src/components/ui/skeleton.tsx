import { type HTMLAttributes } from "react"
import { cn } from "@/lib/utils"

/**
 * 骨架屏基础块（shadcn 风格）
 * 通过 className 控制尺寸/圆角，pulse 动画提供平滑的占位反馈。
 */
function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("animate-pulse rounded-md bg-muted-foreground/15", className)}
      {...props}
    />
  )
}

export { Skeleton }
