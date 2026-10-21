"""OpenAI Chat Completions request/response shapes and the messages → prompt conversion."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str
    text: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    content: str | list[ContentPart] | None = None
    name: str | None = None


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: dict[str, Any] | None = None


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow")
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    # Unknown OpenAI params (temperature, top_p, max_tokens, ...) are accepted and ignored:
    # `claude -p` exposes no equivalent.
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[ChatMessage] = Field(min_length=1)
    effort: Effort | None = None
    reasoning_effort: Effort | None = None
    response_format: ResponseFormat | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None
    user: str | None = None
    # Extension: continue a server-side session ("new" starts one). Only the last user message is sent.
    session_id: str | None = None


class ConversionError(ValueError):
    pass


def text_of(content: str | list[ContentPart] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if part.type != "text":
            raise ConversionError(f"Content part type '{part.type}' is not supported; only text is.")
        parts.append(part.text or "")
    return "\n".join(parts)


_JSON_OBJECT_HINT = "Respond with a single valid JSON object and nothing else."


def to_prompt(messages: list[ChatMessage], response_format: ResponseFormat | None = None) -> tuple[str | None, str]:
    """Split OpenAI messages into (system prompt or None, user prompt for stdin).

    `claude -p` takes one prompt per run, so earlier turns are rendered as a tagged transcript
    ahead of the final user message.
    """
    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []
    for msg in messages:
        text = text_of(msg.content)
        if msg.role in ("system", "developer"):
            system_parts.append(text)
        elif msg.role in ("user", "assistant"):
            turns.append((msg.role, text))
        else:
            raise ConversionError(f"Message role '{msg.role}' is not supported (tools are disabled).")

    if not turns or turns[-1][0] != "user":
        raise ConversionError("The last non-system message must have role 'user'.")

    if response_format is not None and response_format.type == "json_object":
        system_parts.append(_JSON_OBJECT_HINT)

    *history, (_, final) = turns
    system = "\n\n".join(p for p in system_parts if p.strip()) or None
    return system, render_transcript(history, final)


def render_transcript(history: list[tuple[str, str]], final: str) -> str:
    """One prompt carrying earlier (role, text) turns as a tagged transcript, then the new message."""
    if not history:
        return final
    lines = ["<conversation>"]
    for role, text in history:
        lines.append(f"<{role}>\n{text}\n</{role}>")
    lines.append("</conversation>")
    lines.append("")
    lines.append("Continue the conversation above. Reply to this latest user message:")
    lines.append(final)
    return "\n".join(lines)


def usage_json(usage: dict[str, Any]) -> dict:
    """Claude Code's usage in OpenAI's shape; cached input counts as prompt tokens."""
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_creation = usage.get("cache_creation_input_tokens") or 0
    prompt_tokens = (usage.get("input_tokens") or 0) + cache_read + cache_creation
    completion_tokens = usage.get("output_tokens") or 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "prompt_tokens_details": {"cached_tokens": cache_read},
    }


def completion_response(*, completion_id: str, created: int, model: str, text: str, usage: dict[str, Any]) -> dict:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": usage_json(usage),
    }


def completion_chunk(*, completion_id: str, created: int, model: str, delta: dict | None = None,
                     finish_reason: str | None = None, usage: dict[str, Any] | None = None) -> dict:  # fmt: skip
    """One `chat.completion.chunk` of a streamed reply. With `usage`, the final usage-only chunk (no choices)."""
    chunk = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model}
    if usage is not None:
        chunk["choices"] = []
        chunk["usage"] = usage_json(usage)
    else:
        chunk["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason, "logprobs": None}]
    return chunk
