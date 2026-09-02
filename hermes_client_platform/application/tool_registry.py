"""ClientToolRegistry: dynamische Registrierung der client_*-Tools (DD-15).

Die Tools werden pro Session zur Laufzeit registriert. Der Handler ist eine
Closure, die den Tool-Namen und den LoopRecord captured: Er sendet den
Tool-Aufruf ueber den aktuellen Transport, setzt das waiting-Signal und
blockiert auf der client_queue bis zum Timeout.
"""

from __future__ import annotations

import json
import queue
import uuid
from typing import Any, Callable, Optional

from ..domain.models import ClientToolCall, ResponseEvent, ResponseEventKind
from ..domain.ports import RegistryPort, TransportPort
from .loop_runner import LoopRecord

# Ergebnis bei ausbleibender Client-Antwort (DD-05). Der Loop laeuft danach
# natuerlich weiter: Das LLM sieht das Fehler-Result und antwortet final.
_TIMEOUT_RESULT = json.dumps({"error": "client timeout / disconnected"})


def _make_handler(
    tool_name: str,
    loop_record: LoopRecord,
    transport: TransportPort,
    timeout_seconds: float,
) -> Callable[..., str]:
    """Erzeugt den blockierenden Handler fuer ein client_-Tool."""

    def handler(params: dict[str, Any], **kwargs: Any) -> str:
        call = ClientToolCall(
            id=f"call_{uuid.uuid4().hex[:24]}",
            name=tool_name,
            arguments=params or {},
        )
        transport.send_event(ResponseEvent(ResponseEventKind.TOOL_CALL, call))
        loop_record.waiting.set()
        try:
            try:
                return loop_record.client_queue.get(timeout=timeout_seconds)
            except queue.Empty:
                return _TIMEOUT_RESULT
        finally:
            loop_record.waiting.clear()

    return handler


def register_client_tools(
    loop_record: LoopRecord,
    tool_definitions: list[dict[str, Any]],
    registry_port: RegistryPort,
    settings,
) -> None:
    """Registriert die vom Client deklarierten Tools mit client_-Praefix."""
    for definition in tool_definitions:
        function = definition.get("function", definition)
        raw_name = function.get("name", "")
        if not raw_name:
            continue
        prefixed = f"client_{raw_name}"

        # Schema fuer das LLM: Name auf den registrierten Namen setzen.
        schema = dict(function)
        schema["name"] = prefixed

        handler = _make_handler(
            raw_name, loop_record, loop_record.transport, settings.tool_timeout_seconds
        )
        registry_port.register(
            name=prefixed,
            toolset=settings.toolset_name,
            schema={"type": "function", "function": schema},
            handler=handler,
            description=function.get("description", ""),
        )
        loop_record.registered_tools.append(prefixed)


def deregister_client_tools(loop_record: LoopRecord, registry_port: RegistryPort) -> None:
    """Entfernt alle Tools der Session aus der Registry."""
    for name in loop_record.registered_tools:
        registry_port.deregister(name)
    loop_record.registered_tools.clear()
