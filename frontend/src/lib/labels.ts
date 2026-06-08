// 展示标签映射（与后端 constants.py 的 ROLE_LABELS 保持一致）。
// 固定角色模型：租户成员只有 admin / member 两个固定角色，英文名是稳定契约，
// 界面上以中文呈现更直观；未登记的角色名原样展示其名。

const ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  member: '普通成员',
}

/** 角色名 -> 中文展示名；未登记角色回退为原名。 */
export function roleLabel(name: string): string {
  return ROLE_LABELS[name] ?? name
}
