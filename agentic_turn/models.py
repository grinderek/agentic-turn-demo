from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def new_id() -> str:
    return str(uuid4())


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    reason: str


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["answered", "needs_clarification", "no_data", "refused"]
    citations: list[Citation] = Field(default_factory=list, max_length=20)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class FinalReply:
    answer: Answer
    usage: dict[str, Any] | None = None


@dataclass
class ModelReply:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    # Provider-native assistant content is replayed unchanged with tool-result blocks.
    content: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] | None = None
    stop_reason: str = "end_turn"


@dataclass
class ToolResult:
    content: dict[str, Any]
    source_ids: list[str] = field(default_factory=list)
    error: bool = False
    action: dict[str, Any] | None = None
    retired_action_id: str | None = None


class Turn(BaseModel):
    id: str = Field(default_factory=new_id)
    conversation_id: str
    prompt: str
    scenario: Literal["meeting", "tool_error", "bad_citation", "budget"] = "meeting"
    status: Literal["running", "done", "cancelled", "error", "interrupted"] = "running"
    text: str = ""
    seq: int = 0
    outcome: str | None = None
    reason: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    ledger: list[str] = Field(default_factory=list)
    calls: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=4000)
    scenario: Literal["meeting", "tool_error", "bad_citation", "budget"] = "meeting"
