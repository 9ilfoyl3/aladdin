// 知识图谱「视图可见性门控」hook（design.md 5.3.1，Requirements 6.5）。
//
// 三层门控的前两层（入口是否出现）收敛到这里，供 KB 详情页与图谱页共用：
//   1. 全局：GET /system/frontend-config 的 graph_enabled
//      （= 后端 GRAPH_ENABLE 且 Neo4j 可用）。全局关 → 所有 KB 都不显示入口。
//   2. KB 级：KB 详情 config.graph.enabled。仅该 KB 开启图谱才显示入口。
// 第三层「数据态」（entity_count==0 / 503）由图谱页进入后用 stats 判定，不在此 hook。
//
// 数据流：组件只读本 hook 返回的派生布尔值；请求经 react-query 缓存，
// 不在组件内散落 fetch（保持单向、不过度封装）。

import { useQuery } from '@tanstack/react-query'

import { graphApi, systemApi } from '@/lib/api'

interface GraphGating {
  /** 是否应在该 KB 显示「知识图谱」入口（全局开 且 KB 开） */
  showEntry: boolean
  /** 全局能力开关（后端 GRAPH_ENABLE + Neo4j 可用） */
  globalEnabled: boolean
  /** 该 KB 是否开启图谱（config.graph.enabled） */
  kbEnabled: boolean
  /** 门控判定所需数据仍在加载（避免入口闪烁） */
  loading: boolean
}

/**
 * 计算某 KB 的图谱入口可见性（全局 ∧ KB 级双层门控）。
 *
 * @param kbId 知识库 id；为空（未进入具体 KB）时不查询 KB 配置。
 */
export function useGraphGating(kbId: string | undefined): GraphGating {
  // 第一层：全局能力开关。随前端配置一起缓存（与 Documents 页同 queryKey 复用缓存）。
  const { data: frontendConfig, isLoading: globalLoading } = useQuery({
    queryKey: ['frontend-config'],
    queryFn: () => systemApi.getFrontendConfig(),
    staleTime: 60000,
  })
  const globalEnabled = frontendConfig?.graph_enabled === true

  // 第二层：KB 级开关（config.graph.enabled）。仅在全局开启且有 kbId 时才查询，
  // 全局关时直接短路（enabled:false），不产生无谓请求。
  const { data: graphConfig, isLoading: kbLoading } = useQuery({
    queryKey: ['graph-config', kbId],
    queryFn: () => graphApi.getGraphConfig(kbId!),
    enabled: !!kbId && globalEnabled,
    staleTime: 60000,
  })
  const kbEnabled = graphConfig?.enabled === true

  return {
    showEntry: globalEnabled && kbEnabled,
    globalEnabled,
    kbEnabled,
    // 全局加载中；或全局已开、KB 配置仍在加载时，视为加载中。
    loading: globalLoading || (globalEnabled && !!kbId && kbLoading),
  }
}
