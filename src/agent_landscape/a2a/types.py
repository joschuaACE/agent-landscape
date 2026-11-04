"""A2A protocol data model (v0.3 JSON-RPC wire format).

Mirrors https://a2a-protocol.org / github.com/a2aproject/A2A. Objects are
discriminated by ``kind`` and serialised in camelCase – the format LiteLLM's
Agent Gateway serves for agents pinned to ``protocolVersion: "0.3"``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

PROTOCOL_VERSION = "0.3.0"


def new_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class A2AModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel, extra="allow")

    def dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class TaskState(str, Enum):
    submitted = "submitted"
    working = "working"
    input_required = "input-required"
    completed = "completed"
    canceled = "canceled"
    failed = "failed"
    rejected = "rejected"
    auth_required = "auth-required"
    unknown = "unknown"


TERMINAL_STATES = {TaskState.completed, TaskState.canceled, TaskState.failed, TaskState.rejected}


# --- Parts -----------------------------------------------------------------
class TextPart(A2AModel):
    kind: Literal["text"] = "text"
    text: str
    metadata: dict[str, Any] | None = None


class DataPart(A2AModel):
    kind: Literal["data"] = "data"
    data: dict[str, Any]
    metadata: dict[str, Any] | None = None


class FilePart(A2AModel):
    kind: Literal["file"] = "file"
    file: dict[str, Any]
    metadata: dict[str, Any] | None = None


Part = Annotated[Union[TextPart, DataPart, FilePart], Field(discriminator="kind")]


def parts_text(parts: list[Any] | None) -> str:
    return "\n".join(p.text for p in (parts or []) if isinstance(p, TextPart)).strip()


# --- Messages / tasks ------------------------------------------------------
class Message(A2AModel):
    kind: Literal["message"] = "message"
    role: Literal["user", "agent"]
    parts: list[Part]
    message_id: str = Field(default_factory=new_id)
    task_id: str | None = None
    context_id: str | None = None
    reference_task_ids: list[str] | None = None
    metadata: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return parts_text(self.parts)

    @classmethod
    def of(cls, text: str, role: Literal["user", "agent"] = "user", **kw: Any) -> "Message":
        return cls(role=role, parts=[TextPart(text=text)], **kw)


class TaskStatus(A2AModel):
    state: TaskState
    message: Message | None = None
    timestamp: str = Field(default_factory=now_iso)


class Artifact(A2AModel):
    artifact_id: str = Field(default_factory=new_id)
    name: str | None = None
    description: str | None = None
    parts: list[Part]
    metadata: dict[str, Any] | None = None


class Task(A2AModel):
    kind: Literal["task"] = "task"
    id: str = Field(default_factory=new_id)
    context_id: str = Field(default_factory=new_id)
    status: TaskStatus
    artifacts: list[Artifact] = Field(default_factory=list)
    history: list[Message] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None


class TaskStatusUpdateEvent(A2AModel):
    kind: Literal["status-update"] = "status-update"
    task_id: str
    context_id: str
    status: TaskStatus
    final: bool = False
    metadata: dict[str, Any] | None = None


class TaskArtifactUpdateEvent(A2AModel):
    kind: Literal["artifact-update"] = "artifact-update"
    task_id: str
    context_id: str
    artifact: Artifact
    append: bool | None = None
    last_chunk: bool | None = None
    metadata: dict[str, Any] | None = None


StreamEvent = Union[Task, Message, TaskStatusUpdateEvent, TaskArtifactUpdateEvent]


# --- Agent card ------------------------------------------------------------
class AgentSkill(A2AModel):
    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] | None = None
    input_modes: list[str] | None = None
    output_modes: list[str] | None = None


class AgentCapabilities(A2AModel):
    streaming: bool = True
    push_notifications: bool = False
    state_transition_history: bool = True


class AgentProvider(A2AModel):
    organization: str
    url: str


class AgentCard(A2AModel):
    protocol_version: str = PROTOCOL_VERSION
    name: str
    description: str
    url: str
    preferred_transport: str = "JSONRPC"
    version: str = "0.1.0"
    provider: AgentProvider | None = None
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    default_input_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text/plain"])
    skills: list[AgentSkill] = Field(default_factory=list)


# --- JSON-RPC params -------------------------------------------------------
class MessageSendConfiguration(A2AModel):
    accepted_output_modes: list[str] | None = None
    history_length: int | None = None
    blocking: bool | None = None


class MessageSendParams(A2AModel):
    message: Message
    configuration: MessageSendConfiguration | None = None
    metadata: dict[str, Any] | None = None


class TaskQueryParams(A2AModel):
    id: str
    history_length: int | None = None
    metadata: dict[str, Any] | None = None


class TaskIdParams(A2AModel):
    id: str
    metadata: dict[str, Any] | None = None


class TaskListParams(A2AModel):
    context_id: str | None = None


# --- JSON-RPC envelope helpers ----------------------------------------------
class RPCError:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    TASK_NOT_FOUND = -32001
    TASK_NOT_CANCELABLE = -32002
    UNSUPPORTED_OPERATION = -32004


def rpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def parse_event(data: dict[str, Any]) -> StreamEvent:
    kind = data.get("kind")
    if kind == "task":
        return Task.model_validate(data)
    if kind == "message":
        return Message.model_validate(data)
    if kind == "status-update":
        return TaskStatusUpdateEvent.model_validate(data)
    if kind == "artifact-update":
        return TaskArtifactUpdateEvent.model_validate(data)
    raise ValueError(f"Unknown A2A object kind: {kind!r}")


def result_text(obj: StreamEvent | None) -> str:
    """Best-effort extraction of the human readable answer of an A2A result."""
    if obj is None:
        return ""
    if isinstance(obj, Message):
        return obj.text
    if isinstance(obj, Task):
        text = "\n\n".join(parts_text(a.parts) for a in obj.artifacts).strip()
        if not text and obj.status.message:
            text = obj.status.message.text
        return text
    if isinstance(obj, TaskArtifactUpdateEvent):
        return parts_text(obj.artifact.parts)
    if isinstance(obj, TaskStatusUpdateEvent) and obj.status.message:
        return obj.status.message.text
    return ""
