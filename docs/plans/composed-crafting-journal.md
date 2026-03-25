# Plan: Replace Nanobot Agent Loop with Claude Agent SDK (Python)

## Context

Nanobot has a custom Python agent loop (~500 lines in `agent/loop.py`) plus a multi-provider LLM layer (`providers/`). The Claude Agent SDK (`pip install claude-agent-sdk`) provides a production-ready agent loop with built-in tools — the same engine powering Claude Code. NanoClaw already uses this SDK (TypeScript version).

**Goal:** Make nanobot a **config-driven orchestrator** that uses the Claude Agent SDK as its engine. Keep nanobot's unique value (channels, bus, config, skills, custom tools like cron/message). Drop what the SDK already does better (agent loop, file/shell/web tools, retries, streaming, session management).

## Design Principle: Upstream Compatibility

**Do not delete any existing files.** The goal is to stay mergeable with upstream (`HKUDS/nanobot`). Instead of removing the custom loop, providers, and built-in tools, we **bypass them via config**. A config flag (e.g., `"engine": "sdk"` vs `"engine": "legacy"`) selects which path runs. The legacy path stays untouched for upstream compatibility.

## What Gets Bypassed When `engine: "sdk"` (SDK does it better)

| Current Nanobot | SDK Native Equivalent | Why Drop |
|----------------|----------------------|----------|
| `agent/loop.py` — custom agent loop | `query()` / `ClaudeSDKClient` | SDK loop is battle-tested, handles retries, streaming, prompt caching |
| `providers/*` — LiteLLM, 20+ providers | SDK calls Claude API directly | Claude-only is acceptable; removes ~800 lines |
| `agent/tools/filesystem.py` — read/write/edit/list_dir | SDK built-in: Read, Write, Edit, Glob | Identical functionality, zero maintenance |
| `agent/tools/shell.py` — ExecTool | SDK built-in: Bash | Same, plus SDK has its own safety checks |
| `agent/tools/web.py` — search/fetch | SDK built-in: WebSearch, WebFetch | Same functionality |
| `agent/tools/base.py` + `registry.py` — tool framework | SDK `@tool` decorator | SDK validates params, handles schema |
| `agent/tools/mcp.py` — MCP server connection | SDK `mcp_servers={}` in options | **SDK has native MCP support** — stdio, HTTP, SSE transports, tool wrapping |
| `agent/subagent.py` — background task spawning | SDK `agents={}` with `AgentDefinition` | **SDK has native subagents** — define via config, auto-dispatched |
| `session/*` — JSONL conversation history | SDK session management (resume by ID) | **SDK manages sessions internally** — resume, fork, persist |
| `security/network.py` — SSRF protection | SDK's built-in WebFetch safety | SDK validates URLs; only needed if custom tools do HTTP |

## What Gets Kept (no SDK equivalent)

| Component | What It Does | Why Keep | Integration Method |
|-----------|-------------|----------|-------------------|
| `bus/*` | Async queues routing messages between channels and agent | **SDK has no multi-channel concept** — it's a library, not a server | Stays as-is; adapter bridges bus → `query()` |
| `channels/*` | 16 chat platforms (Telegram, Discord, WhatsApp, Slack, etc.) | **Nanobot's main differentiator** — SDK has zero channel support | Stays as-is |
| `agent/tools/message.py` | Agent sends messages + file attachments to specific channels | **No SDK equivalent** — SDK can't route to Telegram/Discord/etc. | Port to `@tool("message", ...)` |
| `agent/tools/cron.py` + `cron/*` | Schedule one-shot/recurring/cron tasks with timezone support | **No SDK equivalent** — SDK has no scheduling | Port tool to `@tool("cron", ...)`; keep CronService backend |
| `heartbeat/*` | Wakes agent periodically to run due scheduled tasks | **No SDK equivalent** — drives cron execution | Stays; calls `query()` instead of `AgentLoop` |
| `agent/memory.py` | Summarizes old messages into MEMORY.md/HISTORY.md | **SDK has `PreCompact` hook but no persistent memory files** — hook fires but doesn't write MEMORY.md | Keep; wire into `PreCompact` hook |
| `agent/context.py` | Assembles system prompt from identity, bootstrap files, memory, skills | **SDK accepts `system_prompt` string but doesn't assemble it** from multiple sources | Keep but simplify; output feeds `system_prompt` param |
| `agent/skills.py` | Markdown skill packs with metadata + dependency checks | **SDK has agents but not markdown skill packs** — nanobot's SKILL.md format with frontmatter, requirements, on-demand loading is richer | Keep; append to `system_prompt` |
| `config/*` | Pydantic config for channels, tools, MCP, cron | **SDK has `ClaudeAgentOptions` but no multi-channel/cron config** | Keep; trim provider-specific sections, map to `ClaudeAgentOptions` |
| `cli/*` | Rich terminal REPL with streaming, thinking spinners | **SDK streams via async iterator but has no terminal UI** | Keep; iterate `query()` results for display |

## Proposed Architecture

