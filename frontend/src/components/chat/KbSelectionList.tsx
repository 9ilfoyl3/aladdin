import { X, Database } from 'lucide-react'
import { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider } from '@/components/ui/tooltip'

interface KnowledgeBaseItem {
  id: string
  name: string
}

interface KbSelectionListProps {
  /** 全部可选知识库（用于把已选 ID 映射为名称） */
  knowledgeBases: KnowledgeBaseItem[]
  /** 已选知识库 ID（按选中顺序） */
  selectedKbIds: string[]
  /** 移除单个已选知识库 */
  onRemove: (kbId: string) => void
}

/**
 * 已选知识库展示区（chip 横向布局），与「已上传文件区」风格一致。
 *
 * 把选中的知识库从 action 工具栏胶囊里抽出来，单独成区渲染，每个 chip 可单独移除。
 * 无选中时整块隐藏（由本组件自行判断，父组件无需条件挂载）。
 */
function KbSelectionList({ knowledgeBases, selectedKbIds, onRemove }: KbSelectionListProps) {
  if (selectedKbIds.length === 0) return null

  // 已选 ID → 名称（按选中顺序保留；找不到名称的库回退显示 ID 末段，避免空白）
  const chips = selectedKbIds.map((id) => ({
    id,
    name: knowledgeBases.find((kb) => kb.id === id)?.name || id,
  }))

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex items-center gap-1.5 flex-wrap px-2.5 pt-2.5">
        {chips.map((c) => (
          <Tooltip key={c.id}>
            <TooltipTrigger asChild>
              <div className="group inline-flex items-center gap-1.5 h-8 pl-2 pr-1 rounded-xl border border-border bg-card text-xs text-foreground transition-colors hover:border-primary/40 hover:bg-muted/40 max-w-[15em]">
                <Database className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <span className="truncate font-medium">{c.name}</span>
                <button
                  type="button"
                  onClick={() => onRemove(c.id)}
                  className="h-5 w-5 shrink-0 flex items-center justify-center rounded-md text-muted-foreground hover:text-destructive hover:bg-destructive/10 cursor-pointer transition-colors"
                  aria-label={`移除知识库 ${c.name}`}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            </TooltipTrigger>
            <TooltipContent side="top" className="max-w-xs">
              <div className="font-medium break-all leading-snug">{c.name}</div>
              <div className="mt-0.5 text-xs text-muted-foreground">点击 × 取消选择该知识库</div>
            </TooltipContent>
          </Tooltip>
        ))}
      </div>
    </TooltipProvider>
  )
}

export default KbSelectionList
