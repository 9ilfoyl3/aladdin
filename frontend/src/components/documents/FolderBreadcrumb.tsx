import { ChevronRight, Home } from 'lucide-react'

// 面包屑项类型
interface BreadcrumbItem {
  id: string | null
  name: string
}

interface FolderBreadcrumbProps {
  items: BreadcrumbItem[]
  onNavigate: (folderId: string | null) => void
}

// Finder 风格面包屑导航
function FolderBreadcrumb({ items, onNavigate }: FolderBreadcrumbProps) {
  return (
    <nav className="flex items-center gap-1 text-sm min-w-0 overflow-hidden">
      {/* 根目录 */}
      <button
        onClick={() => onNavigate(null)}
        className="flex items-center gap-1 px-2 py-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors cursor-pointer shrink-0"
      >
        <Home className="h-3.5 w-3.5" />
        <span>全部文件</span>
      </button>

      {/* 路径分隔 + 文件夹 */}
      {items.map((item, idx) => {
        const isLast = idx === items.length - 1
        return (
          <div key={item.id || idx} className="flex items-center gap-1 min-w-0">
            <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/60 shrink-0" />
            {isLast ? (
              <span className="px-2 py-1 rounded-md text-foreground font-medium truncate">
                {item.name}
              </span>
            ) : (
              <button
                onClick={() => onNavigate(item.id)}
                className="px-2 py-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted/60 transition-colors cursor-pointer truncate max-w-[120px]"
              >
                {item.name}
              </button>
            )}
          </div>
        )
      })}
    </nav>
  )
}

export default FolderBreadcrumb
