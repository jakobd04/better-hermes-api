"""Plugin-Einstieg: register(ctx).

Das Plugin wird von Hermes ueber plugin.yaml + register(ctx) geladen.
Die Registrierung folgt der offiziellen keyword-Signatur
(Spec 3.2; docs/developer-guide/adding-platform-adapters).

Installation im Gateway:
- Paket installieren:  pip install -e /opt/data/vaectra/hermes-client-platform
- Plugin-Stub in ~/.hermes/plugins/hermes-client-platform/:
    plugin.yaml  (Kopie aus dem Projekt)
    __init__.py  mit:  from hermes_client_platform import register
- Platform in config.yaml aktivieren (gateway.platforms.client_platform)
"""

import os


def register(ctx):
    # Lazy-Import: haelt den Plugin-Einstieg import-schwerelos
    # (deferred Platform-Plugin, Spec 3.1).
    from .infrastructure.http_adapter import (
        ClientPlatformAdapter,
        _env_enablement,
        check_requirements,
        validate_config,
    )

    ctx.register_platform(
        name="better-hermes-api",
        label="Better Hermes API (OpenAI-kompatibel, client_*-Tools)",
        adapter_factory=lambda cfg: ClientPlatformAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["BETTER_HERMES_API_KEY"],
        install_hint="Konfiguriert ueber config.yaml (gateway.platforms.client_platform).",
        env_enablement_fn=_env_enablement,
        max_message_length=0,
        platform_hint="",
        emoji="🔌",
    )
