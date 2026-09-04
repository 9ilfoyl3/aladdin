# Agent Note: 采用 DSH 风格 Agent Notes

Status: implemented

[English](2026-09-03-adopt-agent-notes.md) | 中文

## 问题

Artoo 的仓库历史解释过很多实现细节，但没有持续记录契约为什么变化、否决了哪些替代方案、接受了哪些兼容性代价，以及决策应如何验证。Agent 主导的变更尤其容易暴露这个问题：推理过程留在会话历史里，等下一个维护者需要时已经消失。

## 决策

Artoo 在 `.agents/notes` 下使用 DSH 风格 Agent Notes。note 路径编码为 `{lifecycle}/{class}/yyyy-mm-dd-topic-title.md`，生命周期包括 `proposed`、`implemented`、`rejected` 和 `archived`；类别包括 `feature`、`bug-fix`、`simplification`、`architecture`、`process` 和 `testing`；每个活跃 note 都有英文和中文对侧文件。

每个非平凡 PR 必须新增或更新持有相关决策的 note。implemented note 使用 `Problem`、`Decision`、`Alternatives considered` 和 `Consequences`；可以加入描述当前事实的 `Testing` 章节。每个双语文件组都有 `.i18n.yaml` sidecar，记录上一次确认一致时两种语言文件的 Git blob hash。根 `AGENTS.md` 让未来 agent 能发现这条规则。

## 备选方案

**继续依赖 commit body。** 否决。commit body 对单次变更有用，但在事实移动后难以同步更新，也不能提供可搜索的活跃决策清单。

**只使用 Open API 文档。** 否决。它记录当前契约，不记录为什么否决竞争方案，也不记录哪些 trade-off 是有意的。

**创建临时决策日志。** 否决。自由格式日志会偏离实现，也无法区分已交付、提案中、已否决和历史记录。

## 后果

未来 agent 和维护者可以找到契约决策的持有 note，阅读被否决的替代方案，并看到验证证据，不需要重建会话历史。双语 sidecar 能明显暴露某一语言版本漂移的情况。

代价是增加一步文档工作，并且必须在同一个 PR 中同步更新 note。Artoo 目前还没有自动 note 格式或双语配对门禁，因此当前靠 review 和仓库指令约束。
