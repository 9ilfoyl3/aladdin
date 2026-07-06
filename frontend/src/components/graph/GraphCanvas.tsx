// 力导向图画布（design.md 5.3.2 / 5.3.3）：react-force-graph-2d 封装。
//
// 职责：渲染 store 的 nodes/edges，承载全部交互回调（单击 / 双击 / Shift 单击 /
// 拖拽 pin / 缩放标签显隐 / fit-to-view / 类型本地过滤）。组件只读 store 状态、
// 把交互通过 props 回调上抛给页面统一经 store action 触发（数据流单向，
// 组件内不散落 fetch）。
//
// 交互映射（design.md 5.3.2 表）：
// - 单击节点：选中高亮 + centerAt 居中 + 打开详情抽屉（懒加载，由页面经 store）。
// - 双击节点：pivot 到该节点 ego（220ms 窗口内第二次点击判为双击，参考 WeKnora）。
// - Shift+单击：bloom 展开邻居并入当前画布（不开抽屉）。
// - 拖拽节点：onNodeDragEnd 设 fx/fy 固定（pin）。
// - 缩放：zoom 阈值控制标签显隐（degree 高的节点更早显示标签）。
// - fit-to-view：父组件经 ref 调 zoomToFit。

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react'
import ForceGraph2D, {
  type ForceGraphMethods,
  type NodeObject,
  type LinkObject,
} from 'react-force-graph-2d'

import type { GraphEdge, GraphNode } from '@/lib/api'

import { colorForType, isEventNode, getEventColors } from './graphColors'

// 画布节点 = 后端 GraphNode + force-graph 运行时坐标字段（x/y/fx/fy 由库注入）。
type CanvasNode = NodeObject<GraphNode>
type CanvasLink = LinkObject<GraphNode, GraphEdge>

// 父组件可经 ref 调用的命令式方法（fit-to-view / 居中到指定节点）。
export interface GraphCanvasHandle {
  /** 适应屏幕（zoomToFit） */
  fitToView: () => void
  /** 平移居中到指定实体（单击选中时调用） */
  centerNode: (entityId: string) => void
}

interface Props {
  nodes: GraphNode[]
  edges: GraphEdge[]
  /** 本地隐藏的类型（图例点击，design.md 5.3.2 本地过滤 + 重算可见边） */
  hiddenTypes: string[]
  /** 当前选中实体 id（高亮 + 邻接强调） */
  selectedId: string | null
  /** 单击：选中 + 居中 + 抽屉详情 */
  onSingleClick: (entityId: string) => void
  /** 双击：pivot 到该节点 ego */
  onDoubleClick: (entityId: string) => void
  /** Shift+单击：bloom 展开邻居 */
  onShiftClick: (entityId: string) => void
}

// 双击判定窗口（ms，参考 WeKnora 220ms）：窗口内第二次点击判为双击。
const DOUBLE_CLICK_MS = 220
// 标签显隐的缩放阈值：放大到该 zoom 以上显示全部标签；以下仅高 degree 节点显示。
const LABEL_ZOOM_THRESHOLD = 1.2
// 事件标签阈值更低：事件比实体更早显示标签（但总览态仍不强制全显，避免糊成一片）。
const EVENT_LABEL_ZOOM_THRESHOLD = 0.8
// 低 zoom 下仍显示标签的 degree 门槛（高 degree 的枢纽节点更早显示标签）。
const HUB_DEGREE_THRESHOLD = 4
// 节点半径映射：基准 + 随 degree 增长（sqrt 抑制过大），单位为 force-graph 内部坐标。
const NODE_BASE_RADIUS = 4
const NODE_DEGREE_SCALE = 1.4
// 节点半径上限，避免高 degree 枢纽节点过大遮挡（截图里红色中心节点过大）。
const NODE_MAX_RADIUS = 14
// 事件层节点用更大的基准 + 上限：事件是图谱中心，视觉上应显著大于实体圆点。
const EVENT_BASE_RADIUS = 7
const EVENT_DEGREE_SCALE = 1.8
const EVENT_MAX_RADIUS = 20
// 标签字号（屏幕像素，渲染时除以 globalScale 换算到图坐标，保证缩放后视觉字号稳定）。
const LABEL_FONT_PX = 12
// 事件标签字号略大（凸显中心地位）。
const EVENT_LABEL_FONT_PX = 13
// 标签与节点的垂直间距（屏幕像素）。
const LABEL_GAP_PX = 4
// 缩放上下限，避免滚轮缩放过度（过小看不清、过大空旷）。
const MIN_ZOOM = 0.3
const MAX_ZOOM = 6
// 初始 fit 后的缩放下限：overview 常含多个互不相连的文档子图，散布范围大，
// zoomToFit 会把整体缩得很小、主体看不清。fit 后若缩放低于此值则提升到此值，
// 保证主体清晰可见，边缘小簇靠平移/缩放浏览。
const INITIAL_FIT_MIN_ZOOM = 0.7

