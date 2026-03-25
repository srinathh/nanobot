# Plan: Refactor sdk_adapter.py — Switch from `query()` to `ClaudeSDKClient`

## Context

The current `nanobot/agent/sdk_adapter.py` uses `query()` (the simple one-shot SDK interface). However:
1. **Custom SDK MCP tools require `ClaudeSDKClient`** — `query()` only supports external stdio/http MCP servers, not in-process ones created via `create_sdk_mcp_server()`
2. **No persistent sessions** — `query()` creates a new process per call; session resume via `_session_ids` dict is fragile
3. **The user wants per-conversation clients** — each `channel:chat_id` pair (e.g., `telegram:12345`, `twilio_whatsapp:whatsapp:+1234567890`) should get its own persistent `ClaudeSDKClient` with full session state

Additionally, investigation revealed **pre-existing bugs** in the SDK adapter:
- `/new` command is registered as `exact` (not `priority`) so it bypasses `CommandRouter` and gets sent to Claude as a regular message
- `/stop`, `/status`, `/new` all reference legacy `AgentLoop` attributes (`loop.sessions`, `loop.memory_consolidator`, `loop.subagents`, `loop._last_usage`) that don't exist on `SDKAgentLoop`

## Files to Modify

| File | Change |
|------|--------|
| `nanobot/agent/sdk_adapter.py` | **Major rewrite** — switch to `ClaudeSDKClient`, per-session client management |

No changes to `cli/commands.py` — the public interface stays identical.

## Implementation Plan

### 1. Per-Session Client Store

Replace `_session_ids: dict[str, str]` with a client store:

```python
@dataclass
class _SessionClient:
    client: ClaudeSDKClient
    session_id: str | None = None

_clients: dict[str, _SessionClient] = {}  # session_key → client wrapper
```

Session key = `InboundMessage.session_key` = `f"{channel}:{chat_id}"` (already defined in `bus/events.py:24`).

### 2. Client Lifecycle: `_get_or_create_client(session_key)`

New method on `SDKAgentLoop`:

```python
async def _get_or_create_client(self, session_key: str) -> _SessionClient:
    if session_key in self._clients:
        return self._clients[session_key]

    options = self._build_options()
    client = ClaudeSDKClient(options=options)
    await client.__aenter__()  # Manual enter since we manage lifecycle ourselves

    sc = _SessionClient(client=client)
    self._clients[session_key] = sc
    return sc
```

We call `__aenter__`/`__aexit__` manually rather than using `async with` because clients persist across multiple messages.

### 3. Client Cleanup: `_close_client(session_key)` and `close_mcp()`

```python
async def _close_client(self, session_key: str) -> None:
    sc = self._clients.pop(session_key, None)
    if sc and sc.client:
        await sc.client.__aexit__(None, None, None)

async def close_mcp(self) -> None:
    for key in list(self._clients):
        await self._close_client(key)
```

### 4. Rewrite `_process_message()` — Use `client.query()` + `client.receive_response()`

Replace the current `async for message in query(...)` pattern:

```python
async def _process_message(self, msg, ...):
    key = session_key or msg.session_key

    # Set MCP tool routing context
    self._nanobot_server._nanobot_set_context(msg.channel, msg.chat_id, ...)

    # Get or create persistent client
    sc = await self._get_or_create_client(key)

    # Send prompt
    await sc.client.query(msg.content)

    # Stream response
    final_content = ""
    async for message in sc.client.receive_response():
        if isinstance(message, SystemMessage) and message.subtype == "init":
            sc.session_id = message.data.get("session_id")
        elif isinstance(message, AssistantMessage):
            if on_stream:
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        await on_stream(block.text)
        elif isinstance(message, ResultMessage):
            if message.result:
                final_content = message.result

    # ... rest same as current (check was_sent_in_turn, build OutboundMessage)
```

Key changes:
- Use `isinstance()` checks instead of `type().__name__` string matching
- Capture `session_id` from `SystemMessage` (init event), not `ResultMessage`
- Import `AssistantMessage`, `ResultMessage`, `SystemMessage`, `TextBlock` from SDK

### 5. Update `_build_options()` — Remove session resume (client handles it)

Since `ClaudeSDKClient` maintains session state internally, remove the `resume` parameter:

