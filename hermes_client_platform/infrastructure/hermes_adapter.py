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

    def start_turn(self, user_message, history, session_id, session_key,
                on_content_delta, on_tool_start=None, on_tool_complete=None) -> AgentHandle:
        from run_agent import AIAgent
        from gateway.run import (
            _checkpoint_agent_kwargs, _current_max_iterations,
            _resolve_runtime_agent_kwargs, _resolve_gateway_model,
            _load_gateway_config,
        )
        from hermes_cli.tools_config import _get_platform_tools
        from hermes_state import SessionDB

        # Muster aus gateway/platforms/api_server.py (_create_agent)
        runtime_kwargs = _resolve_runtime_agent_kwargs()
        model = _resolve_gateway_model()
        runtime_model = runtime_kwargs.pop("model", None)
        if runtime_model:
            model = runtime_model

        user_config = _load_gateway_config()
        enabled_toolsets = sorted(_get_platform_tools(user_config, "client_sides"))

        agent = AIAgent(
            model=model,
            **runtime_kwargs,
            **_checkpoint_agent_kwargs(user_config),
            max_iterations=_current_max_iterations(),
            quiet_mode=True,
            verbose_logging=False,
            enabled_toolsets=enabled_toolsets,
            session_id=session_id,
            platform="better-hermes-api",
            stream_delta_callback=on_content_delta,
            tool_start_callback=on_tool_start,
            tool_complete_callback=on_tool_complete,
            session_db=SessionDB(),
            gateway_session_key=session_key
        )

        # Agent direkt halten — agent_ref[0] fuer steer() (Agent-Methode)
        agent_ref: list = [agent]
        task = asyncio.ensure_future(
            agent.run_conversation(user_message=user_message, conversation_history=history)
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
