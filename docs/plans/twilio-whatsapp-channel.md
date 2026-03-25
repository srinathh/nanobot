# Plan: Twilio WhatsApp Channel for Nanobot

## Context

Nanobot already has a WhatsApp channel (`channels/whatsapp.py`) that uses a **Node.js bridge** based on `@whiskeysockets/baileys` (unofficial WhatsApp Web protocol). This works but requires:
- Node.js 18+ runtime
- Building a TypeScript bridge (`npm install && npm run build`)
- QR code scanning for authentication (personal WhatsApp account)
- WebSocket communication between Python and Node.js

The Twilio WhatsApp channel takes a fundamentally different approach: it uses the **official Twilio WhatsApp Business API**, which means:
- No bridge process, no Node.js dependency
- Webhook-based (Twilio POSTs to your server)
- API-key authenticated (no QR codes)
- Business-grade delivery, media handling, and compliance

This plan is based on a working Twilio WhatsApp integration from the `srinathh/personal-agents` repository, adapted to nanobot's `BaseChannel` architecture.

---

## Source Reference: personal-agents Integration

The personal-agents repo has two key files:

### `app/server.py` — FastAPI Webhook Server
- **Webhook endpoint** (`POST /whatsapp`): Receives Twilio form-encoded webhooks, extracts `From`, `Body`, `ProfileName`, `MessageSid`, and media URLs
- **Number allowlist**: Rejects messages from numbers not in `ALLOWED_WHATSAPP_NUMBERS`
- **Fire-and-forget processing**: Spawns background `asyncio.Task` per message, returns TwiML immediately
- **Per-user locks**: `defaultdict(asyncio.Lock)` serializes agent calls for the same user
- **Media handling**: Downloads Twilio media attachments via authenticated HTTP, base64-encodes images into `HumanMessage` content parts
- **Reminder endpoint** (`POST /reminders/check`): Cron-driven deadline checker that sends WhatsApp messages

### `app/twilio_client.py` — Twilio REST Client
- **`send_whatsapp_message(to, body)`**: Sends via `twilio_client.messages.create()` with `from_=TWILIO_WHATSAPP_FROM`
- **`download_media_files(media_urls)`**: Downloads Twilio media using Basic Auth (`TWILIO_ACCOUNT_SID:TWILIO_AUTH_TOKEN`), saves to temp files

### Key Design Patterns
1. **Webhook receives, REST API sends** — Twilio POSTs inbound messages; outbound messages use the REST API
2. **Immediate TwiML ack** — Webhook returns a TwiML `<Response><Message>` immediately; actual agent reply comes later via REST
3. **Media requires auth** — Twilio media URLs need Basic Auth with account credentials
4. **Phone numbers as identity** — `From` field (e.g. `whatsapp:+1234567890`) serves as both sender_id and chat_id

---

## Proposed Design for Nanobot

### New File: `nanobot/channels/twilio_whatsapp.py`

A `BaseChannel` subclass that:
1. Runs an embedded HTTP server (using `aiohttp`) to receive Twilio webhooks
2. Sends outbound messages via the Twilio REST API
3. Downloads and forwards media attachments
4. Follows the same patterns as existing nanobot channels (Telegram, Discord, etc.)

### Architecture

```
Twilio Cloud
    │
    ├── POST /twilio/whatsapp ──→ TwilioWhatsAppChannel (aiohttp server)
    │                                    │
    │                                    ├── validate signature (optional)
    │                                    ├── check is_allowed(sender)
    │                                    ├── download media (if any)
    │                                    └── _handle_message() ──→ MessageBus ──→ AgentLoop
    │
    └── ←── twilio_client.messages.create() ←── send(OutboundMessage) ←── MessageBus
```

### Configuration Schema

```python
class TwilioWhatsAppConfig(Base):
    """Twilio WhatsApp channel configuration."""

    enabled: bool = False
    account_sid: str = ""           # TWILIO_ACCOUNT_SID
    auth_token: str = ""            # TWILIO_AUTH_TOKEN
    from_number: str = ""           # e.g. "whatsapp:+14155238886"
    webhook_path: str = "/twilio/whatsapp"  # path on gateway server
    validate_signature: bool = True # verify X-Twilio-Signature header
    allow_from: list[str] = []     # allowlist of whatsapp:+... numbers, or ["*"]
```

Note: This channel **reuses the gateway's existing HTTP server** (port from `config.gateway.port`, default 18790) rather than spinning up its own. The `webhook_path` is registered as a route on the gateway's `aiohttp` app.

**Alternative**: If the gateway doesn't expose an app for route registration, the channel runs its own `aiohttp` server on a configurable port (like the webhook example in the plugin guide).

### Config Example

```json
{
  "channels": {
    "twilio_whatsapp": {
      "enabled": true,
      "accountSid": "ACxxxxxxxxxxxx",
      "authToken": "your_auth_token",
      "fromNumber": "whatsapp:+14155238886",
      "webhookPath": "/twilio/whatsapp",
      "validateSignature": true,
      "allowFrom": ["whatsapp:+1234567890"]
    }
  }
}
```

