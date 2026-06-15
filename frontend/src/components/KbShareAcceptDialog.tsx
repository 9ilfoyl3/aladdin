import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { AlertCircle, UserRound, Eye } from 'lucide-react'
import { kbShareLinkApi } from '@/lib/api'
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import Aurora from '@/components/reactbits/Aurora'

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

  const invalid = isError || (info && !info.valid)
  const canAccept = !!info?.valid && !!info.can_accept

  return (
    <Dialog open={!!token} onOpenChange={(o) => { if (!o) close() }}>
      <DialogContent className="overflow-hidden p-0 sm:max-w-md gap-0">
        {/* Aurora 极光背景：仅顶部弥漫，用 mask 向下平滑淡到全透明（露出干净卡片底色，无硬边、
            不染按钮区）。浅色模式浓度更低，避免白底上发灰发脏。 */}
        <div
          className="pointer-events-none absolute inset-0 opacity-35 dark:opacity-55"
          style={{
            maskImage: 'linear-gradient(to bottom, black 0%, black 30%, transparent 70%)',
            WebkitMaskImage: 'linear-gradient(to bottom, black 0%, black 30%, transparent 70%)',
          }}
        >
          <Aurora colorStops={['#83d68e', '#3a7d44', '#a7e8b0']} blend={1} amplitude={1} speed={0.6} />
        </div>

        <div className="relative px-6 pt-8 pb-6">
          {isLoading ? (
            <div className="flex flex-col items-center gap-4">
              <Skeleton className="h-16 w-16 rounded-full" />
              <Skeleton className="h-5 w-56" />
              <Skeleton className="h-4 w-40" />
            </div>
          ) : invalid ? (
            <div className="flex flex-col items-center gap-3 py-4 text-center">
              <div className="flex h-14 w-14 items-center justify-center rounded-full bg-muted">
                <AlertCircle className="h-7 w-7 text-muted-foreground" />
              </div>
              <DialogTitle className="text-base">分享链接无效</DialogTitle>
              <DialogDescription className="text-sm">
                该分享链接无效、已过期或已被吊销
              </DialogDescription>
            </div>
          ) : info ? (
            <div className="flex flex-col items-center text-center">
              {/* 分享者头像作为主视觉 */}
              <div className="relative">
                {info.owner_avatar ? (
                  <img
                    src={info.owner_avatar}
                    alt={info.owner_username || '分享者'}
                    className="h-16 w-16 rounded-full object-cover ring-2 ring-primary/40 shadow-sm"
                  />
                ) : (
                  <div className="flex h-16 w-16 items-center justify-center rounded-full bg-primary/10 ring-2 ring-primary/40 shadow-sm">
                    <UserRound className="h-8 w-8 text-primary" strokeWidth={1.75} />
                  </div>
                )}
              </div>

              {/* 标题（正常字体） */}
              <DialogTitle className="mt-4 text-base font-semibold">收到一个知识库分享</DialogTitle>

              {/* 句式文案：覆盖团队 / 分享者 / 知识库三要素 */}
              <DialogDescription className="mt-2 text-sm leading-relaxed text-muted-foreground">
                <span className="font-medium text-foreground">{info.owner_tenant_name || '某团队'}</span>
                {' 团队的 '}
                <span className="font-medium text-foreground">{info.owner_username || '一位成员'}</span>
                {' 将他的知识库 '}
                <span className="font-medium text-foreground">{info.kb_name}</span>
                {' 分享给你'}
              </DialogDescription>

              {/* 只读权限徽标 */}
              <div className="mt-3 inline-flex items-center gap-1 rounded-full bg-primary/10 px-2.5 py-1 text-xs font-medium text-primary ring-1 ring-inset ring-primary/20">
                <Eye className="h-3 w-3" />
                只读权限
              </div>

              {/* 不可加入原因 */}
              {!info.can_accept && (
                <div className="mt-4 flex w-full items-start gap-2 rounded-lg bg-amber-50 px-3 py-2 text-left text-xs text-amber-700 dark:bg-amber-950/40 dark:text-amber-400">
                  <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>{info.reason || '当前无法加入该分享'}</span>
                </div>
              )}
            </div>
          ) : null}

          {/* 操作区 */}
          <div className="mt-6 flex items-center gap-2">
            <Button variant="outline" className="flex-1 cursor-pointer" onClick={close} disabled={accepting}>
              不加入
            </Button>
            <Button
              className="flex-1 cursor-pointer"
              onClick={onAccept}
              disabled={accepting || isLoading || !canAccept}
            >
              {accepting ? '加入中…' : '加入知识库'}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
