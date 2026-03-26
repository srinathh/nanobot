# nanobot + Claude Agent SDK

This branch adds a **Claude Agent SDK engine** to [nanobot](https://github.com/HKUDS/nanobot) — replacing nanobot's custom agent loop with the same engine that powers Claude Code, using persistent `ClaudeSDKClient` instances per conversation.

A single config flag (`"engine": "sdk"`) switches between the legacy engine and the SDK. No files are deleted — both engines coexist.

## Prerequisites

- Python 3.11+
- A Claude API key (`ANTHROPIC_API_KEY`) **or** a Claude Max subscription

## Installation

```bash
git clone https://github.com/srinathh/nanobot.git
cd nanobot
git checkout feature/sdk-engine-v2
pip install -e ".[sdk]"
```

## Authentication

The Claude Agent SDK authenticates automatically:

- **API key**: Set `ANTHROPIC_API_KEY` in your environment — standard per-token billing
- **Claude Max**: If you've authenticated via the SDK's OAuth flow (`claude` CLI login), the SDK routes requests through your subscription — no per-token charges

No configuration needed in nanobot — the SDK detects available credentials.

## Configuration

Create or edit `~/.nanobot/config.json`:

### Minimal

```json
{
  "agents": {
    "defaults": {
      "engine": "sdk",
      "model": "claude-sonnet-4-6"
    }
  }
}
```

### Full

```json
{
  "agents": {
    "defaults": {
      "engine": "sdk",
      "model": "claude-sonnet-4-6",
      "maxTokens": 8192,
      "temperature": 0.1,
      "maxToolIterations": 40
    }
  },
  "gateway": {
    "port": 18790
  }
}
```

### Configuration Reference

| Field | Default | Description |
|-------|---------|-------------|
| `agents.defaults.engine` | `"legacy"` | Set to `"sdk"` to use Claude Agent SDK |
| `agents.defaults.model` | `"anthropic/claude-opus-4-5"` | Any Claude model ID |
| `agents.defaults.maxTokens` | `8192` | Max output tokens per response |
| `agents.defaults.temperature` | `0.1` | Sampling temperature |
| `agents.defaults.maxToolIterations` | `40` | Max agent turns before stopping |
| `agents.defaults.reasoningEffort` | `null` | Set to `"low"`, `"medium"`, or `"high"` to enable thinking |

## Running

### Interactive CLI

```bash
nanobot agent
```

You'll see `Engine: Claude Agent SDK` printed on startup. Type messages and get responses.

### Single message

```bash
nanobot agent -m "What files are in this directory?"
```

### Gateway (multi-channel server)

```bash
nanobot gateway
```

Starts the gateway on port 18790 with all enabled channels (Telegram, Discord, Slack, WhatsApp, etc.). Each channel conversation gets its own persistent `ClaudeSDKClient`.

## Slash Commands

| Command | Description |
|---------|-------------|
| `/new` | Start a fresh conversation (closes the current SDK client) |
| `/stop` | Interrupt the current task |
| `/status` | Show engine, model, and active session count |
| `/help` | List available commands |
| `/restart` | Restart the nanobot process |

## Architecture

```
User Message (Telegram/CLI/Discord/...)
    |
Channel --> MessageBus --> SDKAgentLoop.run()
                              |
                   _get_or_create_client(channel:chat_id)
                              |
                   ClaudeSDKClient (persistent per session)
                     |-- Built-in: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch
                     |-- MCP tool: message --> routes reply to correct channel
                     +-- MCP tool: cron --> schedules tasks via CronService
                              |
                   Response --> MessageBus --> Channel --> User
```

- **One `ClaudeSDKClient` per conversation** — keyed by `channel:chat_id` (e.g. `telegram:12345`)
- **Session continuity** — the client maintains conversation history internally; no manual session resume needed
- **`/new` resets** — closes the client; next message creates a fresh one
- **Custom tools via MCP** — `message` and `cron` tools are registered as an in-process MCP server, which is the SDK's required mechanism for custom tools

## What's Different from Upstream

| Aspect | Upstream (legacy engine) | This branch (SDK engine) |
|--------|------------------------|----------------------|
| Agent loop | Custom Python (~500 lines) | Claude Agent SDK (`ClaudeSDKClient`) |
| LLM providers | 20+ via LiteLLM | Claude only (via SDK) |
| Built-in tools | Custom filesystem/shell/web | SDK native (Read, Write, Bash, etc.) |
| Sessions | JSONL files | SDK-managed (per client instance) |
| Config switch | N/A | `"engine": "legacy"` (default) |

The legacy engine is **not deleted** — set `engine: "legacy"` (or omit it) to use the original nanobot behavior.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key (if not using Max subscription) |
| `NANOBOT_MAX_CONCURRENT_REQUESTS` | Max parallel SDK queries (default: 3) |

## License

MIT — same as upstream nanobot.
