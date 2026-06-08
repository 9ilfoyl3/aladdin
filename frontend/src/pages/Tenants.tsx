import { copyToClipboard } from '@/lib/clipboard'
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Building2, Power, Copy, Check, Users as UsersIcon, KeyRound, Pencil, Upload } from 'lucide-react'
import { adminApi, type TenantItem, type TenantCreateResult, type AdminUserItem } from '@/lib/api'
import { validateTenantName, validateUsername } from '@/lib/validation'
import { roleLabel } from '@/lib/labels'
import { useConfirm } from '@/lib/confirm-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import TableSkeleton from '@/components/skeletons/TableSkeleton'
import { AvatarPicker } from '@/components/AvatarPicker'
import { toast } from 'sonner'

// 租户管理页面（平台级，仅 Super_Admin / tenant:manage）：
// 创建租户（自动建初始租户管理员，返回一次性临时密码）+ 启停 + 下钻该租户用户做兜底管理。
function Tenants() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [adminUsername, setAdminUsername] = useState('')
  const [createDesc, setCreateDesc] = useState('')
  const [createAvatar, setCreateAvatar] = useState<string | null>(null)
  const [created, setCreated] = useState<TenantCreateResult | null>(null)
  const [copied, setCopied] = useState(false)
  // 下钻：查看某租户的用户（兜底重置密码 / 启停）
  const [drillTenant, setDrillTenant] = useState<TenantItem | null>(null)
  const [tempResult, setTempResult] = useState<{ username: string; pwd: string } | null>(null)
  const [tempCopied, setTempCopied] = useState(false)
  // 下钻内：超管为该租户新增一名租户管理员（admin 角色）
  const [showAddAdmin, setShowAddAdmin] = useState(false)
  const [newAdminName, setNewAdminName] = useState('')
  // 编辑租户资料（名称/简介/头像，仅超管）
  const [editTenant, setEditTenant] = useState<TenantItem | null>(null)
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editAvatar, setEditAvatar] = useState<string | null>(null)

  const { data: tenants = [], isLoading } = useQuery({
    queryKey: ['admin-tenants'],
    queryFn: () => adminApi.listTenants(),
  })

  const { data: drillUsers = [] } = useQuery({
    queryKey: ['tenant-users', drillTenant?.id],
    queryFn: () => adminApi.listTenantUsers(drillTenant!.id),
    enabled: !!drillTenant,
  })

  const createMutation = useMutation({
    mutationFn: () => adminApi.createTenant(name.trim(), adminUsername.trim(), undefined, createDesc, createAvatar),
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

  const userStatusMutation = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      adminApi.setUserStatus(id, active),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tenant-users', drillTenant?.id] }),
    onError: (e: Error) => toast.error(e.message || '操作失败'),
  })

  const resetPwdMutation = useMutation({
    mutationFn: (userId: string) => adminApi.resetPassword(userId),
    onSuccess: (data) => {
      if (data.temp_password) setTempResult({ username: data.username, pwd: data.temp_password })
      queryClient.invalidateQueries({ queryKey: ['tenant-users', drillTenant?.id] })
    },
    onError: (e: Error) => toast.error(e.message || '重置失败'),
  })

  const addAdminMutation = useMutation({
    mutationFn: () => adminApi.createTenantAdmin(drillTenant!.id, newAdminName.trim()),
    onSuccess: (data) => {
      if (data.temp_password) setTempResult({ username: data.username, pwd: data.temp_password })
      setShowAddAdmin(false)
      setNewAdminName('')
      queryClient.invalidateQueries({ queryKey: ['tenant-users', drillTenant?.id] })
    },
    onError: (e: Error) => toast.error(e.message || '新增管理员失败'),
  })

  const profileMutation = useMutation({
    mutationFn: () => adminApi.updateTenantProfile(editTenant!.id, {
      name: editName.trim(),
      description: editDesc,
      avatar: editAvatar,
    }),
    onSuccess: () => {
      toast.success('租户资料已更新')
      setEditTenant(null)
      queryClient.invalidateQueries({ queryKey: ['admin-tenants'] })
    },
    onError: (e: Error) => toast.error(e.message || '更新失败'),
  })

  function openEditProfile(t: TenantItem) {
    setEditTenant(t)
    setEditName(t.name)
    setEditDesc(t.description ?? '')
    setEditAvatar(t.avatar ?? null)
  }

  function onPickTenantAvatar(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
      toast.error('仅支持 png / jpeg / webp 图片')
      return
    }
    if (file.size > 200 * 1024) {
      toast.error('图片过大，请控制在 200KB 以内')
      return
    }
    const reader = new FileReader()
    reader.onload = () => setEditAvatar(typeof reader.result === 'string' ? reader.result : null)
    reader.readAsDataURL(file)
  }

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

  async function toggleUser(u: AdminUserItem) {
    const ok = await confirm({
      title: u.is_active ? '停用用户' : '启用用户',
      description: u.is_active
        ? <>兜底停用「{u.username}」：该用户无法登录，已签发 JWT 立即失效（数据保留）。</>
        : <>恢复「{u.username}」的登录能力。</>,
      confirmText: u.is_active ? '停用' : '启用',
    })
    if (ok) userStatusMutation.mutate({ id: u.id, active: !u.is_active })
  }

  async function resetUser(u: AdminUserItem) {
    const ok = await confirm({
      title: '兜底重置密码',
      description: <>为「{u.username}」生成临时密码，该用户下次登录需强制改密，旧 JWT 立即失效。</>,
      confirmText: '重置',
    })
    if (ok) resetPwdMutation.mutate(u.id)
  }

  function closeCreate() {
    setShowCreate(false)
    setName('')
    setAdminUsername('')
    setCreateDesc('')
    setCreateAvatar(null)
    setCreated(null)
    setCopied(false)
  }

  async function copyPwd() {
    if (!created?.admin_temp_password) return
    const ok = await copyToClipboard(created.admin_temp_password)
    if (!ok) {
      toast.error('复制失败，请手动选择密码复制')
      return
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  async function copyTemp() {
    if (!tempResult) return
    const ok = await copyToClipboard(tempResult.pwd)
    if (!ok) {
      toast.error('复制失败，请手动选择密码复制')
      return
    }
    setTempCopied(true)
    setTimeout(() => setTempCopied(false), 2000)
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">租户管理</h2>
          <p className="text-muted-foreground text-sm mt-1">创建与启停业务租户。创建时自动生成该租户的初始管理员及一次性临时密码。</p>
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
                  <TableCell className="font-medium">
                    <div className="flex items-center gap-2">
                      {t.avatar ? (
                        <img src={t.avatar} alt="" className="h-8 w-8 rounded-md object-cover border shrink-0" />
                      ) : (
                        <div className="h-8 w-8 rounded-md bg-muted flex items-center justify-center shrink-0">
                          <Building2 className="h-4 w-4 text-muted-foreground/60" />
                        </div>
                      )}
                      <div className="min-w-0">
                        <div className="truncate">{t.name}</div>
                        {t.description && <div className="truncate text-xs text-muted-foreground font-normal max-w-[280px]">{t.description}</div>}
                      </div>
                    </div>
                  </TableCell>
                  <TableCell className="text-muted-foreground text-sm">{t.tenant_type}</TableCell>
                  <TableCell>
                    <Badge variant="outline" className={t.is_active ? 'bg-green-100 text-green-700 border-green-200' : 'bg-red-100 text-red-700 border-red-200'}>
                      {t.is_active ? '启用' : '停用'}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => openEditProfile(t)}
                        title="编辑资料（名称/简介/头像）"
                      >
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => setDrillTenant(t)}
                        title="查看用户 / 管理管理员"
                      >
                        <UsersIcon className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => toggleStatus(t)}
                        disabled={t.tenant_type === 'external'}
                        title={t.tenant_type === 'external' ? '内置外部租户不可停用（停用将阻断全部外部用户接入）' : (t.is_active ? '停用' : '启用')}
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
                  请立即复制初始管理员的临时密码，关闭后将无法再次查看。该管理员首次登录需强制改密。
                </DialogDescription>
              </DialogHeader>
              <div className="space-y-2 mt-4 text-sm">
                <div><span className="text-muted-foreground">租户：</span>{created.name}</div>
                <div><span className="text-muted-foreground">管理员账号：</span><code>{created.admin_username}</code></div>
                <div className="flex items-center gap-2 p-3 bg-muted rounded-md">
                  <span className="text-muted-foreground shrink-0">临时密码：</span>
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
                <DialogDescription>创建业务租户并自动生成其初始管理员，系统将返回一次性临时密码。头像与简介可选。</DialogDescription>
              </DialogHeader>
              <form onSubmit={(e) => { e.preventDefault(); const ne = validateTenantName(name); const ue = validateUsername(adminUsername); if (ne) return toast.error(ne); if (ue) return toast.error(ue); createMutation.mutate() }} className="space-y-4 mt-2">
                <div>
                  <Label>租户头像（可选）</Label>
                  <div className="mt-1.5">
                    <AvatarPicker value={createAvatar} onChange={setCreateAvatar} shape="square" />
                  </div>
                </div>
                <div>
                  <Label>租户名称</Label>
                  <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：法院A" className="mt-1.5" required />
                </div>
                <div>
                  <Label>初始管理员用户名</Label>
                  <Input value={adminUsername} onChange={(e) => setAdminUsername(e.target.value)} placeholder="如：admin_a" className="mt-1.5" required />
                </div>
                <div>
                  <Label>租户简介（可选）</Label>
                  <textarea
                    value={createDesc}
                    onChange={(e) => setCreateDesc(e.target.value)}
                    rows={3}
                    maxLength={500}
                    placeholder="企业/组织简介（≤500 字）"
                    className="mt-1.5 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  />
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

      {/* 下钻：某租户用户的兜底管理（重置密码 / 启停） */}
      <Dialog open={!!drillTenant} onOpenChange={(o) => { if (!o) setDrillTenant(null) }}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>租户用户 · {drillTenant?.name}</DialogTitle>
            <DialogDescription>
              超管兜底（break-glass）：跨租户重置密码、启停用户、补充租户管理员。日常用户管理由该租户管理员自行完成。
            </DialogDescription>
          </DialogHeader>
          <div className="flex justify-end">
            <Button
              variant="outline"
              size="sm"
              onClick={() => { setNewAdminName(''); setShowAddAdmin(true) }}
              disabled={!drillTenant?.is_active}
              title={drillTenant?.is_active ? '新增管理员' : '租户已停用，数据冻结，仅可查看'}
            >
              <Plus className="h-4 w-4" />
              新增管理员
            </Button>
          </div>
          {!drillTenant?.is_active && (
            <p className="text-xs text-amber-600">该租户已停用，数据已冻结，仅可查看，无法修改。</p>
          )}
          {drillUsers.length === 0 ? (
            <p className="text-sm text-muted-foreground py-6 text-center">该租户暂无用户</p>
          ) : (
            <div className="border rounded-lg max-h-[50vh] overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>用户名</TableHead>
                    <TableHead>角色</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>改密标记</TableHead>
                    <TableHead className="text-right">兜底操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {drillUsers.map((u) => (
                    <TableRow key={u.id}>
                      <TableCell className="font-medium">{u.username}</TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          {u.role
                            ? <Badge variant="outline" className="text-xs">{roleLabel(u.role)}</Badge>
                            : <span className="text-muted-foreground text-xs">—</span>}
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline" className={u.is_active ? 'bg-green-100 text-green-700 border-green-200' : 'bg-red-100 text-red-700 border-red-200'}>
                          {u.is_active ? '启用' : '停用'}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-muted-foreground text-xs">{u.must_change_password ? '待改密' : '正常'}</TableCell>
                      <TableCell>
                        <div className="flex justify-end gap-1">
                          {drillTenant?.is_active ? (
                            <>
                              <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => resetUser(u)} title="重置密码">
                                <KeyRound className="h-4 w-4" />
                              </Button>
                              <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => toggleUser(u)} title={u.is_active ? '停用' : '启用'}>
                                <Power className={u.is_active ? 'h-4 w-4 text-destructive' : 'h-4 w-4 text-green-600'} />
                              </Button>
                            </>
                          ) : (
                            <span className="text-muted-foreground text-xs">仅查看</span>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDrillTenant(null)}>关闭</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 编辑租户资料（名称/简介/头像，仅超管） */}
      <Dialog open={!!editTenant} onOpenChange={(o) => { if (!o) setEditTenant(null) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>编辑租户资料</DialogTitle>
            <DialogDescription>租户是企业组织，其名称、简介与头像由平台超级管理员维护。</DialogDescription>
          </DialogHeader>
          <div className="space-y-4 mt-2">
            <div className="flex items-center gap-4">
              {editAvatar ? (
                <img src={editAvatar} alt="" className="h-16 w-16 rounded-md object-cover border" />
              ) : (
                <div className="h-16 w-16 rounded-md bg-muted flex items-center justify-center">
                  <Building2 className="h-8 w-8 text-muted-foreground/50" />
                </div>
              )}
              <div>
                <label className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md border text-sm cursor-pointer hover:bg-muted">
                  <Upload className="h-4 w-4" />
                  上传头像
                  <input type="file" accept="image/png,image/jpeg,image/webp" className="hidden" onChange={onPickTenantAvatar} />
                </label>
                {editAvatar && (
                  <Button type="button" variant="ghost" size="sm" className="ml-2 text-destructive" onClick={() => setEditAvatar(null)}>移除</Button>
                )}
                <p className="text-xs text-muted-foreground mt-1">png / jpeg / webp，≤200KB</p>
              </div>
            </div>
            <div>
              <Label>租户名称</Label>
              <Input value={editName} onChange={(e) => setEditName(e.target.value)} className="mt-1" />
            </div>
            <div>
              <Label>简介</Label>
              <textarea
                value={editDesc}
                onChange={(e) => setEditDesc(e.target.value)}
                rows={3}
                maxLength={500}
                placeholder="企业/组织简介（≤500 字）"
                className="mt-1 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditTenant(null)}>取消</Button>
            <Button onClick={() => { const e = validateTenantName(editName); if (e) return toast.error(e); profileMutation.mutate() }} disabled={profileMutation.isPending || !editName.trim()}>保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 新增租户管理员（超管在下钻内补充管理员） */}
      <Dialog open={showAddAdmin} onOpenChange={(o) => { if (!o) { setShowAddAdmin(false); setNewAdminName('') } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新增租户管理员 · {drillTenant?.name}</DialogTitle>
            <DialogDescription>
              为该租户补充一名管理员（admin 角色），用于原管理员不可用时接管。系统将生成一次性临时密码，其首次登录需强制改密。
            </DialogDescription>
          </DialogHeader>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              const ue = validateUsername(newAdminName)
              if (ue) return toast.error(ue)
              addAdminMutation.mutate()
            }}
            className="space-y-4 mt-2"
          >
            <div>
              <Label>管理员用户名</Label>
              <Input value={newAdminName} onChange={(e) => setNewAdminName(e.target.value)} placeholder="如：admin_b" className="mt-1" required />
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => { setShowAddAdmin(false); setNewAdminName('') }}>取消</Button>
              <Button type="submit" disabled={addAdminMutation.isPending || !newAdminName.trim()}>创建</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* 兜底重置密码结果 */}
      <Dialog open={!!tempResult} onOpenChange={(o) => { if (!o) { setTempResult(null); setTempCopied(false) } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>密码已重置</DialogTitle>
            <DialogDescription>请立即复制临时密码交给该用户，关闭后无法再次查看。其首次登录需强制改密。</DialogDescription>
          </DialogHeader>
          <div className="space-y-2 mt-2 text-sm">
            <div><span className="text-muted-foreground">用户：</span><code>{tempResult?.username}</code></div>
            <div className="flex items-center gap-2 p-3 bg-muted rounded-md">
              <span className="text-muted-foreground shrink-0">临时密码：</span>
              <code className="flex-1 break-all">{tempResult?.pwd}</code>
              <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" onClick={copyTemp}>
                {tempCopied ? <Check className="h-4 w-4 text-green-600" /> : <Copy className="h-4 w-4" />}
              </Button>
            </div>
          </div>
          <DialogFooter>
            <Button onClick={() => { setTempResult(null); setTempCopied(false) }}>完成</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default Tenants
