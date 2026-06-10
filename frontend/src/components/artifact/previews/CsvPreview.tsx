import { useMemo } from 'react'
import { Loader2, AlertCircle } from 'lucide-react'

interface CsvPreviewProps {
  /** 已读取的 CSV 源文本；为 null 时显示加载中 */
  text: string | null
  loading: boolean
  error: string | null
}

/**
 * 轻量 CSV 解析（零依赖）：支持双引号包裹字段、字段内逗号/换行、转义双引号("")。
 * 适合预览常规 CSV；超大文件仅渲染前若干行以保证性能。
 */
function parseCsv(input: string): string[][] {
  const rows: string[][] = []
  let row: string[] = []
  let field = ''
  let inQuotes = false
  let i = 0
  const text = input.replace(/\r\n/g, '\n').replace(/\r/g, '\n')

  while (i < text.length) {
    const ch = text[i]
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"'
          i += 2
          continue
        }
        inQuotes = false
        i++
        continue
      }
      field += ch
      i++
      continue
    }
    if (ch === '"') {
      inQuotes = true
      i++
      continue
    }
    if (ch === ',') {
      row.push(field)
      field = ''
      i++
      continue
    }
    if (ch === '\n') {
      row.push(field)
      rows.push(row)
      row = []
      field = ''
      i++
      continue
    }
    field += ch
    i++
  }
  // 收尾最后一个字段/行
  if (field.length > 0 || row.length > 0) {
    row.push(field)
    rows.push(row)
  }
  return rows
}

const MAX_ROWS = 2000

function CsvPreview({ text, loading, error }: CsvPreviewProps) {
  const { rows, truncated } = useMemo(() => {
    if (!text) return { rows: [] as string[][], truncated: false }
    const all = parseCsv(text).filter((r) => r.length > 0 && !(r.length === 1 && r[0] === ''))
    return { rows: all.slice(0, MAX_ROWS), truncated: all.length > MAX_ROWS }
  }, [text])

  if (error) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-3 text-muted-foreground">
        <AlertCircle className="h-10 w-10 text-destructive/60" />
        <p className="text-sm">{error}</p>
      </div>
    )
  }

  if (loading || text === null) {
    return (
      <div className="h-full flex items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-primary/60" />
      </div>
    )
  }

  if (rows.length === 0) {
    return (
      <div className="h-full flex items-center justify-center text-muted-foreground text-sm">
        空文件
      </div>
    )
  }

  const [header, ...body] = rows

  return (
    <div className="h-full overflow-auto bg-background">
      <table className="w-full text-[13px] border-collapse">
        <thead className="sticky top-0 z-10">
          <tr>
            <th className="border border-border/50 bg-muted/60 px-2 py-1.5 text-muted-foreground font-medium text-right w-12">
              #
            </th>
            {header.map((cell, idx) => (
              <th
                key={idx}
                className="border border-border/50 bg-muted/60 px-2.5 py-1.5 text-left font-medium whitespace-nowrap"
              >
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((r, ri) => (
            <tr key={ri} className="even:bg-muted/20">
              <td className="border border-border/50 px-2 py-1.5 text-muted-foreground text-right tabular-nums">
                {ri + 1}
              </td>
              {header.map((_, ci) => (
                <td key={ci} className="border border-border/50 px-2.5 py-1.5 align-top">
                  {r[ci] ?? ''}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {truncated && (
        <p className="px-3 py-2 text-xs text-muted-foreground">
          仅预览前 {MAX_ROWS} 行
        </p>
      )}
    </div>
  )
}

export default CsvPreview
