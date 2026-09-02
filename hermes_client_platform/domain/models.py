"""Datenmodelle der Domain.

Enthaelt ausschliesslich Typen und Enums ohne I/O-Bezug. Die Modelle werden
von Application und Infrastructure gemeinsam genutzt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class SessionState(Enum):
    """Zustaende einer Session (Spec 2.2)."""

    IDLE = "idle"
    TURN_ACTIVE = "turn_active"
    CLIENT_WAIT = "client_wait"


class StateEvent(Enum):
    """Ereignisse, die einen State-Uebergang ausloesen (Spec 2.2)."""

    USER_MESSAGE = "user_message"
    TOOL_RESPONSE = "tool_response"
    CLIENT_TOOL_CALL = "client_tool_call"
    TIMEOUT = "timeout"
    FINAL_ANSWER = "final_answer"
    CANCEL = "cancel"


@dataclass(frozen=True)
class ClientToolCall:
    """Ein Tool-Aufruf, der an den Client gesendet wird (Spec 2.3, Fall 4)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResponse:
    """Antwort des Clients auf einen ClientToolCall (Spec 2.3, Fall 2)."""

    tool_call_id: str
    content: str


@dataclass(frozen=True)
class Ingress:
    """Zerlegung eines eingehenden Chat-Completions-Requests.

    Die Zerlegung erfolgt in infrastructure/protocol.py. Die Domain unterscheidet
    drei Bestandteile: neue User-Messages, Tool-Responses und Tool-Definitionen.
    history enthaelt die uebrige Konversation (fuer den Erst-Turn einer Session
    bzw. Session-Uebernahme; bei laufenden Sessions wird sie ignoriert, DD-02).
    """

    user_messages: list[str] = field(default_factory=list)
    tool_responses: list[ToolResponse] = field(default_factory=list)
    tool_definitions: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    stream: bool = False


class ResponseEventKind(Enum):
    """Arten von Antwort-Events an den Client (Spec 3.8)."""

    CONTENT_DELTA = "content_delta"
    TOOL_CALL = "tool_call"
    FINAL = "final"
    ERROR = "error"


@dataclass(frozen=True)
class ResponseEvent:
    """Ein Event, das ueber den TransportPort an den Client geht.

    Der Adapter formatiert es als SSE-Chunk oder JSON (Spec DD-12).
    """

    kind: ResponseEventKind
    payload: Any


@dataclass
class AgentHandle:
    """Referenz auf einen laufenden Agent-Turn.

    agent_ref wird von run_conversation befuellt (Muster aus
    gateway/platforms/api_server.py). task ist der asyncio-Task des Loops.
    """

    agent_ref: list[Any] = field(default_factory=list)
    task: Optional[asyncio.Task] = None
