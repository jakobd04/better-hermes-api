"""Driver: Request-Parsing und Response-Formatierung.

Konvertiert OpenAI-Chat-Completions-Requests in Ingress-Objekte und
ResponseEvent-Objekte in OpenAI-Chunks (SSE) bzw. JSON-Responses (Spec 3.8,
DD-12). Keine Hermes-Importe; nur aiohttp-freie, pure Funktionen.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..domain.models import (
    ClientToolCall,
    Ingress,
    ResponseEvent,
    ResponseEventKind,
    ToolResponse,
)


class ProtocolError(ValueError):
    """Fehler beim Parsen eines Requests; http_adapter uebersetzt in HTTP 400."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_chat_completions(body: dict[str, Any]) -> Ingress:
    """Zerlegt einen Chat-Completions-Body in ein Ingress-Objekt.

    Regeln:
    - Alle role=="tool"-Messages werden zu ToolResponse (Tool-Responses).
    - Die letzte role=="user"-Message ist die neue Eingabe.
    - Alle uebrigen Messages sind history (Erst-Turn / Session-Uebernahme).
    """
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ProtocolError("messages fehlt oder ist leer")

    tool_responses: list[ToolResponse] = []
    user_messages: list[str] = []
    history: list[dict[str, Any]] = []

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ProtocolError(f"messages[{index}] ist kein Objekt")
        role = message.get("role")

        if role == "tool":
            tool_responses.append(
                ToolResponse(
                    tool_call_id=str(message.get("tool_call_id", "")),
                    content=_content_to_text(message.get("content")),
                )
            )
        elif role == "user":
            # Nur die letzte user-Message ist die neue Eingabe; alle
            # vorherigen bleiben Teil der history.
            if index == len(messages) - 1:
                user_messages.append(_content_to_text(message.get("content")))
            else:
                history.append(message)
        else:
            history.append(message)

    tool_definitions = body.get("tools")
    if not isinstance(tool_definitions, list):
        tool_definitions = []

    return Ingress(
        user_messages=user_messages,
        tool_responses=tool_responses,
        tool_definitions=tool_definitions,
        history=history,
        stream=bool(body.get("stream", False)),
    )


def _content_to_text(content: Any) -> str:
    """Normalisiert Message-Content auf Text (Strings oder OpenAI-Parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "\n".join(parts)
    return ""


# -- SSE-Formatierung (OpenAI-Chat-Completions-Chunks) ------------------------


def sse_content_delta(delta: str) -> dict[str, Any]:
    return {"choices": [{"delta": {"content": delta}}]}


def sse_tool_call(call: ClientToolCall) -> dict[str, Any]:
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                    ]
                }
            }
        ]
    }


def sse_finish(finish_reason: str) -> dict[str, Any]:
    return {"choices": [{"delta": {}, "finish_reason": finish_reason}]}


def sse_done() -> str:
    return "[DONE]"


# -- JSON-Formatierung (non-streaming) ----------------------------------------


def json_response(
    content: str,
    tool_calls: Optional[list[dict[str, Any]]],
    finish_reason: str,
) -> dict[str, Any]:
    """Baut eine non-streaming Chat-Completions-Response."""
    message: dict[str, Any] = {"role": "assistant", "content": content or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ]
    }


def json_tool_call_payload(call: ClientToolCall) -> dict[str, Any]:
    """Tool-Call-Repraesentation fuer die JSON-Response (OpenAI-Format)."""
    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": json.dumps(call.arguments),
        },
    }
