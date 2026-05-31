import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil } from 'lucide-react'
import { adminApi, type RoleItem, type PermissionDictItem } from '@/lib/api'
import { validateRoleName } from '@/lib/validation'
import { useConfirm } from '@/lib/confirm-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import TableSkeleton from '@/components/skeletons/TableSkeleton'
import { toast } from 'sonner'

const TYPE_LABEL: Record<string, string> = { api: '功能权限 (api)', menu: '菜单 (menu)', btn: '按钮 (btn)' }
const TYPE_ORDER = ['api', 'menu', 'btn']

// 角色管理页面（租户级，role:manage）：自定义角色 CRUD + 三类权限点分配。
function Roles() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()

  const [editRole, setEditRole] = useState<RoleItem | null>(null) // null=未开；id=''表示新建
  const [isCreate, setIsCreate] = useState(false)
  const [name, setName] = useState('')
  const [draft, setDraft] = useState<string[]>([])

  const { data: roles = [], isLoading } = useQuery({
    queryKey: ['admin-roles'],
    queryFn: () => adminApi.listRoles(),
  })

  const { data: permDict = [] } = useQuery({
    queryKey: ['admin-perm-dict'],
    queryFn: () => adminApi.permissionDict(),
  })

  const createMutation = useMutation({
    mutationFn: () => adminApi.createRole(name.trim(), draft),
    onSuccess: () => { toast.success('角色已创建'); closeEditor(); queryClient.invalidateQueries({ queryKey: ['admin-roles'] }) },
    onError: (e: Error) => toast.error(e.message || '创建失败'),
  })

  const updateMutation = useMutation({
    mutationFn: () => adminApi.setRolePermissions(editRole!.id, draft),
    onSuccess: () => { toast.success('权限点已更新，下一次请求即时生效'); closeEditor(); queryClient.invalidateQueries({ queryKey: ['admin-roles'] }) },
    onError: (e: Error) => toast.error(e.message || '更新失败'),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => adminApi.deleteRole(id),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['admin-roles'] }) },
    onError: (e: Error) => toast.error(e.message || '删除失败'),
  })

  function openCreate() {
    setIsCreate(true)
    setEditRole({ id: '', tenant_id: '', name: '', is_builtin: false, permission_codes: [] })
    setName('')
    setDraft([])
  }

  function openEdit(role: RoleItem) {
    setIsCreate(false)
    setEditRole(role)
    setName(role.name)
    setDraft([...role.permission_codes])
  }

  function closeEditor() {
    setEditRole(null)
    setIsCreate(false)
    setName('')
    setDraft([])
  }

  function togglePerm(code: string) {
    setDraft((prev) => (prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code]))
  }

  async function handleDelete(role: RoleItem) {
    const ok = await confirm({
      title: '删除角色',
      description: <>确定删除自定义角色「{role.name}」吗？持有该角色的用户将失去其权限点。此操作不可撤销。</>,
      confirmText: '删除',
    })
    if (ok) deleteMutation.mutate(role.id)
  }

  // 权限点按 type 分组
  const grouped: Record<string, PermissionDictItem[]> = {}
  for (const p of permDict) {
    (grouped[p.type] ||= []).push(p)
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">角色管理</h2>
          <p className="text-muted-foreground text-sm mt-1">自定义本租户角色并分配 api / menu / btn 三类权限点。变更对持有该角色的用户下一次请求即时生效。</p>
        </div>
        <Button onClick={openCreate}>
          <Plus className="h-4 w-4" />
          创建角色
        </Button>
      </div>

      {isLoading ? (
        <TableSkeleton rows={3} columns={3} />
      ) : (
        <div className="border rounded-lg">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>角色名</TableHead>
                <TableHead>权限点数</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {roles.map((r) => (
                <TableRow key={r.id}>
                  <TableCell className="font-medium">
                    {r.name}
                    {r.is_builtin && <Badge variant="outline" className="ml-2 text-xs">内置</Badge>}
                  </TableCell>
                  <TableCell className="text-muted-foreground">{r.permission_codes.length}</TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => openEdit(r)} title="编辑权限点">
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => handleDelete(r)}
                        disabled={r.is_builtin}
                        title={r.is_builtin ? '内置角色不可删除' : '删除'}
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

      <Dialog open={!!editRole} onOpenChange={(o) => { if (!o) closeEditor() }}>
        <DialogContent className="max-w-2xl max-h-[85vh] overflow-auto">
          <DialogHeader>
            <DialogTitle>
              {isCreate ? '创建角色' : `编辑角色 · ${editRole?.name}`}
              {!isCreate && editRole?.is_builtin && <Badge variant="outline" className="ml-2 text-xs">内置</Badge>}
            </DialogTitle>
            <DialogDescription>
              {isCreate ? '设置角色名并勾选权限点。' : '勾选/取消权限点。内置角色亦可调整其权限点集合。'}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 mt-2">
            {isCreate && (
              <div>
                <Label>角色名</Label>
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：只读员" className="mt-1" required />
              </div>
            )}
            {TYPE_ORDER.filter((t) => grouped[t]?.length).map((type) => (
              <div key={type}>
                <p className="text-sm font-medium mb-2">{TYPE_LABEL[type] || type}</p>
                <div className="flex flex-wrap gap-2">
                  {grouped[type].map((p) => (
                    <button
                      type="button"
                      key={p.code}
                      onClick={() => togglePerm(p.code)}
                      className={`px-2.5 py-1 rounded-md text-xs border font-mono transition-colors ${draft.includes(p.code) ? 'bg-primary text-primary-foreground border-primary' : 'bg-background hover:bg-muted'}`}
                    >
                      {p.code}
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={closeEditor}>取消</Button>
            {isCreate ? (
              <Button onClick={() => { const e = validateRoleName(name); if (e) return toast.error(e); createMutation.mutate() }} disabled={createMutation.isPending || !name.trim()}>创建</Button>
            ) : (
              <Button onClick={() => updateMutation.mutate()} disabled={updateMutation.isPending}>保存</Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default Roles
