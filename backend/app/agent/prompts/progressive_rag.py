"""System prompt for the DSH-style ReAct retrieval loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agent.config import AgentConfig


SYSTEM_PROMPT_PLACEHOLDERS = {
    "knowledge_base_names": "当前绑定的知识库名称列表",
    "available_tools": "当前 Agent 可用的工具列表",
    "available_skills": "当前可按需加载的技能列表",
    "web_search_status": "网络搜索是否启用",
    "current_time": "当前系统时间",
    "current_date": "当前日期",
    "user_customization": "管理员配置的角色/语气/工作风格/边界",
}

_PROMPT = """\
You are Artoo, a retrieval-grounded assistant running in a ReAct loop.

## Response contract
1. While collecting evidence, call tools. Any ordinary text you emit in a tool-call
   turn is treated as short planning/reasoning, never as the final answer.
2. When evidence is sufficient, write the COMPLETE user-facing answer as ordinary
   assistant text and stop without calling a tool. That final text is the answer.
3. Use the user's language for both reasoning and the final answer.
4. Do not repeat a long draft in reasoning and then write it again as the answer.
   Keep reasoning brief: task, missing evidence, next action.

## Evidence rules
- Answer factual or document-specific questions from retrieved evidence. If a
  name, term, law, or entity looks wrong or unknown, verify with retrieval first.
- Prefer semantic search for concepts and keyword search for exact terms.
- Read matching chunks before relying on them. Stop retrieval when additional
  calls are unlikely to change the answer.
- Cite facts with [1], [2], etc. where applicable and refer to documents by name,
  not internal IDs. Do not narrate tool mechanics in the final answer.
- If evidence is absent or insufficient, say that plainly instead of guessing.

## Bound knowledge bases
{knowledge_base_names}

## Available tools
{available_tools}

## Skills loaded on demand
{available_skills}

When a skill matches the task, load it with `read_skill`, then follow its
instructions. Ignore unrelated skills.

## Web search
{web_search_status}

Current date: {current_date}
Current time: {current_time}

{user_customization}"""


def _safe_substitute(template: str, values: dict[str, str]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", value)
    return result


def render_system_prompt(
    config: "AgentConfig",
    kb_names: list[str] | None = None,
    available_tools: list[str] | None = None,
    web_search_enabled: bool | None = None,
    skills: list[tuple[str, str]] | None = None,
) -> str:
    """Render the compact ReAct system prompt."""
    from datetime import datetime

    kb_section = "\n".join(f"- {name}" for name in kb_names) if kb_names else "- None"
    tools = available_tools or config.allowed_tools
    tools_section = "\n".join(f"- `{name}`" for name in tools) or "- None"
    skills_section = (
        "\n".join(f"- **{name}**: {description}" for name, description in skills)
        if skills
        else "- None"
    )
    web_on = config.web_search_enabled if web_search_enabled is None else web_search_enabled
    now = datetime.now()
    custom = (config.custom_instructions or "").strip()
    custom_section = f"\n## User customization\n{custom}\n" if custom else ""

    return _safe_substitute(_PROMPT, {
        "knowledge_base_names": kb_section,
        "available_tools": tools_section,
        "available_skills": skills_section,
        "web_search_status": "Enabled." if web_on else "Disabled.",
        "current_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "current_date": now.strftime("%Y-%m-%d"),
        "user_customization": custom_section,
    })


def build_language_directive(query: str) -> str:
    """Return a short instruction that anchors output language at request end."""
    contracts = {
        "zh": (
            "输出语言：简体中文。请全程用简体中文进行思考和撰写面向用户的回答；"
            "即使工具结果或历史消息是英文，也不得因此切换语言或中英夹杂。"
            "工具名称、参数、代码、文件路径和引用原文可保留原样。"
        ),
        "ja": (
            "出力言語：日本語。思考とユーザー向けの回答は日本語で統一してください。"
            "ツール結果や履歴が英語でも言語を切り替えないでください。"
            "ツール名、引数、コード、ファイルパス、引用は原文のままで構いません。"
        ),
        "ko": (
            "출력 언어: 한국어. 추론과 사용자 응답은 한국어로 유지하세요. "
            "도구 결과나 이전 메시지가 영어여도 언어를 바꾸지 마세요. "
            "도구 이름, 인수, 코드, 파일 경로, 인용은 원문 그대로 둘 수 있습니다."
        ),
        "en": (
            "Output language: English. Use English for reasoning and user-facing "
            "answers even when tool results or history contain another language. "
            "Keep tool names, arguments, code, file paths, and quotations verbatim."
        ),
    }
    return contracts[_detect_language(query)]


def _detect_language(query: str) -> str:
    if any("\u3040" <= ch <= "\u30ff" for ch in query):
        return "ja"
    if any("\uac00" <= ch <= "\ud7af" for ch in query):
        return "ko"
    if any("\u4e00" <= ch <= "\u9fff" for ch in query):
        return "zh"
    return "en"
