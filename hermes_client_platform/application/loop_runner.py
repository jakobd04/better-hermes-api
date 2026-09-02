"""Anwendungskern: Loop-Verwaltung.

Haelt den pro Session lebenden Agent-Turn (DD-02) und die beiden Queues:
- client_queue: Tool-Responses vom Client in den blockierenden Handler
- waiting: Signal, dass der Loop im client_-Handler blockiert (CLIENT_WAIT)

Der Transport ist pro Request wechselbar (loop_record.transport): Events des
laufenden Loops gehen an den Request, der gerade offen ist. Nach einem
client_-Tool-Call ist der alte Request geschlossen; die finale Antwort eines
Timeout-Turns wird dann verworfen (DD-10) — der Transport entscheidet das
selbst, wenn er geschlossen ist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from ..domain.models import AgentHandle, ResponseEvent, ResponseEventKind, SessionState, ToolResponse
from ..domain.ports import AgentPort, TransportPort

logger = logging.getLogger(__name__)

# Fehler-Result, das der Handler bei Abbruch durch eine neue Nachricht liefert
# (DD-07). Das LLM sieht den Abbruch als normales Tool-Ergebnis.
ABORT_MESSAGE = json.dumps({"error": "aborted — neue Nachricht eingetroffen"})


@dataclass
class LoopRecord:
    """Zustand eines lebenden Agent-Turns (Spec 2.2, DD-02)."""

    agent_handle: AgentHandle
    client_queue: "queue.Queue[str]" = field(default_factory=queue.Queue)
    waiting: threading.Event = field(default_factory=threading.Event)
    transport: Optional[TransportPort] = None
    state: SessionState = SessionState.TURN_ACTIVE
    registered_tools: list[str] = field(default_factory=list)
    finalize_task: Optional[asyncio.Task] = None
    show_tool_markers: bool = True


# Maximale Laenge der kompakten Argument-Darstellung in Tool-Markern.
_MAX_MARKER_ARGS_LEN = 200


def _compact_args(args: dict[str, Any]) -> str:
    """Kompakte JSON-Darstellung der Tool-Argumente fuer Marker (gekuerzt)."""
    if not args:
        return ""
    text = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    if len(text) > _MAX_MARKER_ARGS_LEN:
        text = text[: _MAX_MARKER_ARGS_LEN - 3] + "..."
    return text


def _emit_tool_marker(loop_record: LoopRecord, name: str, args: dict[str, Any]) -> None:
    """Sendet einen sichtbaren Text-Marker fuer einen serverseitigen Tool-Call.

    client_-Tools werden uebersprungen: Sie erscheinen als echte tool_calls
    im Stream (OpenAI-Format), nicht als Text. Marker sind reine UX
    (DD-18); die Hermes-DB bleibt die kanonische Quelle.
    """
    if not loop_record.show_tool_markers:
        return
    if name.startswith("client_"):
        return
    if loop_record.transport is None:
        return
    marker = f"\nTOOL: {name}({_compact_args(args)})\n"
    loop_record.transport.send_event(
        ResponseEvent(ResponseEventKind.CONTENT_DELTA, marker)
    )


class LoopRunner:
    """Startet und steuert Agent-Turns. Pro Session existiert hoechstens ein Turn."""

    def __init__(self, agent_port: AgentPort) -> None:
        self._agent_port = agent_port

    def start_turn(
        self,
        loop_record: LoopRecord,
        user_message: str,
        history: list[dict[str, Any]],
        session_id: str,
        session_key: str,
    ) -> None:
        """Startet einen Turn und verdrahtet den Content-Delta-Callback auf den Transport."""

        def _on_content_delta(delta: str) -> None:
            # Der Callback laeuft im Loop-Kontext; der Transport ist thread-safe.
            if loop_record.transport is not None:
                loop_record.transport.send_event(
                    ResponseEvent(ResponseEventKind.CONTENT_DELTA, delta)
                )

        def _on_tool_start(tool_call_id: str, name: str, args: dict[str, Any]) -> None:
            _emit_tool_marker(loop_record, name, args)

        def _on_tool_complete(
            tool_call_id: str, name: str, args: dict[str, Any], result: str
        ) -> None:
            # Ergebnisse werden bewusst nicht als Text gesendet (DD-18);
            # optional ergaenzbar (z. B. Abschluss-Marker).
            pass

        handle = self._agent_port.start_turn(
            user_message=user_message,
            history=history,
            session_id=session_id,
            session_key=session_key,
            on_content_delta=_on_content_delta,
            on_tool_start=_on_tool_start,
            on_tool_complete=_on_tool_complete,
        )
        loop_record.agent_handle = handle
        loop_record.state = SessionState.TURN_ACTIVE
        # Wrapper-Task: wartet auf das Turn-Ende und sendet die finale
        # Antwort (falls der Transport noch offen ist, DD-10).
        loop_record.finalize_task = asyncio.create_task(self._await_turn(loop_record))

    async def _await_turn(self, loop_record: LoopRecord) -> None:
        """Wartet auf den Agent-Task und finalisiert den Turn."""
        task = loop_record.agent_handle.task
        if task is None:
            return
        try:
            result = await task
        except asyncio.CancelledError:
            # Abbruch durch Shutdown/Rekonstruktion (DD-05). Keine Finalisierung.
            return
        except Exception:
            logger.exception("Agent-Turn mit Fehler beendet")
            result = {}
        if isinstance(result, dict):
            self.finalize(loop_record, result)
        else:
            loop_record.state = SessionState.IDLE

    def deliver_tool_response(self, loop_record: LoopRecord, response: ToolResponse) -> None:
        """Weckt den blockierenden Handler mit dem Tool-Ergebnis (Spec 2.3, Fall 2)."""
        loop_record.client_queue.put(response.content)

    def inject_user(self, loop_record: LoopRecord, text: str) -> None:
        """Zustellung einer User-Message als Steer (DD-04). Muss vor dem
        Queue-Put erfolgen, damit der Tool-Drain den Steer mitnimmt (DD-06)."""
        self._agent_port.steer(loop_record.agent_handle, text)

    def wake_with_abort(self, loop_record: LoopRecord) -> None:
        """Weckt den Handler sofort mit einem Abbruch-Result (DD-07).

        Voraussetzung: der Handler ist der letzte Schritt der Runde; alle
        serverseitigen Tools sind bereits fertig.
        """
        loop_record.client_queue.put(ABORT_MESSAGE)

    def cancel(self, loop_record: LoopRecord) -> None:
        """Bricht den Turn ab. Nur fuer Shutdown/Rekonstruktion (DD-05)."""
        self._agent_port.cancel(loop_record.agent_handle)

    def finalize(self, loop_record: LoopRecord, result: dict[str, Any]) -> None:
        """Sendet die finale Antwort und schliesst den State (DD-10).

        result enthaelt den finalen Text unter dem Schluessel der jeweiligen
        run_conversation-Variante (z. B. "final_response"); der Zugriff erfolgt
        defensiv, da die genaue Struktur von der Hermes-Version abhaengt.
        """
        final_text = result.get("final_response") or result.get("response") or ""
        if final_text and loop_record.transport is not None:
            loop_record.transport.send_event(
                ResponseEvent(ResponseEventKind.FINAL, final_text)
            )
        loop_record.state = SessionState.IDLE
