import { useState, useEffect } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { toast } from 'sonner'
import { authApi } from '@/lib/api'
import { validatePassword, validateUsername, validateTenantName } from '@/lib/validation'
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

// 租户自助注册页：注册即开一个独立租户，注册人成为该租户管理员。
// 仅当后端 registration_mode=self_serve 时可用（由路由/登录页链接控制可见性）。
export default function Register() {
  const { displayed: tagline, done: taglineDone } = useTypewriter(
    '开启一个独立空间，让团队知识从这里开始沉淀。',
  )
  const navigate = useNavigate()
  const [tenantName, setTenantName] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [submitting, setSubmitting] = useState(false)
  // 失焦校验错误：null/缺省=无错
  const [usernameErr, setUsernameErr] = useState<string | null>(null)
  const [passwordErr, setPasswordErr] = useState<string | null>(null)
  const [confirmErr, setConfirmErr] = useState<string | null>(null)

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

      {/* 右侧：注册表单 */}
      <div className="flex w-full items-center justify-center p-6 lg:w-1/2">
        <div className="w-full max-w-sm">
          {/* 品牌标识 */}
          <div className="mb-10 flex items-center justify-center gap-2">
            <span className="text-2xl font-semibold font-serif tracking-tight text-foreground">Artoo</span>
          </div>

          <div className="mb-8 text-center">
            <h1 className="text-2xl font-semibold text-foreground">创建空间</h1>
            <p className="mt-2 text-sm text-muted-foreground">
              注册将为你开通一个独立空间，你即该空间的管理员
            </p>
          </div>

          <form onSubmit={onSubmit} className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="tenant">
                空间名称<span className="text-primary">*</span>
              </Label>
              <Input
                id="tenant"
                value={tenantName}
                onChange={(e) => setTenantName(e.target.value)}
                placeholder="如：我的知识库 / 组织名"
                autoFocus
                className="mt-2"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="username">
                用户名<span className="text-primary">*</span>
              </Label>
              <Input
                id="username"
                value={username}
                onChange={(e) => { setUsername(e.target.value); if (usernameErr) setUsernameErr(null) }}
                onBlur={() => setUsernameErr(username ? validateUsername(username) : null)}
                placeholder="请输入用户名"
                autoComplete="username"
                className="mt-2"
                aria-invalid={!!usernameErr}
              />
              {usernameErr && <p className="text-xs text-destructive">{usernameErr}</p>}
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">
                密码（≥8 位，含字母与数字）<span className="text-primary">*</span>
              </Label>
              <PasswordInput
                id="password"
                value={password}
                onChange={(e) => { setPassword(e.target.value); if (passwordErr) setPasswordErr(null) }}
                onBlur={() => setPasswordErr(password ? validatePassword(password) : null)}
                placeholder="请输入密码"
                autoComplete="new-password"
                className="mt-2"
                aria-invalid={!!passwordErr}
              />
              {passwordErr && <p className="text-xs text-destructive">{passwordErr}</p>}
            </div>
            <div className="space-y-2">
              <Label htmlFor="confirm">
                确认密码<span className="text-primary">*</span>
              </Label>
              <PasswordInput
                id="confirm"
                value={confirm}
                onChange={(e) => { setConfirm(e.target.value); if (confirmErr) setConfirmErr(null) }}
                onBlur={() => setConfirmErr(confirm && confirm !== password ? '两次输入的密码不一致' : null)}
                placeholder="请再次输入密码"
                autoComplete="new-password"
                className="mt-2"
                aria-invalid={!!confirmErr}
              />
              {confirmErr && <p className="text-xs text-destructive">{confirmErr}</p>}
            </div>
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? '注册中…' : '注册'}
            </Button>
            <p className="text-center text-sm text-muted-foreground">
              已有账号？
              <Link to="/login" className="ml-1 font-medium text-primary hover:underline">
                去登录
              </Link>
            </p>
          </form>

          <p className="mt-12 text-center text-xs text-muted-foreground">
            Powered by <span className="font-semibold font-serif">Artoo</span>
          </p>
        </div>
      </div>
    </div>
  )
}
