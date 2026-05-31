import { useRef } from 'react'
import { Upload, Building2, UserCircle } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'

// 头像选择器：选图后读成 data URL 回传父组件。限 ≤200KB、png/jpeg/webp（与后端一致）。
const MAX_BYTES = 200 * 1024
const TYPES = ['image/png', 'image/jpeg', 'image/webp']

interface Props {
  value: string | null
  onChange: (dataUrl: string | null) => void
  shape?: 'circle' | 'square' // circle=用户，square=租户(企业)
}

export function AvatarPicker({ value, onChange, shape = 'circle' }: Props) {
  const ref = useRef<HTMLInputElement>(null)
  const rounded = shape === 'circle' ? 'rounded-full' : 'rounded-md'
  const Placeholder = shape === 'circle' ? UserCircle : Building2

  function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file) return
    if (!TYPES.includes(file.type)) {
      toast.error('仅支持 png / jpeg / webp 图片')
      return
    }
    if (file.size > MAX_BYTES) {
      toast.error('图片过大，请控制在 200KB 以内')
      return
    }
    const reader = new FileReader()
    reader.onload = () => onChange(typeof reader.result === 'string' ? reader.result : null)
    reader.readAsDataURL(file)
    e.target.value = '' // 允许重复选同一文件
  }

  return (
    <div className="flex items-center gap-4">
      {value ? (
        <img src={value} alt="" className={`h-16 w-16 ${rounded} object-cover border`} />
      ) : (
        <div className={`h-16 w-16 ${rounded} bg-muted flex items-center justify-center`}>
          <Placeholder className="h-8 w-8 text-muted-foreground/50" />
        </div>
      )}
      <div>
        <input ref={ref} type="file" accept="image/png,image/jpeg,image/webp" className="hidden" onChange={onPick} />
        <Button type="button" variant="outline" size="sm" onClick={() => ref.current?.click()}>
          <Upload className="h-4 w-4" />
          上传头像
        </Button>
        {value && (
          <Button type="button" variant="ghost" size="sm" className="ml-2 text-destructive" onClick={() => onChange(null)}>
            移除
          </Button>
        )}
        <p className="text-xs text-muted-foreground mt-1">png / jpeg / webp，≤200KB（可选）</p>
      </div>
    </div>
  )
}
