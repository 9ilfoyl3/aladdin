import { createContext, useContext, useState, useCallback, useRef } from 'react'
import { AlertTriangle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog'

// 确认弹窗配置项
export interface ConfirmOptions {
  /** 标题，默认「确认删除」 */
  title?: string
  /** 描述文案 */
  description?: React.ReactNode
  /** 确认按钮文字，默认「删除」 */
  confirmText?: string
  /** 取消按钮文字，默认「取消」 */
  cancelText?: string
  /** 按钮风格，默认 destructive（删除场景） */
  variant?: 'destructive' | 'default'
}

type ConfirmFn = (options?: ConfirmOptions) => Promise<boolean>

const ConfirmContext = createContext<ConfirmFn | null>(null)

interface DialogState extends Required<Omit<ConfirmOptions, 'description'>> {
  description: React.ReactNode
  open: boolean
}

const DEFAULT_STATE: DialogState = {
  open: false,
  title: '确认删除',
  description: '此操作不可撤销，确定要继续吗？',
  confirmText: '删除',
  cancelText: '取消',
  variant: 'destructive',
}

/**
 * 全局删除/危险操作确认弹窗 Provider。
 * 通过 useConfirm() 获取 confirm 函数，await 其返回值（true=确认，false=取消），
 * 全站删除交互由此统一，无需各页面各写一套 Dialog。
 */
export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<DialogState>(DEFAULT_STATE)
  // 保存当前 confirm 调用的 resolve，按钮点击时回传结果
  const resolveRef = useRef<((value: boolean) => void) | null>(null)

  const confirm = useCallback<ConfirmFn>((options) => {
    setState({ ...DEFAULT_STATE, ...options, open: true })
    return new Promise<boolean>((resolve) => {
      resolveRef.current = resolve
    })
  }, [])

  const handleResolve = useCallback((value: boolean) => {
    resolveRef.current?.(value)
    resolveRef.current = null
    setState((prev) => ({ ...prev, open: false }))
  }, [])

  // 点击遮罩/Esc 关闭等同取消
  const handleOpenChange = useCallback((open: boolean) => {
    if (!open) handleResolve(false)
  }, [handleResolve])

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <Dialog open={state.open} onOpenChange={handleOpenChange}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <div className="flex items-center gap-2.5">
              {state.variant === 'destructive' && (
                <AlertTriangle className="h-5 w-5 text-destructive shrink-0" />
              )}
              <DialogTitle>{state.title}</DialogTitle>
            </div>
            {state.description && (
              <DialogDescription className="pt-1 leading-relaxed">
                {state.description}
              </DialogDescription>
            )}
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => handleResolve(false)}
              className="cursor-pointer"
            >
              {state.cancelText}
            </Button>
            <Button
              type="button"
              variant={state.variant}
              onClick={() => handleResolve(true)}
              className="cursor-pointer"
            >
              {state.confirmText}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </ConfirmContext.Provider>
  )
}

/**
 * 获取统一确认函数。
 * 用法：const confirm = useConfirm(); if (await confirm({ description })) { ...删除 }
 */
export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext)
  if (!ctx) throw new Error('useConfirm must be used within ConfirmProvider')
  return ctx
}
