"""Ports der Domain (hexagonale Architektur).

Die Domain definiert die Schnittstellen, die der Anwendungskern benoetigt.
Die konkreten Implementierungen liegen in infrastructure/:
- AgentPort        -> hermes_adapter.HermesAgentPort
- RegistryPort     -> hermes_adapter.HermesRegistryPort
- SessionStorePort -> hermes_adapter.HermesSessionStorePort
- TransportPort    -> http_adapter.HttpTransport (pro HTTP-Request)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Callable, Optional

from .models import AgentHandle, ResponseEvent


class AgentPort(ABC):
    """Zugriff auf den Hermes-Agent-Loop (Driven-Port, Spec 3.4/3.5)."""

    @abstractmethod
    def start_turn(
        self,
        user_message: str,
        history: list[dict[str, Any]],
        session_id: str,
        session_key: str,
        on_content_delta: Callable[[str], None],
        on_tool_start: Optional[Callable[[str, str, dict], None]] = None,
        on_tool_complete: Optional[Callable[[str, str, dict, str], None]] = None,
    ) -> AgentHandle:
        """Startet einen Turn als asyncio-Task. Der Task ueberlebt das Request-Ende.

        Callback-Signaturen (generisch, unabhaengig von der Hermes-Variante):
        - on_tool_start(tool_call_id, name, args)  — Tool beginnt
        - on_tool_complete(tool_call_id, name, args, result)  — Tool fertig
        """

    @abstractmethod
    def steer(self, agent: AgentHandle, text: str) -> bool:
        """Injiziert eine User-Nachricht in den laufenden Turn (DD-04)."""

    @abstractmethod
    def read_pending_steer(self, agent: AgentHandle) -> Optional[str]:
        """Liest einen noch nicht gedrainten Steer (Uebernahme am Turn-Ende)."""

    @abstractmethod
    def cancel(self, agent: AgentHandle) -> None:
        """Bricht den Turn ab. Nur fuer Shutdown/Rekonstruktion (DD-05)."""

    @abstractmethod
    def is_alive(self, agent: AgentHandle) -> bool:
        """True, wenn der Turn-Task noch laeuft."""


class RegistryPort(ABC):
    """Tool-Registrierung zur Laufzeit (Driven-Port, Spec 3.6, DD-15)."""

    @abstractmethod
    def register(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., str],
        description: str,
    ) -> None:
        """Registriert ein Tool in der zentralen Registry."""

    @abstractmethod
    def deregister(self, name: str) -> None:
        """Entfernt ein Tool aus der zentralen Registry."""


class SessionStorePort(ABC):
    """Lesezugriff auf die persistierte Session-History (Driven-Port, DD-16)."""

    @abstractmethod
    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        """Liefert die vollstaendige Message-History einer Session."""


class TransportPort(ABC):
    """Ausgabe-Kanal an den Client (Driver-Port, Spec 3.8, DD-12).

    Wird pro HTTP-Request instanziiert. Der Anwendungskern sendet Events;
    der Adapter formatiert sie als SSE-Chunk oder sammelt sie fuer JSON.
    """

    @abstractmethod
    def send_event(self, event: ResponseEvent) -> None:
        """Stellt ein Event in die Ausgabe-Queue."""

    @abstractmethod
    def events(self) -> AsyncIterator[ResponseEvent]:
        """Konsumiert die Events (vom HTTP-Handler gelesen)."""

    @abstractmethod
    def close(self) -> None:
        """Schliesst den Kanal (Client-Disconnect)."""
