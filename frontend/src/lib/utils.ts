import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// ============================================================
// 名称校验工具（与后端 validators.py 规则保持一致）
// ============================================================

const MAX_NAME_LENGTH = 200
const FORBIDDEN_CHARS_RE = /[/\\<>:"|?*\x00-\x1f]/
const RESERVED_NAMES = new Set([
  'CON', 'PRN', 'AUX', 'NUL',
  'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
  'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9',
])

/**
 * 校验文件/文件夹名称，返回错误信息或 null（表示通过）
 */
export function validateName(name: string, label = '名称'): string | null {
  const cleaned = name.trim()
  if (!cleaned) return `${label}不能为空`
  if (cleaned.length > MAX_NAME_LENGTH) return `${label}不能超过 ${MAX_NAME_LENGTH} 个字符`

  const match = cleaned.match(FORBIDDEN_CHARS_RE)
  if (match) {
    const char = match[0]
    const desc = char.charCodeAt(0) < 32 ? `控制字符` : `'${char}'`
    return `${label}包含不允许的字符: ${desc}`
  }

  if (cleaned.endsWith('.') || cleaned.endsWith(' ')) return `${label}不能以点号或空格结尾`

  const baseName = cleaned.split('.')[0].toUpperCase()
  if (RESERVED_NAMES.has(baseName)) return `'${cleaned}' 是系统保留名称`

  return null
}
