from __future__ import annotations
from typing import TYPE_CHECKING, Any

from nutshell.provider import Provider
from nutshell.types import Message, ToolCall

if TYPE_CHECKING:
    from nutshell.tool import Tool


class OpenAIProvider(Provider):
    """LLM provider backed by OpenAI."""

    def __init__(self, api_key: str | None = None, max_tokens: int = 4096) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError("Install openai: pip install openai") from None
        self._client = AsyncOpenAI(api_key=api_key)
        self.max_tokens = max_tokens

    async def complete(
        self,
        messages: list[Message],
        tools: list[Tool],
        system_prompt: str,
        model: str,
    ) -> tuple[str, list[ToolCall]]:
        api_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *_to_api_messages(messages),
        ]
        api_tools = [_to_openai_tool(t) for t in tools] if tools else []

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": api_messages,
        }
        if api_tools:
            kwargs["tools"] = api_tools

        response = await self._client.chat.completions.create(**kwargs)
        message = response.choices[0].message

        content_text = message.content or ""
        tool_calls: list[ToolCall] = []

        if message.tool_calls:
            import json
            for tc in message.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    input=json.loads(tc.function.arguments),
                ))

        return content_text, tool_calls


def _to_api_messages(messages: list[Message]) -> list[dict]:
    result = []
    for msg in messages:
        if msg.role == "tool":
            result.append({"role": "tool", "content": msg.content})
        else:
            result.append({"role": msg.role, "content": msg.content})
    return result


def _to_openai_tool(tool: Any) -> dict:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.schema,
        },
    }
