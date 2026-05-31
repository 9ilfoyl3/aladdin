import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ScrollText, Search } from 'lucide-react'
import { adminApi } from '@/lib/api'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/table'
import TableSkeleton from '@/components/skeletons/TableSkeleton'

// 动作 code -> 中文标签
const ACTION_LABEL: Record<string, string> = {
  'tenant.create': '创建租户', 'tenant.set_status': '启停租户',
  'apikey.proxy_create': '签发代理Key', 'apikey.proxy_revoke': '撤销代理Key',
  'user.create': '创建用户', 'user.set_status': '启停用户',
  'user.reset_password': '重置口令', 'user.set_roles': '分配角色',
  'user.transfer_kb': '转移知识库',
  'role.create': '创建角色', 'role.set_permissions': '改角色权限', 'role.delete': '删除角色',
  'apikey.create': '创建Key', 'apikey.revoke': '撤销Key', 'apikey.update_scope': '改Key范围',
  'kb.set_visibility': '改KB可见性', 'kb.share': '共享KB', 'kb.revoke_share': '撤销共享',
  'invitation.create': '创建邀请', 'invitation.revoke': '吊销邀请', 'invitation.accept': '接受邀请',
  'auth.login_success': '登录成功', 'auth.login_fail': '登录失败', 'auth.change_password': '修改口令',
}

// 审计日志页面（user:manage 可读；超管全局、租管限本租户）。只读。
function AuditLogs() {
  const [page, setPage] = useState(1)
  const [actor, setActor] = useState('')

  const { data: logsPage, isLoading } = useQuery({
    queryKey: ['audit-logs', page, actor],
    queryFn: () => adminApi.auditLogs({ page, page_size: 30, actor: actor || undefined }),
  })
  const logs = logsPage?.items ?? []

  function fmt(t: string) {
    return t ? new Date(t).toLocaleString('zh-CN') : '-'
  }

  return (
    <div>
      <div className="mb-6">
        <h2 className="text-2xl font-bold">审计日志</h2>
        <p className="text-muted-foreground text-sm mt-1">记录管理操作的元数据（不含业务内容正文）。超管查看全局，租户管理员查看本租户。</p>
      </div>

      <div className="mb-4 relative max-w-xs">
        <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
        <Input value={actor} onChange={(e) => { setActor(e.target.value); setPage(1) }} placeholder="按操作者搜索" className="pl-8" />
      </div>

      {isLoading ? (
        <TableSkeleton rows={8} columns={5} />
      ) : logs.length === 0 ? (
        <div className="text-center py-12 text-muted-foreground">
          <ScrollText className="h-12 w-12 mx-auto mb-4 opacity-50" />
          <p>暂无审计记录</p>
        </div>
      ) : (
        <div className="border rounded-lg">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>时间</TableHead>
                <TableHead>操作者</TableHead>
                <TableHead>动作</TableHead>
                <TableHead>对象</TableHead>
                <TableHead>结果</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {logs.map((log) => (
                <TableRow key={log.id}>
                  <TableCell className="text-xs text-muted-foreground whitespace-nowrap">{fmt(log.created_at)}</TableCell>
                  <TableCell>
                    {log.actor_username || '-'}
                    {log.actor_is_super_admin && <Badge variant="outline" className="ml-1.5 text-xs">超管</Badge>}
                  </TableCell>
                  <TableCell>{ACTION_LABEL[log.action] || log.action}</TableCell>
                  <TableCell className="text-sm">
                    {log.target_name || log.target_id || '-'}
                    {log.target_type && <span className="text-muted-foreground text-xs ml-1">({log.target_type})</span>}
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline" className={log.result === 'success' ? 'bg-green-100 text-green-700 border-green-200' : 'bg-red-100 text-red-700 border-red-200'}>
                      {log.result === 'success' ? '成功' : '失败'}
                    </Badge>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      {logsPage && logsPage.total > logsPage.page_size && (
        <div className="flex items-center justify-end gap-2 mt-4 text-sm">
          <span className="text-muted-foreground">共 {logsPage.total} 条</span>
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>上一页</Button>
          <span>第 {page} 页</span>
          <Button variant="outline" size="sm" disabled={!logsPage.has_more} onClick={() => setPage((p) => p + 1)}>下一页</Button>
        </div>
      )}
    </div>
  )
}

export default AuditLogs
