import { Skeleton } from "@/components/ui/skeleton"

interface DocumentGridSkeletonProps {
  /** 占位文件数量，默认 18 */
  count?: number
}

/**
 * 文档网格骨架屏（Finder 风格）。
 * 对齐 Documents 页面 grid 视图的文件项：缩略图 + 文件名 + 大小。
 */
function DocumentGridSkeleton({ count = 18 }: DocumentGridSkeletonProps) {
  return (
    <div className="grid grid-cols-4 sm:grid-cols-5 md:grid-cols-6 lg:grid-cols-8 xl:grid-cols-9 2xl:grid-cols-10 gap-2 p-2">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="flex flex-col items-center px-2 py-3">
          {/* 缩略图 */}
          <Skeleton className="w-16 h-20 rounded mb-2.5" />
          {/* 文件名 */}
          <Skeleton className="h-3 w-14 mb-1.5" />
          {/* 文件大小 */}
          <Skeleton className="h-2.5 w-8" />
        </div>
      ))}
    </div>
  )
}

export default DocumentGridSkeleton
