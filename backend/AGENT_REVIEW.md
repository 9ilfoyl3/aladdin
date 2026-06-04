# Agent 代码审查报告

## 审查范围

检查 Artoo `backend/app/agent/` 中 ReAct 循环的关键集成点：主引擎、工具执行、
上下文管理与结果追加、KB 结果 redact、工具注册表（含截断）、输出截断、
LLM 驱动的记忆压缩、简单上下文压缩。

## 发现的问题与修复

### 1. ✅ 工具输出截断未集成到 Registry（已修复）

**问题：** `truncate_tool_output()` 在 `context_manager.py` 中定义但从未被调用。理想做法是在工具注册表执行完工具后立即截断超长输出。

**修复：** 在 `tools/registry.py` 的 `execute()` 方法中，工具执行后检查输出长度，超过 `max_tool_output_chars` 时调用 `truncate_tool_output()`。

### 2. ✅ 上下文压缩未集成到循环（已修复）

**问题：** `ContextManager.compress_messages()` 存在但未在 `_execute_loop` 中调用。理想做法是在每轮迭代开始、LLM 调用前压缩消息。

**修复：** 在 `engine.py` 的 `_execute_loop` 中，每轮 LLM 调用前调用 `self._context_manager.compress_messages(messages, self._config.max_context_tokens)`。

### 3. ✅ 历史 KB 结果 redact 未集成（已修复）

**问题：** `redact_historical_kb_results()` 存在但未在 `execute()` 中调用。理想做法是对跨轮历史上下文做 redact，将历史 KB 工具结果替换为占位符，强制每轮重新检索。

**修复：** 在 `engine.py` 的 `execute()` 方法中，追加 `llm_context` 前先调用 `redact_historical_kb_results()` 处理。

### 4. ✅ Registry 缺少 first-wins 策略和 error hint（已修复）

**问题：** 工具注册应使用 first-wins 策略防止工具名称劫持，工具执行失败时应追加 error hint 引导 LLM 换策略。原 registry 允许覆盖且无 error hint。

**修复：**
- `register()` 改为 first-wins：重复注册时保留第一个，记录 warning
- `execute()` 失败时追加 `[Analyze the error above and try a different approach.]`

### 5. ✅ System Prompt 已正确集成（无需修复）

`execute()` 中已检查 `knowledge_base_ids` 并调用 `render_system_prompt()`，构建系统提示词的模式正确。

## 未实现的增强项（非阻塞）

| 特性 | 现状 | 状态 |
|------|---------|------|
| LLM 驱动的记忆压缩 | 仅简单压缩（保留最近 2 轮），未用 LLM 总结历史 | 可后续增强 |
| API Usage 驱动的 token 估算 | 纯字符估算，未利用上一轮 API 返回的 `Usage.TotalTokens` + BPE delta | 可后续增强 |
| 参数校验（ValidateParams） | 无执行前 JSON Schema 校验 | 可后续增强 |
| 参数类型修复（CastParams） | 无自动修复 LLM 常见类型错误 | 可后续增强 |
| JSON 修复（RepairJSON） | 无畸形 JSON 修复 | 可后续增强 |
| Langfuse 可观测性 | 无完整 span 追踪 | Phase 4 |
| Context cancellation 处理 | 无 ctx 取消检测与 graceful 退出 | 可后续增强 |
| VLM 图片描述 | 工具结果中的图片无自动 VLM 分析 | 可后续增强 |

## 测试影响

修改了 `tests/test_tool_registry.py` 中两个断言：
- `test_execute_tool_not_found`: 改为 `"Tool 'nonexistent' not found" in result.error`
- `test_execute_failing_tool`: 改为 `"Something went wrong" in result.error`

原因：error 消息现在追加了 error hint 后缀。
