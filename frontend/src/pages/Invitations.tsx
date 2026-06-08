import { copyToClipboard } from '@/lib/clipboard'
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Mail, Trash2, Copy, Check, Users as UsersIcon } from 'lucide-react'
import { adminApi, type InvitationItem, type InvitationCreateResult } from '@/lib/api'
import { useConfirm } from '@/lib/confirm-context'
import { useAuth } from '@/lib/auth-context'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import TableSkeleton from '@/components/skeletons/TableSkeleton'
import { toast } from 'sonner'

const SCOPE_LABEL: Record<string, string> = {
  create_tenant: '建租户+管理员',
  create_user: '建本租户用户',
}

// 邀请链接管理页面。超管可发"建租户"邀请；租管(user:manage)可发"建本租户用户"邀请。
function Invitations() {
  const queryClient = useQueryClient()
  const confirm = useConfirm()
  const { isSuperAdmin } = useAuth()

  const [showCreate, setShowCreate] = useState(false)
  const [scope, setScope] = useState(isSuperAdmin ? 'create_tenant' : 'create_user')
  const [expiresHours, setExpiresHours] = useState('168') // 默认 7 天
  const [maxUses, setMaxUses] = useState('') // 空=不限次
  const [created, setCreated] = useState<InvitationCreateResult | null>(null)
  const [copied, setCopied] = useState(false)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  // 查看"通过该邀请创建的用户"
  const [usersInv, setUsersInv] = useState<InvitationItem | null>(null)

  const { data: invPage, isLoading } = useQuery({
    queryKey: ['invitations', page],
    queryFn: () => adminApi.listInvitations({ page, page_size: 20 }),
  })
  const invitations = invPage?.items ?? []

  const { data: createdUsers = [] } = useQuery({
    queryKey: ['invitation-users', usersInv?.id],
    queryFn: () => adminApi.invitationUsers(usersInv!.id),
    enabled: !!usersInv,
  })

  const createMutation = useMutation({
    mutationFn: () =>
      adminApi.createInvitation({
        scope,
        expires_in_hours: Number(expiresHours),
        max_uses: maxUses ? Number(maxUses) : null,
      }),
    onSuccess: (data) => {
      setCreated(data)
      queryClient.invalidateQueries({ queryKey: ['invitations'] })
    },
    onError: (e: Error) => toast.error(e.message || '创建失败'),
  })

  const revokeMutation = useMutation({
    mutationFn: (id: string) => adminApi.revokeInvitation(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['invitations'] }),
    onError: (e: Error) => toast.error(e.message || '吊销失败'),
  })

  function inviteLink(token: string) {
    return `${window.location.origin}/invite/${token}`
  }

  async function copyLink() {
    if (!created) return
    const ok = await copyToClipboard(inviteLink(created.token))
    if (!ok) {
      toast.error('复制失败，请手动选择链接复制')
      return
    }
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  async function copyRowLink(inv: InvitationItem) {
    if (!inv.token) {
      toast.error('该邀请无可复制链接')
      return
    }
    const ok = await copyToClipboard(inviteLink(inv.token))
    if (!ok) {
      toast.error('复制失败，请手动选择链接复制')
      return
    }
    setCopiedId(inv.id)
    setTimeout(() => setCopiedId(null), 2000)
    toast.success('链接已复制')
  }

  async function handleRevoke(inv: InvitationItem) {
    const ok = await confirm({
      title: '停用邀请',
      description: <>停用后该链接立即失效且不可重新启用（需要时请重新生成一条新链接）。已通过它注册的账号不受影响。</>,
      confirmText: '停用',
    })
    if (ok) revokeMutation.mutate(inv.id)
  }

  function closeCreate() {
    setShowCreate(false)
    setScope(isSuperAdmin ? 'create_tenant' : 'create_user')
    setExpiresHours('168')
    setMaxUses('')
    setCreated(null)
    setCopied(false)
  }

  function fmt(t: string) {
    return t ? new Date(t).toLocaleString('zh-CN') : '-'
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold">邀请链接</h2>
          <p className="text-muted-foreground text-sm mt-1">生成带有效期的注册邀请链接。链接可在列表随时复制、重复使用，直到过期 / 用满 / 吊销。</p>
        </div>
        <Button onClick={() => setShowCreate(true)}>
          <Plus className="h-4 w-4" />
          生成邀请
        </Button>
      </div>

      {isLoading ? (
        <TableSkeleton rows={4} columns={5} />
      ) : invitations.length === 0 ? (
        <div className="text-center py-12 text-muted-foreground">
          <Mail className="h-12 w-12 mx-auto mb-4 opacity-50" />
          <p>暂无邀请，点击上方按钮生成</p>
        </div>
      ) : (
        <div className="border rounded-lg">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>用途</TableHead>
                <TableHead>已用/上限</TableHead>
                <TableHead>过期时间</TableHead>
                <TableHead>状态</TableHead>
                <TableHead className="text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {invitations.map((inv) => (
                <TableRow key={inv.id}>
                  <TableCell>{SCOPE_LABEL[inv.scope] || inv.scope}</TableCell>
                  <TableCell className="text-muted-foreground">
                    {inv.used_count} / {inv.max_uses ?? '∞'}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{fmt(inv.expires_at)}</TableCell>
                  <TableCell>
                    <Badge variant="outline" className={inv.is_active ? 'bg-green-100 text-green-700 border-green-200' : 'bg-gray-100 text-gray-600 border-gray-200'}>
                      {inv.is_active ? '有效' : '已失效'}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => setUsersInv(inv)} title="查看通过此链接创建的用户">
                        <UsersIcon className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => copyRowLink(inv)} disabled={!inv.is_active || !inv.token} title="复制链接">
                        {copiedId === inv.id ? <Check className="h-4 w-4 text-green-600" /> : <Copy className="h-4 w-4" />}
                      </Button>
                      <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => handleRevoke(inv)} disabled={!inv.is_active} title="停用（停用后请重新生成新链接）">
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

      {invPage && invPage.total > invPage.page_size && (
        <div className="flex items-center justify-end gap-2 mt-4 text-sm">
          <span className="text-muted-foreground">共 {invPage.total} 条</span>
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>上一页</Button>
          <span>第 {page} 页</span>
          <Button variant="outline" size="sm" disabled={!invPage.has_more} onClick={() => setPage((p) => p + 1)}>下一页</Button>
        </div>
      )}

      <Dialog open={showCreate} onOpenChange={closeCreate}>
        <DialogContent>
          {created ? (
            <div>
              <DialogHeader>
                <DialogTitle>邀请已生成</DialogTitle>
                <DialogDescription>请立即复制链接发送给被邀请人，关闭后无法再次查看完整链接。</DialogDescription>
              </DialogHeader>
              <div className="flex items-center gap-2 p-3 bg-muted rounded-md mt-4">
                <code className="flex-1 text-sm break-all">{inviteLink(created.token)}</code>
                <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" onClick={copyLink}>
                  {copied ? <Check className="h-4 w-4 text-green-600" /> : <Copy className="h-4 w-4" />}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground mt-2">过期时间：{fmt(created.expires_at)}</p>
              <DialogFooter>
                <Button onClick={closeCreate}>完成</Button>
              </DialogFooter>
            </div>
          ) : (
            <div>
              <DialogHeader>
                <DialogTitle>生成邀请链接</DialogTitle>
              </DialogHeader>
              <form onSubmit={(e) => { e.preventDefault(); createMutation.mutate() }} className="space-y-4 mt-4">
                {isSuperAdmin && (
                  <div>
                    <Label>用途</Label>
                    <Select value={scope} onValueChange={setScope}>
                      <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="create_tenant">建租户 + 管理员（超管）</SelectItem>
                        <SelectItem value="create_user">建本租户用户</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                )}
                <div>
                  <Label>有效期（小时）</Label>
                  <Select value={expiresHours} onValueChange={setExpiresHours}>
                    <SelectTrigger className="mt-1"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="24">1 天</SelectItem>
                      <SelectItem value="72">3 天</SelectItem>
                      <SelectItem value="168">7 天</SelectItem>
                      <SelectItem value="720">30 天</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div>
                  <Label>可用次数（留空 = 有效期内不限次）</Label>
                  <Input type="number" min={1} value={maxUses} onChange={(e) => setMaxUses(e.target.value)} placeholder="如 1 = 一次性" className="mt-1" />
                </div>
                <DialogFooter>
                  <Button type="button" variant="outline" onClick={closeCreate}>取消</Button>
                  <Button type="submit" disabled={createMutation.isPending}>生成</Button>
                </DialogFooter>
              </form>
            </div>
          )}
        </DialogContent>
      </Dialog>

      {/* 查看通过此邀请创建的用户 */}
      <Dialog open={!!usersInv} onOpenChange={(o) => { if (!o) setUsersInv(null) }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>通过此链接创建的用户</DialogTitle>
            <DialogDescription>按创建时间倒序。仅统计经该邀请链接自助注册的账号。</DialogDescription>
          </DialogHeader>
          {createdUsers.length === 0 ? (
            <p className="text-sm text-muted-foreground py-6 text-center">暂无用户通过此链接创建</p>
          ) : (
            <div className="border rounded-lg max-h-[50vh] overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>用户名</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>创建时间</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {createdUsers.map((u) => (
                    <TableRow key={u.id}>
                      <TableCell className="font-medium">{u.username}</TableCell>
                      <TableCell>
                        <Badge variant="outline" className={u.is_active ? 'bg-green-100 text-green-700 border-green-200' : 'bg-red-100 text-red-700 border-red-200'}>
                          {u.is_active ? '启用' : '停用'}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs text-muted-foreground">{fmt(u.created_at)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setUsersInv(null)}>关闭</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export default Invitations
