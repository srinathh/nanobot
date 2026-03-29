# Claude Agent Engine: Minimal-Divergence Integration Plan

## Goal

Replace nanobot's default `AgentLoop` with Claude Agent SDK as a config-driven option, with **minimal changes to upstream files**. The Claude agent adapter lives in its own file; upstream code changes are limited to a constructor branch and a few guards.

## Background

### Previous attempt (`feature/sdk-engine-v2`)

The v2 branch modified ~250 lines of `commands.py`, duplicating entire functions (`on_cron_job`, heartbeat setup, `agent()` command) into `if use_claude_agent / else` branches. This creates high merge-conflict risk — any upstream change to cron/heartbeat needs updating in two places.

**Root cause**: `ClaudeAgentLoop` didn't implement the same interface as `AgentLoop`, so every call site that touched `agent.tools` or `agent.sessions` needed engine-specific guards.

### This plan's approach

Make `ClaudeAgentLoop` expose the same public interface consumed by `commands.py`. Where the SDK handles something internally (sessions, tools), expose lightweight stubs or delegates so the calling code doesn't need to branch.

## Architecture

### How the SDK works

The Claude Agent SDK **spawns the** `**claude**` **CLI as a subprocess** (`SubprocessCLITransport`). Key implications:

1.  **Auth**: The CLI reads OAuth tokens from `~/.claude/` (saved by `claude login`). No API key needed — it uses the logged-in session (e.g. Max plan).
2.  **Tools**: The CLI has built-in tools (Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch). Custom tools are exposed via MCP servers.
3.  **Sessions**: The SDK manages conversation context internally per `ClaudeSDKClient` instance. Nanobot's `SessionManager` (JSONL files) is not used for LLM context.
4.  **Binary required**: The `claude` CLI must be installed in the runtime environment.

### Interface contract

`commands.py` uses these attributes/methods on the agent object:

| Member | Type | Usage | Claude agent strategy |
| --- | --- | --- | --- |
| `run()` | async | Main loop consuming from bus | Implement directly |
| `process_direct(content, session_key, channel, chat_id, on_progress, ...)` | async -> OutboundMessage? | Cron jobs, heartbeat, CLI | Implement directly |
| `stop()` | sync | Graceful shutdown | Implement directly |
| `close_mcp()` | async | Cleanup MCP connections | Close all Claude agent clients |
| `model` | str | Passed to HeartbeatService | Expose from config |
| `tools.get("cron")` | \-> CronTool or None | Cron context management | Return `None` (isinstance checks skip gracefully) |
| `tools.get("message")` | \-> MessageTool or None | Check if msg already sent | Return `None` (isinstance checks skip gracefully) |
| `sessions.get_or_create(key)` | \-> Session | Heartbeat history trimming | Delegate to real SessionManager |
| `sessions.save(session)` | \-> None | Persist trimmed session | Delegate to real SessionManager |
| `sessions.list_sessions()` | \-> list\[dict\] | Heartbeat target routing | Delegate — SDK can also register active sessions here |

### Key decisions

`**tools**`: Expose a stub object where `.get()` always returns `None`. The cron and message tools are handled as MCP tools inside the SDK — the nanobot-level `CronTool`/`MessageTool` classes don't exist. The existing `isinstance` checks in `on_cron_job` gracefully skip when `.get()` returns `None`.

`**sessions**`: Use a real `SessionManager` for bookkeeping (which sessions exist, heartbeat trimming), even though the SDK manages LLM context internally. This gives `_pick_heartbeat_target` and heartbeat trimming the data they need without any changes to `commands.py`.

`**provider**`: Still created via `_make_provider(config)` even in Claude agent mode. It's used by `evaluate_response` in the cron callback and by `HeartbeatService` for its Phase 1 decision. The SDK manages its own LLM calls, but these auxiliary features need a provider.

**Cron callback**: The existing `on_cron_job` works as-is:

*   `agent.tools.get("cron")` returns `None` -> `isinstance(None, CronTool)` is `False` -> cron context block skipped
*   `agent.process_direct(...)` works (SDK implements it)
*   `agent.tools.get("message")` returns `None` -> `isinstance(None, MessageTool)` is `False` -> sent-in-turn check skipped
*   `evaluate_response(response, ..., provider, agent.model)` works (provider exists, model exists)
*   Net result: cron jobs work, delivery evaluation works, no code duplication

## Files changed

### Upstream files (minimal)

#### `nanobot/config/schema.py` (+2 lines)

```python
# In AgentDefaults:
engine: Literal["default", "claude_agent"] = "default"
```

#### `nanobot/cli/commands.py` (~20 lines changed)

`**gateway()**` **function** — constructor branch only:

