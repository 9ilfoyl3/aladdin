import { Sparkles } from 'lucide-react'

// 新对话空态的提问示例气泡组。后端暂无推荐问题接口，使用一组静态示例引导用户提问。
const SAMPLE_QUESTIONS = [
  '帮我总结知识库文档的核心要点，并标注每条来自哪份文件',
  '从知识库里找找有没有相关的资料',
  '用通俗的话解释一下这个概念',
  '对比一下这几份文档的主要差异',
  '根据资料帮我列一份要点清单',
  '这个问题在文档里是怎么说明的？',
  '把这部分内容整理成表格',
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
