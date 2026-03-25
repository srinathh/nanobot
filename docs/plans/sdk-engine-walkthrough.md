# SDK Engine Branch Walkthrough: `feature/sdk-engine` vs `main`

## What is Nanobot?

Nanobot is an ultra-lightweight personal AI assistant framework (~4K lines of core code) from HKU's Data Intelligence Lab. It supports **16 chat platforms** (Telegram, Discord, Slack, WhatsApp, etc.), a flexible tool system, scheduled tasks, persistent memory, and 20+ LLM providers via LiteLLM.

---

## The Big Picture: What This Revision Does

The `feature/sdk-engine` branch introduces a **dual-engine architecture** — a single config flag (`"engine": "sdk"` vs `"engine": "legacy"`) lets you swap Nanobot's entire custom agent loop for the **Claude Agent SDK** (the same engine that powers Claude Code). This is captured in one commit:

```
140ac22 feat: add Claude Agent SDK engine as config-driven alternative to legacy loop
```

**5 files changed, 735 insertions, 90 deletions:**

| File | Change |
|------|--------|
| `nanobot/agent/sdk_adapter.py` | **NEW** — 565-line SDK adapter |
| `nanobot/cli/commands.py` | Modified — dual-engine wiring in `gateway` and `agent` commands |
| `nanobot/config/schema.py` | 1 line — new `engine` field on `AgentDefaults` |
| `pyproject.toml` | 3 lines — optional `[sdk]` dependency group |
| `.gitignore` | 1 line — ignore `docs/plans/` |

The design philosophy from the crafting journal is clear: **delete nothing, bypass via config**. The entire legacy codebase stays intact for upstream merge compatibility with `HKUDS/nanobot`.

---

## File-by-File Walkthrough

### 1. Config Toggle — `nanobot/config/schema.py`

A single line added to `AgentDefaults`:

```python
engine: Literal["legacy", "sdk"] = "legacy"
```

Default is `"legacy"`, so existing users see zero behavior change. To opt in:

```json
{
  "agents": {
    "defaults": {
      "engine": "sdk"
    }
  }
}
```

### 2. Dependency — `pyproject.toml`

A new optional dependency group:

```toml
[project.optional-dependencies]
sdk = ["claude-agent-sdk>=0.1.0"]
```

Install with `pip install nanobot-ai[sdk]`. The SDK is **not** a hard dependency — the adapter does a runtime import check and raises a clear error if missing.

### 3. The Core: `nanobot/agent/sdk_adapter.py` (565 lines)

This is the heart of the revision. It has four major sections:

#### a) SDK Availability Check (`_check_sdk_available`)

A guard that produces a helpful error message if you set `engine: "sdk"` but haven't installed the optional dependency.

#### b) In-Process MCP Server (`_build_nanobot_mcp_server`)

The SDK provides built-in tools (Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch), but Nanobot has **two custom tools** with no SDK equivalent:

- **`message` tool** — Routes messages + file attachments to any of the 16 chat channels via the MessageBus. Uses closure variables (`_msg_channel`, `_msg_chat_id`) that get updated per-message via `set_context()`.

- **`cron` tool** — Manages scheduled tasks (recurring via `every_seconds`, cron expressions, or one-shot `at` datetimes). Delegates to the existing `CronService` backend.

These are registered as `@tool` decorated functions on an in-process MCP server created via `create_sdk_mcp_server()`. Two helpers are monkey-patched onto the server object:

- `set_context(channel, chat_id, message_id)` — called before each query to set routing
- `was_sent_in_turn()` — checked after query to avoid duplicate replies

#### c) MCP Server Merging (`_build_mcp_servers_dict`)

Merges the in-process nanobot MCP server with any external MCP servers from config (command-based or URL-based transports). The SDK natively handles stdio, HTTP, and SSE MCP transports.

#### d) `SDKAgentLoop` Class — Drop-in Replacement

This class matches the public interface of the legacy `AgentLoop` (`run()`, `stop()`, `process_direct()`, `close_mcp()`):

- **`__init__`** — Sets up concurrency control (`NANOBOT_MAX_CONCURRENT_REQUESTS`, default 3), builds the MCP server graph, initializes the command router for priority commands (`/stop`, `/new`).

- **`_build_options()`** — Maps Nanobot config to `ClaudeAgentOptions`:

  ```python
  ClaudeAgentOptions(
      system_prompt=...,      # from ContextBuilder (identity + memory + skills)
      model=defaults.model,   # e.g. "anthropic/claude-opus-4-5"
      max_tokens=...,
      temperature=...,
      max_turns=...,          # maps from max_tool_iterations
      mcp_servers=...,        # merged nanobot + external servers
      allowed_tools=[         # SDK built-ins + all MCP tools
          "Read", "Write", "Edit", "Bash", "Glob", "Grep",
          "WebSearch", "WebFetch", "mcp__nanobot__*",
          ...external MCP wildcards...
      ],
      permission_mode="bypassPermissions",  # server-side, no human in loop
      cwd=str(workspace),
      resume=session_id,      # for session continuity
      thinking={...},         # if reasoning_effort is set
  )
  ```

- **`run()`** — Main loop consuming from `bus.consume_inbound()`. Priority commands are dispatched synchronously; regular messages are dispatched as async tasks.

- **`_dispatch(msg)`** — Per-session serialized processing with a global concurrency semaphore. Supports streaming via `on_stream`/`on_stream_end` callbacks that publish delta messages to the bus.

- **`_process_message(msg)`** — The actual SDK call:

  ```python
  async for message in query(prompt=msg.content, options=options):
      if AssistantMessage → stream text blocks
      if ResultMessage → capture final_content + session_id for resume
  ```

  After the query completes, it checks `was_sent_in_turn()` — if the `message` tool already routed a reply to the user's channel, it returns `None` to avoid duplication.

