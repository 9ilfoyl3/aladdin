import { useEffect, useRef, useState } from 'react'
import { Camera, Building2, UserCircle, BadgeCheck } from 'lucide-react'
import { toast } from 'sonner'
import { useAuth } from '@/lib/auth-context'
import { authApi } from '@/lib/api'
import { roleLabel } from '@/lib/labels'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

// 头像上传限制：≤200KB，png/jpeg/webp（与后端 validate_avatar 一致）
const AVATAR_MAX_BYTES = 200 * 1024
const AVATAR_TYPES = ['image/png', 'image/jpeg', 'image/webp']

interface ProfileDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

// 个人资料弹窗：本人自助维护头像与简介。用户名/身份由管理员设定，仅展示不可改。
export default function ProfileDialog({ open, onOpenChange }: ProfileDialogProps) {
  const { profile, refreshProfile } = useAuth()
  const [description, setDescription] = useState('')
  const [avatar, setAvatar] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  // 每次打开时从最新 profile 同步表单
  useEffect(() => {
    if (open && profile) {
      setDescription(profile.description ?? '')
      setAvatar(profile.avatar ?? null)
    }
  }, [open, profile])

  function onPickFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    if (!AVATAR_TYPES.includes(file.type)) {
      toast.error('仅支持 png / jpeg / webp 图片')
      return
    }
    if (file.size > AVATAR_MAX_BYTES) {
      toast.error('图片过大，请控制在 200KB 以内')
      return
    }
    const reader = new FileReader()
    reader.onload = () => setAvatar(typeof reader.result === 'string' ? reader.result : null)
    reader.readAsDataURL(file)
    e.target.value = '' // 允许重复选择同一文件
  }

  async function onSave() {
    setSubmitting(true)
    try {
      await authApi.updateMyProfile({ description, avatar })
      await refreshProfile()
      toast.success('资料已保存')
      onOpenChange(false)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSubmitting(false)
    }
  }

  if (!profile) return null

  const identityText = profile.is_super_admin
    ? '超级管理员'
    : (profile.role_label || (profile.role ? roleLabel(profile.role) : '普通成员'))

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md gap-0 p-0 overflow-hidden">
        <DialogHeader className="px-6 pt-6 pb-4">
          <DialogTitle className="text-xl">个人资料</DialogTitle>
          <DialogDescription>
            维护你的头像与简介，用户名与身份由管理员设定。
          </DialogDescription>
        </DialogHeader>

        {/* 资料头部：头像 + 用户名 + 身份徽章 */}
        <div className="mx-6 rounded-xl border border-border bg-muted/40 p-4 flex items-center gap-4">
          <div className="relative shrink-0">
            {avatar ? (
              <img src={avatar} alt="头像" className="h-20 w-20 rounded-full object-cover border border-border" />
            ) : (
              <div className="h-20 w-20 rounded-full bg-background border border-border flex items-center justify-center">
                <UserCircle className="h-11 w-11 text-muted-foreground/40" />
              </div>
            )}
            <input
              ref={fileRef}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              className="hidden"
              onChange={onPickFile}
            />
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              title="更换头像"
              className="absolute -bottom-0.5 -right-0.5 h-7 w-7 rounded-full bg-primary text-primary-foreground flex items-center justify-center shadow-md ring-2 ring-card hover:bg-primary/90 transition-colors cursor-pointer"
            >
              <Camera className="h-3.5 w-3.5" />
            </button>
          </div>

          <div className="min-w-0 flex-1">
            <div className="text-lg font-semibold leading-tight truncate">{profile.username}</div>
            <div className="mt-1.5 inline-flex items-center gap-1 rounded-full bg-accent px-2.5 py-0.5 text-xs font-medium text-accent-foreground">
              <BadgeCheck className="h-3.5 w-3.5" />
              {identityText}
            </div>
            {profile.tenant_name && (
              <div className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
                <Building2 className="h-3.5 w-3.5 shrink-0" />
                <span className="truncate">{profile.tenant_name}</span>
              </div>
            )}
          </div>
        </div>

        {/* 头像操作提示 */}
        <div className="mx-6 mt-2 flex items-center justify-between text-xs text-muted-foreground">
          <span>支持 png / jpeg / webp，≤200KB</span>
          {avatar && (
            <button
              type="button"
              onClick={() => setAvatar(null)}
              className="text-destructive/80 hover:text-destructive transition-colors cursor-pointer"
            >
              移除头像
            </button>
          )}
        </div>

        {/* 简介 */}
        <div className="px-6 pt-5 pb-6">
          <div className="flex items-center justify-between mb-1.5">
            <Label htmlFor="profile-desc">简介</Label>
            <span className="text-xs text-muted-foreground tabular-nums">{description.length}/500</span>
          </div>
          <textarea
            id="profile-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={4}
            maxLength={500}
            placeholder="介绍一下自己（≤500 字）"
            className="w-full resize-none rounded-lg border border-input bg-background px-3 py-2.5 text-sm placeholder:text-muted-foreground/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:border-ring transition-colors"
          />
        </div>

        {/* 底部操作 */}
        <div className="flex justify-end gap-2 border-t border-border bg-muted/30 px-6 py-4">
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)} className="cursor-pointer">
            取消
          </Button>
          <Button onClick={onSave} disabled={submitting} className="cursor-pointer">
            {submitting ? '保存中…' : '保存'}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}
