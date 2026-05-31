import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { UserPlus } from 'lucide-react'
import { toast } from 'sonner'
import { inviteApi } from '@/lib/api'
import { validatePassword, validateUsername, validateTenantName } from '@/lib/validation'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { PasswordInput } from '@/components/ui/password-input'
import { AvatarPicker } from '@/components/AvatarPicker'
import { Label } from '@/components/ui/label'
import { Card } from '@/components/ui/card'

// 免登录邀请接受页：/invite/:token
// 校验邀请有效性后，被邀请人填用户名/密码（建租户邀请还需填租户名）完成建号。
export default function InviteAccept() {
  const { token = '' } = useParams()
  const navigate = useNavigate()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [tenantName, setTenantName] = useState('')
  const [description, setDescription] = useState('')
  const [avatar, setAvatar] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const { data: info, isLoading, isError } = useQuery({
    queryKey: ['invite-info', token],
    queryFn: () => inviteApi.info(token),
    retry: false,
  })

  const isCreateTenant = info?.scope === 'create_tenant'

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const ue = validateUsername(username)
    if (ue) return toast.error(ue)
    const pe = validatePassword(password)
    if (pe) return toast.error(pe)
    if (password !== confirm) return toast.error('两次输入的密码不一致')
    if (isCreateTenant) {
      const te = validateTenantName(tenantName)
      if (te) return toast.error(te)
    }
    setSubmitting(true)
    try {
      await inviteApi.accept(token, {
        username,
        password,
        tenant_name: isCreateTenant ? tenantName : undefined,
        description,
        avatar,
      })
      toast.success('账号已创建，请登录')
      navigate('/login', { replace: true })
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '接受邀请失败')
    } finally {
      setSubmitting(false)
    }
  }

  if (isLoading) {
    return <div className="flex min-h-screen items-center justify-center text-muted-foreground">加载中…</div>
  }

  if (isError || !info?.valid) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
        <Card className="w-full max-w-sm p-6 text-center">
          <h1 className="text-lg font-semibold mb-2">邀请无效</h1>
          <p className="text-sm text-muted-foreground mb-4">该邀请链接无效、已过期或已用尽。</p>
          <Button variant="outline" onClick={() => navigate('/login')}>前往登录</Button>
        </Card>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-sm p-6">
        <div className="mb-6 flex flex-col items-center gap-2">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
            <UserPlus className="h-6 w-6 text-primary" />
          </div>
          <h1 className="text-lg font-semibold">
            {isCreateTenant ? '创建租户与管理员账号' : '创建账号'}
          </h1>
          {!isCreateTenant && info.tenant_name && (
            <p className="text-sm text-muted-foreground">加入租户：{info.tenant_name}</p>
          )}
        </div>
        <form onSubmit={onSubmit} className="space-y-4">
          {isCreateTenant && (
            <div className="space-y-1.5">
              <Label htmlFor="tenant">租户名称</Label>
              <Input id="tenant" value={tenantName} onChange={(e) => setTenantName(e.target.value)} placeholder="如：法院X" />
            </div>
          )}
          <div className="space-y-1.5">
            <Label htmlFor="username">用户名</Label>
            <Input id="username" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="password">密码（≥8 位，含字母与数字）</Label>
            <PasswordInput id="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="confirm">确认密码</Label>
            <PasswordInput id="confirm" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
          </div>
          <div className="space-y-1.5">
            <Label>头像（可选）</Label>
            <AvatarPicker value={avatar} onChange={setAvatar} shape="circle" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="intro">简介（可选）</Label>
            <textarea
              id="intro"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              maxLength={500}
              placeholder="介绍一下自己（≤500 字）"
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
          </div>
          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? '提交中…' : '创建并完成'}
          </Button>
        </form>
      </Card>
    </div>
  )
}
