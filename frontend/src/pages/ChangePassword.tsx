import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { KeyRound } from 'lucide-react'
import { toast } from 'sonner'
import { useAuth } from '@/lib/auth-context'
import { authApi } from '@/lib/api'
import { validatePassword } from '@/lib/validation'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card } from '@/components/ui/card'

/**
 * 改密页：既用于强制改密闸门（must_change_password），也用于自助改密。
 * 改密成功后后端会使旧 JWT 失效（token_version 自增），故需重新登录。
 */
export default function ChangePassword() {
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
      toast.error('两次输入的新口令不一致')
      return
    }
    setSubmitting(true)
    try {
      await authApi.changePassword(oldPassword, newPassword)
      clearMustChangePassword()
      toast.success('口令已修改，请用新口令重新登录')
      // 旧 token 已失效，强制重新登录
      logout()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '改密失败')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-sm p-6">
        <div className="mb-6 flex flex-col items-center gap-2">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
            <KeyRound className="h-6 w-6 text-primary" />
          </div>
          <h1 className="text-lg font-semibold">修改口令</h1>
          {mustChangePassword && (
            <p className="text-sm text-amber-600">首次登录或口令已被重置，请先修改口令</p>
          )}
        </div>
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="old">当前口令</Label>
            <Input id="old" type="password" value={oldPassword}
              onChange={(e) => setOldPassword(e.target.value)} autoComplete="current-password" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="new">新口令（≥8 位）</Label>
            <Input id="new" type="password" value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)} autoComplete="new-password" />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="confirm">确认新口令</Label>
            <Input id="confirm" type="password" value={confirm}
              onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" />
          </div>
          <div className="flex gap-2">
            <Button type="submit" className="flex-1" disabled={submitting}>
              {submitting ? '提交中…' : '确认修改'}
            </Button>
            {!mustChangePassword && (
              <Button type="button" variant="outline" onClick={() => navigate(-1)}>
                取消
              </Button>
            )}
          </div>
        </form>
      </Card>
    </div>
  )
}
