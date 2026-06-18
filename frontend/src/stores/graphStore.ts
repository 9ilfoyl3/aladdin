import { create } from 'zustand'

import {
  graphApi,
  type GraphEdge,
  type GraphEntityDetail,
  type GraphMeta,
  type GraphNode,
} from '../lib/api'

/**
 * 知识图谱页面全局状态（design.md 5.3.3）。
 *
 * 数据流严格单向：组件触发 store action → action 调 graphApi → 写回 state →
 * 组件只读 state 渲染。组件内不散落 fetch（design.md 5.3.3 / 项目数据流规则）。
 *
 * store 持有：
 * - 查询参数：mode / center / depth / types（类型过滤）。
 * - 结果：nodes / edges / meta。
 * - 选中态：selected（点击节点懒加载的实体详情）。
 * - 加载/错误：loading / error；unavailable（后端 503，服务不可用）。
 */

/** 图谱查询模式：总览 / ego 邻居子图。 */
export type GraphMode = 'overview' | 'ego'

interface GraphState {
  // —— 当前作用的知识库 ——
  kbId: string | null

  // —— 查询参数 ——
  mode: GraphMode
  /** ego 中心节点 id（overview 模式为 null） */
  center: string | null
  /** ego BFS 跳数 */
  depth: number
  /** 选中的类型过滤（空数组=不过滤） */
  types: string[]
  /**
   * 图例本地隐藏的类型（design.md 5.3.2「本地过滤 + 重算可见边」）。
   * 与 types（服务端过滤）不同：这里只控制画布显隐，不重新拉数据，
   * 点哪个图例就把哪个类型藏起来/显出来。
   */
  hiddenTypes: string[]

  // —— 结果 ——
  nodes: GraphNode[]
  edges: GraphEdge[]
  meta: GraphMeta | null

  // —— 选中实体详情（懒加载） ——
  selected: GraphEntityDetail | null
  selectedLoading: boolean

  // —— 加载 / 错误 / 不可用 ——
  loading: boolean
  error: string | null
  /** 后端返回 503（图存储未启用/不可用），用于展示「服务暂不可用」空态 */
  unavailable: boolean

  // —— actions ——
  /** 设定当前 KB（切库时重置图状态） */
  setKb: (kbId: string) => void
  /** 加载总览子图（mode=overview） */
  loadOverview: (limit?: number) => Promise<void>
  /** 加载某中心节点的 ego 子图（mode=ego） */
  loadEgo: (center: string, depth?: number) => Promise<void>
  /** 设置类型过滤并按当前模式重新拉取 */
  setTypes: (types: string[]) => Promise<void>
  /** 图例点击：本地切换某类型显隐（不重新拉数据，仅影响画布渲染） */
  toggleTypeVisibility: (type: string) => void
  /** 点击节点：选中并懒加载实体详情 */
  selectNode: (entityId: string) => Promise<void>
  /** Shift+单击：bloom 展开某节点邻居（depth=1）并并入当前画布，不开抽屉 */
  bloomNode: (center: string) => Promise<void>
  /** 关闭实体详情抽屉（清空选中） */
  clearSelected: () => void
  /** 重置整个图状态（离开页面/切库） */
  reset: () => void
}

// 默认 ego 跳数（后端仍会 clamp 到平台硬上限）。
const DEFAULT_DEPTH = 1

// 服务不可用的后端文案（与 backend/app/api/graph.py 一致），用于区分 503 与普通错误。
const STORE_UNAVAILABLE_DETAIL = '知识图谱未启用或不可用'

// 结果与选中态的初始空值（reset / 切库复用）。
const EMPTY_RESULT = {
  nodes: [] as GraphNode[],
  edges: [] as GraphEdge[],
  meta: null as GraphMeta | null,
  selected: null as GraphEntityDetail | null,
  selectedLoading: false,
  error: null as string | null,
  unavailable: false,
}

// 把 graphApi 抛出的 Error 归一为 { unavailable, message }：503 文案 → 不可用态。
function classifyError(err: unknown): { unavailable: boolean; message: string } {
  const message = err instanceof Error ? err.message : String(err)
  return { unavailable: message.includes(STORE_UNAVAILABLE_DETAIL), message }
}

