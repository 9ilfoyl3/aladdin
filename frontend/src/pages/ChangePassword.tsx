import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { toast } from 'sonner'
import { useAuth } from '@/lib/auth-context'
import { authApi } from '@/lib/api'
import { validatePassword } from '@/lib/validation'
import { Button } from '@/components/ui/button'
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

/**
 * 改密页：既用于强制改密闸门（must_change_password），也用于自助改密。
 * 改密成功后后端会使旧 JWT 失效（token_version 自增），故需重新登录。
 */
export default function ChangePassword() {
  const { displayed: tagline, done: taglineDone } = useTypewriter(
    '安全是信任的起点，定期更新密码，守护你的知识空间。',
  )
  const navigate = useNavigate()
  const { mustChangePassword, logout, clearMustChangePassword } = useAuth()
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const pwdErr = validatePassword(newPassword)
    if (pwdErr) {
      toast.error(pwdErr)
      return
    }
    if (newPassword !== confirm) {
      toast.error('两次输入的新密码不一致')
      return
    }
    setSubmitting(true)
    try {
      await authApi.changePassword(oldPassword, newPassword)
      clearMustChangePassword()
      toast.success('密码已修改，请用新密码重新登录')
      // 旧 token 已失效，强制重新登录
      logout()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '改密失败')
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

      {/* 右侧：改密表单 */}
      <div className="relative flex w-full items-center justify-center p-6 lg:w-1/2">
        {/* 返回按钮：仅自助改密时提供（首次登录强制改密不可返回） */}
        {!mustChangePassword && (
          <button
            type="button"
            onClick={() => navigate(-1)}
            className="absolute left-6 top-6 flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
          >
            <ArrowLeft className="h-4 w-4" />
            返回
          </button>
        )}
        <div className="w-full max-w-sm">
          {/* 品牌标识 */}
          <div className="mb-10 flex items-center justify-center gap-2">
            <span className="text-2xl font-semibold font-serif tracking-tight text-foreground">Artoo</span>
          </div>

          <div className="mb-8 text-center">
            <h1 className="text-2xl font-semibold text-foreground">修改密码</h1>
            <p className="mt-2 text-sm text-muted-foreground">
              {mustChangePassword ? '首次登录或密码已被重置，请先修改密码' : '更新你的登录密码'}
            </p>
          </div>

          <form onSubmit={onSubmit} className="space-y-5">
            <div className="space-y-2">
              <Label htmlFor="old">
                当前密码<span className="text-primary">*</span>
              </Label>
              <PasswordInput
                id="old"
                value={oldPassword}
                onChange={(e) => setOldPassword(e.target.value)}
                placeholder="请输入当前密码"
                autoComplete="current-password"
                autoFocus
                className="mt-2"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="new">
                新密码（≥8 位）<span className="text-primary">*</span>
              </Label>
              <PasswordInput
                id="new"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                placeholder="请输入新密码"
                autoComplete="new-password"
                className="mt-2"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="confirm">
                确认新密码<span className="text-primary">*</span>
              </Label>
              <PasswordInput
                id="confirm"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                placeholder="请再次输入新密码"
                autoComplete="new-password"
                className="mt-2"
              />
            </div>
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? '提交中…' : '确认修改'}
            </Button>
          </form>

          <p className="mt-12 text-center text-xs text-muted-foreground">
            Powered by <span className="font-semibold font-serif">Artoo</span>
          </p>
        </div>
      </div>
    </div>
  )
}
