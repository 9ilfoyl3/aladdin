import { Skeleton } from "@/components/ui/skeleton"

interface ChunkListSkeletonProps {
  /** 占位切片数量，默认 4 */
  count?: number
}

/**
 * 文档切片列表骨架屏。
 * 对齐 ChunkViewer 的切片卡片：头部（序号 + 字符数）+ 多行内容。
 */
function ChunkListSkeleton({ count = 4 }: ChunkListSkeletonProps) {
  return (
    <div className="space-y-3 animate-in fade-in-0 duration-300">
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl border border-border/60 bg-card overflow-hidden"
        >
          {/* 切片头部 */}
          <div className="flex items-center gap-2 px-4 py-2.5 border-b border-border/40 bg-muted/20">
            <Skeleton className="w-6 h-6 rounded-md" />
            <Skeleton className="h-3.5 w-16" />
          </div>
          {/* 切片内容 */}
          <div className="px-4 py-3 space-y-2">
            <Skeleton className="h-3.5 w-full" />
            <Skeleton className="h-3.5 w-11/12" />
            <Skeleton className="h-3.5 w-4/5" />
          </div>
        </div>
      ))}
    </div>
  )
}

export default ChunkListSkeleton
