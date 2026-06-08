import { Skeleton } from "@/components/ui/skeleton"

interface SettingsFormSkeletonProps {
  /** 配置分组数量，默认 4 */
  groups?: number
  /** 每组字段数量，默认 4 */
  fieldsPerGroup?: number
}

/**
 * 系统配置表单骨架屏。
 * 对齐 Settings 页面的分组卡片：分组头（图标 + 标题 + 描述）+ 双列字段。
 */
function SettingsFormSkeleton({
  groups = 4,
  fieldsPerGroup = 4,
}: SettingsFormSkeletonProps) {
  return (
    <div className="space-y-5 animate-in fade-in-0 duration-300">
      {Array.from({ length: groups }).map((_, g) => (
        <div key={g} className="rounded-xl border border-border bg-card p-5">
          {/* 分组头部 */}
          <div className="flex items-center gap-3 mb-4">
            <Skeleton className="w-9 h-9 rounded-lg shrink-0" />
            <div className="space-y-1.5">
              <Skeleton className="h-4 w-28" />
              <Skeleton className="h-3 w-44" />
            </div>
          </div>
          {/* 字段（双列） */}
          <div className="grid gap-4 md:grid-cols-2">
            {Array.from({ length: fieldsPerGroup }).map((_, f) => (
              <div key={f} className="space-y-1.5">
                <Skeleton className="h-3 w-20" />
                <Skeleton className="h-9 w-full rounded-md" />
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

export default SettingsFormSkeleton
