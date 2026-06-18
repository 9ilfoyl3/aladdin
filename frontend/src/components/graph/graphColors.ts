// 实体类型 → 颜色映射（design.md 5.3.2 类型图例 + 颜色映射）。
//
// 纯函数、无状态：图例与画布共用同一映射，保证「同一类型同一颜色」。
// 调色板取自一组区分度较高的色相；未知类型按名称哈希稳定落到调色板某色，
// 保证同名类型每次渲染颜色一致（不随顺序变化）。

// 区分度较高的固定调色板（明度适中，深浅背景下都清晰）。
const PALETTE = [
  '#6366f1', // indigo
  '#10b981', // emerald
  '#f59e0b', // amber
  '#ef4444', // red
  '#3b82f6', // blue
  '#8b5cf6', // violet
  '#ec4899', // pink
  '#14b8a6', // teal
  '#f97316', // orange
  '#84cc16', // lime
  '#06b6d4', // cyan
  '#a855f7', // purple
]

// 常见默认实体类型固定配色（DEFAULT_ENTITY_TYPES，design.md 3.3），其余走哈希兜底。
const FIXED: Record<string, string> = {
  人物: '#6366f1',
  组织: '#10b981',
  地点: '#f59e0b',
  概念: '#3b82f6',
  产品: '#ec4899',
  事件: '#ef4444',
  时间: '#14b8a6',
  作品: '#8b5cf6',
  技术: '#06b6d4',
  其它: '#94a3b8',
}

// 简单稳定字符串哈希（djb2 变体），用于未知类型落到调色板。
function hashString(s: string): number {
  let h = 5381
  for (let i = 0; i < s.length; i++) {
    h = (h * 33) ^ s.charCodeAt(i)
  }
  return Math.abs(h)
}

/** 取某实体类型的颜色（确定性：同名类型恒返回同色）。 */
export function colorForType(type: string): string {
  if (!type) return '#94a3b8'
  if (FIXED[type]) return FIXED[type]
  return PALETTE[hashString(type) % PALETTE.length]
}
