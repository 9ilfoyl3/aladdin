import { Skeleton } from "@/components/ui/skeleton"

interface CardGridSkeletonProps {
  /** 占位卡片数量，默认 6 */
  count?: number
}

/**
 * 卡片网格骨架屏。
 * 结构对齐知识库/模型/Agent 预设/OCR 服务等卡片：图标 + 标题 + 描述 + 底部操作行。
 */
function CardGridSkeleton({ count = 6 }: CardGridSkeletonProps) {
  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl border border-border bg-card p-5"
        >
          {/* 图标 */}
          <Skeleton className="w-10 h-10 rounded-lg mb-3" />
          {/* 标题 */}
          <Skeleton className="h-5 w-2/5 mb-3" />
          {/* 信息行 */}
          <div className="space-y-2 mb-4">
            <Skeleton className="h-3.5 w-4/5" />
            <Skeleton className="h-3.5 w-3/5" />
            <Skeleton className="h-3.5 w-1/2" />
          </div>
          {/* 底部操作行 */}
          <div className="flex items-center gap-2 pt-3 border-t border-border/60">
            <Skeleton className="h-8 w-16 rounded-md" />
            <Skeleton className="h-8 w-16 rounded-md" />
            <Skeleton className="h-8 w-16 rounded-md" />
          </div>
        </div>
      ))}
    </div>
  )
}

export default CardGridSkeleton
