# Agent Notes

[English](README.md) | 中文

**Agent Note** 记录影响 Artoo 的持久决策或提案：代码和 API 文档无法承载的理由、备选方案、后果与验证方式。本目录遵循 DeepSeek Harness 的 Agent Note 约定，并适配 Artoo 的 Python/React 技术栈。

## 布局与命名

每份 note 的路径编码两个维度：

```text
{lifecycle}/{class}/yyyy-mm-dd-topic-title.md
```

生命周期表示状态：

- `proposed/`：实施前评审；工作尚未完整交付。
- `implemented/`：决策已交付，note 是该决策理由的当前持有者。
- `rejected/`：提案已被否决。
- `archived/`：冻结的低长期价值 implemented 记录。归档文件是历史快照，禁止编辑，也不得作为当前行为的权威依据。

类别使用以下封闭集合：

| 类别 | 覆盖范围 |
|---|---|
| `feature` | 面向用户或模型的新能力。 |
| `bug-fix` | 修复缺陷，或弥补事故复盘发现的缺口。 |
| `simplification` | 不新增能力的前提下移除代码、行为或对外范围。 |
| `architecture` | 关于交付源码、运行时词汇、持久化或协议契约的结构决策。 |
| `process` | 代码周边的工具、工作流、发布或仓库策略。 |
| `testing` | 测试基础设施与验证策略。 |

文件名日期是主题首次提出的日期。标题使用小写连字符，具备描述性。每个英文 note 都有同名 `.zh.md` 中文对侧文件；note 之间使用相对 Markdown 链接。

不要添加中央 `INDEX.md`。请浏览或搜索生命周期目录树；活跃记录就是工作清单。

## 何时必须写 note

每个非平凡变更必须在同一个 PR 中新增或更新至少一份 Agent Note。非平凡包括运行时行为、架构、后端/前端契约、Open API、SSE、持久化事件、模型/provider 集成、数据库或迁移行为、配置、部署、测试策略，以及维护者可能重新审视的其他决策。

优先更新已经拥有该决策的现有 note，不要重复创建。note 不能被改写成另一个决策：需要反转时新建 note 并互相链接。implemented note 可以在代码路径、名称、默认值或测试变化时同步事实，但决策和理由保持稳定，除非被正式取代。

## implemented note 格式

前几行必须精确，且在中文文件中也保持英文：

```markdown
# Agent Note: <title>

Status: implemented
```

提案使用 `Status: proposed`。否决使用 `Status: rejected — <一行原因>`。状态值必须与所在生命周期目录一致。

implemented note 使用以下正文骨架：

```markdown
## Problem
## Decision
## Alternatives considered
## Consequences
```

可以在 `Decision` 与 `Alternatives considered` 之间加入专门的技术章节。允许使用描述当前事实的 `Testing` 章节。implemented note 禁止使用提案阶段的标题，例如 `Proposal`、`Plan` 或 `Acceptance criteria`。

`Alternatives considered` 必须存在。记录每个真实考虑过的替代方案及其落选原因；每个方案用一个加粗引导段落。不要编造没有发生过的替代方案。

`Consequences` 同时记录代价与收益，包括兼容性影响、运维成本、职责边界和已知缺口。

## 中文对侧文件

每个 `.md` 文件都有逐章节对应的 `.zh.md` 文件。`# Agent Note: ` 和 `Status:` 头部标记保持英文。技术标识、事件名、文件路径和命令保持原样。两种语言具有同等效力。

每个双语文件组都有一个以英文文件名命名的 `.i18n.yaml` sidecar。sidecar 记录上一次确认一致时两种语言文件的 Git blob hash。任一侧变化时，必须在同一变更中同步另一侧并重新记录两个 hash。
