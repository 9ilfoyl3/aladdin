import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Key, Copy, Check } from 'lucide-react'
import { apiKeyApi } from '@/lib/api'
import { copyToClipboard } from '@/lib/clipboard'
import { useConfirm } from '@/lib/confirm-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import TableSkeleton from '@/components/skeletons/TableSkeleton'

// API Key 数据类型
interface ApiKeyItem {
  id: string
  prefix: string
  name: string
  is_active: boolean
  call_count: number
  last_used_at: string | null
  created_at: string
}

// 创建响应类型（包含完整 key + AK/SK 签名凭据）
interface CreateKeyResponse {
  id: string
  key: string
  prefix: string
  name: string
  access_key: string
  secret_key: string
}

// API Key 管理页面：创建 + 列表 + 撤销
function ApiKeys() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const [showCreate, setShowCreate] = useState(false)
  const [keyName, setKeyName] = useState('')
  const [newKeyData, setNewKeyData] = useState<CreateKeyResponse | null>(null)
  const [copied, setCopied] = useState<string | null>(null)

  // 获取 API Key 列表
  const { data: apiKeys = [], isLoading } = useQuery({
    queryKey: ['api-keys'],
    queryFn: () => apiKeyApi.list() as Promise<ApiKeyItem[]>,
  })

  // 创建 API Key
  const createMutation = useMutation({
    mutationFn: (name: string) => apiKeyApi.create({ name }) as Promise<CreateKeyResponse>,
    onSuccess: (data) => {
      setNewKeyData(data)
      queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
  })

  // 撤销 API Key
  const deleteMutation = useMutation({
    mutationFn: (id: string) => apiKeyApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
  })

  // 撤销 Key（统一确认交互）
  async function handleRevoke(key: ApiKeyItem) {
    const ok = await confirm({
      title: '撤销 API Key',
      description: (
        <>
          确定要撤销 Key「{key.name || key.prefix + '...'}」吗？撤销后使用该 Key 的调用将立即失效，此操作不可撤销。
        </>
      ),
      confirmText: '撤销',
    })
    if (ok) deleteMutation.mutate(key.id)
  }

  // 提交创建
  function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    createMutation.mutate(keyName)
  }

  // 关闭创建对话框
  function closeCreate() {
    setShowCreate(false)
    setKeyName('')
    setNewKeyData(null)
    setCopied(null)
  }

  // 复制指定文本
  function handleCopy(text: string, label: string) {
    copyToClipboard(text)
    setCopied(label)
    setTimeout(() => setCopied(null), 2000)
  }

  // 格式化时间
  function formatTime(time: string | null) {
    if (!time) return '从未使用'
    return new Date(time).toLocaleString('zh-CN')
  }

  return (
    <div>
      {/* 页面标题 */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">API Key 管理</h2>
          <p className="text-muted-foreground text-sm mt-1">管理 API 密钥，用于外部系统接入 Chat API。</p>
        </div>
        <Button onClick={() => setShowCreate(true)}>
          <Plus className="h-4 w-4" />
          创建 Key
        </Button>
      </div>

      {/* Key 列表 */}
      {isLoading ? (
        <TableSkeleton rows={4} columns={6} />
      ) : apiKeys.length === 0 ? (
        <div className="text-center py-12 text-muted-foreground">
          <Key className="h-12 w-12 mx-auto mb-4 opacity-50" />
          <p>暂无 API Key，点击上方按钮创建</p>
        </div>
      ) : (
        <div className="border rounded-lg animate-in fade-in-0 duration-500">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Key</TableHead>
                <TableHead>名称</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>调用次数</TableHead>
                <TableHead>最后使用</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {apiKeys.map((key) => (
                <TableRow key={key.id}>
                  <TableCell className="font-mono text-xs">{key.prefix}...</TableCell>
                  <TableCell>{key.name || '-'}</TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={key.is_active ? 'bg-green-100 text-green-700 border-green-200' : 'bg-red-100 text-red-700 border-red-200'}
                    >
                      {key.is_active ? '活跃' : '已撤销'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">{key.call_count}</TableCell>
                  <TableCell className="text-muted-foreground text-xs">{formatTime(key.last_used_at)}</TableCell>
                  <TableCell>
                    <div className="flex justify-end">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => handleRevoke(key)}
                        disabled={!key.is_active}
                        title="撤销"
                      >
                        <Trash2 className="h-4 w-4 text-destructive" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {/* 创建对话框 */}
      <Dialog open={showCreate} onOpenChange={closeCreate}>
        <DialogContent>
          {newKeyData ? (
            // 显示新创建的凭据（Bearer Key + AK/SK 签名凭据）
            <div>
              <DialogHeader>
                <DialogTitle>API Key 已创建</DialogTitle>
                <DialogDescription>
                  以下凭据仅显示一次，关闭后无法再次查看，请妥善保存。
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-3 mt-4">
                {/* Bearer Key */}
                <div>
                  <Label className="text-xs text-muted-foreground">Bearer Key（明文通道）</Label>
                  <div className="flex items-center gap-2 p-2 bg-muted rounded-md mt-1">
                    <code className="flex-1 text-xs break-all">{newKeyData.key}</code>
                    <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0" onClick={() => handleCopy(newKeyData.key, 'key')}>
                      {copied === 'key' ? <Check className="h-3.5 w-3.5 text-green-600" /> : <Copy className="h-3.5 w-3.5" />}
                    </Button>
                  </div>
                </div>
                {/* AK */}
                <div>
                  <Label className="text-xs text-muted-foreground">Access Key（AK，签名通道用）</Label>
                  <div className="flex items-center gap-2 p-2 bg-muted rounded-md mt-1">
                    <code className="flex-1 text-xs break-all">{newKeyData.access_key}</code>
                    <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0" onClick={() => handleCopy(newKeyData.access_key, 'ak')}>
                      {copied === 'ak' ? <Check className="h-3.5 w-3.5 text-green-600" /> : <Copy className="h-3.5 w-3.5" />}
                    </Button>
                  </div>
                </div>
                {/* SK */}
                <div>
                  <Label className="text-xs text-muted-foreground">Secret Key（SK，签名通道用，切勿外泄）</Label>
                  <div className="flex items-center gap-2 p-2 bg-muted rounded-md mt-1">
                    <code className="flex-1 text-xs break-all">{newKeyData.secret_key}</code>
                    <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0" onClick={() => handleCopy(newKeyData.secret_key, 'sk')}>
                      {copied === 'sk' ? <Check className="h-3.5 w-3.5 text-green-600" /> : <Copy className="h-3.5 w-3.5" />}
                    </Button>
                  </div>
                </div>
              </div>
              <DialogFooter className="mt-4">
                <Button onClick={closeCreate}>已保存，关闭</Button>
              </DialogFooter>
            </div>
          ) : (
            // 创建表单
            <div>
              <DialogHeader>
                <DialogTitle>创建 API Key</DialogTitle>
              </DialogHeader>
              <form onSubmit={handleCreate} className="space-y-4 mt-4">
                <div>
                  <Label>名称（可选）</Label>
                  <Input
                    value={keyName}
                    onChange={(e) => setKeyName(e.target.value)}
                    placeholder="输入 Key 名称，便于识别"
                    className="mt-1"
                  />
                </div>
                <DialogFooter>
                  <Button type="button" variant="outline" onClick={closeCreate}>
                    取消
                  </Button>
                  <Button type="submit" disabled={createMutation.isPending}>
                    创建
                  </Button>
                </DialogFooter>
              </form>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default ApiKeys
