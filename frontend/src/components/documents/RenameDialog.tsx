import { useState, useEffect } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Pencil } from 'lucide-react'

interface RenameDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  currentName: string
  onConfirm: (newName: string) => void
  isLoading?: boolean
}

// 重命名对话框
function RenameDialog({ open, onOpenChange, currentName, onConfirm, isLoading }: RenameDialogProps) {
  const [name, setName] = useState(currentName)

  useEffect(() => {
    if (open) setName(currentName)
  }, [open, currentName])

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const trimmed = name.trim()
    if (!trimmed || trimmed === currentName) return
    onConfirm(trimmed)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Pencil className="h-4 w-4 text-muted-foreground" />
            重命名
          </DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4 mt-2">
          <Input
            autoFocus
            placeholder="输入新名称"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={200}
            onFocus={(e) => {
              // 选中文件名（不含扩展名）
              const dotIdx = e.target.value.lastIndexOf('.')
              if (dotIdx > 0) {
                e.target.setSelectionRange(0, dotIdx)
              } else {
                e.target.select()
              }
            }}
          />
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} className="cursor-pointer">
              取消
            </Button>
            <Button type="submit" disabled={!name.trim() || name.trim() === currentName || isLoading} className="cursor-pointer">
              {isLoading ? '保存中...' : '确定'}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export default RenameDialog