```python
config = _load_runtime_config(config, workspace)
port = port if port is not None else config.gateway.port
use_claude_agent = config.agents.defaults.engine == "claude_agent"

# ... existing code ...

provider = _make_provider(config)           # always — needed for heartbeat/evaluator
session_manager = SessionManager(config.workspace_path)

if use_claude_agent:
    from nanobot.agent.claude_agent import ClaudeAgentLoop
    agent = ClaudeAgentLoop(
        bus=bus,
        provider=provider,
        config=config,
        cron_service=cron,
        session_manager=session_manager,
        channels_config=config.channels,
    )
else:
    agent = AgentLoop(
        bus=bus,
        provider=provider,
        # ... existing params unchanged ...
    )

# Everything below (on_cron_job, heartbeat, channels, run) is UNCHANGED
```

`**agent()**` **command** — same small constructor branch (~10 lines).

#### `pyproject.toml` (+3 lines)

```
sdk = [
    "claude-agent-sdk>=0.1.0",
]
```

### New files

#### `nanobot/agent/claude_agent.py` (~500-600 lines)

The Claude agent adapter. Major sections:

1.  `**_NullToolRegistry**` — stub with `.get()` returning `None`
2.  `**_build_nanobot_mcp_server()**` — in-process MCP server exposing `message` and `cron` tools to the SDK
3.  `**ClaudeAgentLoop**` — main class:
    *   `__init__`: accepts same conceptual params, sets `self.model`, `self.tools = _NullToolRegistry()`, `self.sessions = session_manager`
    *   `run()`: consume from bus, dispatch per-session with serialization
    *   `process_direct()`: same signature as AgentLoop
    *   `stop()`, `close_mcp()`: lifecycle management
    *   `_handle_slash_command()`: engine-aware /new, /stop, /status, /help, /restart
    *   `_process_message()`: core SDK interaction via `ClaudeSDKClient`

### Docker: Unified Dockerfile with build args

Replace `Dockerfile`, `Dockerfile.twilio`, and any future engine-specific Dockerfiles with a single parameterized `Dockerfile`. Build args control which optional components are installed. A single `docker-compose.yml` uses profiles to select variants.

#### Why

- One Dockerfile to maintain instead of 2-4
- Upstream changes to the base image only need updating in one place
- Adding a new feature (channel, engine) = adding a build arg, not a new file
- Compose profiles are the standard Docker mechanism for service variants

#### `Dockerfile` (replaces all existing Dockerfiles)

```dockerfile
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG INSTALL_NODE=false
ARG INSTALL_CLAUDE_CLI=false
ARG INSTALL_BRIDGE=false
ARG EXTRAS=""

# ── Base system deps (always) ──
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# ── Optional: Node.js (needed for WhatsApp bridge or Claude CLI) ──
RUN if [ "$INSTALL_NODE" = "true" ]; then \
      apt-get update && \
      apt-get install -y --no-install-recommends curl gnupg git openssh-client && \
      mkdir -p /etc/apt/keyrings && \
      curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | \
        gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
      echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list && \
      apt-get update && apt-get install -y --no-install-recommends nodejs && \
      apt-get purge -y gnupg && apt-get autoremove -y && \
      rm -rf /var/lib/apt/lists/*; \
    fi

# ── Optional: Claude CLI (requires Node.js) ──
RUN if [ "$INSTALL_CLAUDE_CLI" = "true" ]; then \
      npm install -g @anthropic-ai/claude-code; \
    fi

WORKDIR /app

# ── Python deps (cached layer) ──
COPY pyproject.toml README.md LICENSE ./
RUN mkdir -p nanobot bridge && touch nanobot/__init__.py && \
    if [ -n "$EXTRAS" ]; then \
      uv pip install --system --no-cache ".[$EXTRAS]"; \
    else \
      uv pip install --system --no-cache .; \
    fi && \
    rm -rf nanobot bridge

# ── App source ──
COPY nanobot/ nanobot/

# ── Optional: WhatsApp bridge ──
COPY bridge/ bridge/
RUN if [ "$INSTALL_BRIDGE" = "true" ]; then \
      git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" && \
      cd bridge && npm install && npm run build && cd ..; \
    fi

# ── Final install with source ──
RUN if [ -n "$EXTRAS" ]; then \
      uv pip install --system --no-cache ".[$EXTRAS]"; \
    else \
      uv pip install --system --no-cache .; \
    fi

RUN mkdir -p /root/.nanobot
EXPOSE 18790
ENTRYPOINT ["nanobot"]
CMD ["gateway"]
```

#### `docker-compose.yml` (replaces all compose files)

