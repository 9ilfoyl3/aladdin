import { Skeleton } from "@/components/ui/skeleton"

interface TableSkeletonProps {
  /** 行数，默认 5 */
  rows?: number
  /** 列数，默认 6 */
  columns?: number
}

/**
 * 表格骨架屏。结构对齐 API Key 等表格列表。
 */
function TableSkeleton({ rows = 5, columns = 6 }: TableSkeletonProps) {
  return (
    <div className="border rounded-lg overflow-hidden">
      {/* 表头 */}
      <div className="flex items-center gap-4 px-4 py-3 border-b bg-muted/30">
        {Array.from({ length: columns }).map((_, i) => (
          <Skeleton key={i} className="h-4 flex-1" />
        ))}
      </div>
      {/* 行 */}
      {Array.from({ length: rows }).map((_, r) => (
        <div
          key={r}
          className="flex items-center gap-4 px-4 py-3.5 border-b border-border/50 last:border-0"
        >
          {Array.from({ length: columns }).map((_, c) => (
            <Skeleton
              key={c}
              className="h-4 flex-1"
              style={{ maxWidth: c === columns - 1 ? "4rem" : undefined }}
            />
          ))}
        </div>
      ))}
    </div>
  )
}

export default TableSkeleton
