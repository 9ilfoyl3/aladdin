// 类型图例 + 过滤 + fit-to-view（design.md 5.3.2 / 5.3.3）。
//
// 顶部图例：按当前图中出现的实体类型列出色块，点击切换该类型在画布的本地显隐
// （design.md 5.3.2「本地过滤 + 重算可见边」，不重新拉数据）。另含 fit-to-view 按钮。
//
// 纯展示 + 回调上抛：类型显隐切换经 onToggleType（→ store.toggleTypeVisibility），
// fit 经 onFit（→ 父组件命令式 canvas.fitToView）。组件内不发起请求。

import { Maximize2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

import { colorForType } from './graphColors'

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

  return (
    <div className="pointer-events-none absolute left-3 top-3 z-10 flex max-w-[calc(100%-1.5rem)] flex-wrap items-center gap-1.5">
      {types.map((type) => {
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
