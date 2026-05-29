"""AgentConfig - ReAct Agent 引擎配置"""

from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """ReAct Agent 运行配置

    控制 Agent 引擎的行为参数，包括迭代限制、工具白名单、
    LLM 参数、上下文窗口管理等。
    """

    # 最大迭代次数，达到上限时强制合成最终答案
    max_iterations: int = 20

    # 允许使用的工具列表（白名单）
    allowed_tools: list[str] = field(default_factory=lambda: [
        "knowledge_search", "grep_chunks", "list_knowledge_chunks", "final_answer"
    ])

    # LLM 生成温度
    temperature: float = 0.7

    # 关联的知识库 ID 列表
    knowledge_base_ids: list[str] = field(default_factory=list)

    # 是否启用网页搜索工具
    web_search_enabled: bool = False

    # 是否启用 thinking 工具（内部思考/反思）
    thinking_enabled: bool = True

    # 是否允许并行执行多个工具调用
    parallel_tool_calls: bool = False

    # 最大上下文 token 数，超过阈值触发压缩
    max_context_tokens: int = 200000

    # LLM 单次调用超时时间（秒）
    llm_call_timeout: int = 120

    # 工具输出最大字符数，超过时截断（保留头尾）
    max_tool_output_chars: int = 16000

    # 记忆合并触发阈值（占 max_context_tokens 的比例，有效范围 0-1）
    consolidation_threshold: float = 0.5

    # 是否保留历史检索结果（为 True 时不对历史 KB 工具结果做脱敏处理）
    retain_retrieval_history: bool = False

    # 自定义系统提示词，空字符串表示使用默认 Progressive RAG prompt
    system_prompt: str = ""
