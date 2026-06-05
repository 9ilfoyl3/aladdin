// 知识库容量进度条（session-file-upload Task 17 / Req 7）。
//
// 后端返回 `KBCapacityVO`：
// - used_chunks（精确）/ total_chunks（平台 KB_Chunk_Cap）
// - percent（封顶 1.0）
// - approx_total_files（向下取整，"约可容纳 N 份文档"）
// - approx_used_files（精确，已传文档数）
//
// 真实度量单位是 child chunk（Req 7.2）；文件数仅作辅助翻译，标"约"（Req 7.4/7.5）。
// 颜色档：normal / warning（≥0.8）/ full（≥1.0），Req 7.7。

import { cn } from '@/lib/utils'

// 与后端 KBCapacityVO 对齐的最小字段集
export interface KBCapacity {
  used_chunks: number
  total_chunks: number
  percent: number
  approx_total_files: number
  approx_used_files: number
}

// 颜色阈值：>=1.0 已满（红）；>=0.8 接近上限（橙）；其余正常（蓝）
const FULL_THRESHOLD = 1.0
const WARN_THRESHOLD = 0.8

// 单位换算：百万级 chunk 显示更友好（与文档数一样作辅助文案，不做严格度量）
function formatChunks(n: number): string {
  if (n >= 1_000_000) {
    const v = n / 1_000_000
    return `${v >= 10 ? v.toFixed(0) : v.toFixed(1)}M`
  }
  if (n >= 1_000) {
    const v = n / 1_000
    return `${v >= 10 ? v.toFixed(0) : v.toFixed(1)}k`
  }
  return String(n)
}

interface Props {
  capacity: KBCapacity
  // compact=true 用于知识库列表卡片（极简，单行）；false 用于详情页头部（完整）
  compact?: boolean
  className?: string
}

// 进度条 + 辅助文字（约可容纳 N 份文档），接近/已满变色
export default function KBCapacityBar({ capacity, compact = false, className }: Props) {
  // 兜底：percent 已被后端封顶到 [0,1]，前端再做一次防御
  const pct = Math.max(0, Math.min(1, Number.isFinite(capacity.percent) ? capacity.percent : 0))
  const pctDisplay = Math.round(pct * 100)

  const isFull = pct >= FULL_THRESHOLD
  const isWarn = !isFull && pct >= WARN_THRESHOLD

  // 进度条填充色（使用 tailwind 调色，命中需求 7.7 的"接近/已满颜色提示"）
  const fillCls = isFull
    ? 'bg-red-500'
    : isWarn
      ? 'bg-amber-500'
      : 'bg-primary'

  // 文案前景色（接近/已满时让数字也更醒目）
  const textCls = isFull
    ? 'text-red-600'
    : isWarn
      ? 'text-amber-600'
      : 'text-muted-foreground'

  const usedFiles = Math.max(0, capacity.approx_used_files)
  const approxFiles = Math.max(0, capacity.approx_total_files)

  return (
    <div className={cn('w-full', className)}>
      {/* 顶部数字行：左侧 used/total（chunk 真实度量），右侧百分比 + 状态徽标 */}
      <div className={cn('flex items-baseline justify-between gap-2', compact ? 'text-[11px]' : 'text-xs')}>
        <span className={cn('truncate', textCls)}>
          已用 {formatChunks(capacity.used_chunks)} / {formatChunks(capacity.total_chunks)} chunk
        </span>
        <span className={cn('shrink-0 tabular-nums', textCls)}>
          {pctDisplay}%
          {isFull && <span className="ml-1.5 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium bg-red-100 text-red-700">已满</span>}
          {isWarn && <span className="ml-1.5 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium bg-amber-100 text-amber-700">接近上限</span>}
        </span>
      </div>

      {/* 横向进度条（无第三方依赖，div 实现） */}
      <div
        className={cn('mt-1 w-full overflow-hidden rounded-full bg-muted', compact ? 'h-1.5' : 'h-2')}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pctDisplay}
        aria-label="知识库容量"
      >
        <div
          className={cn('h-full transition-all duration-300', fillCls)}
          style={{ width: `${pct * 100}%` }}
        />
      </div>

      {/* 辅助文字：约可容纳 N 份文档 + 已用 X 份。Req 7.4/7.5 强调"约" */}
      <div className={cn('mt-1 flex items-center justify-between gap-2', compact ? 'text-[10px]' : 'text-xs', 'text-muted-foreground')}>
        <span className="truncate">已用 {usedFiles} / 共约 {approxFiles} 份</span>
        {!compact && (
          <span className="shrink-0" title="估算值仅供参考，真实入库判定以 chunk 为准">
            约可容纳 {approxFiles} 份文档
          </span>
        )}
      </div>
    </div>
  )
}
