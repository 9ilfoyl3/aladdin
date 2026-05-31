import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Building2, Power, Copy, Check } from 'lucide-react'
import { adminApi, type TenantItem, type TenantCreateResult } from '@/lib/api'
import { validateTenantName, validateUsername } from '@/lib/validation'
import { useConfirm } from '@/lib/confirm-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import TableSkeleton from '@/components/skeletons/TableSkeleton'
import { toast } from 'sonner'

// 租户管理页面（平台级，仅 Super_Admin / tenant:manage）：
// 创建租户（自动建初始租户管理员，返回一次性临时口令）+ 启停。
function Tenants() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [adminUsername, setAdminUsername] = useState('')
  const [created, setCreated] = useState<TenantCreateResult | null>(null)
  const [copied, setCopied] = useState(false)

  const { data: tenants = [], isLoading } = useQuery({
    queryKey: ['admin-tenants'],
    queryFn: () => adminApi.listTenants(),
  })

  const createMutation = useMutation({
    mutationFn: () => adminApi.createTenant(name.trim(), adminUsername.trim()),
    onSuccess: (data) => {
      setCreated(data)
      queryClient.invalidateQueries({ queryKey: ['admin-tenants'] })
    },
    onError: (e: Error) => toast.error(e.message || '创建失败'),
  })

  const statusMutation = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      adminApi.setTenantStatus(id, active),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-tenants'] }),
    onError: (e: Error) => toast.error(e.message || '操作失败'),
  })

  async function toggleStatus(t: TenantItem) {
    const ok = await confirm({
      title: t.is_active ? '停用租户' : '启用租户',
      description: t.is_active
        ? <>停用「{t.name}」后，该租户下所有用户登录与 API Key 调用将被阻断（数据保留，停用≠删除）。</>
        : <>重新启用「{t.name}」，恢复其用户登录与 API Key 调用能力。</>,
      confirmText: t.is_active ? '停用' : '启用',
    })
    if (ok) statusMutation.mutate({ id: t.id, active: !t.is_active })
  }

  function closeCreate() {
    setShowCreate(false)
    setName('')
    setAdminUsername('')
    setCreated(null)
    setCopied(false)
  }

  function copyPwd() {
    if (created?.admin_temp_password) {
      navigator.clipboard.writeText(created.admin_temp_password)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">租户管理</h2>
          <p className="text-muted-foreground text-sm mt-1">创建与启停业务租户。创建时自动生成该租户的初始管理员及一次性临时口令。</p>
        </div>
        <Button onClick={() => setShowCreate(true)}>
          <Plus className="h-4 w-4" />
          创建租户
        </Button>
      </div>

      {isLoading ? (
        <TableSkeleton rows={4} columns={4} />
      ) : tenants.length === 0 ? (
        <div className="text-center py-12 text-muted-foreground">
          <Building2 className="h-12 w-12 mx-auto mb-4 opacity-50" />
          <p>暂无租户，点击上方按钮创建</p>
        </div>
      ) : (
        <div className="border rounded-lg">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead>类型</TableHead>
                <TableHead>状态</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tenants.map((t) => (
                <TableRow key={t.id}>
                  <TableCell className="font-medium">{t.name}</TableCell>
                  <TableCell className="text-muted-foreground text-sm">{t.tenant_type}</TableCell>
                  <TableCell>
                    <Badge variant="outline" className={t.is_active ? 'bg-green-100 text-green-700 border-green-200' : 'bg-red-100 text-red-700 border-red-200'}>
                      {t.is_active ? '启用' : '停用'}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => toggleStatus(t)}
                        disabled={t.tenant_type === 'external'}
                        title={t.tenant_type === 'external' ? '内置租户不可停用' : (t.is_active ? '停用' : '启用')}
                      >
                        <Power className={t.is_active ? 'h-4 w-4 text-destructive' : 'h-4 w-4 text-green-600'} />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <Dialog open={showCreate} onOpenChange={closeCreate}>
        <DialogContent>
          {created ? (
            <div>
              <DialogHeader>
                <DialogTitle>租户已创建</DialogTitle>
                <DialogDescription>
                  请立即复制初始管理员的临时口令，关闭后将无法再次查看。该管理员首次登录需强制改密。
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-2 mt-4 text-sm">
                <div><span className="text-muted-foreground">租户：</span>{created.name}</div>
                <div><span className="text-muted-foreground">管理员账号：</span><code>{created.admin_username}</code></div>
                <div className="flex items-center gap-2 p-3 bg-muted rounded-md">
                  <span className="text-muted-foreground shrink-0">临时口令：</span>
                  <code className="flex-1 break-all">{created.admin_temp_password}</code>
                  <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" onClick={copyPwd}>
                    {copied ? <Check className="h-4 w-4 text-green-600" /> : <Copy className="h-4 w-4" />}
                  </Button>
                </div>
              </div>
              <DialogFooter>
                <Button onClick={closeCreate}>完成</Button>
              </DialogFooter>
            </div>
          ) : (
            <div>
              <DialogHeader>
                <DialogTitle>创建租户</DialogTitle>
              </DialogHeader>
              <form onSubmit={(e) => { e.preventDefault(); const ne = validateTenantName(name); const ue = validateUsername(adminUsername); if (ne) return toast.error(ne); if (ue) return toast.error(ue); createMutation.mutate() }} className="space-y-4 mt-4">
                <div>
                  <Label>租户名称</Label>
                  <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：法院A" className="mt-1" required />
                </div>
                <div>
                  <Label>初始管理员用户名</Label>
                  <Input value={adminUsername} onChange={(e) => setAdminUsername(e.target.value)} placeholder="如：admin_a" className="mt-1" required />
                </div>
                <DialogFooter>
                  <Button type="button" variant="outline" onClick={closeCreate}>取消</Button>
                  <Button type="submit" disabled={createMutation.isPending || !name.trim() || !adminUsername.trim()}>创建</Button>
                </DialogFooter>
              </form>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default Tenants
