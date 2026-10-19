"""Provider-neutral model conversation types with tool use (ADR-005)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class _Block(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TextBlock(_Block):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(_Block):
    type: Literal["tool_use"] = "tool_use"
    id: Annotated[str, Field(min_length=1, max_length=128)]
    name: Annotated[str, Field(min_length=1, max_length=128)]
    arguments: dict[str, Any]


class ToolResultBlock(_Block):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: dict[str, Any]
    is_error: bool = False


ContentBlock = Annotated[TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant"]
    content: list[ContentBlock] = Field(min_length=1)


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any]


class TokenUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ModelTurn(BaseModel):
    """One model response: text, tool-use requests, or both."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "refused"]
    content: list[TextBlock | ToolUseBlock] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)

    @property
    def text(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextBlock))

    @property
    def tool_calls(self) -> list[ToolUseBlock]:
        return [block for block in self.content if isinstance(block, ToolUseBlock)]


class ModelClient(Protocol):
    model_id: str

    async def converse(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_output_tokens: int,
    ) -> ModelTurn:
        """Send the conversation and return the model's next turn."""
