import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { Plus, Pencil, Trash2, Zap, Plug, Loader2, CheckCircle, XCircle, Wrench, ChevronDown, ChevronRight, ExternalLink } from 'lucide-react'
import { mcpConfigApi } from '@/lib/api'
import type { MCPAuthType, MCPConfigItem, MCPConfigPayload, MCPTestResult, MCPToolMeta, MCPTransport } from '@/lib/api'
import { useConfirm } from '@/lib/confirm-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import CardGridSkeleton from '@/components/skeletons/CardGridSkeleton'

interface MCPFormData {
  name: string
  url: string
  enabled: boolean
  transport: MCPTransport
  authType: MCPAuthType
  /** 空串表示"不改动已存的凭据"；用户主动清空时走 clearToken */
  authToken: string
  authHeaderName: string
  forwardContext: boolean
  toolPrefix: string
  /** 编辑态下是否要清除已存凭据 */
  clearToken: boolean
}

const emptyForm: MCPFormData = {
  name: '',
  url: '',
  enabled: true,
  transport: 'auto',
  authType: 'none',
  authToken: '',
  authHeaderName: '',
  forwardContext: false,
  toolPrefix: '',
  clearToken: false,
}

const TRANSPORT_LABELS: Record<MCPTransport, string> = {
  auto: '自动（推荐）',
  streamable_http: '标准 MCP（Streamable HTTP）',
  legacy_rest: '兼容模式（旧私有 REST）',
}

const AUTH_LABELS: Record<MCPAuthType, string> = {
  none: '不认证',
  bearer: 'Bearer Token',
  header: '自定义请求头',
}

const PROTOCOL_LABELS: Record<string, string> = {
  streamable_http: '标准 MCP',
  legacy_rest: '旧私有 REST',
  auto: '自动',
}

/** 表单 -> 请求体。凭据字段三态：留空=保持，勾选清除=空串，填了=替换 */
function toPayload(data: MCPFormData, isEdit: boolean): MCPConfigPayload {
  const payload: MCPConfigPayload = {
    name: data.name,
    url: data.url,
    enabled: data.enabled,
    transport: data.transport,
    auth_type: data.authType,
    auth_header_name: data.authType === 'header' ? data.authHeaderName.trim() || null : null,
    forward_context: data.forwardContext,
    tool_prefix: data.toolPrefix.trim() || null,
  }
  if (data.authType === 'none' || data.clearToken) {
    payload.auth_token = ''
  } else if (data.authToken) {
    payload.auth_token = data.authToken
  } else if (!isEdit) {
    payload.auth_token = ''
  }
  return payload
}

/** 连通性测试结果：总体结论 + 实际协议 + 发现的工具列表 */
function McpTestResultPanel({ result }: { result: MCPTestResult }) {
  return (
    <div
      className={`p-2.5 rounded-lg text-xs ${result.reachable ? 'bg-green-50 text-green-700 border border-green-200' : 'bg-red-50 text-red-700 border border-red-200'}`}
    >
      <div className="flex items-start gap-1.5">
        {result.reachable ? (
          <CheckCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
        ) : (
          <XCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
        )}
        <span className="break-words">
          {result.reachable
            ? `连通正常，发现 ${result.tool_count} 个工具${result.protocol ? `（协议：${PROTOCOL_LABELS[result.protocol] ?? result.protocol}）` : ''}`
            : '无法连接'}
        </span>
      </div>

      {result.reachable && result.protocol === 'legacy_rest' && (
        <p className="mt-1.5 pl-5 opacity-90">
          对方仍是旧的私有 REST 接口。建议其升级为标准 MCP（JSON-RPC over Streamable HTTP），以便用官方 SDK 维护。
        </p>
      )}

      {result.reachable && result.tools.length > 0 && (
        <div className="mt-2 space-y-1.5 pl-5">
          {result.tools.map((tool) => (
            <div key={tool.name}>
              <span className="font-medium font-mono">{tool.name}</span>
              {tool.description && <span className="opacity-80"> — {tool.description}</span>}
            </div>
          ))}
        </div>
      )}

      {!result.reachable && result.error && (
        <p className="mt-1.5 opacity-90 break-words">{result.error}</p>
      )}
    </div>
  )
}

