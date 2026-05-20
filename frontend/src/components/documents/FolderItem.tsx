import { Folder as FolderIcon } from 'lucide-react'

// 文件夹数据类型
export interface FolderData {
  id: string
  kb_id: string
  parent_id: string | null
  name: string
  doc_count: number
  subfolder_count: number
  created_at: string
  updated_at: string
}

interface FolderItemProps {
  folder: FolderData
  isSelected: boolean
  onSelect: (id: string) => void
  onOpen: (id: string) => void
}

// Finder 风格文件夹项 - 主题色图标
function FolderItem({ folder, isSelected, onSelect, onOpen }: FolderItemProps) {
  return (
    <div
      className={`group relative flex flex-col items-center rounded-lg px-2 py-3 transition-all duration-150 cursor-pointer select-none ${
        isSelected ? 'bg-primary/8 ring-1 ring-primary/30' : 'hover:bg-muted/40'
      }`}
      onClick={(e) => { e.stopPropagation(); onSelect(folder.id) }}
      onDoubleClick={(e) => { e.stopPropagation(); onOpen(folder.id) }}
    >
      {/* 文件夹图标 - 主题色 */}
      <div className="w-16 h-18 flex items-center justify-center mb-1">
        <FolderIcon className="h-14 w-14 text-primary/80 fill-primary/20" />
      </div>

      {/* 文件夹名 */}
      <p className="text-[11px] text-center text-foreground leading-tight w-full px-0.5 line-clamp-2" title={folder.name}>
        {folder.name}
      </p>
    </div>
  )
}

export default FolderItem
