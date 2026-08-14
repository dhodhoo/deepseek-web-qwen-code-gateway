"""OpenAI-compatible wire schemas (M2 subset).

Request models are deliberately **lenient** (`extra="allow"`): current Qwen
Code sends non-standard fields depending on config/model (`reasoning`,
`reasoning_effort`, `enable_thinking`, `chat_template_kwargs`, ...), and the
gateway must tolerate/ignore unknown request-body fields rather than reject
them (docs/UPSTREAM_NOTES.md, Qwen source verification 2026-08-14).

Response models are strict standard OpenAI shapes (docs/API_CONTRACT.md).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ChatMessage",
    "ChatCompletionRequest",
    "AssistantMessageOut",
    "Choice",
    "ChatCompletionResponse",
    "ModelInfo",
    "ModelList",
]


class ChatMessage(BaseModel):
    """One OpenAI chat message.

    ``content`` may be a plain string, an array of content parts (only
    ``{"type": "text", "text": ...}`` parts are compiled today), or null.
    ``tool_calls``/``tool_call_id`` are accepted but not supported until M6.
    """

    model_config = ConfigDict(extra="allow")

    role: str
    content: str | list[Any] | None = None
    tool_call_id: str | None = None
    tool_calls: list[Any] | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    """POST /v1/chat/completions request body.

    Sampling knobs (temperature, top_p, max_tokens, ...) and any other
    unknown fields are accepted and ignored in M2 (documented behavior, per
    API_CONTRACT.md "accept but initially may ignore").
    """

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    tools: list[Any] | None = None
    tool_choice: Any | None = None


class AssistantMessageOut(BaseModel):
    role: str = "assistant"
    content: str | None = None


class Choice(BaseModel):
    index: int = 0
    message: AssistantMessageOut
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "local"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelInfo]
