# SDK Engine v3: Minimal-Divergence Integration Plan

## Goal

Replace nanobot's legacy `AgentLoop` with Claude Agent SDK as a config-driven option, with **minimal changes to upstream files**. The SDK adapter lives in its own file; upstream code changes are limited to a constructor branch and a few guards.

## Background

### Previous attempt (`feature/sdk-engine-v2`)

The v2 branch modified ~250 lines of `commands.py`, duplicating entire functions (`on_cron_job`, heartbeat setup, `agent()` command) into `if use_sdk / else` branches. This creates high merge-conflict risk — any upstream change to cron/heartbeat needs updating in two places.

**Root cause**: `SDKAgentLoop` didn't implement the same interface as `AgentLoop`, so every call site that touched `agent.tools` or `agent.sessions` needed SDK-specific guards.

### This plan's approach

Make `SDKAgentLoop` expose the same public interface consumed by `commands.py`. Where the SDK handles something internally (sessions, tools), expose lightweight stubs or delegates so the calling code doesn't need to branch.

## Architecture

### How the SDK works

The Claude Agent SDK **spawns the `claude` CLI as a subprocess** (`SubprocessCLITransport`). Key implications:

1. **Auth**: The CLI reads OAuth tokens from `~/.claude/` (saved by `claude login`). No API key needed — it uses the logged-in session (e.g. Max plan).
2. **Tools**: The CLI has built-in tools (Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch). Custom tools are exposed via MCP servers.
3. **Sessions**: The SDK manages conversation context internally per `ClaudeSDKClient` instance. Nanobot's `SessionManager` (JSONL files) is not used for LLM context.
4. **Binary required**: The `claude` CLI must be installed in the runtime environment.

### Interface contract

`commands.py` uses these attributes/methods on the agent object:

| Member | Type | Usage | SDK strategy |
|--------|------|-------|--------------|
| `run()` | async | Main loop consuming from bus | Implement directly |
| `process_direct(content, session_key, channel, chat_id, on_progress, ...)` | async -> OutboundMessage? | Cron jobs, heartbeat, CLI | Implement directly |
| `stop()` | sync | Graceful shutdown | Implement directly |
| `close_mcp()` | async | Cleanup MCP connections | Close all SDK clients |
| `model` | str | Passed to HeartbeatService | Expose from config |
| `tools.get("cron")` | -> CronTool or None | Cron context management | Return `None` (isinstance checks skip gracefully) |
| `tools.get("message")` | -> MessageTool or None | Check if msg already sent | Return `None` (isinstance checks skip gracefully) |
| `sessions.get_or_create(key)` | -> Session | Heartbeat history trimming | Delegate to real SessionManager |
| `sessions.save(session)` | -> None | Persist trimmed session | Delegate to real SessionManager |
| `sessions.list_sessions()` | -> list[dict] | Heartbeat target routing | Delegate — SDK can also register active sessions here |

### Key decisions

1. **`tools`**: Expose a stub object where `.get()` always returns `None`. The cron and message tools are handled as MCP tools inside the SDK — the nanobot-level `CronTool`/`MessageTool` classes don't exist. The existing `isinstance` checks in `on_cron_job` gracefully skip when `.get()` returns `None`.

2. **`sessions`**: Use a real `SessionManager` for bookkeeping (which sessions exist, heartbeat trimming), even though the SDK manages LLM context internally. This gives `_pick_heartbeat_target` and heartbeat trimming the data they need without any changes to `commands.py`.

3. **`provider`**: Still created via `_make_provider(config)` even in SDK mode. It's used by `evaluate_response` in the cron callback and by `HeartbeatService` for its Phase 1 decision. The SDK manages its own LLM calls, but these auxiliary features need a provider.

4. **Cron callback**: The existing `on_cron_job` works as-is:
   - `agent.tools.get("cron")` returns `None` -> `isinstance(None, CronTool)` is `False` -> cron context block skipped
   - `agent.process_direct(...)` works (SDK implements it)
   - `agent.tools.get("message")` returns `None` -> `isinstance(None, MessageTool)` is `False` -> sent-in-turn check skipped
   - `evaluate_response(response, ..., provider, agent.model)` works (provider exists, model exists)
   - Net result: cron jobs work, delivery evaluation works, no code duplication

## Files changed

### Upstream files (minimal)

#### `nanobot/config/schema.py` (+2 lines)

