import { useState } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { UserPlus } from 'lucide-react'
import { toast } from 'sonner'
import { authApi } from '@/lib/api'
import { validatePassword, validateUsername, validateTenantName } from '@/lib/validation'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { PasswordInput } from '@/components/ui/password-input'
import { Label } from '@/components/ui/label'
import { Card } from '@/components/ui/card'

// 租户自助注册页：注册即开一个独立租户，注册人成为该租户管理员。
// 仅当后端 registration_mode=self_serve 时可用（由路由/登录页链接控制可见性）。
export default function Register() {
  const navigate = useNavigate()
  const [tenantName, setTenantName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const te = validateTenantName(tenantName)
    if (te) return toast.error(te)
    const ue = validateUsername(username)
    if (ue) return toast.error(ue)
    const pe = validatePassword(password)
    if (pe) return toast.error(pe)
    if (password !== confirm) return toast.error('两次输入的密码不一致')
    setSubmitting(true)
    try {
      await authApi.register(username, password, tenantName)
      toast.success('注册成功，请登录')
      navigate('/login', { replace: true })
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '注册失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-sm p-6">
        <div className="mb-6 flex flex-col items-center gap-2">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
            <UserPlus className="h-6 w-6 text-primary" />
          </div>
          <h1 className="text-lg font-semibold">注册</h1>
          <p className="text-sm text-muted-foreground text-center">
            注册将为你创建一个独立空间，你即该空间的管理员，可邀请他人加入。
          </p>
        </div>
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="tenant">空间名称</Label>
            <Input id="tenant" value={tenantName} onChange={(e) => setTenantName(e.target.value)} placeholder="如：我的知识库 / 组织名" autoFocus />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="username">用户名</Label>
            <Input id="username" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="password">密码（≥8 位，含字母与数字）</Label>
            <PasswordInput id="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="confirm">确认密码</Label>
            <PasswordInput id="confirm" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
          </div>
          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? '注册中…' : '注册'}
          </Button>
          <p className="text-center text-sm text-muted-foreground">
            已有账号？<Link to="/login" className="text-primary hover:underline">去登录</Link>
          </p>
        </form>
      </Card>
    </div>
  )
}