### Implementation Details

#### Channel Class Structure

```python
class TwilioWhatsAppChannel(BaseChannel):
    name = "twilio_whatsapp"
    display_name = "Twilio WhatsApp"

    def __init__(self, config, bus):
        # Parse config into TwilioWhatsAppConfig
        # Initialize Twilio REST client
        # Set up media download httpx client

    async def start(self):
        # Start aiohttp server with webhook route
        # Block until stopped (standard BaseChannel pattern)

    async def stop(self):
        # Cleanup aiohttp runner, close httpx client

    async def send(self, msg: OutboundMessage):
        # Send text via Twilio REST API
        # Send media files (upload or URL-based)

    async def _handle_webhook(self, request):
        # Parse Twilio form data
        # Optionally validate X-Twilio-Signature
        # Download media attachments if present
        # Call self._handle_message()
        # Return TwiML response
```

#### Webhook Handler (`_handle_webhook`)

Mapped from personal-agents `whatsapp_webhook`:

```python
async def _handle_webhook(self, request: web.Request) -> web.Response:
    form = await request.post()

    # Optional: Validate Twilio signature
    if self._validate_signature:
        signature = request.headers.get("X-Twilio-Signature", "")
        url = str(request.url)
        if not self._request_validator.validate(url, dict(form), signature):
            return web.Response(status=403, text="Invalid signature")

    sender = form.get("From", "")          # "whatsapp:+1234567890"
    body = form.get("Body", "")
    message_sid = form.get("MessageSid", "")
    profile_name = form.get("ProfileName", "")

    # Download media attachments
    num_media = int(form.get("NumMedia", "0"))
    media_files = []
    if num_media > 0:
        media_urls = [form[f"MediaUrl{i}"] for i in range(num_media)]
        media_files = await self._download_media(media_urls)

    # Append media tags to content (matching nanobot's existing whatsapp.py pattern)
    content = body
    for fpath in media_files:
        mime, _ = mimetypes.guess_type(fpath)
        tag = "image" if mime and mime.startswith("image/") else "file"
        content = f"{content}\n[{tag}: {fpath}]" if content else f"[{tag}: {fpath}]"

    # sender_id is the phone number (strip "whatsapp:" prefix for display)
    sender_id = sender.replace("whatsapp:", "")

    await self._handle_message(
        sender_id=sender_id,
        chat_id=sender,       # Keep full "whatsapp:+..." for Twilio replies
        content=content,
        media=media_files,
        metadata={
            "message_sid": message_sid,
            "profile_name": profile_name,
        },
    )

    # Return TwiML ack (empty response — reply comes via REST API)
    return web.Response(
        text='<Response></Response>',
        content_type="application/xml",
    )
```

#### Media Download (`_download_media`)

Mapped from personal-agents `download_media_files`:

```python
async def _download_media(self, media_urls: list[str]) -> list[str]:
    """Download Twilio media with Basic Auth to temp files."""
    paths = []
    async with httpx.AsyncClient(
        auth=(self._account_sid, self._auth_token),
        follow_redirects=True,
    ) as client:
        for url in media_urls:
            resp = await client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "application/octet-stream")
            ext = mimetypes.guess_extension(content_type) or ".bin"
            fd, path = tempfile.mkstemp(suffix=ext, prefix="twilio_wa_")
            os.write(fd, resp.content)
            os.close(fd)
            paths.append(path)
    return paths
```

#### Outbound Messages (`send`)

Mapped from personal-agents `send_whatsapp_message`, adapted for nanobot's `OutboundMessage`:

```python
async def send(self, msg: OutboundMessage) -> None:
    """Send message via Twilio REST API."""
    if not msg.content and not msg.media:
        return

    chat_id = msg.chat_id  # "whatsapp:+1234567890"

    # Send text
    if msg.content:
        # Twilio has a 1600 char limit per message — split if needed
        for chunk in _split_message(msg.content, max_len=1600):
            self._twilio_client.messages.create(
                from_=self._from_number,
                to=chat_id,
                body=chunk,
            )

    # Send media files
    for media_path in msg.media or []:
        # Twilio requires publicly accessible URLs for media
        # Option A: Upload to a temporary hosting service
        # Option B: Use Twilio's media upload API
        # Option C: Base64 encode (not supported by Twilio)
        # For now: skip if local path, log warning
        logger.warning(
            "Twilio WhatsApp media send for local files not yet implemented: {}",
            media_path,
        )
```

**Media sending note**: Twilio requires publicly accessible URLs for outbound media. This is a key difference from the existing WhatsApp bridge which can send local files directly. Options:
1. Skip outbound media initially (text-only replies)
2. Integrate with a file hosting service (S3, Cloudflare R2)
3. Serve media from the gateway's own HTTP server with temporary URLs

### Dependencies

Add to `pyproject.toml`:

