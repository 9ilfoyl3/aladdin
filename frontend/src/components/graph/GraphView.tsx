// 图谱交互视图（design.md 5.3.2 / 5.3.3）：组装 canvas + 图例 + 搜索 + 抽屉。
//
// 本组件是「有数据」分支的容器：进入后经 store.loadOverview 拉总览，之后所有交互
// （切模式 / 过滤 / 单击 / 双击 / Shift 单击 / 搜索 / fit）都经 graphStore action 触发，
// 组件只读 store 状态渲染并把命令式视图操作（居中 / fit）经 ref 下达给 GraphCanvas。
//
// 数据流单向：UI 事件 → store action → graphApi → 写回 state → 组件重渲染（design.md 5.3.3）。
// 组件内不散落 fetch（懒加载详情亦由 store.selectNode 发起）。

import { useEffect, useMemo, useRef } from 'react'

import { useGraphStore } from '@/stores/graphStore'

import GraphCanvas, { type GraphCanvasHandle } from './GraphCanvas'
import GraphLegend from './GraphLegend'
import GraphSearchBar from './GraphSearchBar'
import GraphEntityDrawer from './GraphEntityDrawer'
import GraphEventDrawer from './GraphEventDrawer'

/**
 * 图谱交互视图。挂载时按 overview 拉总览，承载全部交互编排。
 */
export default function GraphView() {
  const canvasRef = useRef<GraphCanvasHandle>(null)

  // 只订阅需要的切片，减少无关重渲染。
  const kbId = useGraphStore((s) => s.kbId)
  const mode = useGraphStore((s) => s.mode)
  const nodes = useGraphStore((s) => s.nodes)
  const edges = useGraphStore((s) => s.edges)
  const meta = useGraphStore((s) => s.meta)
  const hiddenTypes = useGraphStore((s) => s.hiddenTypes)
  const selected = useGraphStore((s) => s.selected)
  const selectedLoading = useGraphStore((s) => s.selectedLoading)
  const selectedEvent = useGraphStore((s) => s.selectedEvent)
  const selectedEventLoading = useGraphStore((s) => s.selectedEventLoading)
  const loading = useGraphStore((s) => s.loading)

  const loadOverview = useGraphStore((s) => s.loadOverview)
  const loadEgo = useGraphStore((s) => s.loadEgo)
  const selectNode = useGraphStore((s) => s.selectNode)
  const selectEvent = useGraphStore((s) => s.selectEvent)
  const bloomNode = useGraphStore((s) => s.bloomNode)
  const toggleTypeVisibility = useGraphStore((s) => s.toggleTypeVisibility)
  const clearSelected = useGraphStore((s) => s.clearSelected)

  // 进入视图（或切库）后拉总览。kbId 由页面 setKb 注入。
  useEffect(() => {
    if (kbId) loadOverview()
  }, [kbId, loadOverview])

  // 图例类型：取当前图中出现的实体类型（去重，按首次出现顺序）。
  const presentTypes = useMemo(() => {
    const seen = new Set<string>()
    const out: string[] = []
    for (const n of nodes) {
      if (!seen.has(n.type)) {
        seen.add(n.type)
        out.push(n.type)
      }
    }
    return out
  }, [nodes])

  // 单击：选中 + 懒加载详情 + 画布居中（design.md 5.3.2）。
  // 事件层节点走事件详情端点（selectEvent），实体走实体详情端点（selectNode）。
  const handleSingleClick = (entityId: string) => {
    const node = nodes.find((n) => n.id === entityId)
    if (node && node.node_type === 'event') {
      selectEvent(entityId)
      canvasRef.current?.centerNode(entityId)
      return
    }
    selectNode(entityId)
    canvasRef.current?.centerNode(entityId)
  }

  // 双击：pivot 到该节点 ego（重新拉子图，关闭抽屉避免错位）。
  // 事件层节点不作为 ego 中心（后端 ego 以实体为中心），双击退化为居中。
  const handleDoubleClick = (entityId: string) => {
    const node = nodes.find((n) => n.id === entityId)
    if (node && node.node_type === 'event') {
      canvasRef.current?.centerNode(entityId)
      return
    }
    clearSelected()
    loadEgo(entityId)
  }

  // 搜索：以实体名为中心拉 ego（命中后 store 写回，canvas 自动 fit）。
  const handleSearch = (name: string) => {
    clearSelected()
    loadEgo(name)
  }

  // 邻居 pivot：从抽屉点击邻居 → 以该邻居为中心 ego。
  const handlePivotNeighbor = (entityId: string) => {
    clearSelected()
    loadEgo(entityId)
  }

  // Shift+单击：bloom 展开邻居。事件层节点无实体 ego，退化为居中不展开。
  const handleShiftClick = (entityId: string) => {
    const node = nodes.find((n) => n.id === entityId)
    if (node && node.node_type === 'event') {
      canvasRef.current?.centerNode(entityId)
      return
    }
    bloomNode(entityId)
  }

  // 截断提示文案（design.md 5.3.2「showing X of Y」）。
  const truncatedHint =
    meta && meta.truncated
      ? `已显示 ${meta.returned} / 共 ${meta.total}，双击节点可展开邻居`
      : null

  return (
    <div className="relative h-full w-full">
      {/* 图例 + fit（左上） */}
      <GraphLegend
        types={presentTypes}
        hiddenTypes={hiddenTypes}
        onToggleType={toggleTypeVisibility}
        onFit={() => canvasRef.current?.fitToView()}
      />

      {/* 搜索 + 返回总览（右上） */}
      <GraphSearchBar
        onSearch={handleSearch}
        egoActive={mode === 'ego'}
        onBackToOverview={() => {
          clearSelected()
          loadOverview()
        }}
      />

      {/* 截断提示（底部居中） */}
      {truncatedHint && (
        <div className="pointer-events-none absolute bottom-3 left-1/2 z-10 -translate-x-1/2 rounded-full border border-border bg-card/90 px-3 py-1 text-xs text-muted-foreground shadow-sm backdrop-blur">
          {truncatedHint}
        </div>
      )}

      {/* 加载遮罩（切模式/过滤/搜索时的轻量提示，不阻断已渲染的图） */}
      {loading && (
        <div className="pointer-events-none absolute bottom-3 right-3 z-10 rounded-full border border-border bg-card/90 px-3 py-1 text-xs text-muted-foreground shadow-sm backdrop-blur">
          加载中…
        </div>
      )}

      {/* 力导向画布 */}
      <GraphCanvas
        ref={canvasRef}
        nodes={nodes}
        edges={edges}
        hiddenTypes={hiddenTypes}
        selectedId={selected?.id ?? selectedEvent?.id ?? null}
        onSingleClick={handleSingleClick}
        onDoubleClick={handleDoubleClick}
        onShiftClick={handleShiftClick}
      />

      {/* 实体详情抽屉（懒加载，右侧覆盖） */}
      <GraphEntityDrawer
        detail={selected}
        loading={selectedLoading}
        onClose={clearSelected}
        onPivotNeighbor={handlePivotNeighbor}
      />

      {/* 事件详情抽屉（事件中心图谱，右侧覆盖；与实体抽屉互斥） */}
      <GraphEventDrawer
        detail={selectedEvent}
        loading={selectedEventLoading}
        onClose={clearSelected}
        onPivotEntity={handlePivotNeighbor}
      />
    </div>
  )
}
