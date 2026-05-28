# Agent 代码审查报告：Aladdin vs WeKnora

## 审查范围

对照 `WeKnora/internal/agent/` 的实现，检查 Aladdin `backend/app/agent/` 中的关键集成点。

**WeKnora 参考文件：**
- `internal/agent/engine.go` — ReAct 循环主引擎
- `internal/agent/act.go` — 工具执行
- `internal/agent/observe.go` — 上下文管理、结果追加、KB 结果 redact
- `internal/agent/tools/registry.go` — 工具注册表（含截断）
- `internal/agent/tools/truncate.go` — 输出截断实现
- `internal/agent/memory/consolidator.go` — LLM 驱动的记忆压缩
- `internal/agent/token/compress.go` — 简单上下文压缩

## 发现的问题与修复

### 1. ✅ 工具输出截断未集成到 Registry（已修复）

**问题：** `truncate_tool_output()` 在 `context_manager.py` 中定义但从未被调用。WeKnora 在 `registry.go` 的 `ExecuteTool()` 中执行完工具后立即截断超长输出。

**修复：** 在 `tools/registry.py` 的 `execute()` 方法中，工具执行后检查输出长度，超过 `max_tool_output_chars` 时调用 `truncate_tool_output()`。

### 2. ✅ 上下文压缩未集成到循环（已修复）

**问题：** `ContextManager.compress_messages()` 存在但未在 `_execute_loop` 中调用。WeKnora 在 `runReActIteration()` 的每轮开始时调用 `manageContextWindow()`，在 LLM 调用前压缩消息。

**修复：** 在 `engine.py` 的 `_execute_loop` 中，每轮 LLM 调用前调用 `self._context_manager.compress_messages(messages, self._config.max_context_tokens)`。

### 3. ✅ 历史 KB 结果 redact 未集成（已修复）

**问题：** `redact_historical_kb_results()` 存在但未在 `execute()` 中调用。WeKnora 在 `buildMessagesWithLLMContext()` 中对 `llmContext`（跨轮历史）调用 `redactHistoryKBResults()`，将历史 KB 工具结果替换为占位符，强制每轮重新检索。

**修复：** 在 `engine.py` 的 `execute()` 方法中，追加 `llm_context` 前先调用 `redact_historical_kb_results()` 处理。

### 4. ✅ Registry 缺少 first-wins 策略和 error hint（已修复）

**问题：** WeKnora 的 `RegisterTool()` 使用 first-wins 策略防止工具名称劫持，`ExecuteTool()` 在失败时追加 error hint 引导 LLM 换策略。Aladdin 的 registry 允许覆盖且无 error hint。

**修复：** 
- `register()` 改为 first-wins：重复注册时保留第一个，记录 warning
- `execute()` 失败时追加 `[Analyze the error above and try a different approach.]`

### 5. ✅ System Prompt 已正确集成（无需修复）

Aladdin 已在 `execute()` 中检查 `knowledge_base_ids` 并调用 `render_system_prompt()`，与 WeKnora 的 `BuildSystemPromptWithOptions()` 模式一致。

## 与 WeKnora 的剩余差异（非阻塞）

| 特性 | WeKnora | Aladdin | 状态 |
|------|---------|---------|------|
| LLM 驱动的记忆压缩 | `memory.Consolidator`（调用 LLM 总结历史） | 仅简单压缩（保留最近 2 轮） | 可后续增强 |
| API Usage 驱动的 token 估算 | 使用上一轮 API 返回的 `Usage.TotalTokens` + BPE delta | 纯字符估算 | 可后续增强 |
| 参数校验（ValidateParams） | 执行前 JSON Schema 校验 | 无 | 可后续增强 |
| 参数类型修复（CastParams） | 自动修复 LLM 常见类型错误 | 无 | 可后续增强 |
| JSON 修复（RepairJSON） | 修复 LLM 生成的畸形 JSON | 无 | 可后续增强 |
| Langfuse 可观测性 | 完整 span 追踪 | 无 | Phase 4 |
| Context cancellation 处理 | 检测 ctx.Done() 并 graceful 退出 | 无 | 可后续增强 |
| VLM 图片描述 | 工具结果中的图片自动 VLM 分析 | 无 | 可后续增强 |

## 测试影响

修改了 `tests/test_tool_registry.py` 中两个断言：
- `test_execute_tool_not_found`: 改为 `"Tool 'nonexistent' not found" in result.error`
- `test_execute_failing_tool`: 改为 `"Something went wrong" in result.error`

原因：error 消息现在追加了 error hint 后缀。
