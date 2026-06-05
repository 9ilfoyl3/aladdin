import { useRef, useEffect } from 'react'
import { Send, Database, Cpu, Library, ChevronDown, Bot, Paperclip } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuCheckboxItem } from '@/components/ui/dropdown-menu'
import { Badge } from '@/components/ui/badge'
import ContextUsageRing from '@/components/chat/ContextUsageRing'
import SessionFileList, { type PendingSessionFile } from '@/components/chat/SessionFileList'
import type { AgentPresetItem, SessionFileResponse } from '@/lib/api'

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
  selectedPreset: string
  auxiliaryKbIds: string[]
  contextUsage: { current: number; max: number }
  knowledgeBases: KnowledgeBaseItem[]
  llmConfigs: LLMConfigItem[]
  agentPresets: AgentPresetItem[]
  onInputChange: (value: string) => void
  onSend: (query?: string) => void
  onKbChange: (value: string) => void
  onModelChange: (value: string) => void
  onPresetChange: (value: string) => void
  onToggleAuxiliaryKb: (kbId: string) => void
  /** 居中静态布局（用于新对话空态），默认 false 时固定在底部 */
  centered?: boolean
  // ====== 会话文件上传（session-file-upload Task 16）======
  /** 已建索引完成的服务端文件列表 */
  sessionFiles?: SessionFileResponse[]
  /** 同步上传中的本地占位（POST 在飞 / 失败） */
  pendingSessionFiles?: PendingSessionFile[]
  /** 是否启用上传按钮：会话存在 + 未在流式响应中（关闭 KB 选择不影响） */
  canUploadSessionFile?: boolean
  /** 选择文件后触发同步上传 */
  onUploadSessionFiles?: (files: FileList) => void
  /** 移除单个已建索引文件 */
  onRemoveSessionFile?: (fileId: string) => void
  /** 关闭一个失败占位（仅本地清理） */
  onDismissPendingSessionFile?: (localId: string) => void
}

function ChatInput({
  input,
  isStreaming,
  selectedKb,
  selectedModel,
  selectedModelName,
  selectedPreset,
  auxiliaryKbIds,
  contextUsage,
  knowledgeBases,
  llmConfigs,
  agentPresets,
  onInputChange,
  onSend,
  onKbChange,
  onModelChange,
  onPresetChange,
  onToggleAuxiliaryKb,
  centered = false,
  sessionFiles = [],
  pendingSessionFiles = [],
  canUploadSessionFile = false,
  onUploadSessionFiles,
  onRemoveSessionFile,
  onDismissPendingSessionFile,
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

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

  function handlePickFile() {
    fileInputRef.current?.click()
  }

  function handleFileInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const files = e.target.files
    if (files && files.length > 0 && onUploadSessionFiles) {
      onUploadSessionFiles(files)
    }
    // 重置以便相同文件名可再次选中触发 change
    if (e.target) e.target.value = ''
  }

  // 上传按钮 + 隐藏 input（centered/底部两套布局共用）
  const uploadButton = (
    <>
      <button
        type="button"
        onClick={handlePickFile}
        disabled={!canUploadSessionFile || isStreaming}
        title={canUploadSessionFile ? '上传文件到本会话（无需选择知识库）' : '请先开始一个会话再上传文件'}
        aria-label="上传会话文件"
        className="h-7 flex items-center gap-1.5 border-none bg-muted/50 hover:bg-muted disabled:opacity-50 disabled:cursor-not-allowed rounded-lg px-2.5 text-xs text-muted-foreground cursor-pointer transition-colors whitespace-nowrap"
      >
        <Paperclip className="h-3 w-3 shrink-0" />
        <span>上传文件</span>
      </button>
      <input
        ref={fileInputRef}
        type="file"
        multiple
        className="hidden"
        onChange={handleFileInputChange}
      />
    </>
  )

  // 已上传文件列表（centered/底部两套布局共用）
  const fileListSlot = (
    <SessionFileList
      files={sessionFiles}
      pending={pendingSessionFiles}
      onRemove={(id) => onRemoveSessionFile?.(id)}
      onDismissPending={(id) => onDismissPendingSessionFile?.(id)}
    />
  )

  // 居中静态布局（新对话空态）
  if (centered) {
    return (
      <div className="w-full">
        <div className="rounded-3xl border border-border bg-card shadow-lg transition-shadow focus-within:shadow-xl focus-within:border-primary/30">
          {fileListSlot}
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => onInputChange(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="输入问题，将基于知识库和网络搜索回答..."
            className="w-full px-5 pt-5 pb-2 text-sm bg-transparent border-none outline-none resize-none placeholder:text-muted-foreground/60 min-h-[60px] max-h-[160px]"
            rows={2}
            disabled={isStreaming}
          />

          <div className="flex items-center justify-between px-3.5 pb-3.5 gap-2 flex-wrap">
            {/* 左侧工具栏 */}
            <div className="flex items-center gap-2 flex-wrap">
              {uploadButton}
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

              <Select value={selectedPreset} onValueChange={onPresetChange}>
                <SelectTrigger className="h-7 w-auto border-none bg-muted/50 hover:bg-muted rounded-lg px-2.5 gap-1.5 text-xs text-muted-foreground shadow-none focus:ring-0">
                  <Bot className="h-3 w-3 shrink-0" />
                  <SelectValue placeholder="选择智能体" />
                </SelectTrigger>
                <SelectContent>
                  {agentPresets.map((preset) => (
                    <SelectItem key={preset.id} value={preset.id}>{preset.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* 右侧 */}
            <div className="flex items-center gap-2">
              <ContextUsageRing
                currentTokens={contextUsage.current}
                maxTokens={contextUsage.max}
                visible={contextUsage.max > 0}
              />

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
                className="h-9 w-9 rounded-full shrink-0"
                onClick={() => onSend()}
                disabled={!input.trim() || isStreaming}
              >
                <Send className="h-4 w-4" />
              </Button>
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="absolute bottom-0 left-0 right-0 p-4 bg-linear-to-t from-background via-background to-transparent pt-10 pointer-events-none">
      <div className="max-w-3xl mx-auto pointer-events-auto">
        <div className="rounded-2xl border border-border bg-card shadow-lg">
          {fileListSlot}
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
              {uploadButton}
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

              <Select value={selectedPreset} onValueChange={onPresetChange}>
                <SelectTrigger className="h-7 w-auto border-none bg-muted/50 hover:bg-muted rounded-lg px-2.5 gap-1.5 text-xs text-muted-foreground shadow-none focus:ring-0">
                  <Bot className="h-3 w-3 shrink-0" />
                  <SelectValue placeholder="选择智能体" />
                </SelectTrigger>
                <SelectContent>
                  {agentPresets.map((preset) => (
                    <SelectItem key={preset.id} value={preset.id}>{preset.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            {/* 右侧 */}
            <div className="flex items-center gap-2">
              <ContextUsageRing
                currentTokens={contextUsage.current}
                maxTokens={contextUsage.max}
                visible={contextUsage.max > 0}
              />

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
                onClick={() => onSend()}
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
