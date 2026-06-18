// 实体详情抽屉（design.md 5.3.2 单击 / 5.3.3 懒加载）。
//
// 右侧抽屉，展示单击节点后懒加载的实体详情（属性 / 别名 / 邻居 / 关联原文 chunk）。
// 数据由 store.selected 提供（store.selectNode 调 /graph/entity/{id} 懒加载），
// 本组件纯展示：点击邻居经 onPivotNeighbor 上抛（→ store.loadEgo），关闭经 onClose。

import { ChevronRight, Loader2, X } from 'lucide-react'

import type { GraphEntityDetail } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

import { colorForType } from './graphColors'

interface Props {
  /** 实体详情（懒加载结果）；null 表示无选中（抽屉关闭） */
  detail: GraphEntityDetail | null
  /** 详情加载中 */
  loading: boolean
  /** 关闭抽屉 */
  onClose: () => void
  /** 点击邻居：以该邻居为中心 pivot ego */
  onPivotNeighbor: (entityId: string) => void
}

/**
 * 实体详情抽屉。detail 为 null 时不渲染（由父组件控制挂载亦可，这里双保险）。
 */
export default function GraphEntityDrawer({
  detail,
  loading,
  onClose,
  onPivotNeighbor,
}: Props) {
  // 加载中但尚无数据：展示骨架占位（避免抽屉空白闪烁）。
  const showLoading = loading && !detail
  if (!detail && !showLoading) return null

  return (
    <div className="absolute right-0 top-0 z-20 flex h-full w-80 flex-col border-l border-border bg-card shadow-xl">
      {/* 头部：实体名 + 类型 + 关闭 */}
      <div className="flex items-start justify-between gap-2 border-b border-border p-4">
        <div className="min-w-0">
          {detail ? (
            <>
              <p className="truncate text-base font-semibold text-foreground" title={detail.name}>
                {detail.name}
              </p>
              <div className="mt-1.5 flex items-center gap-2">
                <span
                  className="h-2.5 w-2.5 rounded-full"
                  style={{ backgroundColor: colorForType(detail.type) }}
                />
                <span className="text-xs text-muted-foreground">{detail.type}</span>
                <span className="text-xs text-muted-foreground">· 度数 {detail.degree}</span>
              </div>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">加载实体详情…</p>
          )}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 rounded-md p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
          title="关闭"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      {/* 内容区滚动 */}
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {showLoading ? (
          <div className="flex items-center justify-center py-10">
            <Loader2 className="h-5 w-5 animate-spin text-primary" />
          </div>
        ) : detail ? (
          <div className="space-y-5">
            {/* 别名 */}
            {detail.aliases.length > 0 && (
              <Section title="别名">
                <div className="flex flex-wrap gap-1.5">
                  {detail.aliases.map((a) => (
                    <Badge key={a} variant="secondary" className="font-normal">
                      {a}
                    </Badge>
                  ))}
                </div>
              </Section>
            )}

            {/* 属性 */}
            {detail.attributes.length > 0 && (
              <Section title="属性">
                <ul className="space-y-1">
                  {detail.attributes.map((attr, i) => (
                    <li key={i} className="text-sm leading-relaxed text-foreground/90">
                      · {attr}
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            {/* 邻居（可点击 pivot） */}
            {detail.neighbors.length > 0 && (
              <Section title={`邻居（${detail.neighbors.length}）`}>
                <ul className="space-y-1">
                  {detail.neighbors.map((n) => (
                    <li key={n.id}>
                      <button
                        type="button"
                        onClick={() => onPivotNeighbor(n.id)}
                        className="group flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-muted"
                        title={`以「${n.name}」为中心展开`}
                      >
                        <span className="flex min-w-0 items-center gap-2">
                          <span
                            className="h-2 w-2 shrink-0 rounded-full"
                            style={{ backgroundColor: colorForType(n.type) }}
                          />
                          <span className="truncate text-sm text-foreground">{n.name}</span>
                          <span className="shrink-0 text-xs text-muted-foreground">
                            {n.rel_type}
                          </span>
                        </span>
                        <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
                      </button>
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            {/* 关联原文 chunk 预览 */}
            {detail.chunks.length > 0 && (
              <Section title={`关联原文（${detail.chunks.length}）`}>
                <ul className="space-y-2">
                  {detail.chunks.map((c) => (
                    <li
                      key={c.chunk_id}
                      className="rounded-md border border-border bg-muted/30 p-2.5 text-xs leading-relaxed text-foreground/80"
                    >
                      {c.content_preview || '（无预览）'}
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            {/* 全空兜底 */}
            {detail.aliases.length === 0 &&
              detail.attributes.length === 0 &&
              detail.neighbors.length === 0 &&
              detail.chunks.length === 0 && (
                <p className="py-6 text-center text-sm text-muted-foreground">
                  该实体暂无更多详情。
                </p>
              )}
          </div>
        ) : null}
      </div>
    </div>
  )
}

// 抽屉内的小节（标题 + 内容），统一间距与标题样式。
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <p className={cn('mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground')}>
        {title}
      </p>
      {children}
    </div>
  )
}
