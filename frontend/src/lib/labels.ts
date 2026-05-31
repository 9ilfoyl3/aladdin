// 展示标签映射（与后端 constants.py 的 ROLE_LABELS 保持一致）。
// 内置角色名是稳定的英文契约（admin/user），界面上以中文呈现更直观；
// 自定义角色无映射时原样展示其名。

const ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  user: '普通用户',
}

/** 角色名 -> 中文展示名；自定义角色回退为原名。 */
export function roleLabel(name: string): string {
  return ROLE_LABELS[name] ?? name
}
