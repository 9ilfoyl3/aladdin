import { useState, useEffect } from 'react'
import { useNavigate, useLocation, Link } from 'react-router-dom'
import { toast } from 'sonner'
import { useAuth } from '@/lib/auth-context'
import { authApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { PasswordInput } from '@/components/ui/password-input'
import { Label } from '@/components/ui/label'
import Prism from '@/components/Prism'

// 打字机效果：按 speed（毫秒/字）逐字输出 text，返回已显示文本与是否输出完成
function useTypewriter(text: string, speed = 80) {
  const [displayed, setDisplayed] = useState('')
  const [done, setDone] = useState(false)

  useEffect(() => {
    setDisplayed('')
    setDone(false)
    let index = 0
    const timer = setInterval(() => {
      index += 1
      setDisplayed(text.slice(0, index))
      if (index >= text.length) {
        clearInterval(timer)
        setDone(true)
      }
    }, speed)
    return () => clearInterval(timer)
  }, [text, speed])

  return { displayed, done }
}

export default function Login() {
  const { displayed: tagline, done: taglineDone } = useTypewriter(
    '连接数据源，沉淀团队知识，让智能问答触手可及。',
  )
  const navigate = useNavigate()
  const location = useLocation()
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
      toast.error('请输入用户名和密码')
      return
    }
    setSubmitting(true)
    try {
      await login(username, password)
      // 登录后跳回来源页（含 query，如分享链接 ?share=token）；无来源则进首页。
      // 强制改密场景由路由守卫接管。
      const from = (location.state as { from?: { pathname?: string; search?: string } } | null)?.from
      const target = from?.pathname
        ? `${from.pathname}${from.search || ''}`
        : '/'
      navigate(target, { replace: true })
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '登录失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen bg-background">
      {/* 左侧：Prism 动态背景 + 品牌文案（小屏隐藏） */}
      <div className="relative hidden w-1/2 overflow-hidden bg-[#070708] lg:block">
        <div className="absolute inset-0">
          <Prism
            animationType="rotate"
            timeScale={0.5}
            height={2.5}
            baseWidth={3.5}
            scale={2.6}
            hueShift={0.5}
            colorFrequency={1.5}
            noise={0.5}
            glow={1.5}
          />
        </div>
        {/* 底部品牌文案 */}
        <div className="absolute inset-x-0 bottom-0 z-10 p-12">
          <h2 className="text-4xl font-semibold leading-tight text-white">
            用 Artoo 构建
            <br />
            你的知识中枢
          </h2>
          <p className="mt-4 max-w-md text-sm leading-relaxed text-white/60">
            {tagline}
            <span
              className={`ml-0.5 inline-block w-0.5 -mb-0.5 h-4 bg-white/60 align-middle ${taglineDone ? 'animate-pulse' : ''}`}
            />
          </p>
        </div>
      </div>

      {/* 右侧：登录表单 */}
      <div className="flex w-full items-center justify-center p-6 lg:w-1/2">
        <div className="w-full max-w-sm">
          {/* 品牌标识 */}
          <div className="mb-10 flex items-center justify-center gap-2">
            {/* <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-primary-foreground">
              <Sparkles className="h-5 w-5" />
            </div> */}
            <span className="text-2xl font-semibold font-serif tracking-tight text-foreground">Artoo</span>
          </div>

          <div className="mb-8 text-center">
            <h1 className="text-2xl font-semibold text-foreground">欢迎回来</h1>
            <p className="mt-2 text-sm text-muted-foreground">登录账号，与 Artoo 携手</p>
          </div>

          <form onSubmit={onSubmit} className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="username">
                用户名<span className="text-primary">*</span>
              </Label>
              <Input
                id="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="请输入用户名"
                autoComplete="username"
                autoFocus
                className='mt-2'
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">
                密码<span className="text-primary">*</span>
              </Label>
              <PasswordInput
                id="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="请输入密码"
                autoComplete="current-password"
                className='mt-2'
              />
            </div>
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? '登录中…' : '登录'}
            </Button>
            {canRegister && (
              <p className="text-center text-sm text-muted-foreground">
                没有账号？
                <Link to="/register" className="ml-1 font-medium text-primary hover:underline">
                  注册一个空间
                </Link>
              </p>
            )}
          </form>

          <p className="mt-12 text-center text-xs text-muted-foreground">
            Powered by <span className='font-semibold font-serif'>Artoo</span>
          </p>
        </div>
      </div>
    </div>
  )
}
