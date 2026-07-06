// graphStore 单元测试（design.md 5.3.3 / 前端测试；Requirements 6.4）。
//
// 覆盖：mock graphApi 返回，验证
//  - overview / ego 模式切换（loadOverview / loadEgo 写回 mode/center/结果）
//  - 类型过滤（setTypes 触发重拉、toggleTypeVisibility 本地切换）
//  - 节点选中（selectNode 懒加载详情、clearSelected 清空）
//  - 503 不可用态归类（classifyError → unavailable）
//  - 切库重置（setKb）、bloom 并入去重

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { graphApi, type GraphSubset, type GraphEntityDetail } from '../lib/api'
import { useGraphStore } from './graphStore'

// mock 整个 api 模块的 graphApi：store 的数据来源全部来自这里。
vi.mock('../lib/api', () => ({
  graphApi: {
    getGraph: vi.fn(),
    getGraphEntity: vi.fn(),
    getGraphStats: vi.fn(),
    getGraphConfig: vi.fn(),
  },
}))

const mockedApi = vi.mocked(graphApi)

// 构造一份 overview 子图。
function overviewSubset(): GraphSubset {
  return {
    nodes: [
      { id: 'e1', name: '张三', type: '人物', degree: 5 },
      { id: 'e2', name: '某公司', type: '组织', degree: 3 },
    ],
    edges: [{ source: 'e1', target: 'e2', type: '任职于', weight: 2 }],
    meta: { mode: 'overview', total: 2, returned: 2, truncated: false },
  }
}

// 构造一份 ego 子图（不同节点集，便于断言切换后结果被替换）。
function egoSubset(): GraphSubset {
  return {
    nodes: [
      { id: 'e1', name: '张三', type: '人物', degree: 5 },
      { id: 'e3', name: '李四', type: '人物', degree: 1 },
    ],
    edges: [{ source: 'e1', target: 'e3', type: '认识', weight: 1 }],
    meta: { mode: 'ego', total: 2, returned: 2, truncated: false, center: 'e1', depth: 1 },
  }
}

function entityDetail(): GraphEntityDetail {
  return {
    id: 'e1',
    name: '张三',
    type: '人物',
    aliases: ['老张'],
    attributes: ['工程师'],
    degree: 5,
    neighbors: [{ id: 'e2', name: '某公司', type: '组织', rel_type: '任职于' }],
    chunks: [{ chunk_id: 'c1', doc_id: 'd1', content_preview: '张三在某公司工作' }],
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  // 每个用例前重置 store 到初始态（singleton）。
  useGraphStore.getState().reset()
})

describe('graphStore 模式切换', () => {
  it('loadOverview 写回 overview 模式与结果', async () => {
    mockedApi.getGraph.mockResolvedValue(overviewSubset())
    useGraphStore.getState().setKb('kb1')

    await useGraphStore.getState().loadOverview()

    const s = useGraphStore.getState()
    expect(s.mode).toBe('overview')
    expect(s.center).toBeNull()
    expect(s.nodes).toHaveLength(2)
    expect(s.edges).toHaveLength(1)
    expect(s.meta?.mode).toBe('overview')
    expect(s.loading).toBe(false)
    expect(mockedApi.getGraph).toHaveBeenCalledWith('kb1', {
      mode: 'overview',
      types: [],
      limit: undefined,
      include_events: true,
    })
  })

  it('loadEgo 切到 ego 模式并记录 center/depth，替换结果', async () => {
    mockedApi.getGraph
      .mockResolvedValueOnce(overviewSubset())
      .mockResolvedValueOnce(egoSubset())
    useGraphStore.getState().setKb('kb1')
    await useGraphStore.getState().loadOverview()

    await useGraphStore.getState().loadEgo('e1', 2)

    const s = useGraphStore.getState()
    expect(s.mode).toBe('ego')
    expect(s.center).toBe('e1')
    expect(s.depth).toBe(2)
    // ego 结果替换 overview（含 e3，不含 e2）
    expect(s.nodes.map((n) => n.id).sort()).toEqual(['e1', 'e3'])
    expect(mockedApi.getGraph).toHaveBeenLastCalledWith('kb1', {
      mode: 'ego',
      center: 'e1',
      depth: 2,
      types: [],
      include_events: true,
    })
  })

  it('无 kbId 时不发起请求', async () => {
    await useGraphStore.getState().loadOverview()
    expect(mockedApi.getGraph).not.toHaveBeenCalled()
  })
})

