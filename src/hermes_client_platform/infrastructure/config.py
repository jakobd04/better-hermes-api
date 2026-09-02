"""Konfiguration der Platform.

Quellen in Reihenfolge: Plattform-Config (config.yaml, `extra`-Block) und
Umgebungsvariablen (CLIENT_PLATFORM_*). Defaults orientieren sich an den
offenen Punkten der Spec (Abschnitt 3.10).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformSettings:
    """Einstellungen der Custom Platform."""

    host: str = "0.0.0.0"
    port: int = 8643
    api_key: str = ""
    toolset_name: str = "client_platform"
    tool_timeout_seconds: float = 60.0
    idle_timeout_seconds: float = 1800.0
    http_read_timeout_seconds: float = 300.0
    show_server_tool_markers: bool = True

    @classmethod
    def from_extra(cls, extra: dict | None) -> "PlatformSettings":
        """Baut die Einstellungen aus dem `extra`-Block der Platform-Config."""
        extra = extra or {}
        env = os.environ
        return cls(
            host=extra.get("host") or env.get("CLIENT_PLATFORM_HOST", "0.0.0.0"),
            port=int(extra.get("port") or env.get("CLIENT_PLATFORM_PORT", "8643")),
            api_key=env.get("CLIENT_PLATFORM_KEY", "") or extra.get("api_key", ""),
            toolset_name=extra.get("toolset", "client_platform"),
            tool_timeout_seconds=float(
                extra.get("tool_timeout") or env.get("CLIENT_PLATFORM_TOOL_TIMEOUT", "60")
            ),
            idle_timeout_seconds=float(
                extra.get("idle_timeout") or env.get("CLIENT_PLATFORM_IDLE_TIMEOUT", "1800")
            ),
            http_read_timeout_seconds=float(
                extra.get("http_read_timeout") or env.get("CLIENT_PLATFORM_HTTP_TIMEOUT", "300")
            ),
            show_server_tool_markers=bool(
                extra.get("show_server_tool_markers", True)
                if "show_server_tool_markers" in (extra or {})
                else env.get("CLIENT_PLATFORM_TOOL_MARKERS", "1") not in ("0", "false", "False")
            ),
        )
