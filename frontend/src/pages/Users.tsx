import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Users as UsersIcon, Power, KeyRound, Copy, Check, Shield, ArrowRightLeft, Search, Eye } from 'lucide-react'
import { adminApi, type AdminUserItem, type AdminUserCreateResult } from '@/lib/api'
import { validateUsername } from '@/lib/validation'
import { roleLabel } from '@/lib/labels'
import { useConfirm } from '@/lib/confirm-context'
import { useAuth } from '@/lib/auth-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import TableSkeleton from '@/components/skeletons/TableSkeleton'
import { AvatarPicker } from '@/components/AvatarPicker'
import { toast } from 'sonner'

// 用户管理页面（租户级，user:manage）：建用户、启停、重置密码、分配角色、转移知识库。
function Users() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const { hasPermission, userId } = useAuth()
  const canManageRoles = hasPermission('role:manage')

  const [showCreate, setShowCreate] = useState(false)
  const [username, setUsername] = useState('')
  const [selectedRoles, setSelectedRoles] = useState<string[]>(['user'])
  const [createDesc, setCreateDesc] = useState('')
  const [createAvatar, setCreateAvatar] = useState<string | null>(null)
  const [tempResult, setTempResult] = useState<{ title: string; username: string; pwd: string } | null>(null)
  const [copied, setCopied] = useState(false)
  const [rolesUser, setRolesUser] = useState<AdminUserItem | null>(null)
  const [rolesDraft, setRolesDraft] = useState<string[]>([])
  const [rolesInitial, setRolesInitial] = useState<string[]>([])
  const [transferUser, setTransferUser] = useState<AdminUserItem | null>(null)
  const [transferTarget, setTransferTarget] = useState<string>('')
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)

  const { data: usersPage, isLoading } = useQuery({
    queryKey: ['admin-users', page, search],
    queryFn: () => adminApi.listUsers({ page, page_size: 20, q: search || undefined }),
  })
  const users = usersPage?.items ?? []

  const { data: roles = [] } = useQuery({
    queryKey: ['admin-roles'],
    queryFn: () => adminApi.listRoles(),
    enabled: canManageRoles,
  })
  // 租户管理员不得分配/移交 admin（管理员）角色，故可选角色里排除 admin
  const assignableRoles = roles.filter((r) => r.name !== 'admin')

  const createMutation = useMutation({
    mutationFn: () => adminApi.createUser(username.trim(), selectedRoles, undefined, createDesc, createAvatar),
    onSuccess: (data: AdminUserCreateResult) => {
      if (data.temp_password) {
        setTempResult({ title: '用户已创建', username: data.username, pwd: data.temp_password })
      }
      setShowCreate(false)
      setUsername('')
      setSelectedRoles(['user'])
      setCreateDesc('')
      setCreateAvatar(null)
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
    },
    onError: (e: Error) => toast.error(e.message || '创建失败'),
  })

  const statusMutation = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) => adminApi.setUserStatus(id, active),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['admin-users'] }),
    onError: (e: Error) => toast.error(e.message || '操作失败'),
  })

  const resetMutation = useMutation({
    mutationFn: (id: string) => adminApi.resetPassword(id),
    onSuccess: (data: AdminUserCreateResult) => {
      if (data.temp_password) {
        setTempResult({ title: '密码已重置', username: data.username, pwd: data.temp_password })
      }
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
    },
    onError: (e: Error) => toast.error(e.message || '重置失败'),
  })

  const setRolesMutation = useMutation({
    mutationFn: () => adminApi.setUserRoles(rolesUser!.id, rolesDraft),
    onSuccess: () => {
      toast.success('角色已更新，下一次请求即时生效')
      setRolesUser(null)
      queryClient.invalidateQueries({ queryKey: ['admin-users'] })
    },
    onError: (e: Error) => toast.error(e.message || '更新失败'),
  })

  // 角色未变化则不触发请求（避免无意义写操作与审计噪声）
  function saveRoles() {
    if (!rolesUser) return
    const before = [...rolesInitial].sort()
    const after = [...rolesDraft].sort()
    const unchanged = before.length === after.length && before.every((v, i) => v === after[i])
    if (unchanged) {
      toast.info('角色未变化')
      setRolesUser(null)
      return
    }
    setRolesMutation.mutate()
  }

  const transferMutation = useMutation({
    mutationFn: () => adminApi.transferKnowledgeBases(transferUser!.id, transferTarget),
    onSuccess: (data) => {
      toast.success(`已转移 ${data.transferred_count} 个知识库`)
      setTransferUser(null)
      setTransferTarget('')
    },
    onError: (e: Error) => toast.error(e.message || '转移失败'),
  })

  async function toggleStatus(u: AdminUserItem) {
    const ok = await confirm({
      title: u.is_active ? '停用用户' : '启用用户',
      description: u.is_active
        ? <>停用「{u.username}」后该用户无法登录，已签发的 JWT 立即失效（数据保留）。</>
        : <>重新启用「{u.username}」，恢复其登录能力。</>,
      confirmText: u.is_active ? '停用' : '启用',
    })
    if (ok) statusMutation.mutate({ id: u.id, active: !u.is_active })
  }

  async function handleReset(u: AdminUserItem) {
    const ok = await confirm({
      title: '重置密码',
      description: <>为「{u.username}」生成新的临时密码，该用户下次登录需强制改密，已签发 JWT 立即失效。</>,
      confirmText: '重置',
    })
    if (ok) resetMutation.mutate(u.id)
  }

  async function openRoles(u: AdminUserItem) {
    const res = await adminApi.getUserRoles(u.id)
    setRolesDraft(res.role_ids)
    setRolesInitial(res.role_ids)
    setRolesUser(u)
  }

  function toggleRole(roleId: string) {
    setRolesDraft((prev) => (prev.includes(roleId) ? prev.filter((r) => r !== roleId) : [...prev, roleId]))
  }

  function toggleCreateRole(name: string) {
    setSelectedRoles((prev) => (prev.includes(name) ? prev.filter((r) => r !== name) : [...prev, name]))
  }

  function copyPwd() {
    if (tempResult) {
      navigator.clipboard.writeText(tempResult.pwd)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">用户管理</h2>
          <p className="text-muted-foreground text-sm mt-1">在本租户内创建用户、启停、重置密码、分配角色、转移知识库。新建用户首次登录需强制改密。</p>
        </div>
        <Button onClick={() => setShowCreate(true)}>
          <Plus className="h-4 w-4" />
          创建用户
        </Button>
      </div>

      <div className="mb-4 relative max-w-xs">
        <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1) }}
          placeholder="搜索用户名"
          className="pl-8"
        />
      </div>

      {isLoading ? (
        <TableSkeleton rows={5} columns={5} />
      ) : users.length === 0 ? (
        <div className="text-center py-12 text-muted-foreground">
          <UsersIcon className="h-12 w-12 mx-auto mb-4 opacity-50" />
          <p>暂无用户，点击上方按钮创建</p>
        </div>
      ) : (
        <div className="border rounded-lg">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>用户名</TableHead>
                <TableHead>角色</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>改密标记</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {users.map((u) => {
                const isSelf = !!userId && u.id === userId
                return (
                <TableRow key={u.id}>
                  <TableCell className="font-medium">
                    <div className="flex items-center gap-2">
                      {u.avatar ? (
                        <img src={u.avatar} alt="" className="h-7 w-7 rounded-full object-cover border shrink-0" />
                      ) : (
                        <div className="h-7 w-7 rounded-full bg-muted flex items-center justify-center shrink-0 text-xs text-muted-foreground">
                          {u.username.slice(0, 1).toUpperCase()}
                        </div>
                      )}
                      <span>{u.username}</span>
                      {isSelf && <Badge variant="outline" className="ml-1 text-xs">我</Badge>}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {(u.role_names && u.role_names.length > 0)
                        ? u.role_names.map((rn) => (
                            <Badge key={rn} variant="outline" className="text-xs">{roleLabel(rn)}</Badge>
                          ))
                        : <span className="text-muted-foreground text-xs">—</span>}
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline" className={u.is_active ? 'bg-green-100 text-green-700 border-green-200' : 'bg-red-100 text-red-700 border-red-200'}>
                      {u.is_active ? '启用' : '停用'}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground text-xs">
                    {u.must_change_password ? '待改密' : '正常'}
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      {u.must_change_password && u.temp_password && (
                        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => setTempResult({ title: '初始/临时密码', username: u.username, pwd: u.temp_password! })} title="查看临时密码">
                          <Eye className="h-4 w-4" />
                        </Button>
                      )}
                      {canManageRoles && !isSelf && (
                        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => openRoles(u)} title="分配角色">
                          <Shield className="h-4 w-4" />
                        </Button>
                      )}
                      {!isSelf && (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8"
                          onClick={() => { setTransferUser(u); setTransferTarget('') }}
                          disabled={u.is_active}
                          title={u.is_active ? '请先停用该用户再转移知识库' : '转移知识库'}
                        >
                          <ArrowRightLeft className="h-4 w-4" />
                        </Button>
                      )}
                      {!isSelf && (
                        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => handleReset(u)} title="重置密码">
                          <KeyRound className="h-4 w-4" />
                        </Button>
                      )}
                      {!isSelf && (
                        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => toggleStatus(u)} title={u.is_active ? '停用' : '启用'}>
                          <Power className={u.is_active ? 'h-4 w-4 text-destructive' : 'h-4 w-4 text-green-600'} />
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </div>
      )}

      {/* 分页 */}
      {usersPage && usersPage.total > usersPage.page_size && (
        <div className="flex items-center justify-end gap-2 mt-4 text-sm">
          <span className="text-muted-foreground">共 {usersPage.total} 人</span>
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>上一页</Button>
          <span>第 {page} 页</span>
          <Button variant="outline" size="sm" disabled={!usersPage.has_more} onClick={() => setPage((p) => p + 1)}>下一页</Button>
        </div>
      )}

      {/* 创建用户 */}
      <Dialog open={showCreate} onOpenChange={(o) => { if (!o) { setShowCreate(false); setUsername(''); setSelectedRoles(['user']); setCreateDesc(''); setCreateAvatar(null) } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>创建用户</DialogTitle>
            <DialogDescription>系统将生成一次性临时密码，请创建后复制交给用户。头像与简介可选，用户后续也能自行修改。</DialogDescription>
          </DialogHeader>
          <form onSubmit={(e) => { e.preventDefault(); const ue = validateUsername(username); if (ue) return toast.error(ue); createMutation.mutate() }} className="space-y-4 mt-2">
            <div>
              <Label>头像（可选）</Label>
              <div className="mt-1.5">
                <AvatarPicker value={createAvatar} onChange={setCreateAvatar} shape="circle" />
              </div>
            </div>
            <div>
              <Label>用户名</Label>
              <Input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="如：faguan1" className="mt-1" required />
            </div>
            {canManageRoles && assignableRoles.length > 0 && (
              <div>
                <Label>角色</Label>
                <div className="flex flex-wrap gap-2 mt-2">
                  {assignableRoles.map((r) => (
                    <button
                      type="button"
                      key={r.id}
                      onClick={() => toggleCreateRole(r.name)}
                      className={`px-2.5 py-1 rounded-md text-xs border transition-colors ${selectedRoles.includes(r.name) ? 'bg-primary text-primary-foreground border-primary' : 'bg-background hover:bg-muted'}`}
                    >
                      {roleLabel(r.name)}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <div>
              <Label>简介（可选）</Label>
              <textarea
                value={createDesc}
                onChange={(e) => setCreateDesc(e.target.value)}
                rows={3}
                maxLength={500}
                placeholder="用户简介（≤500 字）"
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => { setShowCreate(false); setUsername(''); setSelectedRoles(['user']); setCreateDesc(''); setCreateAvatar(null) }}>取消</Button>
              <Button type="submit" disabled={createMutation.isPending || !username.trim()}>创建</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* 临时密码展示 */}
      <Dialog open={!!tempResult} onOpenChange={(o) => { if (!o) { setTempResult(null); setCopied(false) } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{tempResult?.title}</DialogTitle>
            <DialogDescription>请立即复制临时密码，关闭后无法再次查看。用户首次登录需强制改密。</DialogDescription>
          </DialogHeader>
          <div className="space-y-2 mt-2 text-sm">
            <div><span className="text-muted-foreground">用户：</span><code>{tempResult?.username}</code></div>
            <div className="flex items-center gap-2 p-3 bg-muted rounded-md">
              <span className="text-muted-foreground shrink-0">临时密码：</span>
              <code className="flex-1 break-all">{tempResult?.pwd}</code>
              <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" onClick={copyPwd}>
                {copied ? <Check className="h-4 w-4 text-green-600" /> : <Copy className="h-4 w-4" />}
              </Button>
            </div>
          </div>
          <DialogFooter>
            <Button onClick={() => { setTempResult(null); setCopied(false) }}>完成</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 分配角色 */}
      <Dialog open={!!rolesUser} onOpenChange={(o) => { if (!o) setRolesUser(null) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>分配角色 · {rolesUser?.username}</DialogTitle>
            <DialogDescription>勾选该用户的角色。变更经实时权限解析在下一次请求即时生效，无需重新登录。</DialogDescription>
          </DialogHeader>
          <div className="flex flex-wrap gap-2 mt-2">
            {assignableRoles.map((r) => (
              <button
                type="button"
                key={r.id}
                onClick={() => toggleRole(r.id)}
                className={`px-2.5 py-1 rounded-md text-xs border transition-colors ${rolesDraft.includes(r.id) ? 'bg-primary text-primary-foreground border-primary' : 'bg-background hover:bg-muted'}`}
              >
                {roleLabel(r.name)}{r.is_builtin ? ' (内置)' : ''}
              </button>
            ))}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRolesUser(null)}>取消</Button>
            <Button onClick={saveRoles} disabled={setRolesMutation.isPending}>保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 转移知识库 */}
      <Dialog open={!!transferUser} onOpenChange={(o) => { if (!o) { setTransferUser(null); setTransferTarget('') } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>转移知识库 · {transferUser?.username}</DialogTitle>
            <DialogDescription>
              把该用户名下的全部知识库归属转移给同租户内另一启用用户（仅改归属，不搬数据，即时生效）。用户须先停用，转移即资产交接的最后一步。
            </DialogDescription>
          </DialogHeader>
          <div className="mt-2">
            <Label>接收用户</Label>
            <Select value={transferTarget} onValueChange={setTransferTarget}>
              <SelectTrigger className="mt-1">
                <SelectValue placeholder="选择同租户内的接收用户" />
              </SelectTrigger>
              <SelectContent>
                {users
                  .filter((u) => u.id !== transferUser?.id && u.is_active)
                  .map((u) => (
                    <SelectItem key={u.id} value={u.id}>{u.username}</SelectItem>
                  ))}
              </SelectContent>
            </Select>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => { setTransferUser(null); setTransferTarget('') }}>取消</Button>
            <Button onClick={() => transferMutation.mutate()} disabled={transferMutation.isPending || !transferTarget}>转移</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default Users
