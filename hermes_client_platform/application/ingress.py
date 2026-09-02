"""Eingangs-Regeln (Spec 2.3).

Uebersetzt einen eingehenden Ingress in eine geordnete Liste von Aktionen.
Die Funktion route() ist pur: sie entscheidet nur, fuehrt nichts aus. Die
Ausfuehrung uebernimmt der SessionManager (application/session_manager.py).
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from ..domain.models import Ingress, SessionState
from .loop_runner import LoopRecord


class IngressAction(Enum):
    """Aktionen, die aus einem eingehenden Request folgen koennen."""

    START_TURN = "start_turn"
    STEER = "steer"
    STEER_AND_DELIVER = "steer_and_deliver"
    DELIVER_TOOL_RESPONSE = "deliver_tool_response"
    WAKE_ABORT_AND_STEER = "wake_abort_and_steer"
    DROP_STALE_TOOL_RESPONSE = "drop_stale_tool_response"
    ERROR_STALE_TOOL_RESPONSE = "error_stale_tool_response"


def route(
    loop_record: Optional[LoopRecord],
    ingress: Ingress,
) -> list[IngressAction]:
    """Bestimmt die Aktionen fuer einen Ingress.

    Reihenfolge ist signifikant (z. B. steer vor deliver bei DD-06).
    """

    has_user = bool(ingress.user_messages)
    has_tool = bool(ingress.tool_responses)

    if loop_record is None:
        # IDLE: nur eine User-Message startet einen Turn. Tool-Responses ohne
        # Session-State sind immer tot (DD-09): kein offener Call existiert.
        if has_user:
            return [IngressAction.START_TURN]
        if has_tool:
            return [IngressAction.ERROR_STALE_TOOL_RESPONSE]
        return []

    waiting = loop_record.waiting.is_set()

    if has_tool:
        if waiting:
            # Offener Call (CLIENT_WAIT): Ergebnis zustellen. Eine begleitende
            # User-Message wird zuerst als Steer injiziert (DD-06).
            if has_user:
                return [IngressAction.STEER_AND_DELIVER]
            return [IngressAction.DELIVER_TOOL_RESPONSE]
        # Tote Response: der Call ist bereits beendet (Timeout/kein State).
        if has_user:
            # Response verwerfen, Nachricht verarbeiten (DD-09, Drop-Fall).
            return [IngressAction.DROP_STALE_TOOL_RESPONSE, IngressAction.STEER]
        return [IngressAction.ERROR_STALE_TOOL_RESPONSE]

    if has_user:
        # Nur User-Message bei laufendem Turn (DD-04/DD-07).
        if waiting:
            return [IngressAction.WAKE_ABORT_AND_STEER]
        return [IngressAction.STEER]

    return []


def describe_state(loop_record: Optional[LoopRecord]) -> SessionState:
    """Aktueller State der Session (fuer Logging/Status)."""
    if loop_record is None:
        return SessionState.IDLE
    if loop_record.waiting.is_set():
        return SessionState.CLIENT_WAIT
    return loop_record.state
