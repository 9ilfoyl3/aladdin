// GraphCanvas 冒烟测试（design.md 5.3.2 / 5.3.3；Requirements 6.4）。
//
// 验证 react-force-graph-2d 封装在空图/大图/单节点下挂载不崩溃。
// 库本身基于 canvas（jsdom 不支持），mock 成接收 graphData 的占位，
// 重点验证 GraphCanvas 内的 useMemo 数据构造（类型过滤 + 重算可见边 + 坐标缓存）
// 在各规模数据下不抛错。

import { render } from '@testing-library/react'
import { createRef } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { GraphEdge, GraphNode } from '@/lib/api'

import GraphCanvas, { type GraphCanvasHandle } from './GraphCanvas'

// mock 力导向库：仅暴露被传入的节点/边数量，命令式方法为 no-op。
vi.mock('react-force-graph-2d', () => ({
  default: (props: { graphData: { nodes: unknown[]; links: unknown[] } }) => (
    <div
      data-testid="fg2d"
      data-nodes={props.graphData.nodes.length}
      data-links={props.graphData.links.length}
    />
  ),
}))

// jsdom 下 ResizeObserver 不会真正回调尺寸，手动给容器一个非零尺寸触发渲染。
// GraphCanvas 仅在 size>0 时渲染 ForceGraph2D，这里直接断言组件挂载不抛错即可。
function noop() {}

function renderCanvas(nodes: GraphNode[], edges: GraphEdge[], hiddenTypes: string[] = []) {
  const ref = createRef<GraphCanvasHandle>()
  const result = render(
    <GraphCanvas
      ref={ref}
      nodes={nodes}
      edges={edges}
      hiddenTypes={hiddenTypes}
      selectedId={null}
      onSingleClick={noop}
      onDoubleClick={noop}
      onShiftClick={noop}
    />,
  )
  return { ref, ...result }
}

describe('GraphCanvas 冒烟', () => {
  it('空图：挂载不崩溃', () => {
    expect(() => renderCanvas([], [])).not.toThrow()
  })

  it('单节点：挂载不崩溃，ref 暴露命令式方法', () => {
    const { ref } = renderCanvas([{ id: 'e1', name: '张三', type: '人物', degree: 0 }], [])
    // fitToView/centerNode 在无内部 fg 实例时也应安全 no-op，不抛错。
    expect(() => ref.current?.fitToView()).not.toThrow()
    expect(() => ref.current?.centerNode('e1')).not.toThrow()
  })

  it('大图（500 节点）：挂载不崩溃', () => {
    const nodes: GraphNode[] = Array.from({ length: 500 }, (_, i) => ({
      id: `e${i}`,
      name: `实体${i}`,
      type: i % 2 === 0 ? '人物' : '组织',
      degree: i % 10,
    }))
    const edges: GraphEdge[] = Array.from({ length: 499 }, (_, i) => ({
      source: `e${i}`,
      target: `e${i + 1}`,
      type: '关联',
      weight: 1,
    }))
    expect(() => renderCanvas(nodes, edges)).not.toThrow()
  })

  it('类型隐藏：隐藏某类型不崩溃', () => {
    const nodes: GraphNode[] = [
      { id: 'e1', name: '张三', type: '人物', degree: 1 },
      { id: 'e2', name: '某公司', type: '组织', degree: 1 },
    ]
    const edges: GraphEdge[] = [{ source: 'e1', target: 'e2', type: '任职于', weight: 1 }]
    expect(() => renderCanvas(nodes, edges, ['组织'])).not.toThrow()
  })
})
