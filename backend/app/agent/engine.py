"""AgentEngine - ReAct Agent 核心引擎

实现 Reasoning + Acting 循环：LLM 自主决策调用工具、分析结果、
决定是否继续检索或提交最终答案。引擎本身无状态，每次 execute()
创建新的 AgentState。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from app.agent.config import AgentConfig
from app.agent.content_router import ContentStreamRouter
from app.agent.events import AgentEvent, EventBus, EventType
from app.agent.memory.compress import compress_context
from app.agent.memory.consolidator import MemoryConsolidator
from app.agent.memory.token_estimator import TokenEstimator
from app.agent.memory.usage_tracker import UsageTracker
from app.agent.state import AgentState, AgentStep, ToolCallRecord
from app.agent.tools.base import ToolContext, ToolResult
from app.agent.tools.final_answer_parse import extract_inline_answer, parse_final_answer_args
from app.agent.tools.registry import ToolRegistry
from app.agent.tools.text_sanitize import strip_think_blocks
from app.models.provider import ChatResponse, LLMProvider, LLMToolCall, TokenUsage

logger = logging.getLogger(__name__)

# 默认系统提示词（占位，Task 8 会替换为 Progressive RAG prompt）
_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant with access to knowledge base tools. "
    "Use the available tools to search for information and provide accurate answers. "
    "Always call final_answer to submit your response."
)

# 空响应时的 nudge 消息
_NUDGE_MESSAGE = (
    "Please continue. Use the available tools to search for information, "
    "or call final_answer to provide your response."
)

# 最大空响应重试次数
_MAX_EMPTY_RETRIES = 2

# 最大瞬态错误重试次数
_MAX_TRANSIENT_RETRIES = 2

# 瞬态 HTTP 状态码
_TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}

# 连续相同内容检测阈值（stuck loop）
# 当 _previous_responses 中有 _STUCK_LOOP_THRESHOLD-1 个相同记录，
# 且当前响应也相同时触发（即连续 _STUCK_LOOP_THRESHOLD 轮相同 content 且无 tool call）
_STUCK_LOOP_THRESHOLD = 3
_MAX_REPEATED_RESPONSES = _STUCK_LOOP_THRESHOLD - 1

# KB 工具名称集合，用于识别知识库检索工具
_KB_TOOL_NAMES = {"knowledge_search", "grep_chunks", "search_knowledge_base"}

# 历史 KB 结果脱敏占位符
_KB_REDACTION_MARKER = "[Previous retrieval omitted — please perform a fresh search.]"

# final_answer 参数三级容错解析均失败时的兜底文案。
# final_answer 是终止信号：即使无法解析也必须结束循环，否则模型会在下一轮看到
# 工具结果后重复调用 final_answer，向用户输出重复答案。
_FINAL_ANSWER_PARSE_FALLBACK = (
    "抱歉，模型输出的最终答案格式异常，无法解析。请重试或换种方式提问。"
)


def _redact_history_kb_results(messages: list[dict]) -> list[dict]:
    """将历史轮次的 KB 检索工具结果替换为脱敏占位符，强制每轮重新检索

    遍历消息列表，找到 role="tool" 的消息，通过前面 assistant 消息中的
    tool_calls 确定工具名称。如果是 KB 工具且不是最后一组工具调用的结果，
    则替换内容为脱敏标记。

    Args:
        messages: 消息列表（OpenAI 格式）

    Returns:
        处理后的消息列表（浅拷贝，仅修改需要脱敏的消息）
    """
    if not messages:
        return messages

    # 第一步：构建 tool_call_id → tool_name 映射，并追踪每组 tool_calls 的迭代编号
    tool_call_names: dict[str, str] = {}
    tool_call_iterations: dict[str, int] = {}
    iteration = 0

    for msg in messages:
        try:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id", "")
                    func_info = tc.get("function", {})
                    tc_name = func_info.get("name", "")
                    if tc_id:
                        tool_call_names[tc_id] = tc_name
                        tool_call_iterations[tc_id] = iteration
                iteration += 1
        except (TypeError, AttributeError):
            # 消息格式异常时跳过，保持原样
            continue

    # 如果没有找到任何 tool_calls，直接返回原列表
    if not tool_call_iterations:
        return messages

    # 确定最后一组迭代编号（当前轮次，不脱敏）
    last_iteration = max(tool_call_iterations.values())

    # 第二步：遍历消息，对历史 KB 工具结果做脱敏
    result: list[dict] = []
    for msg in messages:
        try:
            if msg.get("role") == "tool":
                tool_call_id = msg.get("tool_call_id", "")
                tool_name = tool_call_names.get(tool_call_id, "")
                msg_iteration = tool_call_iterations.get(tool_call_id, -1)

                # 仅脱敏：KB 工具 + 非最后一组（历史轮次）
                if tool_name in _KB_TOOL_NAMES and msg_iteration != last_iteration:
                    redacted_msg = dict(msg)
                    redacted_msg["content"] = _KB_REDACTION_MARKER
                    result.append(redacted_msg)
                    continue
        except (TypeError, AttributeError):
            # 消息格式异常时跳过脱敏，保持原样
            pass

        result.append(msg)

    return result


@dataclass
class ResponseVerdict:
    """LLM 响应分析结果"""

    should_stop: bool
    reason: str = ""  # "natural_stop" | "final_answer" | "stuck_loop" | "max_iterations"
    content: str = ""
    # 答案是否已在流式阶段逐 token 作为正文（FINAL_ANSWER）发射给前端。
    # 仅 final_answer 路径且模型支持增量工具调用时为 True；
    # natural_stop / Ollama 非增量工具调用 / 兜底文案均为 False，需引擎补发正文。
    answer_streamed: bool = False


class AgentEngine:
    """ReAct Agent 引擎 - 无状态，每次调用创建新的 AgentState

    核心循环: Think → Analyze → Act → Observe → repeat
    """

    def __init__(
        self,
        config: AgentConfig,
        llm: LLMProvider,
        tool_registry: ToolRegistry,
        event_bus: EventBus,
        tool_context: ToolContext | None = None,
    ) -> None:
        self._config = config
        self._llm = llm
        self._tool_registry = tool_registry
        self._event_bus = event_bus
        # 本次请求的调用方上下文（会话 / 租户 / 主体）。引擎只负责在执行工具时把它带下去，
        # 不解释其内容；仅声明 accepts_context 的工具（外部 MCP 工具）会收到。
        # 每次请求新建引擎实例，故这里持有请求态是安全的。
        self._tool_context = tool_context
        # 用于 stuck loop 检测
        self._previous_responses: list[str] = []
        # 三层递进式上下文管理组件
        # ① BPE Token 估算器 ② API Usage 追踪器 ③ LLM 摘要合并器
        self._token_estimator = TokenEstimator()
        self._usage_tracker = UsageTracker(self._token_estimator)
        self._consolidator = MemoryConsolidator(
            self._llm,
            self._token_estimator,
            self._config.max_context_tokens,
            self._config.consolidation_threshold,
        )

    async def execute(
        self,
        session_id: str,
        query: str,
        llm_context: list[dict] | None = None,
        image_urls: list[str] | None = None,
    ) -> AgentState:
        """执行 Agent ReAct 循环

        Args:
            session_id: 会话 ID
            query: 用户查询
            llm_context: 历史对话消息列表（OpenAI 格式）
            image_urls: 附带的图片 URL 列表（暂未使用）

        Returns:
            AgentState: 执行完成后的状态
        """
        logger.info(
            "[Agent] Starting execution: session=%s, query_len=%d, context_msgs=%d",
            session_id,
            len(query),
            len(llm_context) if llm_context else 0,
        )

        # 初始化状态
        state = AgentState()

        # 构建系统提示词
        system_prompt = (
            self._config.system_prompt
            if self._config.system_prompt
            else _DEFAULT_SYSTEM_PROMPT
        )

        # 如果配置了 knowledge_base_ids 且没有自定义 system_prompt，
        # 查询 KB 名称并重新渲染 system prompt
        if self._config.knowledge_base_ids and not self._config.system_prompt:
            try:
                kb_names = await self._query_kb_names(self._config.knowledge_base_ids)
                from app.agent.prompts.progressive_rag import render_system_prompt
                system_prompt = render_system_prompt(
                    self._config,
                    kb_names=kb_names,
                    available_tools=self._tool_registry.list_tools(),
                )
            except Exception as e:
                logger.warning("[Agent] Failed to query KB names: %s", e)

        # 追加「按用户提问语种书写」的强制语言+final_answer 指令到 system prompt 末尾。
        # 结尾位置权重高，且用目标语言本身书写的指令对弱指令模型（尤其 DeepSeek 系）
        # 远比埋在模板中段的英文 "same language" 有效，缓解「输出夹带英文 / 不调用
        # final_answer」的问题。
        try:
            from app.agent.prompts.progressive_rag import build_language_directive
            directive = build_language_directive(query)
            if directive:
                system_prompt = f"{system_prompt}\n\n{directive}"
        except Exception as e:
            logger.warning("[Agent] Failed to build language directive: %s", e)

        # 构建初始消息列表
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        # 追加历史上下文（redact 历史 KB 工具结果，强制每轮重新检索）
        if llm_context:
            redacted_context = _redact_history_kb_results(llm_context)
            messages.extend(redacted_context)

        # 追加当前用户查询
        #
        # 不做前置 Query Rewrite：从不改写 query。指代消解交给带工具的 ReAct
        # 模型在生成检索参数时自行完成——它能看到完整对话历史，比前置改写更准确，且不会
        # 把闲聊/确认类输入（"好的"、"谢谢"）脑补成上一轮的问题从而误触发检索。
        # （单轮检索链路的前置改写在 api/query_understanding.py，仅用于检索、不替换
        # 答案生成所用的 query，与此处的 Agent 链路不是同一回事。）
        #
        # 末端注入一句极简的 final_answer 纪律提醒：上下文末端权重最高且不随多轮稀释，
        # 对抗强角色特化模型（DeepSeek 系等）多轮后"入戏"忽视工具调用纪律的问题。
        # 用换行拼进同一条 user 消息（而非新增独立消息），避免连续两条 user 消息触发
        # 部分 chat template（如 Qwen）的 role 交替异常；提醒置于 query 之后，不改写
        # query 的检索语义。
        user_content = query
        try:
            from app.agent.prompts.progressive_rag import build_turn_reminder
            reminder = build_turn_reminder(query)
            if reminder:
                user_content = f"{query}\n\n{reminder}"
        except Exception as e:
            logger.warning("[Agent] Failed to build turn reminder: %s", e)
        messages.append({"role": "user", "content": user_content})

        # 获取工具定义
        tools = self._tool_registry.get_function_definitions()

        logger.info(
            "[Agent] Ready: %d messages, %d tools",
            len(messages),
            len(tools),
        )

        # 重置 stuck loop 检测状态
        self._previous_responses = []
        self._last_query = query

        # 执行主循环
        await self._execute_loop(state, messages, tools, session_id)

        logger.info(
            "[Agent] Completed: %d steps, complete=%s",
            len(state.steps),
            state.is_complete,
        )

        return state

    async def _execute_loop(
        self,
        state: AgentState,
        messages: list[dict],
        tools: list[dict],
        session_id: str,
    ) -> None:
        """ReAct 主循环

        while iteration < max_iterations:
            1. Think: 调用 LLM
            2. Analyze: 判断是否终止
            3. Act: 执行工具调用
            4. Observe: 追加结果到消息
        """
        empty_retries = 0

        while state.current_round < self._config.max_iterations:
            iteration = state.current_round
            logger.info(
                "[Agent][Round-%d/%d] Starting",
                iteration + 1,
                self._config.max_iterations,
            )

            # 1. Think: 调用 LLM（含重试）
            # 三层递进式上下文管理：
            # ① UsageTracker 估算当前 token 数（API Usage + BPE Delta）
            # ② MemoryConsolidator 在超过 consolidation 阈值（默认 50%）时用 LLM 摘要早期历史
            # ③ compress_context 在超过 80% 阈值时分组截断兜底
            current_tokens = self._usage_tracker.estimate_current_tokens(messages)
            if self._consolidator.should_consolidate(current_tokens):
                messages = await self._consolidator.consolidate(messages)
                current_tokens = self._usage_tracker.estimate_current_tokens(messages)
            messages = compress_context(
                messages,
                self._token_estimator,
                self._config.max_context_tokens,
                current_tokens,
            )

            # 本轮实际发送给 LLM 的消息数量，用于 UsageTracker 的 delta 估算
            sent_count = len(messages)

            try:
                response = await self._call_llm_with_retry(
                    messages, tools, session_id, iteration
                )
            except RuntimeError as e:
                # 永久错误：尝试从已有结果合成答案
                logger.error("[Agent][Round-%d] LLM permanent error: %s", iteration + 1, e)
                if len(state.steps) > 0:
                    await self._synthesize_final_answer(state, session_id)
                else:
                    state.final_answer = f"抱歉，处理您的请求时遇到错误: {e}"
                    state.is_complete = True
                    await self._event_bus.emit(AgentEvent(
                        type=EventType.ERROR,
                        session_id=session_id,
                        data={"error": str(e), "stage": "llm_call"},
                    ))
                return

            # LLM 调用后：从 response.usage 更新 UsageTracker（Task 7.3）
            # 无 usage 或 total_tokens <= 0 时 UsageTracker.update 内部会跳过，
            # 下次估算继续使用全量 BPE（Req 5.2 / 7.3）
            usage: TokenUsage | None = response.usage
            if usage is not None:
                self._usage_tracker.update(usage, sent_count)

            # 每轮 LLM 调用后发射 TOKEN_USAGE 事件，供前端上下文进度条消费（Task 7.4，Req 5.3 / 7.1）
            # 无 API usage 时 prompt/completion/total 为 0，current_context_tokens 为 BPE 估算值（Req 7.3）
            current_context_tokens = self._usage_tracker.estimate_current_tokens(messages)
            await self._event_bus.emit(AgentEvent(
                type=EventType.TOKEN_USAGE,
                session_id=session_id,
                data={
                    "prompt_tokens": usage.prompt_tokens if usage else 0,
                    "completion_tokens": usage.completion_tokens if usage else 0,
                    "total_tokens": usage.total_tokens if usage else 0,
                    "max_context_tokens": self._config.max_context_tokens,
                    "current_context_tokens": current_context_tokens,
                },
            ))

            # 处理空响应（content 为空且无 tool_calls）
            if not response.content and not response.tool_calls:
                empty_retries += 1
                if empty_retries <= _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "[Agent][Round-%d] Empty response, nudging (%d/%d)",
                        iteration + 1,
                        empty_retries,
                        _MAX_EMPTY_RETRIES,
                    )
                    messages.append({"role": "user", "content": _NUDGE_MESSAGE})
                    continue
                else:
                    # 重试耗尽，合成最终答案
                    logger.warning(
                        "[Agent][Round-%d] Empty response after %d retries",
                        iteration + 1,
                        _MAX_EMPTY_RETRIES,
                    )
                    if len(state.steps) > 0:
                        await self._synthesize_final_answer(state, session_id)
                    else:
                        state.final_answer = "抱歉，无法生成回答，请重试。"
                        state.is_complete = True
                    return
            else:
                empty_retries = 0

            # 创建当前步骤
            step = AgentStep(iteration=iteration)

            # 如果有 content（思考内容）且有 tool_calls，记录到 step
            # 注意：THOUGHT 事件已在 _stream_llm_to_event_bus 中实时发射，此处不再重复发射
            if response.content and response.tool_calls:
                step.thought = response.content

            # 2. Analyze: 判断是否终止
            verdict = self._analyze_response(response, iteration, session_id)

            if verdict.should_stop:
                # max_iterations：交给合成流程（其内部自行流式发射 FINAL_ANSWER）
                if verdict.reason == "max_iterations":
                    state.steps.append(step)
                    state.current_round += 1
                    await self._synthesize_final_answer(state, session_id)
                    state.is_complete = True
                    logger.info(
                        "[Agent] Stopped: reason=max_iterations, iteration=%d", iteration
                    )
                    return

                # final_answer / natural_stop / stuck_loop：答案已确定
                answer = verdict.content
                state.final_answer = answer
                state.is_complete = True
                # 注意：不把 response.content 落为 step.thought。
                # natural_stop 时 content 是答案本身（已作 thought 流式发射、由前端转为
                # answer 段）；内联 final_answer 时 content 是原始 JSON（不应作为思考持久化）。
                # 仅保留之前轮次已记录的 step.thought（如有）。
                state.steps.append(step)

                # 是否需要补发答案正文（FINAL_ANSWER content）：
                # 唯一依据是"答案是否已作为正文流式发射"，与具体模型无关。
                #   • final_answer 标准工具 + 已流式（vLLM 增量解析）→ 仅发 done
                #   • final_answer 内联文本（千问把 JSON 写成文本）→ 路由器已流式发过
                #     answer，answer_streamed=True → 仅发 done
                #   • final_answer 标准工具 + 未流式（Ollama 工具调用非增量返回 / 兜底文案）
                #     → 补发完整正文，否则前端正文面板为空
                #   • natural_stop / stuck_loop → content 已作为 thought 流式发射，
                #     前端在收到 done 时把最后一段 thought 转为 answer，
                #     此处不补发，避免思考面板与正文面板重复显示同一段内容
                need_emit_answer = (
                    verdict.reason == "final_answer"
                    and not verdict.answer_streamed
                    and bool(answer)
                )
                if need_emit_answer:
                    await self._event_bus.emit(AgentEvent(
                        type=EventType.FINAL_ANSWER,
                        session_id=session_id,
                        data={"content": answer},
                        done=False,
                    ))

                # 统一发射 done 标记
                await self._event_bus.emit(AgentEvent(
                    type=EventType.FINAL_ANSWER,
                    session_id=session_id,
                    data={"content": ""},
                    done=True,
                ))

                logger.info(
                    "[Agent] Stopped: reason=%s, iteration=%d, answer_streamed=%s, emitted_answer=%s",
                    verdict.reason,
                    iteration,
                    verdict.answer_streamed,
                    need_emit_answer,
                )
                return

            # 3. Act: 执行工具调用
            if response.tool_calls:
                # 追加 assistant 消息（含 tool_calls）到 messages
                assistant_msg: dict = {"role": "assistant"}
                if response.content:
                    assistant_msg["content"] = response.content
                else:
                    assistant_msg["content"] = None
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function_name,
                            "arguments": tc.arguments,
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages.append(assistant_msg)

                # 执行工具调用
                tool_results = await self._execute_tool_calls(
                    response.tool_calls, step, state, session_id,
                    thought_streamed=response.thought_streamed,
                )

                # 4. Observe: 追加工具结果到消息
                for tool_call_id, result in tool_results:
                    content = result.output if result.success else result.error
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": content or "",
                    })

            state.steps.append(step)
            state.current_round += 1

        # 循环结束：max_iterations 耗尽
        logger.warning(
            "[Agent] Max iterations (%d) reached, synthesizing final answer",
            self._config.max_iterations,
        )
        await self._synthesize_final_answer(state, session_id)

    async def _call_llm_with_retry(
        self,
        messages: list[dict],
        tools: list[dict],
        session_id: str,
        iteration: int = 0,
    ) -> ChatResponse:
        """流式调用 LLM，实时发射 THOUGHT 事件，含瞬态错误重试

        - 流式接收 LLM 响应，逐 token 发射 THOUGHT 事件
        - 累积 tool_calls，流结束后构建完整 ChatResponse 返回
        - 如果最终无 tool_calls 且 finish_reason="stop"，说明内容是最终答案
          （由 _execute_loop 中的 _analyze_response 处理）

        重试策略：
        - 瞬态错误（429/500/502/503/504/timeout）：最多重试 2 次，指数退避 1s/2s
        - 永久错误：直接抛出

        Returns:
            ChatResponse

        Raises:
            RuntimeError: 永久错误或重试耗尽
        """
        last_error: Exception | None = None

        for attempt in range(_MAX_TRANSIENT_RETRIES + 1):
            try:
                response = await self._stream_llm_to_event_bus(
                    messages, tools, session_id, iteration
                )
                return response
            except RuntimeError as e:
                error_msg = str(e)
                # 判断是否为瞬态错误
                is_transient = False
                for code in _TRANSIENT_HTTP_CODES:
                    if str(code) in error_msg:
                        is_transient = True
                        break
                if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
                    is_transient = True

                if not is_transient:
                    raise

                last_error = e
                if attempt < _MAX_TRANSIENT_RETRIES:
                    backoff = 2**attempt  # 1s, 2s
                    logger.warning(
                        "[Agent] Transient error (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1,
                        _MAX_TRANSIENT_RETRIES + 1,
                        backoff,
                        error_msg,
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "[Agent] Transient error retries exhausted: %s", error_msg
                    )

        raise RuntimeError(
            f"LLM call failed after {_MAX_TRANSIENT_RETRIES + 1} attempts: {last_error}"
        )

    async def _stream_llm_to_event_bus(
        self,
        messages: list[dict],
        tools: list[dict],
        session_id: str,
        iteration: int,
    ) -> ChatResponse:
        """流式调用 LLM 并实时发射事件到 EventBus

        1. 迭代 StreamChunk，content 实时发射 THOUGHT 事件
        2. 累积 tool_calls（来自最终 chunk）
        3. 流结束后构建 ChatResponse 返回

        Returns:
            ChatResponse: 包含完整 content、tool_calls、finish_reason
        """
        full_content = ""
        tool_calls_accumulated: list[LLMToolCall] = []
        finish_reason = ""
        answer_streamed = False  # final_answer 的正文是否已逐 token 作为 answer 发射
        thought_streamed = False  # thinking 工具的 thought 是否已逐 token 作为 THOUGHT 发射
        # content 路由器：区分普通 content 是「思考」还是「被写成文本 JSON 的 final_answer」
        # （千问等弱 function-calling 模型会把 final_answer 调用写成纯文本输出）
        router = ContentStreamRouter()
        inline_answer = ""  # 路由器识别出的内联 final_answer 答案文本

        async for chunk in self._llm.stream_with_tools(
            messages, tools,
            temperature=self._config.temperature,
            enable_thinking=self._config.thinking_enabled,
        ):
            # 普通 content → 经路由器判别后发射为思考或内联答案
            if chunk.content and chunk.response_type == "content":
                full_content += chunk.content
                kind, text = router.feed(chunk.content)
                if text and kind == "thought":
                    await self._event_bus.emit(AgentEvent(
                        type=EventType.THOUGHT,
                        session_id=session_id,
                        data={"content": text, "iteration": iteration},
                        done=False,
                    ))
                elif text and kind == "answer":
                    answer_streamed = True
                    inline_answer += text
                    await self._event_bus.emit(AgentEvent(
                        type=EventType.FINAL_ANSWER,
                        session_id=session_id,
                        data={"content": text},
                        done=False,
                    ))

            # reasoning_content（模型原生思考字段）→ 一律思考面板
            elif chunk.content and chunk.response_type == "thinking":
                full_content += chunk.content
                await self._event_bus.emit(AgentEvent(
                    type=EventType.THOUGHT,
                    session_id=session_id,
                    data={"content": chunk.content, "iteration": iteration},
                    done=False,
                ))

            # 标准 final_answer 工具的 answer 字段（vLLM 增量解析）→ 答案正文
            elif chunk.content and chunk.response_type == "answer":
                answer_streamed = True
                await self._event_bus.emit(AgentEvent(
                    type=EventType.FINAL_ANSWER,
                    session_id=session_id,
                    data={"content": chunk.content},
                    done=False,
                ))

            # thinking 工具的 thought 字段（vLLM 增量解析）→ 思考面板，逐 token 流式。
            # 注意：不累加进 full_content（thought 不是模型的"回答内容"，且 ThinkingTool
            # 执行时已把完整 thought 落入 step.thought）；此处仅负责实时发射 THOUGHT 事件。
            # ThinkingTool.execute 不再自行 emit THOUGHT，避免与此处重复。
            elif chunk.content and chunk.response_type == "thinking_tool":
                thought_streamed = True
                await self._event_bus.emit(AgentEvent(
                    type=EventType.THOUGHT,
                    session_id=session_id,
                    data={"content": chunk.content, "iteration": iteration},
                    done=False,
                ))

            # 累积 tool_calls（VllmLLM 在最终 chunk 中携带完整列表）
            if chunk.tool_calls:
                tool_calls_accumulated = chunk.tool_calls

            # 捕获 finish_reason
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason

        # 冲刷路由器缓冲（流末仍未判定的残留按思考处理）
        kind, text = router.flush()
        if text and kind == "thought":
            await self._event_bus.emit(AgentEvent(
                type=EventType.THOUGHT,
                session_id=session_id,
                data={"content": text, "iteration": iteration},
                done=False,
            ))

        logger.info(
            "[Agent][Stream] Iteration-%d completed: content=%d chars, "
            "tool_calls=%d, finish_reason=%s, answer_streamed=%s, inline_answer=%d chars",
            iteration + 1,
            len(full_content),
            len(tool_calls_accumulated),
            finish_reason,
            answer_streamed,
            len(inline_answer),
        )

        return ChatResponse(
            content=full_content,
            tool_calls=tool_calls_accumulated,
            finish_reason=finish_reason or "stop",
            answer_streamed=answer_streamed,
            inline_answer=inline_answer,
            thought_streamed=thought_streamed,
        )

    def _analyze_response(
        self,
        response: ChatResponse,
        iteration: int,
        session_id: str,
    ) -> ResponseVerdict:
        """分析 LLM 响应，判断是否应该终止循环

        停止条件（按优先级）：
        1. final_answer (标准工具调用): tool_calls 中包含 final_answer
        2. final_answer (内联文本): 模型把 final_answer 写成纯文本 JSON（千问等）
        3. natural_stop: finish_reason=="stop" 且无 tool_calls
        4. stuck_loop: 连续相同 content 且无 tool_calls
        5. max_iterations
        """
        # 1. 标准 final_answer tool call
        if response.tool_calls:
            for tc in response.tool_calls:
                if tc.function_name == "final_answer":
                    # 三级容错解析（strict → repair → regex），保证保存与展示的
                    # 都是答案正文本身，而非畸形的原始 JSON
                    answer, ok = parse_final_answer_args(tc.arguments)
                    if not ok:
                        logger.warning(
                            "[Agent] Failed to parse final_answer args, using fallback: %s",
                            tc.arguments[:200],
                        )
                        answer = _FINAL_ANSWER_PARSE_FALLBACK
                    # 剥离模型可能嵌入答案的 <think>…</think> 块
                    answer = strip_think_blocks(answer)
                    return ResponseVerdict(
                        should_stop=True,
                        reason="final_answer",
                        content=answer,
                        answer_streamed=response.answer_streamed,
                    )
            # 有 tool_calls 但不是 final_answer → 继续执行
            self._previous_responses = []
            return ResponseVerdict(should_stop=False)

        # 2. 内联 final_answer：模型把 final_answer 调用写成纯文本 JSON 输出
        #    （流式路由器已将 answer 字段值作为 answer 类型流式发射，answer_streamed=True）
        if response.inline_answer:
            answer = strip_think_blocks(response.inline_answer)
            return ResponseVerdict(
                should_stop=True,
                reason="final_answer",
                content=answer,
                answer_streamed=True,
            )

        # 无 tool_calls 的情况
        content = strip_think_blocks(response.content or "")

        # 落库前的最终兜底：流式路由器若漏判内联 final_answer（如出现未覆盖的新前缀
        # 变体），content 可能仍是整段原始 JSON（含 `final_answer` 前缀）。此处统一
        # 再做一次内联答案提取，保证 natural_stop 落库与展示的是答案正文而非原始 JSON。
        inline = extract_inline_answer(content)
        if inline is not None:
            content = inline

        # stuck loop 检测：连续相同 content 且无 tool call
        # REQ-7: 连续 3 轮相同 content 且无 tool call 时自动终止并返回最后内容
        if content and len(self._previous_responses) >= _MAX_REPEATED_RESPONSES:
            if all(prev == content for prev in self._previous_responses[-_MAX_REPEATED_RESPONSES:]):
                logger.warning(
                    "[Agent] Stuck loop detected: LLM repeated same content %d times without tool calls",
                    _STUCK_LOOP_THRESHOLD,
                )
                return ResponseVerdict(
                    should_stop=True,
                    reason="stuck_loop",
                    content=content,
                )

        # 记录当前响应用于 stuck loop 检测
        self._previous_responses.append(content)

        # natural_stop: finish_reason=="stop" 且无 tool_calls
        # content 已在流式阶段作为 THOUGHT 发射（answer_streamed=False），
        # 终止处理时会补发为答案正文。
        if response.finish_reason == "stop" and not response.tool_calls:
            return ResponseVerdict(
                should_stop=True,
                reason="natural_stop",
                content=content,
                answer_streamed=False,
            )

        # max_iterations 检测
        if iteration >= self._config.max_iterations - 1:
            return ResponseVerdict(
                should_stop=True,
                reason="max_iterations",
                content=content,
            )

        return ResponseVerdict(should_stop=False)

    async def _execute_tool_calls(
        self,
        tool_calls: list[LLMToolCall],
        step: AgentStep,
        state: AgentState,
        session_id: str,
        thought_streamed: bool = False,
    ) -> list[tuple[str, ToolResult]]:
        """执行工具调用列表

        当 config.parallel_tool_calls=True 且 tool_calls 数量>1 时，
        使用 asyncio.gather 并行执行所有工具。否则顺序执行。

        结果按原始 tool_calls 顺序返回，tool_call_id 与对应 tool_call 匹配。

        Args:
            thought_streamed: thinking 工具的 thought 是否已在流式阶段逐 token 发射。
                False（非增量 provider，如 Ollama）时，在此对 thinking 工具补发完整
                THOUGHT 事件，保证思考面板有内容；True 时不补发（流式层已逐 token 发过）。

        Returns:
            list of (tool_call_id, ToolResult) tuples
        """
        # 过滤掉 final_answer（已在 _analyze_response 中处理）
        active_calls = [tc for tc in tool_calls if tc.function_name != "final_answer"]

        if not active_calls:
            return []

        # 解析所有参数
        parsed_calls: list[tuple[LLMToolCall, dict]] = []
        for tc in active_calls:
            try:
                args = json.loads(tc.arguments) if tc.arguments else {}
            except json.JSONDecodeError:
                args = {}
                logger.warning(
                    "[Agent] Failed to parse tool arguments for %s: %s",
                    tc.function_name,
                    tc.arguments,
                )
            parsed_calls.append((tc, args))

        # thinking 工具的 thought 内容：非增量 provider 未流式发过时在此补发完整 THOUGHT，
        # 保证思考面板有内容。流式 provider（thought_streamed=True）已逐 token 发过，跳过。
        if not thought_streamed:
            for tc, args in parsed_calls:
                if tc.function_name == "thinking":
                    thought = args.get("thought", "")
                    if thought:
                        await self._event_bus.emit(AgentEvent(
                            type=EventType.THOUGHT,
                            session_id=session_id,
                            data={"content": thought, "iteration": step.iteration},
                            done=False,
                        ))

        # 发射所有 TOOL_CALL 事件
        for tc, args in parsed_calls:
            await self._event_bus.emit(AgentEvent(
                type=EventType.TOOL_CALL,
                session_id=session_id,
                data={
                    "tool_name": tc.function_name,
                    "tool_call_id": tc.id,
                    "arguments": args,
                    "iteration": step.iteration,
                },
            ))

        # 判断是否并行执行
        use_parallel = (
            self._config.parallel_tool_calls
            and len(parsed_calls) > 1
        )

        if use_parallel:
            # 并行执行所有工具
            results = await self._execute_tools_parallel(parsed_calls, step, session_id)
        else:
            # 顺序执行
            results = await self._execute_tools_sequential(parsed_calls, step, session_id)

        return results

    async def _execute_single_tool(
        self, tc: LLMToolCall, args: dict
    ) -> tuple[ToolResult, int]:
        """执行单个工具，返回 (result, duration_ms)"""
        start_time = time.time()
        try:
            result = await self._tool_registry.execute(
                tc.function_name, args, ctx=self._tool_context
            )
        except Exception as e:
            logger.error(
                "[Agent] Tool execution error: %s - %s",
                tc.function_name,
                str(e),
            )
            result = ToolResult(success=False, error=str(e))
        duration_ms = int((time.time() - start_time) * 1000)
        return result, duration_ms

    async def _execute_tools_parallel(
        self,
        parsed_calls: list[tuple[LLMToolCall, dict]],
        step: AgentStep,
        session_id: str,
    ) -> list[tuple[str, ToolResult]]:
        """并行执行工具调用，结果按原始顺序返回"""
        logger.info(
            "[Agent] Executing %d tools in parallel", len(parsed_calls)
        )

        # 并行执行
        tasks = [
            self._execute_single_tool(tc, args)
            for tc, args in parsed_calls
        ]
        outcomes = await asyncio.gather(*tasks)

        # 按原始顺序组装结果
        results: list[tuple[str, ToolResult]] = []
        for i, (tc, args) in enumerate(parsed_calls):
            result, duration_ms = outcomes[i]

            # 记录到 step
            record = ToolCallRecord(
                id=tc.id,
                name=tc.function_name,
                args=args,
                result=result,
                duration_ms=duration_ms,
            )
            step.tool_calls.append(record)

            # 发射 TOOL_RESULT 事件
            await self._event_bus.emit(AgentEvent(
                type=EventType.TOOL_RESULT,
                session_id=session_id,
                data={
                    "tool_call_id": tc.id,
                    "tool_name": tc.function_name,
                    "success": result.success,
                    "duration_ms": duration_ms,
                    "iteration": step.iteration,
                    # 本次工具读到的文件（检索类工具解析 doc_id→文件名/来源），供前端在该
                    # 步骤行内联展示可点击预览的文件（粒度到文件）。
                    "files": (result.data or {}).get("files", []),
                },
            ))

            results.append((tc.id, result))

            logger.info(
                "[Agent] Tool %s completed in %dms: success=%s",
                tc.function_name,
                duration_ms,
                result.success,
            )

        return results

    async def _execute_tools_sequential(
        self,
        parsed_calls: list[tuple[LLMToolCall, dict]],
        step: AgentStep,
        session_id: str,
    ) -> list[tuple[str, ToolResult]]:
        """顺序执行工具调用"""
        results: list[tuple[str, ToolResult]] = []

        for tc, args in parsed_calls:
            result, duration_ms = await self._execute_single_tool(tc, args)

            # 记录到 step
            record = ToolCallRecord(
                id=tc.id,
                name=tc.function_name,
                args=args,
                result=result,
                duration_ms=duration_ms,
            )
            step.tool_calls.append(record)

            # 发射 TOOL_RESULT 事件
            await self._event_bus.emit(AgentEvent(
                type=EventType.TOOL_RESULT,
                session_id=session_id,
                data={
                    "tool_call_id": tc.id,
                    "tool_name": tc.function_name,
                    "success": result.success,
                    "duration_ms": duration_ms,
                    "iteration": step.iteration,
                    # 本次工具读到的文件（检索类工具解析 doc_id→文件名/来源），供前端在该
                    # 步骤行内联展示可点击预览的文件（粒度到文件）。
                    "files": (result.data or {}).get("files", []),
                },
            ))

            results.append((tc.id, result))

            logger.info(
                "[Agent] Tool %s completed in %dms: success=%s",
                tc.function_name,
                duration_ms,
                result.success,
            )

        return results

    async def _synthesize_final_answer(
        self,
        state: AgentState,
        session_id: str,
    ) -> None:
        """合成最终答案（graceful degradation）

        当 max_iterations 耗尽或 LLM 永久错误但已有工具结果时，
        尝试调用 LLM 流式生成答案，逐 token 发射 FINAL_ANSWER 事件。
        """
        # 收集所有成功的工具输出
        tool_outputs: list[str] = []
        for step in state.steps:
            for tc_record in step.tool_calls:
                if tc_record.result and tc_record.result.success and tc_record.result.output:
                    tool_outputs.append(tc_record.result.output)

        if not tool_outputs:
            state.final_answer = "抱歉，在有限的迭代次数内未能找到足够的信息来回答您的问题。请尝试重新提问或提供更多细节。"
            logger.warning("[Agent] No tool outputs available for synthesis")
            state.is_complete = True
            await self._event_bus.emit(AgentEvent(
                type=EventType.FINAL_ANSWER,
                session_id=session_id,
                data={"content": state.final_answer},
                done=True,
            ))
            return

        # 尝试调用 LLM 流式生成答案
        try:
            from app.agent.prompts.progressive_rag import render_system_prompt

            synthesis_messages = [
                {
                    "role": "system",
                    "content": render_system_prompt(
                        self._config,
                        available_tools=self._tool_registry.list_tools(),
                    ),
                },
                {"role": "user", "content": self._last_query},
            ]

            # 追加所有工具结果作为上下文
            for step in state.steps:
                for tc_record in step.tool_calls:
                    if tc_record.result and tc_record.result.success and tc_record.result.output:
                        synthesis_messages.append({
                            "role": "user",
                            "content": f"Tool {tc_record.name} returned: {tc_record.result.output}",
                        })

            # 追加最终答案生成 prompt
            synthesis_messages.append({
                "role": "user",
                "content": (
                    "Based on the above tool call results, generate a complete answer for the user's question.\n\n"
                    "Requirements:\n"
                    "1. Answer based on the actually retrieved content\n"
                    "2. Clearly cite information sources using [1][2] notation\n"
                    "3. Organize the answer in a structured format\n"
                    "4. If information is insufficient, honestly state so\n"
                    "5. IMPORTANT: Respond in the same language as the user's question\n\n"
                    "Now generate the final answer:"
                ),
            })

            # 流式生成，逐 token 发射 FINAL_ANSWER 事件
            full_answer = ""
            async for token in self._llm.stream(synthesis_messages):
                full_answer += token
                await self._event_bus.emit(AgentEvent(
                    type=EventType.FINAL_ANSWER,
                    session_id=session_id,
                    data={"content": token},
                    done=False,
                ))

            # 落库前清理可能的 <think> 残留（流式展示已逐 token 发出，此处仅规整存储值）
            state.final_answer = strip_think_blocks(full_answer)
            # 发射完成标记
            await self._event_bus.emit(AgentEvent(
                type=EventType.FINAL_ANSWER,
                session_id=session_id,
                data={"content": ""},
                done=True,
            ))
            logger.info("[Agent] Synthesized final answer via streaming LLM from %d tool outputs", len(tool_outputs))

        except Exception as e:
            logger.warning("[Agent] LLM synthesis failed, using fallback: %s", e)
            state.final_answer = "抱歉，检索到了相关内容但未能生成完整答案。请重试或调整问题。"
            await self._event_bus.emit(AgentEvent(
                type=EventType.FINAL_ANSWER,
                session_id=session_id,
                data={"content": state.final_answer},
                done=True,
            ))

        state.is_complete = True

    async def _query_kb_names(self, kb_ids: list[str]) -> list[str]:
        """从数据库查询知识库名称列表

        Args:
            kb_ids: 知识库 ID 列表

        Returns:
            知识库名称列表
        """
        from sqlalchemy import select
        from app.schema.db import KnowledgeBase
        from app.storage.database import async_session

        kb_names: list[str] = []
        try:
            async with async_session() as session:
                result = await session.execute(
                    select(KnowledgeBase.id, KnowledgeBase.name).where(
                        KnowledgeBase.id.in_(kb_ids)
                    )
                )
                rows = result.all()
                # 按原始 kb_ids 顺序返回
                id_to_name = {row.id: row.name for row in rows}
                kb_names = [id_to_name.get(kb_id, kb_id) for kb_id in kb_ids]
        except Exception as e:
            logger.warning("[Agent] Failed to query KB names: %s", e)
            kb_names = kb_ids  # fallback 使用 ID

        return kb_names
