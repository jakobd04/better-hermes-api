"""State-Machine der Session-Zustaende (Spec 2.2).

Pure Domain: keine I/O, keine Hermes-Importe. Die Uebergaenge sind die in der
Spec definierten zulaessigen Transitionen; ungueltige Uebergaenge werfen
ValueError, damit Fehler frueh und deterministisch sichtbar sind.
"""

from __future__ import annotations

from .models import SessionState, StateEvent

# Zulaessige Uebergaenge: (aktueller Zustand, Ereignis) -> neuer Zustand.
_TRANSITIONS: dict[tuple[SessionState, StateEvent], SessionState] = {
    # IDLE: neue User-Message startet einen Turn.
    (SessionState.IDLE, StateEvent.USER_MESSAGE): SessionState.TURN_ACTIVE,
    # TURN_ACTIVE: client_-Call blockiert den Loop im Handler.
    (SessionState.TURN_ACTIVE, StateEvent.CLIENT_TOOL_CALL): SessionState.CLIENT_WAIT,
    # CLIENT_WAIT: Tool-Response oder Timeout lassen den Loop weiterlaufen.
    (SessionState.CLIENT_WAIT, StateEvent.TOOL_RESPONSE): SessionState.TURN_ACTIVE,
    (SessionState.CLIENT_WAIT, StateEvent.TIMEOUT): SessionState.TURN_ACTIVE,
    # TURN_ACTIVE: finale Antwort schliesst den State sofort (DD-10).
    (SessionState.TURN_ACTIVE, StateEvent.FINAL_ANSWER): SessionState.IDLE,
    # CANCEL nur als Sonderfall: Shutdown oder DB-Rekonstruktion (DD-05).
    (SessionState.TURN_ACTIVE, StateEvent.CANCEL): SessionState.IDLE,
    (SessionState.CLIENT_WAIT, StateEvent.CANCEL): SessionState.IDLE,
}


def transition(current: SessionState, event: StateEvent) -> SessionState:
    """Berechnet den Folge-Zustand. ValueError bei unzulaessigem Uebergang."""
    try:
        return _TRANSITIONS[(current, event)]
    except KeyError:
        raise ValueError(
            f"Ungueltiger Uebergang: {current.value} + {event.value}"
        ) from None
