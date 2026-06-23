// KnowledgeGraph 页面门控/空态测试（design.md 5.3.1 / 5.3.4；Requirements 6.5, 6.6）。
//
// 验证「数据态」分支决定是否渲染力导向图：
//  - 全局/KB 门控未通过 → 不渲染图（跳转 KB 详情）
//  - entity_count==0 → 渲染空态，不渲染 GraphView
//  - 503 → 渲染「服务不可用」空态，不渲染 GraphView
//  - entity_count>0 → 渲染 GraphView
//
// GraphView 被 mock 成哨兵，避免引入 force-graph；门控与 stats 经 mock 控制。

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { createElement, type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { graphApi } from '@/lib/api'
import { useGraphGating } from '@/components/graph/useGraphGating'

import KnowledgeGraph from './KnowledgeGraph'

// mock 门控 hook：直接控制 showEntry/loading，不走真实 react-query 取数。
vi.mock('@/components/graph/useGraphGating', () => ({
  useGraphGating: vi.fn(),
}))

// mock GraphView：哨兵，断言「是否渲染图」只看它是否出现。
vi.mock('@/components/graph/GraphView', () => ({
  default: () => createElement('div', { 'data-testid': 'graph-view' }, 'GRAPH'),
}))

vi.mock('@/lib/api', () => ({
  graphApi: { getGraphStats: vi.fn() },
}))

const mockedGating = vi.mocked(useGraphGating)
const mockedApi = vi.mocked(graphApi)

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  })
  const ui: ReactNode = createElement(
    QueryClientProvider,
    { client },
    createElement(
      MemoryRouter,
      { initialEntries: ['/knowledge-bases/kb1/graph'] },
      createElement(
        Routes,
        null,
        createElement(Route, {
          path: '/knowledge-bases/:id/graph',
          element: createElement(KnowledgeGraph),
        }),
        createElement(Route, {
          path: '/knowledge-bases/:id',
          element: createElement('div', { 'data-testid': 'kb-detail' }, 'KB_DETAIL'),
        }),
      ),
    ),
  )
  return render(ui)
}

function gating(showEntry: boolean, loading = false) {
  return { showEntry, globalEnabled: showEntry, kbEnabled: showEntry, loading }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('KnowledgeGraph 门控与空态', () => {
  it('门控未通过 → 不渲染图，跳转 KB 详情', async () => {
    mockedGating.mockReturnValue(gating(false))

    renderPage()

    await waitFor(() => expect(screen.getByTestId('kb-detail')).toBeInTheDocument())
    expect(screen.queryByTestId('graph-view')).not.toBeInTheDocument()
    expect(mockedApi.getGraphStats).not.toHaveBeenCalled()
  })

  it('entity_count==0 → 渲染空态，不渲染图', async () => {
    mockedGating.mockReturnValue(gating(true))
    mockedApi.getGraphStats.mockResolvedValue({
      entity_count: 0,
      relation_count: 0,
      types: {},
      orphan_count: 0,
      status: 'completed',
    })

    renderPage()

    await waitFor(() => expect(screen.getByText('暂无图谱数据')).toBeInTheDocument())
    expect(screen.queryByTestId('graph-view')).not.toBeInTheDocument()
  })

  it('503 不可用 → 渲染「服务暂不可用」空态，不渲染图', async () => {
    mockedGating.mockReturnValue(gating(true))
    mockedApi.getGraphStats.mockRejectedValue(new Error('知识图谱未启用或不可用'))

    renderPage()

    await waitFor(() =>
      expect(screen.getByText('知识图谱服务暂不可用')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('graph-view')).not.toBeInTheDocument()
  })

  it('entity_count>0 → 渲染力导向图', async () => {
    mockedGating.mockReturnValue(gating(true))
    mockedApi.getGraphStats.mockResolvedValue({
      entity_count: 12,
      relation_count: 8,
      types: { 人物: 6, 组织: 6 },
      orphan_count: 0,
      status: 'completed',
    })

    renderPage()

    await waitFor(() => expect(screen.getByTestId('graph-view')).toBeInTheDocument())
  })
})
