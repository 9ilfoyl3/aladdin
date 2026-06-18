// 知识图谱页面（design.md 5.3.1 三层门控 + 5.3.4 降级与空态）。
//
// 门控 + 空态/不可用态 + 有数据时的力导向图交互视图：
//   - 第一/二层门控（全局 + KB 级）：useGraphGating。未通过 → 不进入图谱，
//     回退到 KB 详情页（入口本就不该出现，直达 URL 时也不暴露图谱）。
//   - 第三层数据态：进入后调 GET /graph/stats。
//       · 503（图存储不可用）→ GraphEmptyState variant="unavailable"
//       · entity_count == 0 →   GraphEmptyState variant="empty"（status=processing 时显示「构建中」）
//       · 加载错误 →            GraphEmptyState variant="error"
//       · entity_count > 0 →    渲染力导向图交互视图（GraphView）
//
// 数据流单向：页面用 react-query 拉 stats（带缓存），渲染分支只读派生状态；
// 图查询/交互经 graphStore 触发，组件内不散落 fetch。

import { useEffect } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft } from 'lucide-react'

import { graphApi } from '@/lib/api'
import { useGraphStore } from '@/stores/graphStore'
import GraphEmptyState from '@/components/graph/GraphEmptyState'
import GraphView from '@/components/graph/GraphView'
import { useGraphGating } from '@/components/graph/useGraphGating'

// 与后端 graph.py 一致的 503 文案，用于把普通错误与「服务不可用」区分开。
const STORE_UNAVAILABLE_DETAIL = '知识图谱未启用或不可用'

// stats.status 处于这些值时，entity_count==0 解读为「构建中」而非「暂无数据」。
const BUILDING_STATUSES = new Set(['pending', 'processing'])

function KnowledgeGraph() {
  const { id: kbId } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const setKb = useGraphStore((s) => s.setKb)

  // —— 第一/二层门控（全局 + KB 级）——
  const { showEntry, loading: gatingLoading } = useGraphGating(kbId)

  // 门控未通过且已判定完成：图谱入口本不该出现，直达 URL 时退回 KB 详情，不暴露图谱。
  useEffect(() => {
    if (!gatingLoading && !showEntry && kbId) {
      navigate(`/knowledge-bases/${kbId}`, { replace: true })
    }
  }, [gatingLoading, showEntry, kbId, navigate])

  // 进入页面时把当前 KB 同步给 graphStore（切库重置图状态，供画布使用）。
  useEffect(() => {
    if (kbId) setKb(kbId)
  }, [kbId, setKb])

  // —— 第三层数据态：图谱统计 ——
  // 仅在门控通过后才查询；503 不重试（图存储不可用是稳定态，重试无意义）。
  const {
    data: stats,
    isLoading: statsLoading,
    error: statsError,
    refetch,
    isRefetching,
  } = useQuery({
    queryKey: ['graph-stats', kbId],
    queryFn: () => graphApi.getGraphStats(kbId!),
    enabled: !!kbId && showEntry,
    retry: false,
  })

  // 门控判定中 / stats 首次加载中：渲染轻量加载占位，避免空白或闪烁。
  if (gatingLoading || (showEntry && statsLoading)) {
    return (
      <PageShell kbId={kbId}>
        <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
          加载中…
        </div>
      </PageShell>
    )
  }

  // 门控未通过：useEffect 已触发跳转，这里渲染空避免闪现。
  if (!showEntry) return null

  // 错误分支：区分 503（不可用）与其它错误。
  if (statsError) {
    const message = statsError instanceof Error ? statsError.message : String(statsError)
    const unavailable = message.includes(STORE_UNAVAILABLE_DETAIL)
    return (
      <PageShell kbId={kbId}>
        <GraphEmptyState
          variant={unavailable ? 'unavailable' : 'error'}
          message={unavailable ? null : message}
          onRetry={() => refetch()}
          retrying={isRefetching}
        />
      </PageShell>
    )
  }

  // 空数据分支：entity_count==0 → 构建中 / 暂无数据。
  if (!stats || stats.entity_count === 0) {
    const building = !!stats && BUILDING_STATUSES.has(stats.status)
    return (
      <PageShell kbId={kbId}>
        <GraphEmptyState
          variant="empty"
          building={building}
          onRetry={() => refetch()}
          retrying={isRefetching}
        />
      </PageShell>
    )
  }

  // 有数据：渲染力导向图交互视图（GraphCanvas/Legend/Drawer/SearchBar）。
  return (
    <PageShell kbId={kbId}>
      <GraphView />
    </PageShell>
  )
}

// 页面外壳：统一头部（返回 KB 详情 + 标题），内容区由调用方填充。
function PageShell({ kbId, children }: { kbId: string | undefined; children: React.ReactNode }) {
  return (
    <div className="relative flex h-full flex-col">
      <div className="mb-4 flex shrink-0 items-center gap-3">
        <Link to={kbId ? `/knowledge-bases/${kbId}` : '/knowledge-bases'}>
          <button className="flex h-8 w-8 cursor-pointer items-center justify-center rounded-lg transition-colors hover:bg-muted">
            <ArrowLeft className="h-4 w-4 text-muted-foreground" />
          </button>
        </Link>
        <h1 className="text-2xl font-bold tracking-tight">知识图谱</h1>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-card">
        {children}
      </div>
    </div>
  )
}

export default KnowledgeGraph