export const useGraphStore = create<GraphState>((set, get) => ({
  kbId: null,
  mode: 'overview',
  center: null,
  depth: DEFAULT_DEPTH,
  types: [],
  hiddenTypes: [],
  ...EMPTY_RESULT,
  loading: false,

  setKb: (kbId) => {
    if (get().kbId === kbId) return
    // 切库：清空结果与查询参数，回到 overview。
    set({
      kbId,
      mode: 'overview',
      center: null,
      depth: DEFAULT_DEPTH,
      types: [],
      hiddenTypes: [],
      loading: false,
      ...EMPTY_RESULT,
    })
  },

  loadOverview: async (limit) => {
    const { kbId, types } = get()
    if (!kbId) return
    set({ loading: true, error: null, unavailable: false, mode: 'overview', center: null })
    try {
      const subset = await graphApi.getGraph(kbId, { mode: 'overview', types, limit })
      set({ nodes: subset.nodes, edges: subset.edges, meta: subset.meta, loading: false })
    } catch (err) {
      const { unavailable, message } = classifyError(err)
      set({ loading: false, error: message, unavailable })
    }
  },

  loadEgo: async (center, depth) => {
    const { kbId, types, depth: prevDepth } = get()
    if (!kbId) return
    const effDepth = depth ?? prevDepth
    set({
      loading: true,
      error: null,
      unavailable: false,
      mode: 'ego',
      center,
      depth: effDepth,
    })
    try {
      const subset = await graphApi.getGraph(kbId, { mode: 'ego', center, depth: effDepth, types })
      set({ nodes: subset.nodes, edges: subset.edges, meta: subset.meta, loading: false })
    } catch (err) {
      const { unavailable, message } = classifyError(err)
      set({ loading: false, error: message, unavailable })
    }
  },

  setTypes: async (types) => {
    set({ types })
    // 按当前模式重新拉取，使过滤生效。
    const { mode, center } = get()
    if (mode === 'ego' && center) {
      await get().loadEgo(center)
    } else {
      await get().loadOverview()
    }
  },

  toggleTypeVisibility: (type) => {
    const { hiddenTypes } = get()
    set({
      hiddenTypes: hiddenTypes.includes(type)
        ? hiddenTypes.filter((t) => t !== type)
        : [...hiddenTypes, type],
    })
  },

  selectNode: async (entityId) => {
    const { kbId } = get()
    if (!kbId) return
    set({ selectedLoading: true })
    try {
      const detail = await graphApi.getGraphEntity(kbId, entityId)
      set({ selected: detail, selectedLoading: false })
    } catch (err) {
      const { message } = classifyError(err)
      set({ selectedLoading: false, error: message })
    }
  },

  bloomNode: async (center) => {
    const { kbId, types } = get()
    if (!kbId) return
    set({ loading: true, error: null, unavailable: false })
    try {
      // 取该节点 depth=1 邻居子图，并入当前画布（不替换、不开抽屉）。
      const subset = await graphApi.getGraph(kbId, { mode: 'ego', center, depth: 1, types })
      const { nodes, edges } = get()
      // 按 id / source-target-type 去重并入，保留已有节点的力学坐标。
      const nodeIds = new Set(nodes.map((n) => n.id))
      const mergedNodes = [...nodes]
      for (const n of subset.nodes) {
        if (!nodeIds.has(n.id)) {
          nodeIds.add(n.id)
          mergedNodes.push(n)
        }
      }
      const edgeKey = (e: GraphEdge) => `${e.source}__${e.target}__${e.type}`
      const edgeKeys = new Set(edges.map(edgeKey))
      const mergedEdges = [...edges]
      for (const e of subset.edges) {
        const k = edgeKey(e)
        if (!edgeKeys.has(k)) {
          edgeKeys.add(k)
          mergedEdges.push(e)
        }
      }
      set({ nodes: mergedNodes, edges: mergedEdges, loading: false })
    } catch (err) {
      const { unavailable, message } = classifyError(err)
      set({ loading: false, error: message, unavailable })
    }
  },

  clearSelected: () => set({ selected: null }),

  reset: () =>
    set({
      kbId: null,
      mode: 'overview',
      center: null,
      depth: DEFAULT_DEPTH,
      types: [],
      hiddenTypes: [],
      loading: false,
      ...EMPTY_RESULT,
    }),
}))
