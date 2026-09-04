"""SessionManager: Session-Map, FIFO-Verarbeitung, Lifecycle, Rekonstruktion.

Eine Session = ein lebender Loop (DD-02). Eingehende Requests werden pro
Session sequentiell verarbeitet (FIFO); der Verarbeitungs-Task laeuft im
Gateway-Event-Loop. Die client_queue des Loops ist eine thread-safe queue.Queue,
weil Tool-Handler in Hermes in Worker-Threads laufen koennen.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from ..domain.models import Ingress, ResponseEvent, ResponseEventKind, SessionState
from ..domain.ports import AgentPort, RegistryPort, SessionStorePort, TransportPort
from ..infrastructure.config import PlatformSettings
from . import tool_registry
from .ingress import IngressAction, route
from .loop_runner import LoopRecord, LoopRunner

logger = logging.getLogger(__name__)


@dataclass
class SessionRecord:
    """Zustand einer Session im SessionManager."""

    session_id: str
    session_key: str
    loop_record: Optional[LoopRecord] = None
    ingress_queue: "asyncio.Queue[tuple[Ingress, TransportPort]]" = field(
        default_factory=asyncio.Queue
    )
    processing_task: Optional[asyncio.Task] = None
    last_active: float = field(default_factory=time.time)


class SessionManager:
    """Verwaltet alle aktiven Sessions der Platform."""

    def __init__(
        self,
        agent_port: AgentPort,
        registry_port: RegistryPort,
        session_store: SessionStorePort,
        settings: PlatformSettings,
    ) -> None:
        self._agent_port = agent_port
        self._registry_port = registry_port
        self._session_store = session_store
        self._settings = settings
        self._runner = LoopRunner(agent_port)
        self._sessions: dict[str, SessionRecord] = {}

    # -- Session-Lebenszyklus -------------------------------------------------

    def get_or_create(self, session_id: str, session_key: str) -> SessionRecord:
        record = self._sessions.get(session_id)
        if record is None:
            record = SessionRecord(session_id=session_id, session_key=session_key)
            self._sessions[session_id] = record
            record.processing_task = asyncio.create_task(self._processing_loop(record))
            logger.info("Session erstellt: %s", session_id)
        record.last_active = time.time()
        return record

    def submit(self, record: SessionRecord, ingress: Ingress, transport: TransportPort) -> None:
        """Reiht einen Request in die FIFO der Session ein.

        Der Transport wird am LoopRecord gesetzt, damit Events des laufenden
        Loops an den aktuell offenen Request gehen.
        """
        record.last_active = time.time()
        if record.loop_record is not None:
            record.loop_record.transport = transport
        record.ingress_queue.put_nowait((ingress, transport))

    def shutdown(self) -> None:
        """Bricht alle aktiven Turns ab und beendet die Verarbeitungs-Tasks."""
        for record in self._sessions.values():
            if record.loop_record is not None:
                self._runner.cancel(record.loop_record)
            if record.processing_task is not None:
                record.processing_task.cancel()
        self._sessions.clear()

    def cleanup_idle(self) -> None:
        """Entfernt Sessions ohne Aktivitaet innerhalb des Idle-Timeouts."""
        cutoff = time.time() - self._settings.idle_timeout_seconds
        stale = [
            sid for sid, rec in self._sessions.items() if rec.last_active < cutoff
        ]
        for sid in stale:
            record = self._sessions.pop(sid)
            if record.loop_record is not None:
                self._runner.cancel(record.loop_record)
                tool_registry.deregister_client_tools(
                    record.loop_record, self._registry_port
                )
            logger.info("Session entfernt (idle): %s", sid)

    # -- FIFO-Verarbeitung ----------------------------------------------------

    async def _processing_loop(self, record: SessionRecord) -> None:
        while True:
            try:
                ingress, transport = await asyncio.wait_for(
                    record.ingress_queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                # Poll-Intervall: erlaubt Idle-Cleanup und Shutdown-Erkennung.
                continue
            try:
                actions = route(record.loop_record, ingress)
                await self._apply_actions(record, ingress, transport, actions)
            except Exception:
                logger.exception("Fehler bei Request-Verarbeitung (Session %s)", record.session_id)

    async def _apply_actions(
        self,
        record: SessionRecord,
        ingress: Ingress,
        transport: TransportPort,
        actions: list[IngressAction],
    ) -> None:
        for action in actions:
            if action is IngressAction.START_TURN:
                self._start_turn(record, ingress, transport)
            elif action is IngressAction.STEER:
                for text in ingress.user_messages:
                    self._runner.inject_user(record.loop_record, text)
            elif action is IngressAction.STEER_AND_DELIVER:
                for text in ingress.user_messages:
                    self._runner.inject_user(record.loop_record, text)
                self._deliver_all(record, ingress)
            elif action is IngressAction.DELIVER_TOOL_RESPONSE:
                self._deliver_all(record, ingress)
            elif action is IngressAction.WAKE_ABORT_AND_STEER:
                for text in ingress.user_messages:
                    self._runner.inject_user(record.loop_record, text)
                self._runner.wake_with_abort(record.loop_record)
            elif action is IngressAction.DROP_STALE_TOOL_RESPONSE:
                logger.info("Tote Tool-Response verworfen (Session %s)", record.session_id)
            elif action is IngressAction.ERROR_STALE_TOOL_RESPONSE:
                transport.send_event(
                    ResponseEvent(
                        ResponseEventKind.ERROR,
                        {"code": "no_pending_tool_call", "message": "Keine offene Tool-Request fuer diese Response."},
                    )
                )

    def _deliver_all(self, record: SessionRecord, ingress: Ingress) -> None:
        for response in ingress.tool_responses:
            self._runner.deliver_tool_response(record.loop_record, response)

    # -- Turn-Start (inkl. Rekonstruktion, DD-16) -----------------------------

    def _start_turn(
        self, record: SessionRecord, ingress: Ingress, transport: TransportPort
    ) -> None:
        if not ingress.user_messages:
            return
        user_message = "\n".join(ingress.user_messages)

        # Rekonstruktion nach Prozess-Neustart: History aus der DB laden
        # (DD-16). Bei neuer Session ist die Liste leer; dann wird die
        # mitgesendete Request-History als Start-Kontext genutzt
        # (Session-Uebernahme, Spec 2.3 Regel 1).
        history = self._session_store.get_history(record.session_id)
        if not history:
            history = ingress.history

        loop_record = LoopRecord(
            agent_handle=None,
            transport=transport,
            show_tool_markers=self._settings.show_server_tool_markers,
        )
        record.loop_record = loop_record

        tool_registry.register_client_tools(
            loop_record, ingress.tool_definitions, self._registry_port, self._settings
        )

        self._runner.start_turn(
            loop_record,
            user_message=user_message,
            history=history,
            session_id=record.session_id,
            session_key=record.session_key,
        )
