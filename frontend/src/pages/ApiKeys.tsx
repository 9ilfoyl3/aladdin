import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Key, Copy, Check } from 'lucide-react'
import { apiKeyApi } from '@/lib/api'
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

// 创建响应类型（包含完整 key）
interface CreateKeyResponse {
  id: string
  key: string
  prefix: string
  name: string
}

// API Key 管理页面：创建 + 列表 + 撤销
function ApiKeys() {
  const queryClient = useQueryClient()
  const [showCreate, setShowCreate] = useState(false)
  const [keyName, setKeyName] = useState('')
  const [newKey, setNewKey] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  // 获取 API Key 列表
  const { data: apiKeys = [], isLoading } = useQuery({
    queryKey: ['api-keys'],
    queryFn: () => apiKeyApi.list() as Promise<ApiKeyItem[]>,
  })

  // 创建 API Key
  const createMutation = useMutation({
    mutationFn: (name: string) => apiKeyApi.create({ name }) as Promise<CreateKeyResponse>,
    onSuccess: (data) => {
      setNewKey(data.key)
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

  // 提交创建
  function handleCreate(e: React.FormEvent) {
    e.preventDefault()
    createMutation.mutate(keyName)
  }

  // 关闭创建对话框
  function closeCreate() {
    setShowCreate(false)
    setKeyName('')
    setNewKey(null)
    setCopied(false)
  }

  // 复制 Key
  function copyKey() {
    if (newKey) {
      navigator.clipboard.writeText(newKey)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
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
                        onClick={() => deleteMutation.mutate(key.id)}
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
          {newKey ? (
            // 显示新创建的 Key
            <div>
              <DialogHeader>
                <DialogTitle>API Key 已创建</DialogTitle>
                <DialogDescription>
                  请立即复制此 Key，关闭后将无法再次查看。
                </DialogDescription>
              </DialogHeader>
              <div className="flex items-center gap-2 p-3 bg-muted rounded-md mt-4">
                <code className="flex-1 text-sm break-all">{newKey}</code>
                <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" onClick={copyKey}>
                  {copied ? <Check className="h-4 w-4 text-green-600" /> : <Copy className="h-4 w-4" />}
                </Button>
              </div>
              <DialogFooter>
                <Button onClick={closeCreate}>完成</Button>
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