describe('graphStore 类型过滤', () => {
  it('setTypes 记录服务端过滤并按 overview 模式重拉', async () => {
    mockedApi.getGraph.mockResolvedValue(overviewSubset())
    useGraphStore.getState().setKb('kb1')

    await useGraphStore.getState().setTypes(['人物'])

    const s = useGraphStore.getState()
    expect(s.types).toEqual(['人物'])
    expect(mockedApi.getGraph).toHaveBeenLastCalledWith('kb1', {
      mode: 'overview',
      types: ['人物'],
      limit: undefined,
      include_events: true,
    })
  })

  it('ego 模式下 setTypes 以当前 center 重拉 ego', async () => {
    mockedApi.getGraph.mockResolvedValue(egoSubset())
    useGraphStore.getState().setKb('kb1')
    await useGraphStore.getState().loadEgo('e1', 1)

    await useGraphStore.getState().setTypes(['人物'])

    expect(mockedApi.getGraph).toHaveBeenLastCalledWith('kb1', {
      mode: 'ego',
      center: 'e1',
      depth: 1,
      types: ['人物'],
      include_events: true,
    })
  })

  it('toggleTypeVisibility 本地切换 hiddenTypes，不重新拉数据', () => {
    useGraphStore.getState().setKb('kb1')

    useGraphStore.getState().toggleTypeVisibility('组织')
    expect(useGraphStore.getState().hiddenTypes).toEqual(['组织'])

    // 再次切换同类型 → 取消隐藏
    useGraphStore.getState().toggleTypeVisibility('组织')
    expect(useGraphStore.getState().hiddenTypes).toEqual([])

    expect(mockedApi.getGraph).not.toHaveBeenCalled()
  })
})

describe('graphStore 节点选中', () => {
  it('selectNode 懒加载实体详情写入 selected', async () => {
    mockedApi.getGraphEntity.mockResolvedValue(entityDetail())
    useGraphStore.getState().setKb('kb1')

    await useGraphStore.getState().selectNode('e1')

    const s = useGraphStore.getState()
    expect(mockedApi.getGraphEntity).toHaveBeenCalledWith('kb1', 'e1')
    expect(s.selected?.id).toBe('e1')
    expect(s.selectedLoading).toBe(false)
  })

  it('clearSelected 清空选中', async () => {
    mockedApi.getGraphEntity.mockResolvedValue(entityDetail())
    useGraphStore.getState().setKb('kb1')
    await useGraphStore.getState().selectNode('e1')

    useGraphStore.getState().clearSelected()
    expect(useGraphStore.getState().selected).toBeNull()
  })
})

describe('graphStore 错误与不可用态', () => {
  it('503 文案归类为 unavailable', async () => {
    mockedApi.getGraph.mockRejectedValue(new Error('知识图谱未启用或不可用'))
    useGraphStore.getState().setKb('kb1')

    await useGraphStore.getState().loadOverview()

    const s = useGraphStore.getState()
    expect(s.unavailable).toBe(true)
    expect(s.loading).toBe(false)
    expect(s.error).toContain('知识图谱未启用或不可用')
  })

  it('普通错误不置 unavailable', async () => {
    mockedApi.getGraph.mockRejectedValue(new Error('网络错误'))
    useGraphStore.getState().setKb('kb1')

    await useGraphStore.getState().loadOverview()

    const s = useGraphStore.getState()
    expect(s.unavailable).toBe(false)
    expect(s.error).toBe('网络错误')
  })
})

describe('graphStore 切库与 bloom', () => {
  it('setKb 切到新库时重置图状态', async () => {
    mockedApi.getGraph.mockResolvedValue(overviewSubset())
    useGraphStore.getState().setKb('kb1')
    await useGraphStore.getState().loadOverview()
    expect(useGraphStore.getState().nodes).toHaveLength(2)

    useGraphStore.getState().setKb('kb2')
    const s = useGraphStore.getState()
    expect(s.kbId).toBe('kb2')
    expect(s.nodes).toHaveLength(0)
    expect(s.mode).toBe('overview')
  })

  it('bloomNode 把邻居子图去重并入当前画布', async () => {
    mockedApi.getGraph
      .mockResolvedValueOnce(overviewSubset())
      // bloom 返回与现有部分重叠（e1 重复、e3 新增）
      .mockResolvedValueOnce(egoSubset())
    useGraphStore.getState().setKb('kb1')
    await useGraphStore.getState().loadOverview()

    await useGraphStore.getState().bloomNode('e1')

    const s = useGraphStore.getState()
    // e1/e2（overview）+ e3（bloom 新增），e1 不重复
    expect(s.nodes.map((n) => n.id).sort()).toEqual(['e1', 'e2', 'e3'])
    expect(s.edges).toHaveLength(2)
  })
})
