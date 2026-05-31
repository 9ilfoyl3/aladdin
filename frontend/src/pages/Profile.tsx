import { useState, useRef, useEffect } from 'react'
import { UserCircle, Upload } from 'lucide-react'
import { toast } from 'sonner'
import { useAuth } from '@/lib/auth-context'
import { authApi } from '@/lib/api'
import { roleLabel } from '@/lib/labels'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Card } from '@/components/ui/card'

// 头像上传限制：≤200KB，png/jpeg/webp（与后端 validate_avatar 一致）
const AVATAR_MAX_BYTES = 200 * 1024
const AVATAR_TYPES = ['image/png', 'image/jpeg', 'image/webp']

// 个人资料页：本人自助维护简介与头像。
export default function Profile() {
  const { profile, refreshProfile } = useAuth()
  const [description, setDescription] = useState('')
  const [avatar, setAvatar] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (profile) {
      setDescription(profile.description ?? '')
      setAvatar(profile.avatar ?? null)
    }
  }, [profile])

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
  }

  async function onSave() {
    setSubmitting(true)
    try {
      await authApi.updateMyProfile({ description, avatar })
      await refreshProfile()
      toast.success('资料已保存')
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '保存失败')
    } finally {
      setSubmitting(false)
    }
  }

  if (!profile) {
    return <div className="text-muted-foreground">加载中…</div>
  }

  const identityText = profile.is_super_admin
    ? '超级管理员'
    : (profile.role_names.length > 0 ? profile.role_names.map(roleLabel).join('、') : '普通用户')

  return (
    <div className="max-w-xl">
      <h2 className="text-2xl font-bold mb-1">个人资料</h2>
      <p className="text-muted-foreground text-sm mb-6">维护你的头像与简介。用户名与身份由管理员设定，不可自行修改。</p>

      <Card className="p-6 space-y-6">
        {/* 头像 */}
        <div className="flex items-center gap-4">
          {avatar ? (
            <img src={avatar} alt="" className="h-20 w-20 rounded-full object-cover border" />
          ) : (
            <div className="h-20 w-20 rounded-full bg-muted flex items-center justify-center">
              <UserCircle className="h-12 w-12 text-muted-foreground/50" />
            </div>
          )}
          <div className="space-y-2">
            <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" className="hidden" onChange={onPickFile} />
            <Button type="button" variant="outline" size="sm" onClick={() => fileRef.current?.click()}>
              <Upload className="h-4 w-4" />
              上传头像
            </Button>
            {avatar && (
              <Button type="button" variant="ghost" size="sm" className="ml-2 text-destructive" onClick={() => setAvatar(null)}>
                移除
              </Button>
            )}
            <p className="text-xs text-muted-foreground">png / jpeg / webp，≤200KB</p>
          </div>
        </div>

        {/* 只读身份信息 */}
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <Label className="text-muted-foreground">用户名</Label>
            <div className="mt-1 font-medium">{profile.username}</div>
          </div>
          <div>
            <Label className="text-muted-foreground">身份</Label>
            <div className="mt-1 font-medium">{identityText}</div>
          </div>
          {profile.tenant_name && (
            <div className="col-span-2">
              <Label className="text-muted-foreground">所属租户</Label>
              <div className="mt-1 font-medium">{profile.tenant_name}</div>
            </div>
          )}
        </div>

        {/* 简介 */}
        <div>
          <Label htmlFor="desc">简介</Label>
          <textarea
            id="desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={4}
            maxLength={500}
            placeholder="介绍一下自己（≤500 字）"
            className="mt-1.5 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <p className="text-xs text-muted-foreground mt-1 text-right">{description.length}/500</p>
        </div>

        <div className="flex justify-end">
          <Button onClick={onSave} disabled={submitting}>{submitting ? '保存中…' : '保存'}</Button>
        </div>
      </Card>
    </div>
  )
}