```
Channel Message → MessageBus (stays)
                      ↓
                Dispatcher (new, thin)
                      ↓
          ┌─── ClaudeAgentOptions ───┐
          │  system_prompt (from ContextBuilder + skills)
          │  model (from config)
          │  mcp_servers (from config)
          │  allowed_tools (from config)
          │  hooks:
          │    PreCompact → memory consolidation
          │    Stop → session bookkeeping
          │  agents: (subagent definitions)
          └──────────────────────────┘
                      ↓
              query(prompt, options)
                ├── SDK built-in: Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch
                ├── @tool message → bus.publish_outbound()
                ├── @tool cron → CronService.add/update/list/delete
                └── MCP servers (from config passthrough)
                      ↓
              async for message in query():
                ├── AssistantMessage → stream deltas to channel
                ├── ResultMessage → final response to bus
                └── session_id → store for resume
```

## Implementation Steps

### Step 1: Add `claude-agent-sdk` dependency
- Add to `pyproject.toml`
- Remove `litellm` dependency

### Step 2: Create `agent/sdk_adapter.py` (new dispatcher)
- Thin adapter that:
  - Takes `InboundMessage` from bus
  - Builds `ClaudeAgentOptions` from nanobot config (model, tools, mcp_servers, system_prompt)
  - Calls `query(prompt, options)` or uses `ClaudeSDKClient` for session continuity
  - Iterates async results, publishing `OutboundMessage` to bus
  - Stores `session_id` from `ResultMessage` for resume

### Step 3: Port custom tools to `@tool` decorators
```python
@tool("message", "Send message to a chat channel", {
    "channel": str, "chat_id": str, "content": str
})
async def message_tool(args):
    await bus.publish_outbound(OutboundMessage(...))
    return {"content": [{"type": "text", "text": "Message sent"}]}

@tool("cron", "Manage scheduled tasks", {
    "action": str, "name": str, "schedule": str, "message": str
})
async def cron_tool(args):
    result = await cron_service.handle(args)
    return {"content": [{"type": "text", "text": result}]}
```

Register via `create_sdk_mcp_server()`:
```python
nanobot_server = create_sdk_mcp_server(
    name="nanobot",
    version="0.1.0",
    tools=[message_tool, cron_tool]
)
```

### Step 4: Wire memory consolidation into hooks
```python
async def on_pre_compact(input_data, tool_use_id, context):
    # Trigger memory consolidation before context compaction
    await memory_consolidator.consolidate(session)
    return {"continue_": True}

hooks = {
    "PreCompact": [HookMatcher(hooks=[on_pre_compact])]
}
```

### Step 5: Map config → ClaudeAgentOptions
```python
def build_options(config, context_builder, skills_loader) -> ClaudeAgentOptions:
    system_prompt = context_builder.build_system_prompt()
    system_prompt += skills_loader.get_always_skills_content()

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=config.agents.model,
        max_tokens=config.agents.max_tokens,
        temperature=config.agents.temperature,
        mcp_servers=config.tools.mcp_servers,  # passthrough
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep",
                       "WebSearch", "WebFetch", "mcp__nanobot__*",
                       *(f"mcp__{k}__*" for k in config.tools.mcp_servers)],
        permission_mode="bypassPermissions",  # nanobot is server-side
        hooks=hooks,
        mcp_servers={"nanobot": nanobot_server, **config.tools.mcp_servers},
    )
```

### Step 6: Update CLI and gateway entry points
- `cli/commands.py`: `agent` command calls new adapter instead of `AgentLoop`
- Gateway mode: same adapter, driven by bus messages

### Step 7: Add engine switch to config and entry points
- Add `"engine": "sdk" | "legacy"` field to config schema (`config/schema.py`)
- Default to `"legacy"` so upstream behavior is unchanged
- In `cli/commands.py` and gateway startup:
  - If `engine == "sdk"`: instantiate SDK adapter (`agent/sdk_adapter.py`)
  - If `engine == "legacy"`: instantiate existing `AgentLoop` (no change)
- **No files deleted** — all existing code stays for upstream merge compatibility
- `claude-agent-sdk` added as an optional dependency (e.g., `pip install nanobot[sdk]`)

## Key Files to Modify

| File | Change |
|------|--------|
| `pyproject.toml` | Add `claude-agent-sdk`, remove `litellm` |
| `agent/sdk_adapter.py` | **NEW** — thin dispatcher wrapping SDK |
| `agent/tools/message.py` | Port to `@tool` decorator |
| `agent/tools/cron.py` | Port to `@tool` decorator |
| `agent/context.py` | Keep, output feeds `system_prompt` param |
| `agent/memory.py` | Keep, triggered via `PreCompact` hook |
| `agent/skills.py` | Keep, output appended to `system_prompt` |
| `cli/commands.py` | Call new adapter instead of `AgentLoop` |
| `channels/manager.py` | No change (bus-driven) |
| `config/schema.py` | Simplify provider config, add SDK-specific options |
| `bus/*` | No change |

## What's Lost

- **Multi-provider support** — SDK is Claude-only (acceptable per user)
- **Fine-grained token tracking** — SDK exposes `usage` on `ResultMessage` but less granular
- **Custom retry tuning** — SDK manages its own retries

## Verification

1. `nanobot agent -m "Hello"` — basic CLI chat works
2. `nanobot agent -m "Read pyproject.toml"` — SDK built-in Read tool works
3. `nanobot agent -m "Search the web for python asyncio"` — WebSearch works
4. `nanobot gateway` — channels receive/send messages through bus
5. Test custom tools: message sending, cron scheduling
6. Test memory consolidation on long conversations
7. Test session resume across CLI invocations
8. Run existing test suite (adapt tests that reference removed modules)
