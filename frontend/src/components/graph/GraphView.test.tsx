// 图谱组件冒烟测试（design.md 前端测试；Requirements 6.4）。
//
// 目标：力导向图视图在「空图 / 大图（截断）/ 单节点」三种数据下挂载不崩溃，
// 且交互编排正确（单击触发详情懒加载）。
//
// react-force-graph-2d 基于 canvas，在 jsdom 下无法真实渲染，故 mock 成
// 暴露交互回调的哨兵：渲染每个节点为一个按钮，点击/双击映射到 onNodeClick，
// 用以验证 GraphView → store action 的交互编排不崩溃。

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { forwardRef } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { graphApi, type GraphSubset, type GraphEntityDetail } from '@/lib/api'
import { useGraphStore } from '@/stores/graphStore'

import GraphView from './GraphView'

vi.mock('@/lib/api', () => ({
  graphApi: {
    getGraph: vi.fn(),
    getGraphEntity: vi.fn(),
  },
}))

// mock 力导向画布：把节点渲染成按钮，单击触发 onNodeClick（非 shift、非双击）。
// forwardRef 承接 GraphView 经 ref 下达的命令式调用（fitToView/centerNode），避免 ref 警告。
vi.mock('./GraphCanvas', () => ({
  default: forwardRef(function GraphCanvasMock(
    props: {
      nodes: { id: string; name: string }[]
      onSingleClick: (id: string) => void
    },
    _ref,
  ) {
    return (
      <div data-testid="canvas">
        {props.nodes.map((n) => (
          <button key={n.id} data-testid={`node-${n.id}`} onClick={() => props.onSingleClick(n.id)}>
            {n.name}
          </button>
        ))}
      </div>
    )
  }),
}))

const mockedApi = vi.mocked(graphApi)

function detail(): GraphEntityDetail {
  return {
    id: 'e1',
    name: '张三',
    type: '人物',
    aliases: [],
    attributes: [],
    degree: 1,
    neighbors: [],
    chunks: [],
  }
}

// 空图：无节点无边。
const EMPTY: GraphSubset = {
  nodes: [],
  edges: [],
  meta: { mode: 'overview', total: 0, returned: 0, truncated: false },
}

// 单节点：一个孤立节点。
const SINGLE: GraphSubset = {
  nodes: [{ id: 'e1', name: '张三', type: '人物', degree: 0 }],
  edges: [],
  meta: { mode: 'overview', total: 1, returned: 1, truncated: false },
}

// 大图（截断）：500 节点，链式相连，meta.truncated=true。
function largeSubset(): GraphSubset {
  const nodes = Array.from({ length: 500 }, (_, i) => ({
    id: `e${i}`,
    name: `实体${i}`,
    type: i % 2 === 0 ? '人物' : '组织',
    degree: i % 10,
  }))
  const edges = Array.from({ length: 499 }, (_, i) => ({
    source: `e${i}`,
    target: `e${i + 1}`,
    type: '关联',
    weight: 1,
  }))
  return {
    nodes,
    edges,
    meta: { mode: 'overview', total: 5000, returned: 500, truncated: true },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  useGraphStore.getState().reset()
  useGraphStore.getState().setKb('kb1')
})

describe('GraphView 冒烟（空图/大图/单节点不崩溃）', () => {
  it('空图：挂载不崩溃，无节点按钮', async () => {
    mockedApi.getGraph.mockResolvedValue(EMPTY)

    render(<GraphView />)

    await waitFor(() => expect(screen.getByTestId('canvas')).toBeInTheDocument())
    expect(screen.queryByTestId(/^node-/)).not.toBeInTheDocument()
  })

  it('单节点：渲染该节点，点击触发详情懒加载', async () => {
    mockedApi.getGraph.mockResolvedValue(SINGLE)
    mockedApi.getGraphEntity.mockResolvedValue(detail())

    render(<GraphView />)

    await waitFor(() => expect(screen.getByTestId('node-e1')).toBeInTheDocument())

    await userEvent.click(screen.getByTestId('node-e1'))

    await waitFor(() => expect(mockedApi.getGraphEntity).toHaveBeenCalledWith('kb1', 'e1'))
  })

  it('大图（截断）：渲染 500 节点不崩溃，显示截断提示', async () => {
    mockedApi.getGraph.mockResolvedValue(largeSubset())

    render(<GraphView />)

    await waitFor(() => expect(screen.getByTestId('node-e0')).toBeInTheDocument())
    expect(screen.getByTestId('node-e499')).toBeInTheDocument()
    // meta.truncated → 截断提示
    expect(screen.getByText(/已显示 500 \/ 共 5000/)).toBeInTheDocument()
  })
})
