import { Skeleton } from "@/components/ui/skeleton"

interface RetrievalResultsSkeletonProps {
  /** 占位结果数量，默认 4 */
  count?: number
}

/**
 * 检索结果骨架屏。
 * 对齐 Retrieval 页面：顶部概览（结果数 + 耗时）+ 扁平结果卡片（文件名行 + 正文）。
 */
function RetrievalResultsSkeleton({ count = 4 }: RetrievalResultsSkeletonProps) {
  return (
    <div className="animate-in fade-in-0 duration-300">
      {/* 概览头 */}
      <div className="flex items-baseline justify-between mb-4">
        <Skeleton className="h-7 w-28" />
        <Skeleton className="h-3.5 w-16" />
      </div>
      {/* 结果列表 */}
      <div className="space-y-2.5">
        {Array.from({ length: count }).map((_, i) => (
          <div
            key={i}
            className="rounded-xl border border-border/60 bg-card shadow-sm p-4"
          >
            {/* 顶部行：文件名 + 分数 */}
            <div className="flex items-center justify-between mb-2.5">
              <div className="flex items-center gap-2">
                <Skeleton className="h-3.5 w-5" />
                <Skeleton className="h-3.5 w-44" />
                <Skeleton className="h-4 w-10 rounded-md" />
              </div>
              <Skeleton className="h-5 w-12" />
            </div>
            {/* 正文 */}
            <div className="space-y-2">
              <Skeleton className="h-3.5 w-full" />
              <Skeleton className="h-3.5 w-11/12" />
              <Skeleton className="h-3.5 w-2/3" />
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export default RetrievalResultsSkeleton
