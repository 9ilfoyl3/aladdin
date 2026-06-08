import { forwardRef, useState, type InputHTMLAttributes } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { cn } from '@/lib/utils'

/**
 * 带"小眼睛"显隐切换的口令输入框。
 *
 * 用法与原生 <Input type="password" /> 完全一致（透传所有 input 属性），
 * 仅在右侧叠加一个明文/密文切换按钮。默认密文。
 * 注意：`type` 由本组件内部托管（明文 text / 密文 password），不接受外部传入 type。
 */
type PasswordInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'type'>

const PasswordInput = forwardRef<HTMLInputElement, PasswordInputProps>(
  ({ className, disabled, value, ...props }, ref) => {
    const [visible, setVisible] = useState(false)
    // 框内有内容时才显示“小眼睛”。
    // 编辑场景下密钥明文不会回传（仅以 placeholder 提示已设置），此时框为空，
    // 显隐切换无内容可切，露出按钮反而让人误以为“点了没反应”。
    const hasValue = value !== undefined && value !== null && String(value).length > 0
    return (
      <div className="relative">
        <input
          ref={ref}
          type={visible ? 'text' : 'password'}
          disabled={disabled}
          value={value}
          className={cn(
            'flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 pr-10 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50',
            className
          )}
          {...props}
        />
        {hasValue && (
          <button
            type="button"
            tabIndex={-1}
            onClick={() => setVisible((v) => !v)}
            disabled={disabled}
            aria-label={visible ? '隐藏口令' : '显示口令'}
            title={visible ? '隐藏口令' : '显示口令'}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {visible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        )}
      </div>
    )
  }
)
PasswordInput.displayName = 'PasswordInput'

export { PasswordInput }