- **Session continuity** — `session_id` from `ResultMessage` is stored in `_session_ids[key]` and passed as `resume` on subsequent queries for the same session key.

### 4. CLI Wiring — `nanobot/cli/commands.py`

Both the `gateway` and `agent` commands follow the same pattern:

```python
use_sdk = config.agents.defaults.engine == "sdk"

if use_sdk:
    from nanobot.agent.sdk_adapter import SDKAgentLoop
    agent = SDKAgentLoop(bus=bus, config=config, cron_service=cron, ...)
else:
    from nanobot.agent.loop import AgentLoop
    provider = _make_provider(config)
    agent = AgentLoop(bus=bus, provider=provider, workspace=..., ...)
```

Key branching points:

- **Cron callbacks** — SDK path is simpler (no `CronTool.set_cron_context`, no `evaluate_response` LLM call to decide notification worthiness — it just sends)
- **Heartbeat** — SDK mode creates a separate LiteLLM provider just for heartbeat's decision phase, or disables heartbeat entirely if no provider is available
- **Session management** — Skipped in SDK mode; the SDK manages its own sessions internally

---

## What Gets Replaced vs Kept

| Replaced by SDK | Kept (no SDK equivalent) |
|---|---|
| `agent/loop.py` — custom agent loop | `bus/*` — async message routing |
| `providers/*` — LiteLLM multi-provider | `channels/*` — 16 chat platforms |
| `agent/tools/filesystem.py` | `agent/tools/message.py` (ported to `@tool`) |
| `agent/tools/shell.py` | `agent/tools/cron.py` (ported to `@tool`) |
| `agent/tools/web.py` | `agent/context.py` — system prompt assembly |
| `agent/tools/mcp.py` | `agent/memory.py` — persistent memory |
| `session/*` — JSONL history | `agent/skills.py` — markdown skill packs |
| `security/network.py` | `config/*`, `cli/*`, `cron/*`, `heartbeat/*` |

---

## How Claude Max Plan is Accessed by the Agent SDK

### The Claude Agent SDK Authentication Model

The Claude Agent SDK (`claude-agent-sdk`) calls the **Claude API directly** — it doesn't go through LiteLLM or any other proxy. When you call `query(prompt, options)`, the SDK authenticates with Anthropic's API using one of two mechanisms:

1. **API Key** (`ANTHROPIC_API_KEY` env var) — Standard API billing, pay-per-token
2. **Claude Max subscription** — The SDK supports OAuth-based authentication tied to a Claude Max (or Claude Pro/Team) subscription, where the user's **subscription plan** provides the token budget rather than per-call billing

### How Nanobot Connects to Claude Max

In the `SDKAgentLoop._build_options()` method:

```python
ClaudeAgentOptions(
    model=defaults.model,          # e.g. "anthropic/claude-opus-4-5"
    permission_mode="bypassPermissions",
    ...
)
```

The SDK resolves the model and authentication **internally**. The flow is:

1. **Config sets the model** — `config.agents.defaults.model` (e.g., `"anthropic/claude-opus-4-5"` or `"claude-sonnet-4-6"`)
2. **SDK checks for credentials** — It looks for `ANTHROPIC_API_KEY` or an existing OAuth session (from `claude-agent-sdk` CLI login, which authenticates against Claude Max)
3. **Claude Max path** — If the user has authenticated via OAuth (i.e., logged into their Claude Max account through the SDK's auth flow), the SDK routes requests through the **subscription-backed endpoint** rather than the metered API. This means:
   - No per-token charges
   - Usage counts against the Max plan's rate limits
   - The same models are available (Opus, Sonnet, Haiku)
4. **`bypassPermissions` mode** — Since Nanobot is a server-side agent (not interactive), it sets `permission_mode="bypassPermissions"` so the SDK doesn't prompt for tool approval

### What Nanobot Gives Up for This

The trade-off is explicit in the crafting journal:

- **Lost**: Multi-provider support (no more OpenAI, DeepSeek, Groq, etc. via LiteLLM)
- **Gained**: Battle-tested agent loop, built-in tools, session management, streaming, retries, prompt caching — all from the same engine as Claude Code
- **Claude Max benefit**: Users on a Max subscription can run Nanobot's 16-channel agent **without per-token API costs**, using their subscription allowance

### The Practical Setup

To use Nanobot with Claude Max:

```bash
# Install with SDK support
pip install nanobot-ai[sdk]

# Authenticate with Claude Max account (SDK handles OAuth)
# This stores credentials that the SDK picks up automatically

# Configure nanobot to use SDK engine
# In config.json:
{
  "agents": {
    "defaults": {
      "engine": "sdk",
      "model": "claude-sonnet-4-6"
    }
  }
}

# Run
nanobot gateway   # or: nanobot agent
```

The SDK handles all the authentication plumbing — Nanobot just passes the model name and the SDK figures out whether to use API key billing or Max subscription based on available credentials.

---

## Summary

This is a clean, non-destructive architectural change that adds a second engine option to Nanobot. The legacy engine (custom loop + LiteLLM + 20+ providers) remains untouched for upstream compatibility. The new SDK engine replaces ~1,300 lines of custom agent loop, tool framework, session management, and provider code with calls to `claude_agent_sdk.query()`, while preserving Nanobot's unique value: its 16-channel message bus, cron scheduling, persistent memory, markdown skills, and rich CLI. The Claude Max integration comes "for free" through the SDK's built-in authentication, letting subscription users run a full multi-channel AI agent without per-token costs.