```python
def _build_options(self) -> ClaudeAgentOptions:
    # Remove resume_session_id parameter
    # Remove options_kwargs["resume"] logic
    # Everything else stays the same
```

Session continuity is now handled by the persistent client instance itself.

### 6. Fix `/new` Command — Reset Session Client

Add `/new` handling in the `run()` loop alongside priority commands, or handle it in `_dispatch`:

```python
# In run() loop, before dispatching to _dispatch:
raw = msg.content.strip()
if raw == "/new":
    await self._close_client(msg.session_key)
    await self.bus.publish_outbound(OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content="New session started.",
    ))
    continue
```

This closes the existing `ClaudeSDKClient` for that session. The next message will create a fresh client.

### 7. Fix `/stop` Command — Interrupt Client

The existing `cmd_stop` references `loop.subagents` which doesn't exist. Override with SDK-native interrupt:

```python
# In run() loop for /stop:
if self.commands.is_priority(raw) and raw.strip() == "/stop":
    sc = self._clients.get(msg.session_key)
    if sc and sc.client:
        await sc.client.interrupt()
    # Also cancel active asyncio tasks (existing logic)
    tasks = self._active_tasks.pop(msg.session_key, [])
    cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
    await self.bus.publish_outbound(OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=f"Stopped." if cancelled else "No active task.",
    ))
    continue
```

### 8. Fix `/status` and `/help` Commands

Override these in the `run()` loop to avoid calling `cmd_status` which references legacy attributes:

```python
if raw == "/status":
    await self.bus.publish_outbound(OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content=f"nanobot v{__version__} | engine: sdk | model: {self.model} | sessions: {len(self._clients)}",
    ))
    continue

if raw == "/help":
    await self.bus.publish_outbound(OutboundMessage(
        channel=msg.channel, chat_id=msg.chat_id,
        content="nanobot commands:\n/new — Start a new conversation\n/stop — Stop the current task\n/status — Show bot status\n/help — Show available commands",
    ))
    continue
```

### 9. Update `stop()` — Clean Up All Clients

```python
def stop(self) -> None:
    self._running = False
    # Schedule cleanup of all clients
    for key in list(self._clients):
        # Can't await in sync method — close_mcp() handles async cleanup
    logger.info("SDK agent loop stopping")
```

### 10. Keep Unchanged

- `_build_nanobot_mcp_server()` — stays exactly as-is (already compatible with `ClaudeSDKClient`)
- `_build_mcp_servers_dict()` — stays as-is
- `_check_sdk_available()` — stays as-is
- `_dispatch()` — same structure (lock + gate + streaming callbacks)
- `process_direct()` — same interface, uses the refactored `_process_message()` internally

## Summary of Changes

| Component | Before (query) | After (ClaudeSDKClient) |
|-----------|----------------|------------------------|
| SDK call | `async for msg in query(prompt, options)` | `await client.query(prompt)` + `async for msg in client.receive_response()` |
| Session state | `_session_ids` dict + `options.resume` | Persistent `ClaudeSDKClient` per session key |
| Client lifecycle | None (stateless) | `__aenter__` on first message, `__aexit__` on `/new` or `close_mcp()` |
| `/new` command | Broken (sent to Claude as text) | Closes client, next message creates fresh one |
| `/stop` command | Broken (`loop.subagents` missing) | `client.interrupt()` + cancel tasks |
| Message type checks | `type().__name__` strings | `isinstance()` with imported types |
| MCP tools | Works via `query()` (incorrect per docs) | Works correctly via `ClaudeSDKClient` |

## Verification

1. **Lint**: `ruff check nanobot/agent/sdk_adapter.py`
2. **Import check**: `python -c "from nanobot.agent.sdk_adapter import SDKAgentLoop"` (with deps installed)
3. **Existing tests**: `pytest tests/` — should pass since public interface is unchanged
4. **Manual test** (with SDK installed):
   - Set `engine: "sdk"` in config
   - `nanobot agent -m "Hello"` — basic CLI works
   - Send multiple messages — verify session continuity (client reuse)
   - `/new` — verify fresh session (new client)
   - `/stop` during long task — verify interrupt works
   - Gateway mode with Telegram/Twilio — verify per-channel:chat_id sessions
