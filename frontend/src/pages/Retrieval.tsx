import { useState, useEffect } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { Search, FileText, Zap, AlertCircle, ChevronDown, ChevronRight } from 'lucide-react'
import { retrievalApi, knowledgeBaseApi, llmConfigApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { Label } from '@/components/ui/label'
import { Slider } from '@/components/ui/slider'
import { Badge } from '@/components/ui/badge'
import RetrievalResultsSkeleton from '@/components/skeletons/RetrievalResultsSkeleton'

// 知识库类型
interface KnowledgeBaseItem {
  id: string
  name: string
}

// LLM 模型配置类型
interface LLMConfigItem {
  id: string
  name: string
  is_default: boolean
}

// 检索结果类型
interface RetrievalResult {
  chunk_id: string
  content: string
  child_content: string
  score: number
  doc_id: string
  filename: string
  metadata?: Record<string, unknown>
}

// 检索响应类型
interface RetrievalResponse {
  results: RetrievalResult[]
  iterations?: number
  degraded?: boolean
}

// Agent 进度步骤
interface AgentStep {
  step: string
  detail: string
}

// 检索测试页面
function Retrieval() {
  const [query, setQuery] = useState('')
  const [selectedKb, setSelectedKb] = useState('')
  const [selectedModel, setSelectedModel] = useState('')
  const [mode, setMode] = useState('hybrid')
  const [topK, setTopK] = useState(10)
  const [expandedItems, setExpandedItems] = useState<Set<number>>(new Set())
  const [agentSteps, setAgentSteps] = useState<AgentStep[]>([])
  const [isSearching, setIsSearching] = useState(false)
  const [searchResult, setSearchResult] = useState<RetrievalResponse | null>(null)
  const [searchError, setSearchError] = useState<string | null>(null)

  // 获取知识库列表
  const { data: knowledgeBases = [] } = useQuery({
    queryKey: ['knowledge-bases'],
    queryFn: () => knowledgeBaseApi.list() as Promise<KnowledgeBaseItem[]>,
  })

  // 获取 LLM 模型列表
  const { data: llmConfigs = [] } = useQuery({
    queryKey: ['llm-configs'],
    queryFn: () => llmConfigApi.list() as Promise<LLMConfigItem[]>,
  })

  // 默认选中 is_default 的模型
  useEffect(() => {
    if (llmConfigs.length > 0 && !selectedModel) {
      const defaultConfig = llmConfigs.find((c) => c.is_default)
      if (defaultConfig) {
        setSelectedModel(defaultConfig.id)
      }
    }
  }, [llmConfigs, selectedModel])

  // 检索请求（非 agent 模式）
  const searchMutation = useMutation({
    mutationFn: () =>
      retrievalApi.test({
        query,
        knowledge_base_id: selectedKb,
        mode,
        top_k: topK,
        model_config_id: mode === 'agent' ? (selectedModel || undefined) : undefined,
      } as Parameters<typeof retrievalApi.test>[0] & { model_config_id?: string }) as Promise<RetrievalResponse>,
    onSuccess: (data) => {
      setSearchResult(data)
      setSearchError(null)
    },
    onError: (err: Error) => {
      setSearchError(err.message || '检索失败')
      setSearchResult(null)
    },
  })

  // 执行检索
  async function handleSearch(e: React.FormEvent) {
    e.preventDefault()
    if (!query.trim() || !selectedKb) return

    setExpandedItems(new Set())
    setAgentSteps([])
    setSearchError(null)

    if (mode === 'agent') {
      // Agent 模式：SSE 流式
      setIsSearching(true)
      setSearchResult(null)
      try {
        const response = await fetch('/api/retrieval/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            query,
            knowledge_base_id: selectedKb,
            mode: 'agent',
            top_k: topK,
            model_config_id: selectedModel || undefined,
          }),
        })

        if (!response.ok) {
          throw new Error(`请求失败: ${response.status}`)
        }

        const reader = response.body?.getReader()
        const decoder = new TextDecoder()
        const steps: AgentStep[] = []

        if (reader) {
          while (true) {
            const { done, value } = await reader.read()
            if (done) break

            const text = decoder.decode(value, { stream: true })
            const lines = text.split('\n')

            for (const line of lines) {
              if (!line.startsWith('data: ')) continue
              const data = line.slice(6).trim()
              if (data === '[DONE]') continue

              try {
                const parsed = JSON.parse(data)
                if (parsed.type === 'progress') {
                  steps.push({ step: parsed.step, detail: parsed.detail })
                  setAgentSteps([...steps])
                } else if (parsed.type === 'result') {
                  setSearchResult({
                    results: parsed.results,
                    iterations: parsed.iterations,
                    degraded: parsed.degraded,
                  })
                }
              } catch {
                // 忽略解析错误
              }
            }
          }
        }
      } catch (err) {
        setSearchError(err instanceof Error ? err.message : '检索失败')
      } finally {
        setIsSearching(false)
      }
    } else {
      // 非 agent 模式：普通请求
      searchMutation.mutate()
    }
  }

  const results: RetrievalResult[] = searchResult?.results || searchMutation.data?.results || []
  const isPending = isSearching || searchMutation.isPending
  const isError = !!searchError || searchMutation.isError
  const errorMessage = searchError || searchMutation.error?.message || '检索失败'
  const isSuccess = !!searchResult || searchMutation.isSuccess

  // 分数样式
  function scoreStyle(score: number) {
    if (score >= 0.8) return 'text-green-700 bg-green-50 border-green-200'
    if (score >= 0.5) return 'text-amber-700 bg-amber-50 border-amber-200'
    return 'text-red-600 bg-red-50 border-red-200'
  }

  // 切换展开
  function toggleExpand(idx: number) {
    setExpandedItems((prev) => {
      const next = new Set(prev)
      if (next.has(idx)) {
        next.delete(idx)
      } else {
        next.add(idx)
      }
      return next
    })
  }

  // 在父块内容中高亮子块命中部分
  function highlightChild(parentContent: string, childContent: string) {
    if (!childContent || !parentContent.includes(childContent)) {
      return <span>{parentContent}</span>
    }
    const idx = parentContent.indexOf(childContent)
    const before = parentContent.slice(0, idx)
    const match = parentContent.slice(idx, idx + childContent.length)
    const after = parentContent.slice(idx + childContent.length)
    return (
      <>
        {before && <span>{before}</span>}
        <mark className="bg-primary/15 text-foreground font-medium rounded-sm px-0.5">{match}</mark>
        {after && <span>{after}</span>}
      </>
    )
  }

  return (
    <div>
      {/* 页面头部 + 搜索栏 紧凑布局 */}
      <form onSubmit={handleSearch} className="mb-6">
        <div className="flex items-center gap-3 mb-4">
          <h1 className="text-xl font-bold tracking-tight shrink-0">检索测试</h1>
          <div className="relative flex-1">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="输入检索查询..."
              className="h-9 pl-9 pr-3 text-sm"
            />
          </div>
          <Button
            type="submit"
            size="sm"
            disabled={!query.trim() || !selectedKb || isPending}
            className="gap-1.5 shrink-0 cursor-pointer"
          >
            {isPending ? (
              <div className="h-3.5 w-3.5 border-2 border-primary-foreground border-t-transparent rounded-full animate-spin" />
            ) : (
              <Search className="h-3.5 w-3.5" />
            )}
            检索
          </Button>
        </div>

        {/* 参数行 - 单行紧凑 */}
        <div className="flex items-end gap-4 flex-wrap">
          <div className="space-y-1 min-w-[160px]">
            <Label className="text-xs text-muted-foreground">知识库</Label>
            <Select value={selectedKb} onValueChange={setSelectedKb}>
              <SelectTrigger className="h-8 text-xs">
                <SelectValue placeholder="选择知识库" />
              </SelectTrigger>
              <SelectContent>
                {knowledgeBases.map((kb) => (
                  <SelectItem key={kb.id} value={kb.id}>{kb.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1 min-w-[140px]">
            <Label className="text-xs text-muted-foreground">模式</Label>
            <Select value={mode} onValueChange={setMode}>
              <SelectTrigger className="h-8 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="direct">直接检索</SelectItem>
                <SelectItem value="hybrid">混合检索</SelectItem>
                <SelectItem value="agent">Agent</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1 w-[160px]">
            <div className="flex items-center justify-between">
              <Label className="text-xs text-muted-foreground">Top-K</Label>
              <span className="text-xs font-medium tabular-nums">{topK}</span>
            </div>
            <div className="h-8 flex items-center">
              <Slider
                min={1}
                max={50}
                step={1}
                value={[topK]}
                onValueChange={(val) => setTopK(val[0])}
              />
            </div>
          </div>

          {mode === 'agent' && (
            <div className="space-y-1 min-w-[140px]">
              <Label className="text-xs text-muted-foreground">LLM 模型</Label>
              <Select value={selectedModel} onValueChange={setSelectedModel}>
                <SelectTrigger className="h-8 text-xs">
                  <SelectValue placeholder="默认" />
                </SelectTrigger>
                <SelectContent>
                  {llmConfigs.map((config) => (
                    <SelectItem key={config.id} value={config.id}>{config.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
        </div>
      </form>

      {/* 错误提示 */}
      {isError && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 mb-4">
          <AlertCircle className="h-4 w-4 text-destructive shrink-0 mt-0.5" />
          <p className="text-sm text-destructive">{errorMessage}</p>
        </div>
      )}

      {/* Agent 进度步骤 */}
      {isPending && mode === 'agent' && agentSteps.length > 0 && (
        <div className="mb-4 rounded-lg border border-border/60 bg-card p-4">
          <div className="space-y-2">
            {agentSteps.map((step, idx) => {
              const isLatest = idx === agentSteps.length - 1
              return (
                <div key={idx} className={`flex items-center gap-2 text-xs ${isLatest ? 'text-primary font-medium' : 'text-muted-foreground'}`}>
                  {isLatest ? (
                    <span className="w-2 h-2 rounded-full bg-primary animate-pulse shrink-0" />
                  ) : (
                    <span className="w-2 h-2 rounded-full bg-muted-foreground/40 shrink-0" />
                  )}
                  <span>{step.detail}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* 检索结果骨架屏：检索中且暂无结果时占位（agent 模式有进度步骤时不重复显示） */}
      {isPending && results.length === 0 && !(mode === 'agent' && agentSteps.length > 0) && (
        <RetrievalResultsSkeleton count={4} />
      )}

      {/* 检索结果 */}
      {results.length > 0 && (
        <div className="animate-in fade-in-0 duration-500">
          {/* 结果头部 */}
          <div className="flex items-center justify-between mb-3 pb-3 border-b border-border/60">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold">检索结果</h2>
              <Badge variant="secondary" className="text-xs px-1.5 py-0">
                {results.length} 条
              </Badge>
            </div>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Zap className="h-3 w-3" />
              <span>{mode === 'direct' ? '直接检索' : mode === 'hybrid' ? '混合检索' : 'Agent'}</span>
              {searchResult?.iterations !== undefined && searchResult.iterations > 0 && (
                <span className="text-muted-foreground">
                  · 迭代 {searchResult.iterations} 次
                  {searchResult.degraded && ' (降级)'}
                </span>
              )}
            </div>
          </div>

          {/* 结果列表 */}
          <div className="space-y-2">
            {results.map((result, idx) => {
              const isExpanded = expandedItems.has(idx)
              const hasParent = result.content && result.child_content && result.content !== result.child_content

              return (
                <div
                  key={result.chunk_id || idx}
                  className="rounded-lg border border-border/60 bg-card overflow-hidden"
                >
                  {/* 结果头 */}
                  <div className="flex items-center justify-between px-4 py-2 bg-muted/30 border-b border-border/40">
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <span className="font-medium text-foreground/70">#{idx + 1}</span>
                      <FileText className="h-3 w-3" />
                      <span className="truncate max-w-[300px]">{result.filename || result.doc_id?.slice(0, 12)}</span>
                    </div>
                    <Badge variant="outline" className={`text-xs font-mono tabular-nums border ${scoreStyle(result.score)}`}>
                      {result.score?.toFixed(4)}
                    </Badge>
                  </div>

                  {/* 命中内容（子块） */}
                  <div className="px-4 py-3">
                    <p className="text-sm leading-relaxed whitespace-pre-wrap text-foreground">
                      {result.child_content || result.content}
                    </p>
                  </div>

                  {/* 展开父块 */}
                  {hasParent && (
                    <div className="border-t border-border/40">
                      <button
                        onClick={() => toggleExpand(idx)}
                        className="flex items-center gap-1 px-4 py-2 text-xs text-muted-foreground hover:text-foreground transition-colors w-full text-left cursor-pointer"
                      >
                        {isExpanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                        {isExpanded ? '收起完整上下文（父块）' : '查看完整上下文（父块）'}
                      </button>
                      {isExpanded && (
                        <div className="px-4 pb-3">
                          <div className="rounded-md bg-muted/40 p-3 text-sm leading-relaxed whitespace-pre-wrap text-foreground/80 border-l-2 border-primary/30">
                            {highlightChild(result.content, result.child_content)}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* 空结果 */}
      {isSuccess && results.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16">
          <Search className="h-10 w-10 text-muted-foreground/30 mb-3" />
          <p className="text-muted-foreground">未找到相关结果</p>
          <p className="text-xs text-muted-foreground/70 mt-1">尝试调整查询内容或切换检索模式</p>
        </div>
      )}

      {/* 初始状态 */}
      {!isSuccess && !isError && !isPending && (
        <div className="flex flex-col items-center justify-center py-16">
          <Search className="h-10 w-10 text-primary/20 mb-3" />
          <p className="text-sm text-muted-foreground">选择知识库并输入查询内容开始检索</p>
        </div>
      )}
    </div>
  )
}

export default Retrieval
