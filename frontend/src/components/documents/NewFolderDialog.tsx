import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { FolderPlus } from 'lucide-react'
import { validateName } from '@/lib/utils'

interface NewFolderDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onConfirm: (name: string) => void
  isLoading?: boolean
}

// 新建文件夹对话框
function NewFolderDialog({ open, onOpenChange, onConfirm, isLoading }: NewFolderDialogProps) {
  const [name, setName] = useState('')

  const error = name.trim() ? validateName(name, '文件夹名') : null

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const trimmed = name.trim()
    if (!trimmed || error) return
    onConfirm(trimmed)
    setName('')
  }

  function handleOpenChange(val: boolean) {
    if (!val) setName('')
    onOpenChange(val)
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <FolderPlus className="h-5 w-5 text-amber-500" />
            新建文件夹
          </DialogTitle>
        </DialogHeader>
        <form onSubmit={handleSubmit} className="space-y-4 mt-2">
          <div>
            <Input
              autoFocus
              placeholder="输入文件夹名称"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={200}
              className={error ? 'border-destructive' : ''}
            />
            {error && (
              <p className="text-xs text-destructive mt-1.5">{error}</p>
            )}
          </div>
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => handleOpenChange(false)} className="cursor-pointer">
              取消
            </Button>
            <Button type="submit" disabled={!name.trim() || !!error || isLoading} className="cursor-pointer">
              {isLoading ? '创建中...' : '创建'}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export default NewFolderDialog
