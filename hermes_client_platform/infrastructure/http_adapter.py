"""Driver: aiohttp-HTTP/SSE-Endpoint (Spec 3.3, DD-14).

ClientPlatformAdapter erfuellt das BasePlatformAdapter-Interface fuer den
Gateway-Lifecycle (connect/disconnect/Status/Config), verarbeitet Turns aber
ueber den eigenen SessionManager — nicht ueber den Gateway-Runner.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import queue
import threading
from typing import Any, AsyncIterator, Optional

from aiohttp import web

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

from ..application.session_manager import SessionManager
from ..domain.models import Ingress, ResponseEvent, ResponseEventKind
from ..domain.ports import TransportPort
from . import protocol
from .config import PlatformSettings
from .hermes_adapter import HermesAgentPort, HermesRegistryPort, HermesSessionStorePort

logger = logging.getLogger(__name__)


class HttpTransport(TransportPort):
    """TransportPort-Implementierung fuer einen einzelnen HTTP-Request.

    Thread-safe: send_event wird aus Loop-Callbacks und Tool-Handler-Threads
    aufgerufen. Nach close() werden Events verworfen (DD-10: keine Pufferung
    verwaister Antworten).
    """

    def __init__(self) -> None:
        self._queue: "queue.Queue[ResponseEvent]" = queue.Queue()
        self._closed = False
        self._lock = threading.Lock()

    def send_event(self, event: ResponseEvent) -> None:
        with self._lock:
            if self._closed:
                return
            self._queue.put(event)

    async def events(self) -> AsyncIterator[ResponseEvent]:
        while True:
            try:
                event = await asyncio.to_thread(self._queue.get, timeout=1.0)
                yield event
            except queue.Empty:
                if self._closed:
                    return

    def close(self) -> None:
        with self._lock:
            self._closed = True


def _derive_session_id(first_user_message: str) -> str:
    """Deterministische Session-ID (Fallback, DD-11).

    Muster aus _derive_chat_session_id im api_server: Hash aus System-Prompt
    (hier konstant) und erster User-Message. Stabil, solange der Client die
    volle History mitschickt.
    """
    seed = f"hermes-agent\n{first_user_message}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"api-{digest}"


class ClientPlatformAdapter(BasePlatformAdapter):
    """aiohttp-Plattform-Adapter der Custom Platform."""

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform("better-hermes-api"))
        self._settings = PlatformSettings.from_extra(config.extra)
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._session_manager: Optional[SessionManager] = None
        self._idle_task: Optional[asyncio.Task] = None

    # -- Gateway-Lifecycle ----------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._settings.api_key:
            logger.error("BETTER_HERMES_API_KEY ist nicht gesetzt; Platform startet nicht.")
            return False

        self._session_manager = SessionManager(
            agent_port=HermesAgentPort(),
            registry_port=HermesRegistryPort(),
            session_store=HermesSessionStorePort(),
            settings=self._settings,
        )

        self._app = web.Application()
        self._app.router.add_post("/v1/chat/completions", self.handle_chat)
        self._app.router.add_get("/v1/models", self.handle_models)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._settings.host, self._settings.port)
        await self._site.start()

        self._idle_task = asyncio.create_task(self._idle_cleanup_loop())
        self._mark_connected()
        logger.info("Client Platform gestartet auf %s:%s", self._settings.host, self._settings.port)
        return True

    async def disconnect(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
        if self._session_manager is not None:
            self._session_manager.shutdown()
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        # Kein aktives Messaging — Antworten laufen ausschliesslich ueber HTTP/SSE.
        return SendResult(success=True, message_id="")

    async def _idle_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60.0)
            if self._session_manager is not None:
                self._session_manager.cleanup_idle()

    # -- Auth ---------------------------------------------------------------

    def _check_auth(self, request: web.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        token = header[len("Bearer "):]
        return hmac.compare_digest(token, self._settings.api_key)

    # -- HTTP-Handler ---------------------------------------------------------

    async def handle_chat(self, request: web.Request) -> web.StreamResponse:
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "unauthorized", "type": "auth_error"}}, status=401
            )
        try:
            body = await request.json()
            ingress = protocol.parse_chat_completions(body)
        except (protocol.ProtocolError, ValueError) as exc:
            return web.json_response(
                {"error": {"message": str(exc), "type": "invalid_request_error"}},
                status=400,
            )

        session_id, session_key = self._resolve_session(request, ingress)
        transport = HttpTransport()

        assert self._session_manager is not None
        record = self._session_manager.get_or_create(session_id, session_key)
        self._session_manager.submit(record, ingress, transport)

        if ingress.stream:
            return await self._write_sse(request, transport, session_id)
        return await self._write_json(request, transport, session_id)

    async def handle_models(self, request: web.Request) -> web.Response:
        if not self._check_auth(request):
            return web.json_response(
                {"error": {"message": "unauthorized", "type": "auth_error"}}, status=401
            )
        return web.json_response(
            {"data": [{"id": "hermes-agent", "object": "model", "owned_by": "hermes"}]}
        )

    def _resolve_session(
        self, request: web.Request, ingress: Ingress
    ) -> tuple[str, str]:
        """Session-ID-Aufloesung nach DD-11: Header, sonst deterministischer Hash."""
        header_id = request.headers.get("X-Hermes-Session-Id", "").strip()
        if header_id:
            session_id = header_id
        else:
            first_user = ingress.user_messages[0] if ingress.user_messages else ""
            session_id = _derive_session_id(first_user)
        session_key = (
            request.headers.get("X-Hermes-Session-Key", "").strip() or session_id
        )
        return session_id, session_key

    # -- Response-Schreiber ---------------------------------------------------

    async def _write_sse(
        self, request: web.Request, transport: HttpTransport, session_id: str
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Hermes-Session-Id": session_id,
            },
        )
        await response.prepare(request)
        try:
            async for event in transport.events():
                if event.kind is ResponseEventKind.CONTENT_DELTA:
                    await self._sse_write(response, protocol.sse_content_delta(event.payload))
                elif event.kind is ResponseEventKind.TOOL_CALL:
                    # Stream endet mit der Tool-Anfrage; der Loop laeuft weiter
                    # (client_wait). Der Client antwortet im naechsten Request.
                    await self._sse_write(response, protocol.sse_tool_call(event.payload))
                    await self._sse_write(response, protocol.sse_finish("tool_calls"))
                    await response.write(f"data: {protocol.sse_done()}\n\n".encode())
                    break
                elif event.kind is ResponseEventKind.FINAL:
                    await self._sse_write(response, protocol.sse_content_delta(event.payload))
                    await self._sse_write(response, protocol.sse_finish("stop"))
                    await response.write(f"data: {protocol.sse_done()}\n\n".encode())
                    break
                elif event.kind is ResponseEventKind.ERROR:
                    await response.write(
                        f"data: {json.dumps({'error': event.payload})}\n\n".encode()
                    )
                    await response.write(f"data: {protocol.sse_done()}\n\n".encode())
                    break
        finally:
            transport.close()
        return response

    async def _write_json(
        self, request: web.Request, transport: HttpTransport, session_id: str
    ) -> web.Response:
        events: list[ResponseEvent] = []
        async for event in transport.events():
            events.append(event)
            if event.kind in (ResponseEventKind.TOOL_CALL, ResponseEventKind.FINAL, ResponseEventKind.ERROR):
                break
        transport.close()

        for event in events:
            if event.kind is ResponseEventKind.ERROR:
                return web.json_response({"error": event.payload}, status=409)

        content = "".join(
            e.payload for e in events if e.kind is ResponseEventKind.CONTENT_DELTA
        )
        tool_calls = [
            protocol.json_tool_call_payload(e.payload)
            for e in events
            if e.kind is ResponseEventKind.TOOL_CALL
        ]
        finish_reason = "tool_calls" if tool_calls else "stop"
        response = web.json_response(
            protocol.json_response(content, tool_calls or None, finish_reason)
        )
        response.headers["X-Hermes-Session-Id"] = session_id
        return response

    @staticmethod
    async def _sse_write(response: web.StreamResponse, payload: dict[str, Any]) -> None:
        await response.write(f"data: {json.dumps(payload)}\n\n".encode())


# -- Plugin-Hilfsfunktionen (Spec 3.2) ----------------------------------------


def check_requirements() -> bool:
    return bool(os.getenv("BETTER_HERMES_API_KEY"))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(os.getenv("BETTER_HERMES_API_KEY") or extra.get("api_key"))


def _env_enablement() -> Optional[dict]:
    """Seed fuer PlatformConfig.extra aus Env-Variablen (Env-Auto-Enable)."""
    if not os.getenv("BETTER_HERMES_API_KEY"):
        return None
    return {
        "host": os.getenv("BETTER_HERMES_API_HOST", "0.0.0.0"),
        "port": int(os.getenv("BETTER_HERMES_API_PORT", "8643")),
    }
