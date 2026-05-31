import { useState, useEffect } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { LogIn } from 'lucide-react'
import { toast } from 'sonner'
import { useAuth } from '@/lib/auth-context'
import { authApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card } from '@/components/ui/card'

export default function Login() {
  const navigate = useNavigate()
  const { login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [canRegister, setCanRegister] = useState(false)

  // 仅当后端开放租户自助注册时，显示"注册"入口
  useEffect(() => {
    authApi.registrationMode().then((r) => setCanRegister(r.self_serve)).catch(() => setCanRegister(false))
  }, [])

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username || !password) {
      toast.error('请输入用户名和口令')
      return
    }
    setSubmitting(true)
    try {
      await login(username, password)
      // 强制改密由路由守卫接管；否则进首页
      navigate('/', { replace: true })
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '登录失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-sm p-6">
        <div className="mb-6 flex flex-col items-center gap-2">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
            <LogIn className="h-6 w-6 text-primary" />
          </div>
          <h1 className="text-lg font-semibold">登录 Aladdin</h1>
          <p className="text-sm text-muted-foreground">请输入你的账号凭据</p>
        </div>
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="username">用户名</Label>
            <Input
              id="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="password">口令</Label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
            />
          </div>
          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? '登录中…' : '登录'}
          </Button>
          {canRegister && (
            <p className="text-center text-sm text-muted-foreground">
              没有账号？<Link to="/register" className="text-primary hover:underline">注册一个空间</Link>
            </p>
          )}
        </form>
      </Card>
    </div>
  )
}
