import { Skeleton } from "@/components/ui/skeleton"

interface RetrievalResultsSkeletonProps {
  /** 占位结果数量，默认 4 */
  count?: number
}

/**
 * 检索结果骨架屏。
 * 对齐 Retrieval 页面的结果卡片：头部（序号 + 文件名 + 分数）+ 命中内容文本。
 */
function RetrievalResultsSkeleton({ count = 4 }: RetrievalResultsSkeletonProps) {
  return (
    <div className="animate-in fade-in-0 duration-300">
      {/* 结果头部 */}
      <div className="flex items-center justify-between mb-3 pb-3 border-b border-border/60">
        <Skeleton className="h-4 w-24" />
        <Skeleton className="h-3.5 w-20" />
      </div>
      {/* 结果列表 */}
      <div className="space-y-2">
        {Array.from({ length: count }).map((_, i) => (
          <div
            key={i}
            className="rounded-lg border border-border/60 bg-card overflow-hidden"
          >
            {/* 结果头 */}
            <div className="flex items-center justify-between px-4 py-2 bg-muted/30 border-b border-border/40">
              <Skeleton className="h-3.5 w-40" />
              <Skeleton className="h-4 w-12" />
            </div>
            {/* 命中内容 */}
            <div className="px-4 py-3 space-y-2">
              <Skeleton className="h-3.5 w-full" />
              <Skeleton className="h-3.5 w-11/12" />
              <Skeleton className="h-3.5 w-3/4" />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default RetrievalResultsSkeleton
