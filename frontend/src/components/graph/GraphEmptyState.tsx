// 知识图谱空状态 / 不可用态（design.md 5.3.4，Requirements 6.6）。
//
// 第三层「数据态」门控的展示组件：进入图谱视图后，依据 graphStore 的状态决定
// 渲染力导向图还是本组件，避免出现空白画布或运行时崩溃。
//
// 三种态（互斥，按优先级）：
// - unavailable：后端返回 503（图存储未启用/不可用）→「服务暂不可用」。
// - empty：entity_count == 0（尚未抽取 / 抽取中）→「图谱构建中或暂无数据」。
// - error：其它加载错误（网络等）→ 通用错误提示 + 重试。
//
// 本组件无副作用、纯展示：重试由父组件经 store action 触发（数据流单向，
// 组件内不散落 fetch）。task 6.3 的图谱页面在 nodes 为空时渲染本组件。

import { AlertTriangle, Loader2, Network, RefreshCw } from 'lucide-react'

import { Button } from '@/components/ui/button'

/** 空态种类。父组件依据 graphStore 状态映射：unavailable > error > empty。 */
export type GraphEmptyVariant = 'unavailable' | 'empty' | 'error'

interface Props {
  variant: GraphEmptyVariant
  /** error 态的具体文案（来自 store.error），unavailable/empty 用内置文案 */
  message?: string | null
  /** entity_count==0 但抽取仍在进行（status=processing/pending）时展示「构建中」语义 */
  building?: boolean
  /** 重试回调（由父组件经 store action 实现，可选） */
  onRetry?: () => void
  /** 重试进行中（禁用按钮并转圈） */
  retrying?: boolean
}

// 各态的图标 / 标题 / 描述（集中配置，渲染逻辑只读取，便于维护）。
const VARIANT_META: Record<
  GraphEmptyVariant,
  { icon: typeof Network; title: string; desc: string; tone: string }
> = {
  unavailable: {
    icon: AlertTriangle,
    title: '知识图谱服务暂不可用',
    desc: '图谱存储未启用或暂时无法连接，请稍后重试或联系管理员。',
    tone: 'text-amber-500',
  },
  empty: {
    icon: Network,
    title: '暂无图谱数据',
    desc: '该知识库已开启图谱功能，但尚未抽取出实体与关系。新增文档入库后会自动构建。',
    tone: 'text-muted-foreground/50',
  },
  error: {
    icon: AlertTriangle,
    title: '图谱加载失败',
    desc: '加载图谱数据时出现问题，请重试。',
    tone: 'text-destructive',
  },
}

// 「构建中」覆盖 empty 态文案（entity_count==0 且抽取进行中）。
const BUILDING_META = {
  title: '图谱构建中',
  desc: '正在从文档中抽取实体与关系，完成后图谱会自动出现。可稍后刷新查看进度。',
}

/**
 * 图谱空态 / 不可用态展示。纯展示组件，不发起请求；重试经 onRetry 上抛父组件。
 */
export default function GraphEmptyState({
  variant,
  message,
  building = false,
  onRetry,
  retrying = false,
}: Props) {
  const meta = VARIANT_META[variant]
  // empty 态在「构建中」时替换标题/描述，并把图标换成转圈以传达进行中语义。
  const isBuilding = variant === 'empty' && building
  const Icon = isBuilding ? Loader2 : meta.icon
  const title = isBuilding ? BUILDING_META.title : meta.title
  // error 态优先展示后端/网络的具体 message，回退到内置描述。
  const desc =
    variant === 'error' && message
      ? message
      : isBuilding
        ? BUILDING_META.desc
        : meta.desc

  return (
    <div className="flex h-full w-full flex-col items-center justify-center px-6 py-16 text-center">
      <div className="mb-4 flex h-20 w-20 items-center justify-center rounded-2xl bg-muted/40">
        <Icon className={`h-10 w-10 ${meta.tone} ${isBuilding ? 'animate-spin' : ''}`} />
      </div>
      <p className="mb-1 text-base font-medium text-foreground">{title}</p>
      <p className="max-w-md text-sm text-muted-foreground/80">{desc}</p>

      {onRetry && (
        <Button
          variant="outline"
          size="sm"
          className="mt-5 gap-1.5"
          onClick={onRetry}
          disabled={retrying}
        >
          <RefreshCw className={`h-4 w-4 ${retrying ? 'animate-spin' : ''}`} />
          {retrying ? '刷新中…' : '刷新重试'}
        </Button>
      )}
    </div>
  )
}
