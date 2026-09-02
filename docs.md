# Hermes Client Platform — Projekt

Plugin-basierte Custom Platform für Hermes: OpenAI-kompatible HTTP/SSE-Endpoints mit
`client_*`-Tool-Passthrough über blockierende Handler. Der Agent-Loop bleibt unverändert.

**Referenz-Spec:** `/opt/data/vaectra/hermes-custom-platform-spec.md` (Design Decisions DD-01 … DD-17)

## Architektur

Hexagonale Architektur, von innen nach außen:

- **domain/** — pure Domain: Datenmodelle, Ports (ABCs), State-Machine. Keine Importe aus
  Hermes oder aiohttp. Wiederverwendbar und isoliert testbar.
- **application/** — Anwendungskern: Loop-Verwaltung, Eingangs-Regeln, Session-Manager,
  Tool-Registrierung. Nutzt ausschließlich die Ports aus `domain/ports.py`.
- **infrastructure/** — Adapter:
  - `hermes_adapter.py` (Driven): implementiert die Ports gegen die echte Hermes-API
    (`run_conversation`, `agent.steer`, `tools.registry`, `hermes_state.SessionDB`).
  - `http_adapter.py` + `protocol.py` (Driver): aiohttp-HTTP/SSE-Endpoint, Request-Parsing,
    Response-Formatierung.

## Verzeichnisstruktur

```
src/hermes_client_platform/
├── __init__.py                 # register(ctx) — Plugin-Einstieg
├── domain/
│   ├── models.py               # SessionState, ClientToolCall, ToolResponse, Ingress, ResponseEvent
│   ├── ports.py                # AgentPort, RegistryPort, SessionStorePort, TransportPort
│   └── state_machine.py        # zulässige State-Übergänge
├── application/
│   ├── loop_runner.py          # LoopRecord + LoopRunner (Task, Queue, steer, cancel)
│   ├── ingress.py              # IngressAction + route(): Eingangs-Regeln (Spec 2.3)
│   ├── session_manager.py      # SessionManager (FIFO, Lifecycle, Rekonstruktion)
│   └── tool_registry.py        # register_client_tools / deregister_client_tools
└── infrastructure/
    ├── config.py               # PlatformSettings
    ├── hermes_adapter.py       # HermesAgentPort, HermesRegistryPort, HermesSessionStorePort
    ├── protocol.py             # parse_chat_completions, SSE-/JSON-Formatierung
    └── http_adapter.py         # ClientPlatformAdapter (aiohttp), HttpTransport
```

## Design-Entscheidungen (Kurzfassung)

Vollständige Begründungen in der Spec (Abschnitt + ADR-Index).

- **DD-01** Blockierende Handler statt Pause/Resume-Patch — Loop bleibt Standard.
- **DD-02** Stateful Loops: eine Session = ein lebender Agent-Loop.
- **DD-03** Hermes-DB ist kanonisch; serverseitige Runden werden nicht an den Client übertragen.
- **DD-04** `steer()` ist die einzige Zustellung während laufender Turns.
- **DD-05** Timeout statt Abbruch — der Loop schließt sich natürlich.
- **DD-06** Tool-Response + User-Message: erst steer, dann Queue-Put; nie ein zweiter Loop.
- **DD-07** Nur-User-Message bei `client_wait`: sofort wecken + steer.
- **DD-08** Provider-Invariante: nie `[assistant(tool_calls), user]` ohne tool-Antwort.
- **DD-09** Tote Tool-Responses: 409 oder Drop.
- **DD-10** Kein Puffer für verwaiste Antworten.
- **DD-11** Session-ID: Header + Response-Header + deterministischer Hash.
- **DD-12** SSE und HTTP als zwei Formatierungen desselben Loops; SSE als Default.
- **DD-13** aiohttp statt FastAPI/uvicorn.
- **DD-14** Eigener Verarbeitungspfad statt Gateway-Runner.
- **DD-15** Registry-Lebenszyklus: dynamische Tools über register/deregister.
- **DD-16** DB-Rekonstruktion nach Prozess-Neustart.
- **DD-17** dsh primär, OpenAI-kompatibel sekundär (Phase 1: HTTP/SSE, Phase 2: WS).
- **DD-18** Server-Tool-Marker als sichtbarer Text im Stream (UX, kein Kontext-Sync) — `tool_start_callback` → Content-Delta-Marker.

## Abhängigkeiten

Keine neuen Packages. Laufzeit-Umgebung ist das Hermes-Venv (aiohttp 3.14.1 vorhanden,
vom Gateway genutzt). `pyproject.toml` deklariert aiohttp nur für die eigenständige
Entwicklung in VS Code.

## Installation im Hermes-Gateway

1. Paket installieren:
   ```
   pip install -e /opt/data/vaectra/hermes-client-platform
   ```
2. Plugin-Stub in `~/.hermes/plugins/hermes-client-platform/` anlegen:
   - `plugin.yaml` (Kopie aus dem Projekt)
   - `__init__.py` mit einer Zeile: `from hermes_client_platform import register`
3. Env-Variable setzen: `CLIENT_PLATFORM_KEY=<geheimer Key>`
4. Platform in `config.yaml` aktivieren:
   ```yaml
   gateway:
     platforms:
       client_platform:
         enabled: true
   ```
5. Gateway neu starten (`hermes gateway restart`).

Der Stub ist bewusst minimal: Er reicht `register` aus dem installierten Paket
durch. Die Alternative — das Plugin-Verzeichnis direkt auf `src/` zeigen zu
lassen — vermeidet den Install-Schritt, koppelt aber an den Projektpfad.

## Status der Implementierung

Alle Dateien geschrieben (2026-08-30):

- `domain/models.py` — SessionState, ClientToolCall, ToolResponse, Ingress, ResponseEvent, AgentHandle
- `domain/ports.py` — AgentPort, RegistryPort, SessionStorePort, TransportPort (ABCs)
- `domain/state_machine.py` — zulässige State-Übergänge (pure)
- `application/loop_runner.py` — LoopRecord, LoopRunner (Start, steer, deliver, wake_with_abort, Finalisierung)
- `application/ingress.py` — IngressAction + route() (Spec 2.3, pur)
- `application/session_manager.py` — SessionManager (FIFO, Lifecycle, Rekonstruktion)
- `application/tool_registry.py` — register/deregister client_*-Tools (Closure-Handler)
- `infrastructure/config.py` — PlatformSettings
- `infrastructure/hermes_adapter.py` — HermesAgentPort, HermesRegistryPort, HermesSessionStorePort
- `infrastructure/protocol.py` — parse_chat_completions, SSE-/JSON-Formatierung
- `infrastructure/http_adapter.py` — ClientPlatformAdapter (aiohttp), HttpTransport, Plugin-Hilfsfunktionen
- `application/loop_runner.py` — zusätzlich: Tool-Marker (DD-18) via `_emit_tool_marker`
- `__init__.py` — register(ctx)

## Integrationsannahmen (beim ersten Lauf gegen den Gateway verifizieren)

Die Stellen sind im Code mit `# Annahme:` markiert:

- `run_conversation` befüllt `agent_ref` (Muster aus `gateway/platforms/api_server.py`).
- `run_conversation` liefert ein Dict mit dem finalen Text unter
  `final_response` oder `response` (defensiver Zugriff in `loop_runner.finalize`).
- `SessionDB().get_messages_as_conversation(session_id)` liefert die History im
  OpenAI-Message-Format.
- `registry.register` akzeptiert `name`, `toolset`, `schema`, `handler`,
  `description` als Keyword-Argumente.
- Der Tool-Dispatcher ruft Handler mit `(params, **kwargs)` auf und kann in
  Worker-Threads laufen (deshalb `queue.Queue` statt `asyncio.Queue`).
- `tool_start_callback(tool_call_id, function_name, function_args)` und
  `tool_complete_callback(tool_call_id, function_name, function_args,
  function_result)` sind die von run_conversation akzeptierten Signaturen
  (verifiziert am api_server-Muster, 2026-09-01).

## Offene Punkte

Siehe Spec Abschnitt 3.10. Nicht implementiert (bewusst): WebSocket-Adapter für
dsh (Phase 2, DD-17), Tests, HTTP-Timeout-Disconnect im SSE-Writer (der Stream
hängt bis zum ersten Event oder http_read_timeout).
