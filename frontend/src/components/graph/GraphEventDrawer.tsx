// 事件详情抽屉（事件中心图谱）。
//
// 右侧抽屉，展示单击事件节点后懒加载的事件详情（标题 / 摘要 / 完整内容 / 关联实体 /
// 来源原文预览）。数据由 store.selectedEvent 提供（store.selectEvent 调 /graph/event/{id}
// 懒加载），本组件纯展示：点击关联实体经 onPivotEntity 上抛（→ store.loadEgo），关闭经 onClose。

import { ChevronRight, Loader2, X } from 'lucide-react'

import type { GraphEventDetail } from '@/lib/api'
import { cn } from '@/lib/utils'

import { colorForType, getEventColors, EVENT_LAYER_LABEL } from './graphColors'

interface Props {
  /** 事件详情（懒加载结果）；null 表示无选中（抽屉关闭） */
  detail: GraphEventDetail | null
  /** 详情加载中 */
  loading: boolean
  /** 关闭抽屉 */
  onClose: () => void
  /** 点击关联实体：以该实体为中心 pivot ego */
  onPivotEntity: (entityId: string) => void
}

/**
 * 事件详情抽屉。detail 为 null 且非加载中时不渲染。
 */
export default function GraphEventDrawer({ detail, loading, onClose, onPivotEntity }: Props) {
  const showLoading = loading && !detail
  if (!detail && !showLoading) return null

  const eventColors = getEventColors()
  const title = detail?.title || detail?.summary || '事件'

  return (
    <div className="absolute right-0 top-0 z-20 flex h-full w-80 flex-col border-l border-border bg-card shadow-xl">
      {/* 头部：事件标题 + 事件脉络标记 + 关闭 */}
      <div className="flex items-start justify-between gap-2 border-b border-border p-4">
        <div className="min-w-0">
          {detail ? (
            <>
              <p className="text-base font-semibold leading-snug text-foreground" title={title}>
                {title}
              </p>
              <div className="mt-1.5 flex items-center gap-2">
                <span
                  className="h-2.5 w-2.5 rounded-full"
                  style={{ backgroundColor: eventColors.fill }}
                />
                <span className="text-xs text-muted-foreground">{EVENT_LAYER_LABEL}</span>
                {detail.mentions.length > 0 && (
                  <span className="text-xs text-muted-foreground">
                    · 关联 {detail.mentions.length} 个实体
                  </span>
                )}
              </div>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">加载事件详情…</p>
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
            {/* 摘要 */}
            {detail.summary && detail.summary !== detail.title && (
              <Section title="摘要">
                <p className="text-sm leading-relaxed text-foreground/90">{detail.summary}</p>
              </Section>
            )}

            {/* 完整内容 */}
            {detail.content && (
              <Section title="事件内容">
                <p className="whitespace-pre-wrap text-sm leading-relaxed text-foreground/90">
                  {detail.content}
                </p>
              </Section>
            )}

            {/* 关联实体（可点击 pivot） */}
            {detail.mentions.length > 0 && (
              <Section title={`关联实体（${detail.mentions.length}）`}>
                <ul className="space-y-1">
                  {detail.mentions.map((m) => (
                    <li key={m.id}>
                      <button
                        type="button"
                        onClick={() => onPivotEntity(m.id)}
                        className="group flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-muted"
                        title={`以「${m.name}」为中心展开`}
                      >
                        <span className="flex min-w-0 items-center gap-2">
                          <span
                            className="h-2 w-2 shrink-0 rounded-full"
                            style={{ backgroundColor: colorForType(m.type) }}
                          />
                          <span className="truncate text-sm text-foreground">{m.name}</span>
                          {m.type && (
                            <span className="shrink-0 text-xs text-muted-foreground">{m.type}</span>
                          )}
                        </span>
                        <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
                      </button>
                    </li>
                  ))}
                </ul>
              </Section>
            )}

            {/* 来源原文预览 */}
            {detail.chunk && detail.chunk.content_preview && (
              <Section title="来源原文">
                <div className="rounded-md border border-border bg-muted/30 p-2.5 text-xs leading-relaxed text-foreground/80">
                  {detail.chunk.content_preview}
                </div>
              </Section>
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