function McpServers() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const [testResults, setTestResults] = useState<Record<string, MCPTestResult>>({})
  const [testingIds, setTestingIds] = useState<Set<string>>(new Set())
  const [showDialog, setShowDialog] = useState(false)
  const [editingItem, setEditingItem] = useState<MCPConfigItem | null>(null)
  const [form, setForm] = useState<MCPFormData>(emptyForm)
  const [formError, setFormError] = useState<string | null>(null)
  const [dialogTestResult, setDialogTestResult] = useState<MCPTestResult | null>(null)
  const [dialogTesting, setDialogTesting] = useState(false)
  const [expandedTools, setExpandedTools] = useState<Record<string, MCPToolMeta[] | null>>({})
  const [loadingTools, setLoadingTools] = useState<Set<string>>(new Set())

  const { data: configs = [], isLoading } = useQuery({
    queryKey: ['mcp-configs'],
    queryFn: () => mcpConfigApi.list(),
  })

  const createMutation = useMutation({
    mutationFn: (data: MCPFormData) => mcpConfigApi.create(toPayload(data, false)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp-configs'] })
      closeDialog()
    },
    onError: (err: Error) => {
      setFormError(err.message)
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: MCPFormData }) =>
      mcpConfigApi.update(id, toPayload(data, true)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp-configs'] })
      closeDialog()
    },
    onError: (err: Error) => {
      setFormError(err.message)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => mcpConfigApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['mcp-configs'] })
      toast('MCP 服务已删除')
    },
    onError: (err: Error) => {
      toast(`删除失败: ${err.message}`)
    },
  })

  async function handleDelete(config: MCPConfigItem) {
    const ok = await confirm({
      title: '删除 MCP 服务',
      description: <>确定要删除 MCP 服务「{config.name}」吗？删除后其工具将不再注入任何 Agent 预设。此操作不可撤销。</>,
    })
    if (ok) deleteMutation.mutate(config.id)
  }

  function openCreate() {
    setEditingItem(null)
    setForm(emptyForm)
    setFormError(null)
    setDialogTestResult(null)
    setShowDialog(true)
  }

  function openEdit(item: MCPConfigItem) {
    setEditingItem(item)
    setForm({
      name: item.name,
      url: item.url,
      enabled: item.enabled,
      transport: item.transport,
      authType: item.auth_type,
      authToken: '', // 明文不回显，留空即保持原凭据
      authHeaderName: item.auth_header_name ?? '',
      forwardContext: item.forward_context,
      toolPrefix: item.tool_prefix ?? '',
      clearToken: false,
    })
    setFormError(null)
    setDialogTestResult(null)
    setShowDialog(true)
  }

  function closeDialog() {
    setShowDialog(false)
    setEditingItem(null)
    setFormError(null)
    setDialogTestResult(null)
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setFormError(null)
    if (!form.url.trim()) {
      setFormError('请输入服务地址')
      return
    }
    // 认证方式的必填项前端先拦一道，避免白跑一次请求
    const hasStoredToken = Boolean(editingItem?.has_auth_token) && !form.clearToken
    if (form.authType !== 'none' && !form.authToken.trim() && !hasStoredToken) {
      setFormError('选择了认证方式时必须填写凭据')
      return
    }
    if (form.authType === 'header' && !form.authHeaderName.trim()) {
      setFormError('自定义请求头认证需要填写请求头名称')
      return
    }
    if (editingItem) {
      updateMutation.mutate({ id: editingItem.id, data: form })
    } else {
      createMutation.mutate(form)
    }
  }

  async function handleTestInDialog() {
    if (!form.url.trim()) return
    setDialogTesting(true)
    setDialogTestResult(null)
    try {
      // 带上表单里待用的凭据一起测：否则需要认证的 server 一定测不通
      const result = await mcpConfigApi.test({
        url: form.url,
        transport: form.transport,
        auth_type: form.authType,
        auth_token: form.authToken || undefined,
        auth_header_name: form.authType === 'header' ? form.authHeaderName : undefined,
      })
      setDialogTestResult(result)
    } catch {
      setDialogTestResult({ reachable: false, tool_count: 0, tools: [], protocol: null, error: '请求失败' })
    } finally {
      setDialogTesting(false)
    }
  }

  async function handleTest(id: string) {
    setTestingIds((prev) => new Set(prev).add(id))
    try {
      const result = await mcpConfigApi.testSaved(id)
      setTestResults((prev) => ({ ...prev, [id]: result }))
    } catch {
      setTestResults((prev) => ({ ...prev, [id]: { reachable: false, tool_count: 0, tools: [], protocol: null, error: '请求失败' } }))
    } finally {
      setTestingIds((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next
      })
    }
  }

  async function handleToggleTools(id: string) {
    // 已展开 → 收起；未展开 → 拉取工具列表
    if (expandedTools[id] !== undefined) {
      setExpandedTools((prev) => {
        const next = { ...prev }
        delete next[id]
        return next
      })
      return
    }
    setLoadingTools((prev) => new Set(prev).add(id))
    try {
      const tools = await mcpConfigApi.tools(id)
      setExpandedTools((prev) => ({ ...prev, [id]: tools }))
    } catch (err) {
      toast(`获取工具列表失败: ${(err as Error).message}`)
    } finally {
      setLoadingTools((prev) => {
        const next = new Set(prev)
        next.delete(id)
        return next
      })
    }
  }

  return (
    <div>
      {/* 页面头部 */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">MCP 服务</h1>
          <p className="text-muted-foreground text-sm mt-1">配置远端 MCP Server，其工具经 Agent 预设 allowed_tools 显式白名单注入（默认不注入任何预设）</p>
        </div>
        <Button onClick={openCreate} className="gap-2 cursor-pointer">
          <Plus className="h-4 w-4" />
          添加服务
        </Button>
      </div>

      {/* 服务列表 */}
      {isLoading ? (
        <CardGridSkeleton count={3} />
      ) : configs.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20">
          <div className="w-16 h-16 rounded-2xl bg-muted/60 flex items-center justify-center mb-4">
            <Plug className="h-8 w-8 text-muted-foreground/60" />
          </div>
          <p className="text-muted-foreground mb-4">暂无 MCP 服务配置，添加一个开始吧</p>
          <Button onClick={openCreate} variant="outline" className="gap-2 cursor-pointer">
            <Plus className="h-4 w-4" />
            添加服务
          </Button>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 animate-in fade-in-0 duration-500">
          {configs.map((config) => (
            <div
              key={config.id}
              className="group relative rounded-xl border border-border bg-card p-5 transition-all duration-200 hover:shadow-lg hover:border-primary/20 hover:-translate-y-0.5"
            >
              {/* 启用状态标记 */}
              <div className="absolute top-4 right-4">
                <Badge variant={config.enabled ? 'outline' : 'secondary'} className={`text-xs shrink-0 ${config.enabled ? 'bg-green-50 text-green-700 border-green-200' : ''}`}>
                  {config.enabled ? '已启用' : '已停用'}
                </Badge>
              </div>

              {/* 图标 */}
              <div className="w-10 h-10 rounded-lg bg-primary/8 flex items-center justify-center mb-3">
                <Plug className="h-5 w-5 text-primary" />
              </div>

              {/* 名称 */}
              <h3 className="font-semibold text-base truncate mb-1">{config.name}</h3>

              {/* 地址 + 关键配置一览（协议 / 凭据 / 上下文透传 / 前缀） */}
              <div className="space-y-1.5 text-sm text-muted-foreground mb-3">
                <p className="truncate">
                  <span className="text-foreground/60">地址:</span> {config.url}
                </p>
              </div>
              <div className="flex flex-wrap gap-1.5 mb-4">
                <Badge variant="secondary" className="text-[11px]">
                  {TRANSPORT_LABELS[config.transport] ?? config.transport}
                </Badge>
                <Badge
                  variant="secondary"
                  className={`text-[11px] ${config.auth_type === 'none' ? 'text-amber-700 bg-amber-50' : ''}`}
                >
                  {config.auth_type === 'none'
                    ? '未配置凭据'
                    : `${AUTH_LABELS[config.auth_type]}${config.auth_token_masked ? ` ${config.auth_token_masked}` : ''}`}
                </Badge>
                {config.forward_context && (
                  <Badge variant="secondary" className="text-[11px] text-blue-700 bg-blue-50">
                    透传调用方上下文
                  </Badge>
                )}
                {config.tool_prefix && (
                  <Badge variant="secondary" className="text-[11px] font-mono">
                    前缀 {config.tool_prefix}
                  </Badge>
                )}
              </div>

              {/* 操作按钮 */}
              <div className="flex items-center gap-1 pt-3 border-t border-border/60">
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 text-xs gap-1 cursor-pointer"
                  onClick={() => handleTest(config.id)}
                  disabled={testingIds.has(config.id)}
                >
                  {testingIds.has(config.id) ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Zap className="h-3.5 w-3.5" />
                  )}
                  {testingIds.has(config.id) ? '测试中' : '测试'}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 text-xs gap-1 cursor-pointer"
                  onClick={() => handleToggleTools(config.id)}
                  disabled={loadingTools.has(config.id)}
                >
                  {loadingTools.has(config.id) ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : expandedTools[config.id] !== undefined && expandedTools[config.id] ? (
                    <ChevronDown className="h-3.5 w-3.5" />
                  ) : (
                    <ChevronRight className="h-3.5 w-3.5" />
                  )}
                  <Wrench className="h-3.5 w-3.5" />
                  工具
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 text-xs gap-1 cursor-pointer"
                  onClick={() => openEdit(config)}
                >
                  <Pencil className="h-3.5 w-3.5" />
                  编辑
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 text-xs gap-1 text-destructive hover:text-destructive cursor-pointer"
                  onClick={() => handleDelete(config)}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  删除
                </Button>
              </div>

              {/* 工具列表（展开） */}
              {expandedTools[config.id] !== undefined && expandedTools[config.id] !== null && (
                <div className="mt-3 p-2.5 rounded-lg bg-muted/40 border border-border/60">
                  <p className="text-xs text-muted-foreground mb-2">
                    {expandedTools[config.id]!.length > 0
                      ? `该服务暴露 ${expandedTools[config.id]!.length} 个工具`
                      : '该服务未暴露任何工具'}
                  </p>
                  <div className="space-y-1.5">
                    {expandedTools[config.id]!.map((tool) => (
                      <div key={tool.name} className="flex items-start gap-1.5 text-xs">
                        <ExternalLink className="h-3.5 w-3.5 mt-0.5 shrink-0 text-muted-foreground" />
                        <div className="break-all">
                          <span className="font-mono font-medium">{tool.name}</span>
                          {tool.description && <span className="text-muted-foreground"> — {tool.description}</span>}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 测试结果 */}
              {testResults[config.id] && (
                <div className="mt-3">
                  <McpTestResultPanel result={testResults[config.id]} />
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* 创建/编辑对话框 */}
      <Dialog open={showDialog} onOpenChange={closeDialog}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editingItem ? '编辑 MCP 服务' : '添加 MCP 服务'}</DialogTitle>
          </DialogHeader>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <Label>名称</Label>
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如：law-agent-lite"
                className="mt-1.5"
                required
              />
            </div>
            <div>
              <Label>服务地址</Label>
              <Input
                value={form.url}
                onChange={(e) => setForm({ ...form, url: e.target.value })}
                placeholder="如：http://localhost:8081"
                className="mt-1.5"
                required
              />
              <p className="text-xs text-muted-foreground mt-1.5">
                填 base URL。标准 MCP 服务端只需暴露一个{' '}
                <code className="font-mono">POST {'{base}'}/mcp</code>（JSON-RPC 2.0 /
                Streamable HTTP），可直接用官方 SDK 实现
              </p>
            </div>

            <div>
              <Label>传输模式</Label>
              <select
                value={form.transport}
                onChange={(e) => setForm({ ...form, transport: e.target.value as MCPTransport })}
                className="mt-1.5 w-full h-9 rounded-md border border-input bg-background px-3 text-sm"
              >
                {(Object.keys(TRANSPORT_LABELS) as MCPTransport[]).map((key) => (
                  <option key={key} value={key}>
                    {TRANSPORT_LABELS[key]}
                  </option>
                ))}
              </select>
              <p className="text-xs text-muted-foreground mt-1.5">
                自动模式先按标准 MCP 连接，对方不支持时回落旧的私有 REST 接口
              </p>
            </div>

            <div>
              <Label>认证方式</Label>
              <select
                value={form.authType}
                onChange={(e) => setForm({ ...form, authType: e.target.value as MCPAuthType })}
                className="mt-1.5 w-full h-9 rounded-md border border-input bg-background px-3 text-sm"
              >
                {(Object.keys(AUTH_LABELS) as MCPAuthType[]).map((key) => (
                  <option key={key} value={key}>
                    {AUTH_LABELS[key]}
                  </option>
                ))}
              </select>
            </div>

            {form.authType === 'header' && (
              <div>
                <Label>请求头名称</Label>
                <Input
                  value={form.authHeaderName}
                  onChange={(e) => setForm({ ...form, authHeaderName: e.target.value })}
                  placeholder="如：X-Api-Key"
                  className="mt-1.5"
                />
              </div>
            )}

            {form.authType !== 'none' && (
              <div>
                <Label>凭据</Label>
                <Input
                  type="password"
                  value={form.authToken}
                  onChange={(e) => setForm({ ...form, authToken: e.target.value, clearToken: false })}
                  placeholder={
                    editingItem?.has_auth_token
                      ? `已配置（${editingItem.auth_token_masked ?? '******'}），留空表示不修改`
                      : '对方系统签发给 Artoo 的 token'
                  }
                  className="mt-1.5"
                  autoComplete="new-password"
                />
                <p className="text-xs text-muted-foreground mt-1.5">
                  加密存储，保存后不再回显明文。同时用作上下文透传的签名密钥
                </p>
                {editingItem?.has_auth_token && (
                  <label className="flex items-center gap-2 mt-2 text-xs cursor-pointer">
                    <input
                      type="checkbox"
                      checked={form.clearToken}
                      onChange={(e) => setForm({ ...form, clearToken: e.target.checked, authToken: '' })}
                      className="rounded border-border"
                    />
                    清除已保存的凭据
                  </label>
                )}
              </div>
            )}

            <div>
              <Label>工具名前缀（可选）</Label>
              <Input
                value={form.toolPrefix}
                onChange={(e) => setForm({ ...form, toolPrefix: e.target.value })}
                placeholder="如：law_"
                className="mt-1.5"
              />
              <p className="text-xs text-muted-foreground mt-1.5">
                多个 MCP 服务暴露同名工具时用它区分；留空则使用原始工具名。注意：前缀会成为
                Agent 预设 <code className="font-mono">allowed_tools</code> 里要写的名字
              </p>
            </div>

            <div className="rounded-lg border border-border/60 p-3">
              <label className="flex items-start gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.forwardContext}
                  onChange={(e) => setForm({ ...form, forwardContext: e.target.checked })}
                  className="rounded border-border mt-0.5"
                />
                <span className="text-sm">
                  透传调用方上下文
                  <span className="block text-xs text-muted-foreground mt-1">
                    把当前会话 ID、租户与终端用户标识随调用带给该服务，供其做自己的权限隔离。
                    仅在你信任该服务时开启；配了凭据时会附带 HMAC 签名，对方可验证来源。
                  </span>
                </span>
              </label>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="mcp_enabled"
                checked={form.enabled}
                onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
                className="rounded border-border"
              />
              <Label htmlFor="mcp_enabled" className="text-sm font-normal cursor-pointer">启用该服务（停用后其工具不再被发现）</Label>
            </div>

            {/* 对话框内测试结果 */}
            {dialogTestResult && <McpTestResultPanel result={dialogTestResult} />}

            {/* 表单错误提示 */}
            {formError && (
              <div className="p-2.5 rounded-lg text-xs bg-red-50 text-red-700 border border-red-200">
                <div className="flex items-center gap-1.5">
                  <XCircle className="h-3.5 w-3.5" />
                  <span>{formError}</span>
                </div>
              </div>
            )}

            <DialogFooter>
              <Button type="button" variant="outline" onClick={handleTestInDialog} disabled={dialogTesting || !form.url.trim()} className="cursor-pointer">
                <Zap className="h-4 w-4" />
                {dialogTesting ? '测试中...' : '测试连接'}
              </Button>
              <Button type="button" variant="outline" onClick={closeDialog} className="cursor-pointer">取消</Button>
              <Button type="submit" disabled={createMutation.isPending || updateMutation.isPending} className="cursor-pointer">
                {createMutation.isPending || updateMutation.isPending ? '提交中...' : editingItem ? '保存' : '创建'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default McpServers
