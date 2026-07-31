// 类型图例 + 过滤 + fit-to-view（design.md 5.3.2 / 5.3.3）。
//
// 顶部图例：按当前图中出现的实体类型列出色块，点击切换该类型在画布的本地显隐
// （design.md 5.3.2「本地过滤 + 重算可见边」，不重新拉数据）。另含 fit-to-view 按钮。
//
// 纯展示 + 回调上抛：类型显隐切换经 onToggleType（→ store.toggleTypeVisibility），
// fit 经 onFit（→ 父组件命令式 canvas.fitToView）。组件内不发起请求。

import { Maximize2 } from 'lucide-react'
import { useMemo } from 'react'

import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

import { colorForType, getEventColors, EVENT_LAYER_LABEL } from './graphColors'

interface Props {
  /** 当前图中出现的类型（去重，按出现顺序），用于生成图例项 */
  types: string[]
  /** 本地隐藏的类型集合（点击图例切换） */
  hiddenTypes: string[]
  /** 切换某类型显隐 */
  onToggleType: (type: string) => void
  /** 适应屏幕 */
  onFit: () => void
}

/**
 * 图例栏。每个类型一个可点击的色块 chip：点亮=显示，置灰=隐藏。
 */
export default function GraphLegend({ types, hiddenTypes, onToggleType, onFit }: Props) {
  const hidden = new Set(hiddenTypes)
  // 事件层（后端 type='event'）与实体类型分开渲染：改名「事件脉络」+ 主题事件色，
  // 消除与实体类型「事件」的语义/配色冲突。
  const hasEventLayer = types.includes('event')
  const entityTypes = types.filter((t) => t !== 'event')
  const eventColors = useMemo(() => getEventColors(), [])

  return (
    <div className="pointer-events-none absolute left-3 top-3 z-10 flex max-w-[calc(100%-1.5rem)] flex-wrap items-center gap-1.5">
      {/* 事件层图例：置于首位（事件是图谱中心）。与实体类型同款边框，仅用色块颜色区分。 */}
      {hasEventLayer && (
        <button
          type="button"
          onClick={() => onToggleType('event')}
          className={cn(
            'pointer-events-auto flex items-center gap-1.5 rounded-full border bg-card/90 px-2.5 py-1 text-xs font-medium shadow-sm backdrop-blur transition-opacity',
            hidden.has('event') ? 'opacity-40' : 'opacity-100',
          )}
          title={hidden.has('event') ? `点击显示「${EVENT_LAYER_LABEL}」` : `点击隐藏「${EVENT_LAYER_LABEL}」`}
        >
          <span
            className="h-2.5 w-2.5 rounded-full"
            style={{ backgroundColor: eventColors.fill }}
          />
          <span className={cn(hidden.has('event') && 'line-through')}>{EVENT_LAYER_LABEL}</span>
        </button>
      )}

      {entityTypes.map((type) => {
        const isHidden = hidden.has(type)
        return (
          <button
            key={type}
            type="button"
            onClick={() => onToggleType(type)}
            className={cn(
              'pointer-events-auto flex items-center gap-1.5 rounded-full border bg-card/90 px-2.5 py-1 text-xs font-medium shadow-sm backdrop-blur transition-opacity',
              isHidden ? 'opacity-40' : 'opacity-100',
            )}
            title={isHidden ? `点击显示「${type}」` : `点击隐藏「${type}」`}
          >
            <span
              className="h-2.5 w-2.5 rounded-full"
              style={{ backgroundColor: colorForType(type) }}
            />
            <span className={cn(isHidden && 'line-through')}>{type}</span>
          </button>
        )
      })}

      <Button
        type="button"
        variant="outline"
        size="sm"
        className="pointer-events-auto ml-1 h-7 gap-1.5 bg-card/90 px-2.5 text-xs shadow-sm backdrop-blur"
        onClick={onFit}
        title="适应屏幕"
      >
        <Maximize2 className="h-3.5 w-3.5" />
        适应屏幕
      </Button>
    </div>
  )
}
