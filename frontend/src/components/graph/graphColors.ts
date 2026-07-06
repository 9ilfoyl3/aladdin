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

// ============================================================
// 事件层（event-centric graph）专用视觉
// ============================================================
//
// 事件中心图谱里，「事件」是独立于实体的一层节点（node_type==='event'，后端 type 亦为
// 'event'）。它与实体类型「事件」（DEFAULT_ENTITY_TYPES 中的中文类别）语义完全不同：
//   - 实体类型「事件」：被归类为事件的**实体**，走 FIXED['事件'] 红色。
//   - 事件层节点：连接多个实体的**事件脉络**，是图谱的中心，用「主题色（降饱和）」+ 更大尺寸突出。
//
// 事件层不再用固定紫色，而是取当前主题的 --primary 并降一点饱和度，保证与整体配色协调、
// 又与任一实体类型区分。颜色在运行时从 CSS 变量解析（随主题切换而变）。

/** 事件层在图例/抽屉里的显示名（与实体类型「事件」区分，消除语义冲突）。 */
export const EVENT_LAYER_LABEL = '事件脉络'

// 主题色解析失败时的兜底（一个中性偏冷的灰蓝，避免纯黑/纯白）。
const EVENT_COLOR_FALLBACK = '#64748b'

/** 解析 rgb/rgba/hex 字符串为 [r,g,b]（0-255）；失败返回 null。 */
function parseRgb(input: string): [number, number, number] | null {
  const s = input.trim()
  const rgbMatch = s.match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/i)
  if (rgbMatch) {
    return [Number(rgbMatch[1]), Number(rgbMatch[2]), Number(rgbMatch[3])]
  }
  const hex = s.replace('#', '')
  if (hex.length === 6) {
    return [
      parseInt(hex.slice(0, 2), 16),
      parseInt(hex.slice(2, 4), 16),
      parseInt(hex.slice(4, 6), 16),
    ]
  }
  return null
}

/** 向灰阶方向混合以降低饱和度（amount 0=不变，1=完全灰）。 */
function desaturate([r, g, b]: [number, number, number], amount: number): [number, number, number] {
  const gray = 0.299 * r + 0.587 * g + 0.114 * b
  const mix = (c: number) => Math.round(c * (1 - amount) + gray * amount)
  return [mix(r), mix(g), mix(b)]
}

/** 按因子调整明度（factor <1 变暗，用于描边）。 */
function scale([r, g, b]: [number, number, number], factor: number): [number, number, number] {
  const clamp = (c: number) => Math.max(0, Math.min(255, Math.round(c * factor)))
  return [clamp(r), clamp(g), clamp(b)]
}

const toRgb = ([r, g, b]: [number, number, number]) => `rgb(${r}, ${g}, ${b})`
const toRgba = ([r, g, b]: [number, number, number], a: number) => `rgba(${r}, ${g}, ${b}, ${a})`

/**
 * 取事件层配色（填充 / 描边 / MENTIONS 边），基于当前主题 --primary 降饱和度派生。
 *
 * 运行时读取 CSS 变量，故随主题切换自动变化。调用方（画布/图例）应在主题变化时重算。
 */
export function getEventColors(): { fill: string; border: string; edge: string } {
  let base: [number, number, number] | null = null
  if (typeof window !== 'undefined' && typeof getComputedStyle === 'function') {
    const primary = getComputedStyle(document.documentElement).getPropertyValue('--primary')
    base = parseRgb(primary)
  }
  const rgb = base ? desaturate(base, 0.28) : (parseRgb(EVENT_COLOR_FALLBACK) as [number, number, number])
  return {
    fill: toRgb(rgb),
    border: toRgb(scale(rgb, 0.78)),
    edge: toRgba(rgb, 0.5),
  }
}

/** 判定一个节点是否为事件层节点（优先 node_type，兼容后端 type='event'）。 */
export function isEventNode(node: { node_type?: string; type?: string }): boolean {
  return node.node_type === 'event' || node.type === 'event'
}