// 计算节点可视半径（带上限）。事件层节点用更大的基准/上限以凸显中心地位。
function nodeRadius(degree: number, isEvent = false): number {
  if (isEvent) {
    return Math.min(EVENT_BASE_RADIUS + Math.sqrt(Math.max(0, degree)) * EVENT_DEGREE_SCALE, EVENT_MAX_RADIUS)
  }
  return Math.min(NODE_BASE_RADIUS + Math.sqrt(Math.max(0, degree)) * NODE_DEGREE_SCALE, NODE_MAX_RADIUS)
}

/**
 * 力导向图画布。受控于 store 数据，交互上抛父组件。
 *
 * 设计：内部维护一个 id→CanvasNode 的缓存，使 store 更新（如 bloom 并入新节点）时
 * 已有节点保留其力学坐标（x/y/fx/fy），避免整图重排导致的跳动。
 */
const GraphCanvas = forwardRef<GraphCanvasHandle, Props>(function GraphCanvas(
  { nodes, edges, hiddenTypes, selectedId, onSingleClick, onDoubleClick, onShiftClick },
  ref,
) {
  const fgRef = useRef<ForceGraphMethods<CanvasNode, CanvasLink>>()
  const containerRef = useRef<HTMLDivElement>(null)
  // 容器尺寸：force-graph 需要显式 width/height，监听容器自适应。
  const [size, setSize] = useState({ width: 0, height: 0 })
  // 当前缩放级别（onZoom 更新），用于标签显隐判定。
  const zoomRef = useRef(1)
  // 单击/双击区分定时器。
  const clickTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // 节点对象缓存：保留已有节点的运行时坐标，新增节点（bloom）才新建对象。
  const nodeCacheRef = useRef<Map<string, CanvasNode>>(new Map())

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const ro = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect
      if (rect) setSize({ width: rect.width, height: rect.height })
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // 可见类型集合：隐藏类型从渲染中剔除，并重算可见边（两端都可见才保留）。
  const hidden = useMemo(() => new Set(hiddenTypes), [hiddenTypes])

  // 事件层配色：基于当前主题 --primary 降饱和度派生（随主题切换重算）。
  const eventColors = useMemo(() => getEventColors(), [])

  // 构造 force-graph 数据：合并缓存坐标 + 本地类型过滤 + 重算可见边。
  const graphData = useMemo(() => {
    const cache = nodeCacheRef.current
    const presentIds = new Set(nodes.map((n) => n.id))
    // 清理缓存中已不存在的节点（避免内存累积 / 切库残留）。
    for (const id of [...cache.keys()]) {
      if (!presentIds.has(id)) cache.delete(id)
    }
    const visibleIds = new Set<string>()
    const visibleNodes: CanvasNode[] = []
    for (const n of nodes) {
      if (hidden.has(n.type)) continue
      const existing = cache.get(n.id)
      // 复用已有对象（保留 x/y/fx/fy），仅刷新业务字段；新节点入缓存。
      const merged: CanvasNode = existing
        ? Object.assign(existing, { name: n.name, type: n.type, degree: n.degree, node_type: n.node_type })
        : { ...n }
      cache.set(n.id, merged)
      visibleNodes.push(merged)
      visibleIds.add(n.id)
    }
    // 仅保留两端均可见的边（本地过滤后重算）。
    const links: CanvasLink[] = edges
      .filter((e) => visibleIds.has(e.source) && visibleIds.has(e.target))
      .map((e) => ({ source: e.source, target: e.target, type: e.type, weight: e.weight }))
    return { nodes: visibleNodes, links }
  }, [nodes, edges, hidden])

  // 选中节点的邻接 id 集合（高亮强调用）。
  const adjacency = useMemo(() => {
    if (!selectedId) return new Set<string>()
    const adj = new Set<string>()
    for (const e of edges) {
      if (e.source === selectedId) adj.add(e.target)
      else if (e.target === selectedId) adj.add(e.source)
    }
    return adj
  }, [edges, selectedId])

  // 暴露命令式方法给父组件。
  useImperativeHandle(
    ref,
    () => ({
      fitToView: () => fgRef.current?.zoomToFit(400, 40),
      centerNode: (entityId: string) => {
        const node = nodeCacheRef.current.get(entityId)
        if (node && node.x != null && node.y != null) {
          fgRef.current?.centerAt(node.x, node.y, 600)
          fgRef.current?.zoom(Math.min(Math.max(zoomRef.current, 1.6), MAX_ZOOM), 600)
        }
      },
    }),
    [],
  )

  // 进入新图（节点集合数量变化）后：调力学参数增大间距，再自动 fit。
  useEffect(() => {
    if (graphData.nodes.length === 0) return
    const fg = fgRef.current
    if (fg) {
      // 斥力：事件层节点用更强斥力，把周围实体推开、腾出「中心辐射」的留白。
      const charge = fg.d3Force('charge')
      if (charge) {
        charge
          .strength((n: CanvasNode) => (isEventNode(n) ? -520 : -220))
          .distanceMax(480)
      }
      // 连接距离：MENTIONS（事件→实体）用更短距离，把关联实体紧紧拉到事件周围，
      // 形成以事件为核心的星形簇；普通实体关系维持较大间距。
      const link = fg.d3Force('link')
      if (link && typeof link.distance === 'function') {
        link.distance((l: CanvasLink) => (l.type === 'MENTIONS' ? 55 : 90))
      }
      // 弱中心引力把互不相连的游离子图往中间收拢，避免散得太开导致 fit 后整体过小。
      const center = fg.d3Force('center')
      if (center && typeof center.strength === 'function') center.strength(0.06)
      fg.d3ReheatSimulation()
    }
    // fit 后做缩放下限保护：overview 多簇散布时 zoomToFit 会过度缩小，提升到下限保证主体清晰。
    const t = setTimeout(() => {
      fg?.zoomToFit(500, 60)
      // zoomToFit 带动画，等动画基本结束再校正缩放下限。
      setTimeout(() => {
        if (fg && zoomRef.current < INITIAL_FIT_MIN_ZOOM) {
          fg.zoom(INITIAL_FIT_MIN_ZOOM, 400)
        }
      }, 550)
    }, 350)
    return () => clearTimeout(t)
  }, [graphData.nodes.length])

  // 卸载时清掉残留的单击定时器。
  useEffect(() => {
    return () => {
      if (clickTimerRef.current) clearTimeout(clickTimerRef.current)
    }
  }, [])

  // 单击/双击/Shift 单击区分（design.md 5.3.2，借鉴 WeKnora 220ms 窗口）。
  const handleNodeClick = (node: CanvasNode, event: MouseEvent) => {
    const id = String(node.id)
    if (event.shiftKey) {
      // Shift+单击：bloom，不开抽屉，不参与单/双击定时。
      if (clickTimerRef.current) {
        clearTimeout(clickTimerRef.current)
        clickTimerRef.current = null
      }
      onShiftClick(id)
      return
    }
    if (clickTimerRef.current) {
      // 窗口内第二次点击 → 双击：取消单击、pivot ego。
      clearTimeout(clickTimerRef.current)
      clickTimerRef.current = null
      onDoubleClick(id)
      return
    }
    // 首次点击：延迟判定，等待可能的双击。
    clickTimerRef.current = setTimeout(() => {
      clickTimerRef.current = null
      onSingleClick(id)
    }, DOUBLE_CLICK_MS)
  }

  // 拖拽结束 pin：固定 fx/fy（design.md 5.3.2）。
  const handleNodeDragEnd = (node: CanvasNode) => {
    node.fx = node.x
    node.fy = node.y
  }

  // 节点自定义渲染（极简）：所有节点都是「实心圆点」，只用两件事表达层级——
  //   1) 颜色：实体按类型着色，事件统一用事件色；
  //   2) 尺寸：事件半径更大（nodeRadius isEvent 分支），天然成为视觉重心。
  // 不再叠加同心环 / 白心点 / 卡片底衬等装饰（那是上一版杂乱的根源）。
  // 选中态是唯一会加「光晕」的情形，作为明确且独占的高亮信号。
  const paintNode = (node: CanvasNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
    const id = String(node.id)
    const degree = node.degree ?? 0
    const isEvent = isEventNode(node)
    const radius = nodeRadius(degree, isEvent)
    const baseColor = isEvent ? eventColors.fill : colorForType(node.type)

    const isSelected = id === selectedId
    const isAdjacent = adjacency.has(id)
    const dimmed = selectedId != null && !isSelected && !isAdjacent

    const x = node.x ?? 0
    const y = node.y ?? 0

    ctx.globalAlpha = dimmed ? 0.15 : 1

    // 选中光晕：仅选中节点有，柔和放射，作为独占的高亮信号。
    if (isSelected) {
      const glow = ctx.createRadialGradient(x, y, radius * 0.5, x, y, radius + 8 / globalScale)
      glow.addColorStop(0, isEvent ? eventColors.edge : 'rgba(15,23,42,0.22)')
      glow.addColorStop(1, 'rgba(0,0,0,0)')
      ctx.beginPath()
      ctx.arc(x, y, radius + 8 / globalScale, 0, 2 * Math.PI)
      ctx.fillStyle = glow
      ctx.fill()
    }

    // 实心圆点（唯一造型）。
    ctx.beginPath()
    ctx.arc(x, y, radius, 0, 2 * Math.PI)
    ctx.fillStyle = baseColor
    ctx.fill()

    // 细描边：选中用深色强调，其余用细白边提升与连线/背景的对比（不喧宾夺主）。
    if (isSelected) {
      ctx.lineWidth = 2 / globalScale
      ctx.strokeStyle = '#0f172a'
      ctx.stroke()
    } else if (!dimmed) {
      ctx.lineWidth = (isEvent ? 1.5 : 1) / globalScale
      ctx.strokeStyle = 'rgba(255,255,255,0.9)'
      ctx.stroke()
    }

    // 标签显隐：事件用更低的缩放阈值（比实体更早显示），但总览态不强制全显，
    // 避免几十个事件标签叠成一片糊。选中/邻接始终显示。
    const showLabel =
      isSelected ||
      isAdjacent ||
      globalScale >= (isEvent ? EVENT_LABEL_ZOOM_THRESHOLD : LABEL_ZOOM_THRESHOLD) ||
      degree >= HUB_DEGREE_THRESHOLD
    if (showLabel && node.name) {
      // 关键：不用「亚像素字号」（fontSize/globalScale 放大时会小到字形塌缩粘连），
      // 改为始终用正常字号渲染，再用 ctx.scale(1/k) 缩到恒定屏幕尺寸。
      const k = globalScale
      const fontPx = isEvent ? EVENT_LABEL_FONT_PX : LABEL_FONT_PX
      const labelY = y + radius + LABEL_GAP_PX / k

      // 事件标签可能是整句，做省略截断避免糊满画布。
      const label = isEvent && node.name.length > 14 ? `${node.name.slice(0, 14)}…` : node.name

      ctx.save()
      ctx.translate(x, labelY)
      ctx.scale(1 / k, 1 / k)
      ctx.font = `${isEvent ? 500 : 400} ${fontPx}px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'top'

      // 去掉底衬方框：改用「白色描边光晕」垫在文字下方，干净且始终可读。
      ctx.globalAlpha = dimmed ? 0.35 : 1
      ctx.lineWidth = 3
      ctx.strokeStyle = 'rgba(255,255,255,0.92)'
      ctx.lineJoin = 'round'
      ctx.strokeText(label, 0, 0)

      ctx.fillStyle = isEvent ? eventColors.border : isSelected ? '#0f172a' : '#334155'
      ctx.fillText(label, 0, 0)
      ctx.restore()
    }
    ctx.globalAlpha = 1
  }

  // 点击命中区域用半径绘制（与可视半径一致，避免标签干扰命中）。
  const paintPointerArea = (
    node: CanvasNode,
    color: string,
    ctx: CanvasRenderingContext2D,
  ) => {
    const radius = nodeRadius(node.degree ?? 0, isEventNode(node))
    ctx.beginPath()
    ctx.arc(node.x ?? 0, node.y ?? 0, radius + 3, 0, 2 * Math.PI)
    ctx.fillStyle = color
    ctx.fill()
  }

  return (
    <div ref={containerRef} className="h-full w-full">
      {size.width > 0 && size.height > 0 && (
        <ForceGraph2D<GraphNode, GraphEdge>
          ref={fgRef}
          width={size.width}
          height={size.height}
          graphData={graphData}
          nodeId="id"
          backgroundColor="transparent"
          nodeCanvasObject={paintNode}
          nodePointerAreaPaint={paintPointerArea}
          linkColor={(l) =>
            (l as CanvasLink).type === 'MENTIONS' ? eventColors.edge : 'rgba(148,163,184,0.28)'
          }
          linkWidth={(l) => {
            const link = l as CanvasLink
            const base = Math.min(0.8 + Math.log1p(link.weight ?? 1), 3)
            // MENTIONS（事件→实体）边略粗，凸显事件的辐射结构。
            return link.type === 'MENTIONS' ? base + 0.6 : base
          }}
          linkDirectionalArrowLength={3.5}
          linkDirectionalArrowRelPos={1}
          linkCurvature={0.08}
          minZoom={MIN_ZOOM}
          maxZoom={MAX_ZOOM}
          warmupTicks={40}
          enableNodeDrag
          onNodeClick={handleNodeClick}
          onNodeDragEnd={handleNodeDragEnd}
          onZoom={(t) => {
            zoomRef.current = t.k
          }}
          cooldownTicks={120}
          d3VelocityDecay={0.32}
        />
      )}
    </div>
  )
})

export default GraphCanvas
