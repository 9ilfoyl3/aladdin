// useGraphGating 门控逻辑测试（design.md 5.3.1，Requirements 6.5）。
//
// 验证「全局 ∧ KB 级」双层门控：
//  - 全局关 → showEntry=false，且不查询 KB 级配置（短路）
//  - 全局开 + KB 关 → showEntry=false
//  - 全局开 + KB 开 → showEntry=true
//
// hook 经 react-query 取数，测试用 QueryClientProvider 包裹并 mock api。

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { createElement, type ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { graphApi, systemApi } from '@/lib/api'

import { useGraphGating } from './useGraphGating'

vi.mock('@/lib/api', () => ({
  graphApi: { getGraphConfig: vi.fn() },
  systemApi: { getFrontendConfig: vi.fn() },
}))

const mockedGraphApi = vi.mocked(graphApi)
const mockedSystemApi = vi.mocked(systemApi)

// 每个用例新建 QueryClient，禁用重试与缓存复用，隔离用例。
function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  })
  return createElement(QueryClientProvider, { client }, children)
}

function frontendConfig(graphEnabled: boolean) {
  return {
    upload_max_concurrent: 3,
    upload_max_file_size_mb: 50,
    graph_enabled: graphEnabled,
  }
}

function graphConfig(enabled: boolean) {
  return {
    enabled,
    entity_types: [],
    relation_types: [],
    extract_granularity: 'parent',
    extract_model_id: null,
    enable_alias_dedup: true,
    alias_sim_threshold: 0.92,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useGraphGating', () => {
  it('全局关闭 → 不显示入口且不查询 KB 配置', async () => {
    mockedSystemApi.getFrontendConfig.mockResolvedValue(frontendConfig(false))

    const { result } = renderHook(() => useGraphGating('kb1'), { wrapper })

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.globalEnabled).toBe(false)
    expect(result.current.showEntry).toBe(false)
    // 全局关时 KB 级查询被 enabled:false 短路
    expect(mockedGraphApi.getGraphConfig).not.toHaveBeenCalled()
  })

  it('全局开 + KB 关 → 不显示入口', async () => {
    mockedSystemApi.getFrontendConfig.mockResolvedValue(frontendConfig(true))
    mockedGraphApi.getGraphConfig.mockResolvedValue(graphConfig(false))

    const { result } = renderHook(() => useGraphGating('kb1'), { wrapper })

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.globalEnabled).toBe(true)
    expect(result.current.kbEnabled).toBe(false)
    expect(result.current.showEntry).toBe(false)
  })

  it('全局开 + KB 开 → 显示入口', async () => {
    mockedSystemApi.getFrontendConfig.mockResolvedValue(frontendConfig(true))
    mockedGraphApi.getGraphConfig.mockResolvedValue(graphConfig(true))

    const { result } = renderHook(() => useGraphGating('kb1'), { wrapper })

    await waitFor(() => expect(result.current.showEntry).toBe(true))
    expect(result.current.globalEnabled).toBe(true)
    expect(result.current.kbEnabled).toBe(true)
  })

  it('无 kbId → 全局开但不查询 KB，入口不显示', async () => {
    mockedSystemApi.getFrontendConfig.mockResolvedValue(frontendConfig(true))

    const { result } = renderHook(() => useGraphGating(undefined), { wrapper })

    await waitFor(() => expect(result.current.globalEnabled).toBe(true))
    expect(result.current.showEntry).toBe(false)
    expect(mockedGraphApi.getGraphConfig).not.toHaveBeenCalled()
  })
})
