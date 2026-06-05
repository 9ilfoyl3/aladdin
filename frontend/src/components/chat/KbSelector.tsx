import { Database, ChevronDown } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuCheckboxItem,
} from '@/components/ui/dropdown-menu'

interface KnowledgeBaseItem {
  id: string
  name: string
}

interface KbSelectorProps {
  /** 可选知识库列表 */
  knowledgeBases: KnowledgeBaseItem[]
  /** 已选知识库 ID（按选中顺序，首个作为后端主库 kb_ids[0]，权重更高） */
  selectedKbIds: string[]
  /** 切换某个知识库的选中状态（多选） */
  onToggle: (kbId: string) => void
}

/**
 * 知识库多选器：作为选择入口。未选时显示「知识库」文本；已选时隐藏文本，仅在数据库图标
 * 后显示一个圆形数字角标表示已选数量。选中的具体知识库由独立的 KbSelectionList 区域渲染。
 */
function KbSelector({ knowledgeBases, selectedKbIds, onToggle }: KbSelectorProps) {
  const count = selectedKbIds.length

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="h-7 flex items-center gap-1.5 border-none bg-muted/50 hover:bg-muted rounded-lg px-2.5 text-xs text-muted-foreground cursor-pointer transition-colors whitespace-nowrap">
          <Database className="h-3 w-3 shrink-0" />
          {count > 0 ? (
            <span className="h-4 min-w-4 px-1 flex items-center justify-center rounded-full bg-muted-foreground/20 text-foreground text-[10px] leading-none">
              {count}
            </span>
          ) : (
            <span>知识库</span>
          )}
          <ChevronDown className="h-3 w-3" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start">
        {knowledgeBases.length === 0 ? (
          <div className="px-3 py-2 text-xs text-muted-foreground">暂无可用知识库</div>
        ) : (
          knowledgeBases.map((kb) => (
            <DropdownMenuCheckboxItem
              key={kb.id}
              checked={selectedKbIds.includes(kb.id)}
              onCheckedChange={() => onToggle(kb.id)}
              onSelect={(e) => e.preventDefault()}
            >
              {kb.name}
            </DropdownMenuCheckboxItem>
          ))
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export default KbSelector
