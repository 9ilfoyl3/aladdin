import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

interface ContextUsageRingProps {
  currentTokens: number
  maxTokens: number
  visible: boolean
}

// SVG 圆环尺寸常量：直径 24px，描边宽度 3px
const SIZE = 24
const STROKE_WIDTH = 3
const RADIUS = (SIZE - STROKE_WIDTH) / 2
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

/**
 * 根据利用率返回前景环的描边颜色：
 * <50% primary（绿色）、50-80% amber-500（黄色）、>80% destructive（红色）
 */
function getStrokeColor(percentage: number): string {
  if (percentage > 80) return 'stroke-destructive'
  if (percentage >= 50) return 'stroke-amber-500'
  return 'stroke-primary'
}

function ContextUsageRing({ currentTokens, maxTokens, visible }: ContextUsageRingProps) {
  // visible=false 或 maxTokens=0 时不渲染
  if (!visible || maxTokens === 0) {
    return null
  }

  const percentage = maxTokens > 0 ? Math.min(100, Math.round((currentTokens / maxTokens) * 100)) : 0
  const strokeColor = getStrokeColor(percentage)
  const dashOffset = CIRCUMFERENCE * (1 - percentage / 100)

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <div className="relative inline-flex items-center justify-center" style={{ width: SIZE, height: SIZE }}>
            <svg
              width={SIZE}
              height={SIZE}
              viewBox={`0 0 ${SIZE} ${SIZE}`}
              className="-rotate-90"
            >
              {/* 背景环 */}
              <circle
                cx={SIZE / 2}
                cy={SIZE / 2}
                r={RADIUS}
                fill="none"
                strokeWidth={STROKE_WIDTH}
                className="stroke-muted"
              />
              {/* 前景进度环 */}
              <circle
                cx={SIZE / 2}
                cy={SIZE / 2}
                r={RADIUS}
                fill="none"
                strokeWidth={STROKE_WIDTH}
                strokeLinecap="round"
                strokeDasharray={CIRCUMFERENCE}
                strokeDashoffset={dashOffset}
                className={cn('transition-[stroke-dashoffset] duration-300', strokeColor)}
              />
            </svg>
            {/* 圆环中心百分比数字 */}
            <span className="absolute text-[9px] font-medium text-muted-foreground tabular-nums">
              {percentage}
            </span>
          </div>
        </TooltipTrigger>
        <TooltipContent>
          <div className="space-y-1.5">
            <div className="text-xs font-medium text-foreground">Context usage</div>
            <div className="grid grid-cols-[auto_auto] gap-x-4 gap-y-0.5 text-xs text-muted-foreground">
              <span>Conversation</span>
              <span className="text-right tabular-nums">{percentage}%</span>
              <span>Tools</span>
              <span className="text-right tabular-nums">{percentage}%</span>
              <span>Total</span>
              <span className="text-right tabular-nums">{percentage}%</span>
            </div>
          </div>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

export default ContextUsageRing
