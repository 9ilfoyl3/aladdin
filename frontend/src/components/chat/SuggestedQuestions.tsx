import { Sparkles } from 'lucide-react'

// 新对话空态的提问示例气泡组。后端暂无推荐问题接口，使用一组静态示例引导用户提问。
const SAMPLE_QUESTIONS = [
  '帮我总结知识库里最近上传文档的核心内容',
  '这个项目的整体架构是怎样的？',
  '如何快速接入并调用对话接口？',
  '知识库支持哪些文档格式？',
  '检索结果不准确时该如何优化？',
  '介绍一下系统的核心功能模块',
]

interface SuggestedQuestionsProps {
  /** 点击气泡时回调，填充并发送该问题 */
  onSelect: (question: string) => void
}

function SuggestedQuestions({ onSelect }: SuggestedQuestionsProps) {
  return (
    <div className="flex flex-col items-center w-full">
      <div className="flex items-center gap-1.5 text-sm text-muted-foreground mb-4">
        <Sparkles className="h-3.5 w-3.5 text-primary" />
        <span>你可以这样问我</span>
      </div>
      <div className="flex flex-wrap gap-2.5 justify-center">
        {SAMPLE_QUESTIONS.map((question, index) => (
          <button
            key={question}
            onClick={() => onSelect(question)}
            style={{ animationDelay: `${index * 60}ms` }}
            className="group animate-in fade-in slide-in-from-bottom-2 fill-mode-both px-4 py-2.5 rounded-full border border-border bg-card text-sm text-foreground/80 hover:border-primary hover:bg-accent hover:text-foreground hover:shadow-sm transition-all cursor-pointer"
          >
            {question}
          </button>
        ))}
      </div>
    </div>
  )
}

export default SuggestedQuestions
