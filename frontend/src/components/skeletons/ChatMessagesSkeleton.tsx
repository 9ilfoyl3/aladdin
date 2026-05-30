import { Skeleton } from "@/components/ui/skeleton"

/**
 * 对话消息骨架屏。
 * 用于切换历史会话、加载消息记录时占位：模拟「用户提问气泡 + 助手回答」交替结构，
 * 与 MessageBubble 的真实布局对齐（用户消息右对齐，助手消息左侧带头像）。
 */
function ChatMessagesSkeleton() {
  return (
    <div className="max-w-3xl mx-auto py-6 px-4 space-y-8 animate-in fade-in-0 duration-300">
      {/* 用户提问气泡（右对齐） */}
      <div className="flex justify-end">
        <Skeleton className="h-12 w-2/5 rounded-2xl rounded-br-md" />
      </div>

      {/* 助手回答（左侧头像 + 多行文本） */}
      <div className="flex gap-3 items-start">
        <Skeleton className="w-8 h-8 rounded-full shrink-0 mt-1" />
        <div className="flex-1 space-y-2.5 pt-1">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-11/12" />
          <Skeleton className="h-4 w-4/5" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      </div>

      {/* 第二轮 */}
      <div className="flex justify-end">
        <Skeleton className="h-10 w-1/3 rounded-2xl rounded-br-md" />
      </div>

      <div className="flex gap-3 items-start">
        <Skeleton className="w-8 h-8 rounded-full shrink-0 mt-1" />
        <div className="flex-1 space-y-2.5 pt-1">
          <Skeleton className="h-4 w-10/12" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-3/5" />
        </div>
      </div>
    </div>
  )
}

export default ChatMessagesSkeleton
