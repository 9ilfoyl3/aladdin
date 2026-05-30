import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Pencil, Trash2, Star, Shield, Zap, ScanText, Globe, Loader2, CheckCircle, XCircle } from 'lucide-react'
import { ocrConfigApi } from '@/lib/api'
import type { OCRConfigItem, OCRTestResult } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from '@/components/ui/dialog'
import CardGridSkeleton from '@/components/skeletons/CardGridSkeleton'

interface OCRFormData {
  name: string
  provider_type: string
  api_url: string
  api_key: string
  timeout: string
  is_default: boolean
  is_fallback: boolean
}

const emptyForm: OCRFormData = {
  name: '',
  provider_type: 'external_api',
  api_url: '',
  api_key: '',
  timeout: '30',
  is_default: false,
  is_fallback: false,
}

function OcrServices() {
  const queryClient = useQueryClient()
  const [testResults, setTestResults] = useState<Record<string, OCRTestResult>>({})
  const [testingIds, setTestingIds] = useState<Set<string>>(new Set())
  const [showDialog, setShowDialog] = useState(false)
  const [editingItem, setEditingItem] = useState<OCRConfigItem | null>(null)
  const [deletingItem, setDeletingItem] = useState<OCRConfigItem | null>(null)
  const [form, setForm] = useState<OCRFormData>(emptyForm)
  const [formError, setFormError] = useState<string | null>(null)
  const [dialogTestResult, setDialogTestResult] = useState<OCRTestResult | null>(null)
  const [dialogTesting, setDialogTesting] = useState(false)

  const { data: configs = [], isLoading } = useQuery({
    queryKey: ['ocr-configs'],
    queryFn: () => ocrConfigApi.list(),
  })

  const createMutation = useMutation({
    mutationFn: (data: OCRFormData) => {
      return ocrConfigApi.create({
        name: data.name,
        provider_type: data.provider_type,
        api_url: data.api_url,
        api_key: data.api_key || undefined,
        timeout: parseFloat(data.timeout) || 30,
        is_default: data.is_default,
        is_fallback: data.is_fallback,
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ocr-configs'] })
      closeDialog()
    },
    onError: (err: Error) => {
      setFormError(err.message)
    },
  })

  const updateMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: OCRFormData }) => {
      const payload: Record<string, unknown> = {
        name: data.name,
        provider_type: data.provider_type,
        api_url: data.api_url,
        timeout: parseFloat(data.timeout) || 30,
        is_default: data.is_default,
        is_fallback: data.is_fallback,
      }
      // api_key 留空表示不修改
      if (data.api_key) {
        payload.api_key = data.api_key
      }
      return ocrConfigApi.update(id, payload)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ocr-configs'] })
      closeDialog()
    },
    onError: (err: Error) => {
      setFormError(err.message)
    },
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => ocrConfigApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ocr-configs'] })
      setDeletingItem(null)
    },
    onError: (err: Error) => {
      setFormError(err.message)
    },
  })

  function openCreate() {
    setEditingItem(null)
    setForm(emptyForm)
    setFormError(null)
    setDialogTestResult(null)
    setShowDialog(true)
  }

  function openEdit(item: OCRConfigItem) {
    setEditingItem(item)
    setForm({
      name: item.name,
      provider_type: item.provider_type,
      api_url: item.api_url,
      api_key: '',
      timeout: String(item.timeout),
      is_default: item.is_default,
      is_fallback: item.is_fallback,
    })
    setFormError(null)
    setDialogTestResult(null)
    setShowDialog(true)
  }

  function openDelete(item: OCRConfigItem) {
    setFormError(null)
    setDeletingItem(item)
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
    if (editingItem) {
      updateMutation.mutate({ id: editingItem.id, data: form })
    } else {
      createMutation.mutate(form)
    }
  }

  async function handleTestInDialog() {
    setDialogTesting(true)
    setDialogTestResult(null)
    try {
      let result: OCRTestResult
      if (editingItem && !form.api_key && editingItem.api_key_set) {
        result = await ocrConfigApi.testSaved(editingItem.id)
      } else {
        result = await ocrConfigApi.test({
          provider_type: form.provider_type,
          api_url: form.api_url,
          api_key: form.api_key || undefined,
          timeout: parseFloat(form.timeout) || 30,
        })
      }
      setDialogTestResult(result)
    } catch {
      setDialogTestResult({ success: false, message: '请求失败', elapsed_ms: null })
    } finally {
      setDialogTesting(false)
    }
  }

  async function handleTest(id: string) {
    setTestingIds((prev) => new Set(prev).add(id))
    try {
      const result = await ocrConfigApi.testSaved(id)
      setTestResults((prev) => ({ ...prev, [id]: result }))
    } catch {
      setTestResults((prev) => ({ ...prev, [id]: { success: false, message: '请求失败', elapsed_ms: null } }))
    } finally {
      setTestingIds((prev) => {
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
          <h1 className="text-2xl font-bold tracking-tight">OCR 服务管理</h1>
          <p className="text-muted-foreground text-sm mt-1">配置远程 OCR 服务（TextIn / 通用外部 API），支持默认 + 备用自动切换</p>
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
            <ScanText className="h-8 w-8 text-muted-foreground/60" />
          </div>
          <p className="text-muted-foreground mb-4">暂无 OCR 服务配置，添加一个开始吧</p>
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
              {/* 默认/Fallback 标记 */}
              {config.is_default && (
                <div className="absolute top-4 right-4">
                  <Star className="h-4 w-4 text-yellow-500 fill-yellow-500" />
                </div>
              )}
              {config.is_fallback && (
                <div className="absolute top-4 right-4">
                  <Shield className="h-4 w-4 text-blue-500" />
                </div>
              )}

              {/* 类型图标 */}
              <div className="w-10 h-10 rounded-lg bg-primary/8 flex items-center justify-center mb-3">
                <Globe className="h-5 w-5 text-primary" />
              </div>

              {/* 名称 + Provider Type */}
              <div className="flex items-center gap-2 mb-3">
                <h3 className="font-semibold text-base truncate">{config.name}</h3>
                <Badge variant="outline" className="text-xs bg-primary/5 text-primary border-primary/20 shrink-0">
                  {config.provider_type === 'textin' ? 'TextIn' : '外部 API'}
                </Badge>
              </div>

              {/* 详细信息 */}
              <div className="space-y-1.5 text-sm text-muted-foreground mb-4">
                <p className="truncate">
                  <span className="text-foreground/60">地址:</span> {config.api_url}
                </p>
                <p>
                  <span className="text-foreground/60">密钥:</span> {config.api_key_set ? '已设置' : '未设置'}
                </p>
                <p>
                  <span className="text-foreground/60">超时:</span> {config.timeout}s
                </p>
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
                  onClick={() => openEdit(config)}
                >
                  <Pencil className="h-3.5 w-3.5" />
                  编辑
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-8 text-xs gap-1 text-destructive hover:text-destructive cursor-pointer"
                  onClick={() => openDelete(config)}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  删除
                </Button>
              </div>

              {/* 测试结果 */}
              {testResults[config.id] && (
                <div className={`mt-3 p-2.5 rounded-lg text-xs ${testResults[config.id].success ? 'bg-green-50 text-green-700 border border-green-200' : 'bg-red-50 text-red-700 border border-red-200'}`}>
                  <div className="flex items-center gap-1.5">
                    {testResults[config.id].success ? <CheckCircle className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
                    <span>{testResults[config.id].message}</span>
                  </div>
                  {testResults[config.id].elapsed_ms != null && (
                    <p className="mt-1 text-muted-foreground">耗时: {testResults[config.id].elapsed_ms}ms</p>
                  )}
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
            <DialogTitle>{editingItem ? '编辑 OCR 服务' : '添加 OCR 服务'}</DialogTitle>
          </DialogHeader>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <Label>名称</Label>
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="如：TextIn OCR"
                className="mt-1.5"
                required
              />
            </div>
            <div>
              <Label>服务类型</Label>
              <Select value={form.provider_type} onValueChange={(val) => setForm({ ...form, provider_type: val })}>
                <SelectTrigger className="mt-1.5">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="textin">TextIn</SelectItem>
                  <SelectItem value="external_api">外部 API（通用）</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div>
              <Label>API 地址</Label>
              <Input
                value={form.api_url}
                onChange={(e) => setForm({ ...form, api_url: e.target.value })}
                placeholder="如：http://10.30.1.2:8909/parse"
                className="mt-1.5"
                required
              />
            </div>
            <div>
              <Label>API Key</Label>
              <Input
                type="password"
                value={form.api_key}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                placeholder={editingItem ? '留空保持不变' : '输入 API 密钥（可选）'}
                className="mt-1.5"
              />
            </div>
            <div>
              <Label>超时时间（秒）</Label>
              <Input
                type="number"
                value={form.timeout}
                onChange={(e) => setForm({ ...form, timeout: e.target.value })}
                placeholder="30"
                min={1}
                max={300}
                className="mt-1.5"
              />
            </div>
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="ocr_is_default"
                checked={form.is_default}
                onChange={(e) => setForm({ ...form, is_default: e.target.checked })}
                className="rounded border-border"
              />
              <Label htmlFor="ocr_is_default" className="text-sm font-normal cursor-pointer">设为默认服务</Label>
            </div>
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                id="ocr_is_fallback"
                checked={form.is_fallback}
                onChange={(e) => setForm({ ...form, is_fallback: e.target.checked })}
                className="rounded border-border"
              />
              <Label htmlFor="ocr_is_fallback" className="text-sm font-normal cursor-pointer">设为备用服务</Label>
            </div>

            {/* 对话框内测试结果 */}
            {dialogTestResult && (
              <div className={`p-2.5 rounded-lg text-xs ${dialogTestResult.success ? 'bg-green-50 text-green-700 border border-green-200' : 'bg-red-50 text-red-700 border border-red-200'}`}>
                <div className="flex items-center gap-1.5">
                  {dialogTestResult.success ? <CheckCircle className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
                  <span>{dialogTestResult.message}</span>
                </div>
                {dialogTestResult.elapsed_ms != null && (
                  <p className="mt-1 text-muted-foreground">耗时: {dialogTestResult.elapsed_ms}ms</p>
                )}
              </div>
            )}

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
              <Button type="button" variant="outline" onClick={handleTestInDialog} disabled={dialogTesting || !form.api_url} className="cursor-pointer">
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

      {/* 删除确认对话框 */}
      <Dialog open={!!deletingItem} onOpenChange={() => setDeletingItem(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>确认删除</DialogTitle>
          </DialogHeader>
          <p className="text-sm text-muted-foreground">
            确定要删除 OCR 服务 <span className="font-medium text-foreground">{deletingItem?.name}</span> 吗？此操作不可撤销。
          </p>
          {formError && (
            <div className="p-2.5 rounded-lg text-xs bg-red-50 text-red-700 border border-red-200">
              <div className="flex items-center gap-1.5">
                <XCircle className="h-3.5 w-3.5" />
                <span>{formError}</span>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeletingItem(null)} className="cursor-pointer">取消</Button>
            <Button
              variant="destructive"
              onClick={() => deletingItem && deleteMutation.mutate(deletingItem.id)}
              disabled={deleteMutation.isPending}
              className="cursor-pointer"
            >
              {deleteMutation.isPending ? '删除中...' : '确认删除'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default OcrServices
