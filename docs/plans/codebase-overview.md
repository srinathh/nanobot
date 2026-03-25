# Nanobot Codebase Overview

Nanobot is an **ultra-lightweight personal AI assistant** (~4K lines of core code) from HKU's Data Intelligence Lab. It replicates core agent functionality with multi-channel chat integration.

## Architecture at a Glance

```
User Input (CLI / Chat Channel)
    ↓
  MessageBus (async queues - decouples channels from agent)
    ↓
  AgentLoop (core engine) — OR — SDKAgentLoop (Claude Agent SDK engine)
    ├── ContextBuilder → system prompt (identity, memory, skills)
    ├── LLM Provider → chat completion (streaming + retry)       [legacy only]
    ├── Claude Agent SDK query() → built-in tools + custom @tool  [sdk only]
    ├── ToolRegistry → execute tool calls concurrently            [legacy only]
    ├── MemoryConsolidator → summarize when near token limit
    └── Session → persist conversation as JSONL                   [legacy only]
    ↓
  OutboundMessage → ChannelManager → correct platform
```

## Engine Modes

- **`engine: "legacy"`** (default) — Custom Python agent loop with LiteLLM (20+ providers)
- **`engine: "sdk"`** — Claude Agent SDK powers the loop; nanobot provides channels, bus, config, custom tools via MCP

## Key Modules

| Directory | Purpose |
|-----------|---------|
| `nanobot/agent/loop.py` | Legacy agent loop |
| `nanobot/agent/sdk_adapter.py` | SDK engine adapter (new) |
| `nanobot/agent/tools/` | Built-in tools: shell, filesystem, web search/fetch, messaging, MCP, cron, spawn |
| `nanobot/providers/` | LLM abstraction via LiteLLM (legacy engine only) |
| `nanobot/channels/` | 16 chat platforms — Telegram, Discord, WhatsApp, Slack, WeChat, Email, Matrix, etc. |
| `nanobot/bus/` | Async message bus decoupling channels from agent |
| `nanobot/session/` | Per-chat conversation history (JSONL storage, legacy engine only) |
| `nanobot/agent/memory.py` | Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (searchable log) |
| `nanobot/cron/` | Scheduled tasks (cron expressions, intervals, one-shot) |
| `nanobot/heartbeat/` | Periodic wake-up to check for due scheduled tasks |
| `nanobot/skills/` | Markdown-based skill packs that teach the agent capabilities |
| `nanobot/config/` | Pydantic config schema, loader, path resolution |
| `nanobot/cli/` | Typer CLI — `onboard`, `agent` (interactive), `gateway` (multi-channel server) |
| `nanobot/security/` | SSRF protection, shell command deny-lists |
| `bridge/` | Node.js WhatsApp Web bridge |

## Two Run Modes

- **`nanobot agent`** — Interactive CLI chat with streaming responses
- **`nanobot gateway`** — Long-running server connecting all enabled chat channels

## Key Design Choices

- **Async throughout** — `asyncio` with per-session serialization, cross-session concurrency
- **Config-driven engine** — Switch between legacy loop and SDK via config, no code changes
- **Upstream compatible** — No files deleted from upstream; SDK path added alongside legacy
- **Append-only sessions** — optimized for Anthropic prompt caching (legacy)
- **Memory consolidation** — LLM summarizes conversations into MEMORY.md when nearing context limits
- **Plugin-friendly channels** — discovered via `pkgutil` + `entry_points`
- **MCP support** — Model Context Protocol for external tool servers
