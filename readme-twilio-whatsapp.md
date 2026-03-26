# nanobot + Twilio WhatsApp Channel

This branch adds a **Twilio WhatsApp channel** to [nanobot](https://github.com/HKUDS/nanobot) — a pure-Python, webhook-based alternative to the existing Node.js Baileys bridge.

| Aspect | Existing WhatsApp (Baileys) | This channel (Twilio) |
|--------|---------------------------|----------------------|
| Protocol | Unofficial WhatsApp Web | Official Twilio Business API |
| Runtime | Node.js bridge + WebSocket | Pure Python (aiohttp webhook) |
| Auth | QR code scan (personal account) | API key (business account) |
| Setup | Complex (Node.js, npm build, QR) | Simple (API keys, webhook URL) |
| Cost | Free (unofficial) | Twilio per-message pricing |
| Reliability | Depends on reverse-engineering | Official API, SLA-backed |

Both channels coexist — the new one is named `twilio_whatsapp` to avoid conflicts.

## Prerequisites

- Python 3.11+
- A Twilio account with WhatsApp enabled
- Any nanobot-supported LLM provider (Anthropic, OpenAI, DeepSeek, etc.)

## Installation

```bash
git clone https://github.com/srinathh/nanobot.git
cd nanobot
git checkout feature/twilio-whatsapp-channel
pip install -e ".[twilio]"
```

## Twilio Account Setup

1. Sign up at [twilio.com](https://www.twilio.com/)
2. Note your **Account SID** and **Auth Token** from the Twilio Console dashboard
3. For testing: activate the [WhatsApp sandbox](https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn)
4. For production: request a dedicated WhatsApp number

## Configuration

Edit `~/.nanobot/config.json`:

```json
{
  "agents": {
    "defaults": {
      "model": "anthropic/claude-sonnet-4-6"
    }
  },
  "providers": {
    "anthropic": {
      "apiKey": "sk-ant-..."
    }
  },
  "channels": {
    "twilio_whatsapp": {
      "enabled": true,
      "accountSid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "authToken": "your_auth_token_here",
      "fromNumber": "whatsapp:+14155238886",
      "webhookPath": "/twilio/whatsapp",
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
| `accountSid` | `""` | Twilio Account SID |
| `authToken` | `""` | Twilio Auth Token |
| `fromNumber` | `""` | Twilio WhatsApp number (e.g. `whatsapp:+14155238886`) |
| `webhookPath` | `"/twilio/whatsapp"` | URL path for incoming Twilio webhooks |
| `webhookPort` | `0` | Port for webhook server (0 = use gateway port) |
| `validateSignature` | `false` | Verify `X-Twilio-Signature` header for security |
| `allowFrom` | `[]` | Allowed sender numbers (e.g. `["whatsapp:+1234567890"]`), or `["*"]` for all |
| `groupPolicy` | `"open"` | `"open"` responds to all group messages, `"mention"` only when @mentioned |

## Running

### 1. Start the gateway

```bash
nanobot gateway
```

### 2. Expose the webhook

Twilio needs a public URL to send webhooks to. For local development:

```bash
ngrok http 18790
```

### 3. Configure Twilio webhook

In the Twilio Console, set your WhatsApp sandbox/number webhook to:

```
POST https://xxxx.ngrok.io/twilio/whatsapp
```

For production, point to your server's public URL:

```
POST https://your-server.com:18790/twilio/whatsapp
```

### 4. Send a message

Send a WhatsApp message to your Twilio number. The agent processes it and replies via the Twilio REST API.

## How It Works

```
Twilio Cloud
    |
    |-- POST /twilio/whatsapp --> TwilioWhatsAppChannel (aiohttp)
    |                                |
    |                                |-- validate signature (optional)
    |                                |-- check allowFrom
    |                                |-- download media (if any)
    |                                +-- _handle_message() --> MessageBus --> AgentLoop
    |
    +-- <-- twilio.messages.create() <-- send() <-- MessageBus (reply)
```

- **Inbound**: Twilio POSTs form-encoded webhook data; the channel parses sender, body, and media
- **Media**: Attachments are downloaded via authenticated HTTP (Basic Auth with Account SID / Auth Token) to temp files
- **Outbound**: Replies sent via the Twilio REST API; messages over 1600 chars are automatically split
- **Access control**: Standard nanobot `allowFrom` list — works like all other channels

## Features

- Automatic media download with Twilio Basic Auth
- Message splitting at Twilio's 1600-character limit
- Optional `X-Twilio-Signature` validation for webhook security
- Health check endpoint at `GET /health`
- Auto-discovered by nanobot's channel registry (no code changes needed)
- Works with any nanobot LLM provider (not limited to Claude)

## License

MIT — same as upstream nanobot.
