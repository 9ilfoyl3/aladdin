import { Database, ChevronDown } from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuCheckboxItem,
} from '@/components/ui/dropdown-menu'
import { Badge } from '@/components/ui/badge'

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
 * 知识库多选器：合并原"主知识库 + 关联知识库"两个下拉为单一多选下拉。
 *
 * - 选中顺序的首个库即后端 ``kb_ids[0]``（检索权重 1.0），用户无需理解主/副概念。
 * - 同一库不会重复出现，从根上消除"主库与副库可选到同一个库"的交互矛盾。
 */
function KbSelector({ knowledgeBases, selectedKbIds, onToggle }: KbSelectorProps) {
  const count = selectedKbIds.length
  const firstName = knowledgeBases.find((kb) => kb.id === selectedKbIds[0])?.name

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="h-7 flex items-center gap-1.5 border-none bg-muted/50 hover:bg-muted rounded-lg px-2.5 text-xs text-muted-foreground cursor-pointer transition-colors whitespace-nowrap">
          <Database className="h-3 w-3 shrink-0" />
          <span>{count === 0 ? '知识库' : firstName || '知识库'}</span>
          {count > 1 && (
            <Badge
              variant="outline"
              className="text-[10px] px-1 py-0 h-4 min-w-4 flex items-center justify-center"
            >
              {count}
            </Badge>
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
