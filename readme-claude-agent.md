# nanobot + Claude Agent SDK

This is a fork of [HKUDS/nanobot](https://github.com/HKUDS/nanobot) that adds two major features:

1. **Claude Agent SDK engine** — Replaces nanobot's custom agent loop with the same engine that powers Claude Code, using persistent `ClaudeSDKClient` instances per conversation
2. **Twilio WhatsApp channel** — Pure-Python, webhook-based WhatsApp integration via the official Twilio Business API (no Node.js bridge needed)

## Prerequisites

- Python 3.11+
- A Claude API key (`ANTHROPIC_API_KEY`) **or** a Claude Max subscription (authenticated via the SDK's OAuth flow)
- For Twilio WhatsApp: a Twilio account with WhatsApp enabled

## Installation

```bash
# Clone and install with SDK + Twilio support
git clone https://github.com/srinathh/nanobot.git
cd nanobot
git checkout feature/twilio-whatsapp
pip install -e ".[sdk,twilio]"
```

Or install just the SDK engine (no Twilio):

```bash
pip install -e ".[sdk]"
```

## Authentication

The Claude Agent SDK authenticates automatically:

- **API key**: Set `ANTHROPIC_API_KEY` in your environment — standard per-token billing
- **Claude Max**: If you've authenticated via the SDK's OAuth flow (`claude` CLI login), the SDK routes requests through your subscription — no per-token charges

No configuration needed in nanobot — the SDK detects available credentials.

## Configuration

Create or edit `~/.nanobot/config.json`:

### Minimal (SDK engine, CLI only)

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

### Full (SDK engine + Twilio WhatsApp)

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
  "channels": {
    "twilio_whatsapp": {
      "enabled": true,
      "accountSid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "authToken": "your_auth_token_here",
      "fromNumber": "whatsapp:+14155238886",
      "webhookPath": "/twilio/whatsapp",
      "webhookPort": 0,
      "validateSignature": false,
      "allowFrom": ["whatsapp:+1234567890"]
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
| `channels.twilio_whatsapp.accountSid` | `""` | Twilio Account SID |
| `channels.twilio_whatsapp.authToken` | `""` | Twilio Auth Token |
| `channels.twilio_whatsapp.fromNumber` | `""` | Your Twilio WhatsApp number (e.g. `whatsapp:+14155238886`) |
| `channels.twilio_whatsapp.webhookPath` | `"/twilio/whatsapp"` | URL path for Twilio webhooks |
| `channels.twilio_whatsapp.webhookPort` | `0` | Port for webhook server (0 = use gateway port) |
| `channels.twilio_whatsapp.validateSignature` | `false` | Verify `X-Twilio-Signature` header |
| `channels.twilio_whatsapp.allowFrom` | `[]` | Allowed sender numbers, or `["*"]` for all |

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

This starts the gateway on port 18790 (default) with all enabled channels. The Twilio WhatsApp webhook will listen at `http://your-server:18790/twilio/whatsapp`.

## Twilio WhatsApp Setup

### 1. Get Twilio credentials

- Sign up at [twilio.com](https://www.twilio.com/)
- Note your **Account SID** and **Auth Token** from the dashboard
- Enable the WhatsApp sandbox (for testing) or request a production WhatsApp number

### 2. Configure the webhook

In the Twilio Console, set your WhatsApp sandbox/number webhook to:

```
POST https://your-server:18790/twilio/whatsapp
```

For local development, use a tunnel:

```bash
# Using ngrok
ngrok http 18790

# Then set webhook to: https://xxxx.ngrok.io/twilio/whatsapp
```

### 3. Start the gateway

```bash
nanobot gateway
```

### 4. Send a message

Send a WhatsApp message to your Twilio number. The agent will process it and reply via the Twilio REST API.

## Slash Commands

These work in any channel (CLI, WhatsApp, Telegram, etc.):

| Command | Description |
|---------|-------------|
| `/new` | Start a fresh conversation (closes the current SDK client) |
| `/stop` | Interrupt the current task |
| `/status` | Show engine, model, and active session count |
| `/help` | List available commands |
| `/restart` | Restart the nanobot process |

## Architecture

```
User Message (WhatsApp/Telegram/CLI/...)
    ↓
Channel → MessageBus → SDKAgentLoop.run()
                            ↓
                   _get_or_create_client(channel:chat_id)
                            ↓
                   ClaudeSDKClient (persistent per session)
                     ├── Built-in: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch
                     ├── MCP tool: message → routes reply to correct channel
                     └── MCP tool: cron → schedules tasks via CronService
                            ↓
                   Response → MessageBus → Channel → User
```

- **One `ClaudeSDKClient` per conversation** — keyed by `channel:chat_id` (e.g. `twilio_whatsapp:whatsapp:+1234567890`)
- **Session continuity** — the client maintains conversation history internally; no manual session resume needed
- **`/new` resets** — closes the client; next message creates a fresh one
- **Custom tools via MCP** — `message` and `cron` tools are registered as an in-process MCP server, which is the SDK's mechanism for custom tools

## What's Different from Upstream

| Aspect | Upstream (legacy engine) | This fork (SDK engine) |
|--------|------------------------|----------------------|
| Agent loop | Custom Python (~500 lines) | Claude Agent SDK (`ClaudeSDKClient`) |
| LLM providers | 20+ via LiteLLM | Claude only (via SDK) |
| Built-in tools | Custom filesystem/shell/web | SDK native (Read, Write, Bash, etc.) |
| Sessions | JSONL files | SDK-managed (per client instance) |
| WhatsApp | Node.js bridge (Baileys) | Also: Twilio Business API (pure Python) |
| Config switch | N/A | `"engine": "legacy"` (default) |

The legacy engine is **not deleted** — set `engine: "legacy"` (or omit it) to use the original nanobot behavior. Both engines coexist.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key (if not using Max subscription) |
| `NANOBOT_MAX_CONCURRENT_REQUESTS` | Max parallel SDK queries (default: 3) |

## License

MIT — same as upstream nanobot.