```yaml
x-common: &common
  build:
    context: .
    dockerfile: Dockerfile
  volumes:
    - ~/.nanobot:/root/.nanobot
  command: ["gateway"]
  restart: unless-stopped
  ports:
    - 18790:18790

services:
  # ── Twilio only, default engine (slim, no Node.js) ──
  nanobot-twilio:
    <<: *common
    profiles: [twilio]
    build:
      context: .
      args:
        EXTRAS: twilio
    deploy:
      resources:
        limits: { cpus: '1', memory: 512M }
        reservations: { cpus: '0.25', memory: 128M }

  # ── Twilio + Claude agent engine ──
  nanobot-twilio-claude:
    <<: *common
    profiles: [twilio-claude]
    build:
      context: .
      args:
        EXTRAS: "twilio,sdk"
        INSTALL_NODE: "true"
        INSTALL_CLAUDE_CLI: "true"
    volumes:
      - ~/.nanobot:/root/.nanobot
      - ~/.claude:/root/.claude
    deploy:
      resources:
        limits: { cpus: '2', memory: 1G }
        reservations: { cpus: '0.5', memory: 256M }

  # ── Full gateway (all channels + WhatsApp bridge) ──
  nanobot-gateway:
    <<: *common
    profiles: [gateway]
    build:
      context: .
      args:
        INSTALL_NODE: "true"
        INSTALL_BRIDGE: "true"
    deploy:
      resources:
        limits: { cpus: '1', memory: 1G }
        reservations: { cpus: '0.25', memory: 256M }

  # ── Full gateway + Claude agent engine ──
  nanobot-gateway-claude:
    <<: *common
    profiles: [gateway-claude]
    build:
      context: .
      args:
        INSTALL_NODE: "true"
        INSTALL_BRIDGE: "true"
        INSTALL_CLAUDE_CLI: "true"
    volumes:
      - ~/.nanobot:/root/.nanobot
      - ~/.claude:/root/.claude
    deploy:
      resources:
        limits: { cpus: '2', memory: 1G }
        reservations: { cpus: '0.5', memory: 256M }

  # ── CLI (interactive, any profile) ──
  nanobot-cli:
    build:
      context: .
    volumes:
      - ~/.nanobot:/root/.nanobot
    profiles: [cli]
    command: ["status"]
    stdin_open: true
    tty: true
```

Usage:

```bash
docker compose --profile twilio up -d           # what you run now
docker compose --profile twilio-claude up -d     # add Claude agent engine
docker compose --profile gateway up -d           # upstream-equivalent full build
docker compose --profile gateway-claude up -d    # full + Claude agent engine
```

#### Auth

The Claude agent engine requires Claude CLI auth. Two options:

1. **Max plan (recommended)**: Run `claude login` on the host. Mount `~/.claude:/root/.claude` in compose (already configured in the `-claude` profiles above). The CLI subprocess inherits the OAuth tokens.

2. **API key fallback**: Set `ANTHROPIC_API_KEY` as an environment variable in compose. Bypasses Max plan benefits but doesn't require `~/.claude` mount.

#### Layer caching notes

- Base + Python deps are shared across all variants (cached)
- `INSTALL_NODE` layer is shared between bridge and Claude CLI variants
- Switching between profiles (e.g. `twilio` to `twilio-claude`) rebuilds from the first divergent arg onwards
- Rebuilds of the *same* profile reuse cache normally

## Migration from v2 branch

1.  Start fresh from `main` (don't rebase `feature/sdk-engine-v2`)
2.  Replace `Dockerfile`, `Dockerfile.twilio`, `docker-compose.yml`, `docker-compose.twilio.yml` with unified versions
3.  Port `claude_agent.py` with the interface fixes (add `tools`, `sessions`)
4.  Apply the minimal `commands.py` and `schema.py` changes
5.  Add `sdk` extra to `pyproject.toml`
6.  Delete `readme-claude-agent.md` (was on v2 branch, not needed)

## Test plan

*   All existing 644 tests pass (no regressions)
*   `engine: "default"` (default) — gateway starts normally, all channels work
*   `engine: "claude_agent"` — gateway starts with Claude agent engine, processes messages via Claude CLI
*   Claude agent mode: cron jobs execute and deliver via `evaluate_response`
*   Claude agent mode: heartbeat finds target channel from session list
*   Claude agent mode: `/new`, `/stop`, `/status` slash commands work
*   Claude agent mode: message tool sends replies via bus (no duplicate responses)
*   Docker: `~/.claude` mount provides auth to Claude CLI subprocess
*   Docker: fallback to `ANTHROPIC_API_KEY` env var works

## Naming conventions

| Old (v2) | New (v3) |
| --- | --- |
| `engine: "legacy"` | `engine: "default"` |
| `engine: "sdk"` | `engine: "claude_agent"` |
| `SDKAgentLoop` | `ClaudeAgentLoop` |
| `nanobot/agent/sdk_adapter.py` | `nanobot/agent/claude_agent.py` |
| `use_sdk` | `use_claude_agent` |
| `feature/sdk-engine` | `feature/claude-agent-engine` |

## Risks

1.  **Claude CLI version**: SDK requires CLI >= 2.0.0. Must be installed and on PATH in Docker.
2.  **Auth token expiry**: OAuth tokens in `~/.claude` may expire. User needs to re-run `claude login` on host.
3.  **Subprocess overhead**: Each SDK session spawns a `claude` subprocess. Memory usage is higher than the legacy loop.
4.  **SDK API stability**: `claude-agent-sdk` is pre-1.0. Breaking changes are possible.