```python
# In AgentDefaults:
engine: Literal["legacy", "sdk"] = "legacy"
```

#### `nanobot/cli/commands.py` (~20 lines changed)

**`gateway()` function** — constructor branch only:

```python
config = _load_runtime_config(config, workspace)
port = port if port is not None else config.gateway.port
use_sdk = config.agents.defaults.engine == "sdk"

# ... existing code ...

provider = _make_provider(config)           # always — needed for heartbeat/evaluator
session_manager = SessionManager(config.workspace_path)

if use_sdk:
    from nanobot.agent.sdk_adapter import SDKAgentLoop
    agent = SDKAgentLoop(
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

**`agent()` command** — same small constructor branch (~10 lines).

#### `pyproject.toml` (+3 lines)

```toml
sdk = [
    "claude-agent-sdk>=0.1.0",
]
```

### New files

#### `nanobot/agent/sdk_adapter.py` (~500-600 lines)

The SDK adapter. Major sections:

1. **`_NullToolRegistry`** — stub with `.get()` returning `None`
2. **`_build_nanobot_mcp_server()`** — in-process MCP server exposing `message` and `cron` tools to the SDK
3. **`SDKAgentLoop`** — main class:
   - `__init__`: accepts same conceptual params, sets `self.model`, `self.tools = _NullToolRegistry()`, `self.sessions = session_manager`
   - `run()`: consume from bus, dispatch per-session with serialization
   - `process_direct()`: same signature as AgentLoop
   - `stop()`, `close_mcp()`: lifecycle management
   - `_handle_slash_command()`: SDK-aware /new, /stop, /status, /help, /restart
   - `_process_message()`: core SDK interaction via `ClaudeSDKClient`

### Docker changes

#### `Dockerfile.sdk` (new) or extend existing Dockerfile

The SDK spawns `claude` CLI as a subprocess, so the Docker image needs:

1. **Node.js** (for `claude` CLI) — already in the main `Dockerfile`
2. **Claude CLI**: `npm install -g @anthropic-ai/claude-code`
3. **Mount `~/.claude`**: contains OAuth tokens from `claude login`

```dockerfile
# Add to Dockerfile or create Dockerfile.sdk:
RUN npm install -g @anthropic-ai/claude-code
```

#### `docker-compose.twilio.yml` / `docker-compose.yml`

Add volume mount for Claude auth:

```yaml
volumes:
  - ~/.nanobot:/root/.nanobot
  - ~/.claude:/root/.claude    # SDK auth tokens
```

**Important**: The user must run `claude login` on the host first. The saved OAuth tokens in `~/.claude/` are what the CLI subprocess uses to authenticate against the Max plan.

#### Alternative: API key auth

If running without a logged-in CLI session, the SDK can use an API key via environment:

```yaml
environment:
  - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
```

This bypasses Max plan benefits but doesn't require mounting `~/.claude`.

## Migration from v2 branch

1. Start fresh from `main` (don't rebase `feature/sdk-engine-v2`)
2. Port `sdk_adapter.py` with the interface fixes (add `tools`, `sessions`)
3. Apply the minimal `commands.py` changes (constructor branch only)
4. Delete `readme-claude-agent.md` (was on v2 branch, not needed)

## Test plan

- [ ] All existing 644 tests pass (no regressions)
- [ ] `engine: "legacy"` (default) — gateway starts normally, all channels work
- [ ] `engine: "sdk"` — gateway starts with SDK, processes messages via Claude CLI
- [ ] SDK mode: cron jobs execute and deliver via `evaluate_response`
- [ ] SDK mode: heartbeat finds target channel from session list
- [ ] SDK mode: `/new`, `/stop`, `/status` slash commands work
- [ ] SDK mode: message tool sends replies via bus (no duplicate responses)
- [ ] Docker: `~/.claude` mount provides auth to SDK subprocess
- [ ] Docker: fallback to `ANTHROPIC_API_KEY` env var works

## Risks

1. **Claude CLI version**: SDK requires CLI >= 2.0.0. Must be installed and on PATH in Docker.
2. **Auth token expiry**: OAuth tokens in `~/.claude` may expire. User needs to re-run `claude login` on host.
3. **Subprocess overhead**: Each SDK session spawns a `claude` subprocess. Memory usage is higher than the legacy loop.
4. **SDK API stability**: `claude-agent-sdk` is pre-1.0. Breaking changes are possible.
