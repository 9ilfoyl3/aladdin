// 实体搜索框（design.md 5.3.2 搜索定位 / 5.3.3）。
//
// 输入实体名 → 回车/点击搜索 → 经 onSearch 上抛（页面调 store.loadEgo(name) 以该名
// 为中心拉 ego 子图，命中后飞行居中）。组件只持有输入框本地草稿值，不发起请求、
// 不直接操作图状态（数据流单向）。

import { useState } from 'react'
import { Search, X } from 'lucide-react'

import { Input } from '@/components/ui/input'

interface Props {
  /** 提交搜索（实体名）。空串不触发。 */
  onSearch: (name: string) => void
  /** 当前是否处于 ego（搜索/钻取）模式，提供「返回总览」入口 */
  egoActive: boolean
  /** 返回 overview 总览 */
  onBackToOverview: () => void
}

/**
 * 实体搜索框。回车或点击放大镜提交；ego 模式下显示「返回总览」。
 */
export default function GraphSearchBar({ onSearch, egoActive, onBackToOverview }: Props) {
  const [draft, setDraft] = useState('')

  const submit = () => {
    const name = draft.trim()
    if (name) onSearch(name)
  }

  return (
    <div className="absolute right-3 top-3 z-10 flex items-center gap-2">
      <div className="relative">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit()
          }}
          placeholder="搜索实体名…"
          className="h-9 w-56 bg-card/90 pl-8 pr-8 shadow-sm backdrop-blur"
        />
        {draft && (
          <button
            type="button"
            onClick={() => setDraft('')}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            title="清空"
          >
            <X className="h-4 w-4" />
          </button>
        )}
      </div>

      {egoActive && (
        <button
          type="button"
          onClick={onBackToOverview}
          className="h-9 whitespace-nowrap rounded-md border border-border bg-card/90 px-3 text-sm shadow-sm backdrop-blur transition-colors hover:bg-muted"
        >
          返回总览
        </button>
      )}
    </div>
  )
}
