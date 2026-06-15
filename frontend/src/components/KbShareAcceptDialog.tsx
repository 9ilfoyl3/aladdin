import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { FileText, Building2, User, ShieldCheck, Loader2 } from 'lucide-react'
import { kbShareLinkApi } from '@/lib/api'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'

// 跨租户知识库分享领取弹窗（cross-tenant-kb-share）。
// 由知识库列表页挂载：当 URL 带 ?share=<token> 时自动弹出确认框，支持「加入 / 不加入」。
// 加入成功后刷新知识库列表缓存，使被授权库立即出现在列表中。
export default function KbShareAcceptDialog() {
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const token = searchParams.get('share') || ''
  const [accepting, setAccepting] = useState(false)

  const { data: info, isLoading, isError } = useQuery({
    queryKey: ['kb-share-info', token],
    queryFn: () => kbShareLinkApi.info(token),
    enabled: !!token,
    retry: false,
  })

  // 关闭：清掉 ?share= 查询参数（保留其它参数），弹窗随之关闭。
  function close() {
    const next = new URLSearchParams(searchParams)
    next.delete('share')
    setSearchParams(next, { replace: true })
  }

  async function onAccept() {
    setAccepting(true)
    try {
      await kbShareLinkApi.accept(token)
      // 关键：领取后刷新列表缓存，被授权库才会出现在「我的知识库」列表中。
      await queryClient.invalidateQueries({ queryKey: ['knowledge-bases'] })
      toast.success('已加入，知识库已出现在你的列表中')
      close()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '加入失败')
    } finally {
      setAccepting(false)
    }
  }

  const open = !!token

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) close() }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>收到一个知识库分享</DialogTitle>
          <DialogDescription>加入后将以只读方式出现在你的知识库列表中</DialogDescription>
        </DialogHeader>

        {isLoading ? (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" /> 加载中…
          </div>
        ) : isError || !info?.valid ? (
          <div className="py-6 text-center text-sm text-muted-foreground">
            该分享链接无效、已过期或已被吊销。
          </div>
        ) : (
          <div className="space-y-3 rounded-lg border border-border bg-muted/30 p-4 text-sm">
            <div className="flex items-center gap-2">
              <FileText className="h-4 w-4 text-muted-foreground shrink-0" />
              <span className="text-muted-foreground">知识库</span>
              <span className="ml-auto font-medium text-foreground truncate">{info.kb_name}</span>
            </div>
            <div className="flex items-center gap-2">
              <User className="h-4 w-4 text-muted-foreground shrink-0" />
              <span className="text-muted-foreground">分享者</span>
              <span className="ml-auto text-foreground truncate">{info.owner_username || '—'}</span>
            </div>
            <div className="flex items-center gap-2">
              <Building2 className="h-4 w-4 text-muted-foreground shrink-0" />
              <span className="text-muted-foreground">来源团队</span>
              <span className="ml-auto text-foreground truncate">{info.owner_tenant_name || '—'}</span>
            </div>
            <div className="flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-muted-foreground shrink-0" />
              <span className="text-muted-foreground">权限</span>
              <span className="ml-auto text-foreground">只读</span>
            </div>
          </div>
        )}

        {!isLoading && !isError && info?.valid && !info.can_accept && (
          <p className="text-center text-sm text-amber-600">{info.reason || '当前无法加入该分享'}</p>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={close} disabled={accepting}>
            不加入
          </Button>
          <Button
            onClick={onAccept}
            disabled={accepting || isLoading || isError || !info?.valid || !info?.can_accept}
          >
            {accepting ? '加入中…' : '加入'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
