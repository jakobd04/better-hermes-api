"""Driven-Adapter: Implementierung der Ports gegen die Hermes-API.

Diese Datei ist der einzige Ort mit direkten Hermes-Importen. Die Importe sind
bewusst lazy (innerhalb der Methoden), damit der Plugin-Einstieg
import-schwerelos bleibt (deferred Platform-Plugin, Spec 3.1).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from ..domain.models import AgentHandle
from ..domain.ports import AgentPort, RegistryPort, SessionStorePort

logger = logging.getLogger(__name__)


class HermesAgentPort(AgentPort):
    """AgentPort gegen run_conversation und agent.steer (Spec 3.4/3.5)."""

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
        from run_agent import run_conversation  # lazy: Hermes-Prozess-Kontext

        # Annahme: agent_ref wird von run_conversation befuellt (Muster aus
        # gateway/platforms/api_server.py). Beim ersten Lauf verifizieren.
        agent_ref: list[Any] = []
        task = asyncio.ensure_future(
            run_conversation(
                user_message=user_message,
                conversation_history=history,
                session_id=session_id,
                gateway_session_key=session_key,
                stream_delta_callback=on_content_delta,
                # Signatur wie im api_server-Muster:
                #   tool_start_callback(tool_call_id, function_name, function_args)
                #   tool_complete_callback(tool_call_id, function_name, function_args, function_result)
                tool_start_callback=on_tool_start,
                tool_complete_callback=on_tool_complete,
                agent_ref=agent_ref,
            )
        )
        return AgentHandle(agent_ref=agent_ref, task=task)

    def steer(self, agent: AgentHandle, text: str) -> bool:
        if not agent.agent_ref:
            return False
        return bool(agent.agent_ref[0].steer(text))

    def read_pending_steer(self, agent: AgentHandle) -> Optional[str]:
        if not agent.agent_ref:
            return None
        return getattr(agent.agent_ref[0], "_pending_steer", None)

    def cancel(self, agent: AgentHandle) -> None:
        if agent.task is not None and not agent.task.done():
            agent.task.cancel()

    def is_alive(self, agent: AgentHandle) -> bool:
        return agent.task is not None and not agent.task.done()


class HermesRegistryPort(RegistryPort):
    """RegistryPort gegen die zentrale Tool-Registry (Spec 3.6, DD-15)."""

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., str],
        description: str,
    ) -> None:
        from tools.registry import registry  # lazy

        registry.register(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            description=description,
        )

    def deregister(self, name: str) -> None:
        from tools.registry import registry  # lazy

        registry.deregister(name)


class HermesSessionStorePort(SessionStorePort):
    """SessionStorePort gegen die Session-DB (DD-16)."""

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        from hermes_state import SessionDB  # lazy

        # Annahme: get_messages_as_conversation liefert die persistierte
        # History im OpenAI-Message-Format. Beim ersten Lauf verifizieren.
        db = SessionDB()
        return db.get_messages_as_conversation(session_id)