```toml
[project.optional-dependencies]
twilio = [
    "twilio>=9.0.0",
    "aiohttp>=3.9.0",
]
```

Core dependencies (`httpx`) are already in nanobot's requirements.

### Key Differences from Existing WhatsApp Channel

| Aspect | Existing (`whatsapp.py`) | New (`twilio_whatsapp.py`) |
|--------|-------------------------|---------------------------|
| Protocol | Baileys (unofficial WA Web) | Twilio Business API (official) |
| Runtime | Node.js bridge + WebSocket | Pure Python, webhook-based |
| Auth | QR code scan (personal account) | API key (business account) |
| Media inbound | Bridge downloads, sends paths | Download from Twilio with Basic Auth |
| Media outbound | Send local files via bridge | Requires public URLs |
| Group support | Yes (with mention policy) | Yes (Twilio group messaging) |
| Cost | Free (unofficial) | Twilio per-message pricing |
| Reliability | Depends on Baileys reverse-engineering | Official API, SLA-backed |
| Setup | Complex (Node.js, npm build, QR) | Simple (API keys, webhook URL) |

### Naming: Why `twilio_whatsapp` not `whatsapp`

The existing `whatsapp.py` channel uses the Baileys bridge. Rather than replacing it, we add a **separate channel** named `twilio_whatsapp`. Users can enable either or both. This follows the upstream compatibility principle from the SDK engine plan.

---

## Implementation Steps

### Step 1: Create `nanobot/channels/twilio_whatsapp.py`

- `TwilioWhatsAppConfig` Pydantic model
- `TwilioWhatsAppChannel(BaseChannel)` with `start()`, `stop()`, `send()`
- Webhook handler with optional Twilio signature validation
- Media download helper using `httpx` with Basic Auth
- Message splitting for Twilio's 1600-char limit

### Step 2: Add optional dependency

```toml
# pyproject.toml
[project.optional-dependencies]
twilio = ["twilio>=9.0.0"]
```

`aiohttp` is already used by other channels. `httpx` is already a core dependency.

### Step 3: Auto-discovery (zero config needed)

The channel auto-discovers via `nanobot/channels/registry.py` — it scans all modules in `nanobot/channels/`, finds `TwilioWhatsAppChannel` as a `BaseChannel` subclass, and registers it under `twilio_whatsapp`. No changes to registry needed.

### Step 4: Guard the Twilio import

Like other optional channels, wrap the `twilio` import so the channel is silently skipped if the package isn't installed:

```python
try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator
except ImportError:
    TwilioClient = None
    RequestValidator = None
```

### Step 5: Documentation

Update `docs/CHANNEL_PLUGIN_GUIDE.md` or add a section to README covering:
- Twilio account setup (Account SID, Auth Token, WhatsApp sandbox or production number)
- Webhook URL configuration (pointing Twilio to `https://your-server:18790/twilio/whatsapp`)
- Config example

---

## Verification

1. **Unit test**: Mock Twilio webhook POST, verify `_handle_message` is called with correct params
2. **Integration test with Twilio sandbox**:
   - Configure Twilio sandbox webhook to point to ngrok/tunnel
   - Send WhatsApp message to sandbox number
   - Verify agent receives message, processes it, and replies via REST API
3. **Media test**: Send image via WhatsApp, verify download and content tagging
4. **Allowlist test**: Send from unauthorized number, verify 403 rejection
5. **Signature validation test**: Send request with invalid signature, verify rejection

---

## What's Adapted from personal-agents vs What's New

| From personal-agents | Adapted for nanobot |
|---------------------|---------------------|
| `whatsapp_webhook()` FastAPI handler | `_handle_webhook()` aiohttp handler on BaseChannel |
| `ALLOWED_WHATSAPP_NUMBERS` env var | `allow_from` config list (BaseChannel standard) |
| `run_agent()` background task with LangGraph | `_handle_message()` → MessageBus → AgentLoop (nanobot standard) |
| `send_whatsapp_message()` simple wrapper | `send(OutboundMessage)` with message splitting |
| `download_media_files()` httpx + temp files | `_download_media()` (same pattern, integrated into channel) |
| `_build_human_message()` base64 encoding | Media paths + content tags (nanobot convention) |
| `_thread_locks` per-user serialization | Handled by AgentLoop/SDKAgentLoop (per-session locks) |
| `_background_tasks` set for GC prevention | Handled by AgentLoop task management |
| `/reminders/check` cron endpoint | Handled by nanobot's CronService + cron tool |

---

## Summary

This plan adds a Twilio-based WhatsApp channel to nanobot as a pure-Python, webhook-driven alternative to the existing Baileys bridge channel. It adapts a proven integration from `srinathh/personal-agents`, mapping its FastAPI + LangGraph patterns onto nanobot's `BaseChannel` + `MessageBus` architecture. The channel is self-contained in a single file, auto-discovered by the registry, and gated behind an optional `twilio` dependency — no changes to existing code required.
