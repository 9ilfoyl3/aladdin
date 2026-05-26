import { useRef, useEffect } from 'react'
import { Send, Database, Cpu, Library, ChevronDown } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuCheckboxItem } from '@/components/ui/dropdown-menu'
import { Badge } from '@/components/ui/badge'

interface KnowledgeBaseItem {
  id: string
  name: string
}

interface LLMConfigItem {
  id: string
  name: string
  provider: string
  base_url: string
  model: string
  is_default: boolean
}

interface ChatInputProps {
  input: string
  isStreaming: boolean
  selectedKb: string
  selectedModel: string
  selectedModelName: string
  retrievalMode: string
  auxiliaryKbIds: string[]
  knowledgeBases: KnowledgeBaseItem[]
  llmConfigs: LLMConfigItem[]
  onInputChange: (value: string) => void
  onSend: () => void
  onKbChange: (value: string) => void
  onModelChange: (value: string) => void
  onRetrievalModeChange: (value: string) => void
  onToggleAuxiliaryKb: (kbId: string) => void
}

function ChatInput({
  input,
  isStreaming,
  selectedKb,
  selectedModel,
  selectedModelName,
  retrievalMode,
  auxiliaryKbIds,
  knowledgeBases,
  llmConfigs,
  onInputChange,
  onSend,
  onKbChange,
  onModelChange,
  onRetrievalModeChange,
  onToggleAuxiliaryKb,
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto'
      textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 120) + 'px'
    }
  }, [input])

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSend()
    }
  }

  return (
    <div className="absolute bottom-0 left-0 right-0 p-4 bg-linear-to-t from-background via-background to-transparent pt-10 pointer-events-none">
      <div className="max-w-3xl mx-auto pointer-events-auto">
        <div className="rounded-2xl border border-border bg-card shadow-lg">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => onInputChange(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入问题，按 Enter 发送..."
            className="w-full px-4 pt-4 pb-2 text-sm bg-transparent border-none outline-none resize-none placeholder:text-muted-foreground/60 min-h-[44px] max-h-[120px]"
            rows={1}
            disabled={isStreaming}
          />

          <div className="flex items-center justify-between px-3 pb-3 gap-2 flex-wrap">
            {/* 左侧工具栏 */}
            <div className="flex items-center gap-2 flex-wrap">
              <Select value={selectedKb} onValueChange={onKbChange}>
                <SelectTrigger className="h-7 w-auto border-none bg-muted/50 hover:bg-muted rounded-lg px-2.5 gap-1.5 text-xs text-muted-foreground shadow-none focus:ring-0">
                  <Database className="h-3 w-3 shrink-0" />
                  <SelectValue placeholder="全部知识库" />
                </SelectTrigger>
                <SelectContent>
                  {knowledgeBases.map((kb) => (
                    <SelectItem key={kb.id} value={kb.id}>{kb.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>

              <Select value={retrievalMode} onValueChange={onRetrievalModeChange}>
                <SelectTrigger className="h-7 w-auto border-none bg-muted/50 hover:bg-muted rounded-lg px-2.5 gap-1.5 text-xs text-muted-foreground shadow-none focus:ring-0">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="auto">智能检索</SelectItem>
                  <SelectItem value="hybrid">快速检索</SelectItem>
                </SelectContent>
              </Select>

              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button className="h-7 flex items-center gap-1.5 border-none bg-muted/50 hover:bg-muted rounded-lg px-2.5 text-xs text-muted-foreground cursor-pointer transition-colors whitespace-nowrap">
                    <Library className="h-3 w-3 shrink-0" />
                    <span>关联知识库</span>
                    {auxiliaryKbIds.length > 0 && (
                      <Badge variant="outline" className="text-[10px] px-1 py-0 h-4 min-w-4 flex items-center justify-center">
                        {auxiliaryKbIds.length}
                      </Badge>
                    )}
                    <ChevronDown className="h-3 w-3" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent side="top" align="start">
                  {knowledgeBases.length === 0 ? (
                    <div className="px-3 py-2 text-xs text-muted-foreground">暂无可用知识库</div>
                  ) : (
                    knowledgeBases
                      .filter((kb) => kb.id !== selectedKb)
                      .map((kb) => (
                        <DropdownMenuCheckboxItem
                          key={kb.id}
                          checked={auxiliaryKbIds.includes(kb.id)}
                          onCheckedChange={() => onToggleAuxiliaryKb(kb.id)}
                          onSelect={(e) => e.preventDefault()}
                        >
                          {kb.name}
                        </DropdownMenuCheckboxItem>
                      ))
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>

            {/* 右侧 */}
            <div className="flex items-center gap-2">
              <Select value={selectedModel} onValueChange={onModelChange}>
                <SelectTrigger className="h-7 w-auto border-none bg-transparent hover:bg-muted/50 rounded-lg px-2 gap-1 text-xs text-muted-foreground shadow-none focus:ring-0">
                  <Cpu className="h-3 w-3 shrink-0" />
                  <span className="max-w-[15em] truncate">{selectedModelName || '模型'}</span>
                </SelectTrigger>
                <SelectContent>
                  {llmConfigs.length === 0 ? (
                    <div className="px-3 py-2 text-xs text-muted-foreground">暂无可用的对话模型</div>
                  ) : (
                    llmConfigs.map((config) => (
                      <SelectItem key={config.id} value={config.id}>{config.name}</SelectItem>
                    ))
                  )}
                </SelectContent>
              </Select>

              <Button
                size="icon"
                className="h-8 w-8 rounded-full shrink-0"
                onClick={onSend}
                disabled={!input.trim() || isStreaming}
              >
                <Send className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default ChatInput
