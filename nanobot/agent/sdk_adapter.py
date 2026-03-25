"""SDK adapter: thin dispatcher wrapping the Claude Agent SDK.

When config.agents.defaults.engine == "sdk", this module replaces the
legacy AgentLoop with ClaudeSDKClient — one persistent client per session
(keyed by channel:chat_id).
"""

from __future__ import annotations

import asyncio
import os
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot import __version__
from nanobot.agent.context import ContextBuilder
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, Config
    from nanobot.cron.service import CronService


# ---------------------------------------------------------------------------
# SDK availability check
# ---------------------------------------------------------------------------

def _check_sdk_available() -> None:
    """Raise a clear error if claude-agent-sdk is not installed."""
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        raise ImportError(
            "Claude Agent SDK not installed. Install it with: pip install nanobot-ai[sdk]"
        )


# ---------------------------------------------------------------------------
# In-process MCP server for nanobot-specific tools (message, cron)
# ---------------------------------------------------------------------------

def _build_nanobot_mcp_server(
    bus: MessageBus,
    cron_service: "CronService | None",
) -> Any:
    """Build an in-process MCP server with nanobot-specific tools."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    tools = []

    # --- message tool ---
    _bus = bus
    _msg_channel = ""
    _msg_chat_id = ""
    _msg_message_id: str | None = None
    _msg_sent_in_turn = False

    @tool(
        "message",
        "Send a message to the user, optionally with file attachments. "
        "This is the ONLY way to deliver files (images, documents, audio, video) to the user. "
        "Use the 'media' parameter with file paths to attach files.",
        {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The message content to send",
                },
                "channel": {
                    "type": "string",
                    "description": "Optional: target channel (telegram, discord, etc.)",
                },
                "chat_id": {
                    "type": "string",
                    "description": "Optional: target chat/user ID",
                },
                "media": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: list of file paths to attach",
                },
            },
            "required": ["content"],
        },
    )
    async def message_tool(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal _msg_sent_in_turn
        channel = args.get("channel") or _msg_channel
        chat_id = args.get("chat_id") or _msg_chat_id
        media = args.get("media") or []
        content = args.get("content", "")

        if not channel or not chat_id:
            return {
                "content": [{"type": "text", "text": "Error: No target channel/chat specified"}],
                "is_error": True,
            }

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media,
            metadata={"message_id": _msg_message_id},
        )
        try:
            await _bus.publish_outbound(msg)
            if channel == _msg_channel and chat_id == _msg_chat_id:
                _msg_sent_in_turn = True
            media_info = f" with {len(media)} attachments" if media else ""
            return {
                "content": [
                    {"type": "text", "text": f"Message sent to {channel}:{chat_id}{media_info}"}
                ]
            }
        except Exception as e:
            return {
                "content": [{"type": "text", "text": f"Error sending message: {e}"}],
                "is_error": True,
            }

    tools.append(message_tool)

    # --- cron tool ---
    if cron_service:
        from nanobot.cron.types import CronSchedule

        @tool(
            "cron",
            "Schedule reminders and recurring tasks. Actions: add, list, remove.",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "list", "remove"],
                        "description": "Action to perform",
                    },
                    "message": {
                        "type": "string",
                        "description": "Reminder message (for add)",
                    },
                    "every_seconds": {
                        "type": "integer",
                        "description": "Interval in seconds (for recurring tasks)",
                    },
                    "cron_expr": {
                        "type": "string",
                        "description": "Cron expression like '0 9 * * *'",
                    },
                    "tz": {
                        "type": "string",
                        "description": "IANA timezone for cron expressions",
                    },
                    "at": {
                        "type": "string",
                        "description": "ISO datetime for one-time execution",
                    },
                    "job_id": {"type": "string", "description": "Job ID (for remove)"},
                },
                "required": ["action"],
            },
        )
        async def cron_tool(args: dict[str, Any]) -> dict[str, Any]:
            action = args.get("action", "")
            if action == "add":
                message = args.get("message", "")
                if not message:
                    return _cron_err("message is required for add")
                if not _msg_channel or not _msg_chat_id:
                    return _cron_err("no session context (channel/chat_id)")

                every_seconds = args.get("every_seconds")
                cron_expr = args.get("cron_expr")
                tz = args.get("tz")
                at = args.get("at")

                if tz and not cron_expr:
                    return _cron_err("tz can only be used with cron_expr")
                if tz:
                    from zoneinfo import ZoneInfo

                    try:
                        ZoneInfo(tz)
                    except (KeyError, Exception):
                        return _cron_err(f"unknown timezone '{tz}'")

                delete_after = False
                if every_seconds:
                    schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
                elif cron_expr:
                    schedule = CronSchedule(kind="cron", expr=cron_expr, tz=tz)
                elif at:
                    from datetime import datetime

                    try:
                        dt = datetime.fromisoformat(at)
                    except ValueError:
                        return _cron_err(f"invalid ISO datetime format '{at}'")
                    at_ms = int(dt.timestamp() * 1000)
                    schedule = CronSchedule(kind="at", at_ms=at_ms)
                    delete_after = True
                else:
                    return _cron_err("either every_seconds, cron_expr, or at is required")

                job = cron_service.add_job(
                    name=message[:30],
                    schedule=schedule,
                    message=message,
                    deliver=True,
                    channel=_msg_channel,
                    to=_msg_chat_id,
                    delete_after_run=delete_after,
                )
                return _cron_ok(f"Created job '{job.name}' (id: {job.id})")

            elif action == "list":
                jobs = cron_service.list_jobs()
                if not jobs:
                    return _cron_ok("No scheduled jobs.")
                lines = []
                for j in jobs:
                    lines.append(f"- {j.name} (id: {j.id}, {j.schedule.kind})")
                return _cron_ok("Scheduled jobs:\n" + "\n".join(lines))

            elif action == "remove":
                job_id = args.get("job_id")
                if not job_id:
                    return _cron_err("job_id is required for remove")
                if cron_service.remove_job(job_id):
                    return _cron_ok(f"Removed job {job_id}")
                return _cron_err(f"Job {job_id} not found")

            return _cron_err(f"Unknown action: {action}")

        def _cron_ok(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": text}]}

        def _cron_err(text: str) -> dict[str, Any]:
            return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}

        tools.append(cron_tool)

    server = create_sdk_mcp_server(name="nanobot", version="0.1.0", tools=tools)

    # Expose context-setting helpers so the adapter can update routing per-message.
    def set_context(channel: str, chat_id: str, message_id: str | None = None) -> None:
        nonlocal _msg_channel, _msg_chat_id, _msg_message_id, _msg_sent_in_turn
        _msg_channel = channel
        _msg_chat_id = chat_id
        _msg_message_id = message_id
        _msg_sent_in_turn = False

    def was_sent_in_turn() -> bool:
        return _msg_sent_in_turn

    server._nanobot_set_context = set_context
    server._nanobot_was_sent_in_turn = was_sent_in_turn

    return server


# ---------------------------------------------------------------------------
# MCP server merging (in-process + external config-defined servers)
# ---------------------------------------------------------------------------

def _build_mcp_servers_dict(
    nanobot_server: Any,
    config_mcp_servers: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge in-process nanobot MCP server with config-defined external MCP servers."""
    servers: dict[str, Any] = {"nanobot": nanobot_server}
    if not config_mcp_servers:
        return servers

    for name, mcp_cfg in config_mcp_servers.items():
        entry: dict[str, Any] = {}
        if hasattr(mcp_cfg, "command") and mcp_cfg.command:
            entry["command"] = mcp_cfg.command
            entry["args"] = list(mcp_cfg.args) if mcp_cfg.args else []
            if mcp_cfg.env:
                entry["env"] = dict(mcp_cfg.env)
        elif hasattr(mcp_cfg, "url") and mcp_cfg.url:
            transport_type = mcp_cfg.type or "http"
            entry["type"] = transport_type
            entry["url"] = mcp_cfg.url
            if mcp_cfg.headers:
                entry["headers"] = dict(mcp_cfg.headers)
        servers[name] = entry

    return servers


# ---------------------------------------------------------------------------
# Per-session client wrapper
# ---------------------------------------------------------------------------

@dataclass
class _SessionClient:
    """Wraps a persistent ClaudeSDKClient for one conversation session."""

    client: Any  # ClaudeSDKClient
    session_id: str | None = None


# ---------------------------------------------------------------------------
# SDK Agent Loop
# ---------------------------------------------------------------------------

class SDKAgentLoop:
    """Agent loop backed by the Claude Agent SDK (ClaudeSDKClient).

    Drop-in replacement for AgentLoop when engine="sdk".
    Maintains one persistent ClaudeSDKClient per session key (channel:chat_id).
    Public interface: run(), stop(), process_direct(), close_mcp().
    """

    def __init__(
        self,
        bus: MessageBus,
        config: "Config",
        cron_service: "CronService | None" = None,
        channels_config: "ChannelsConfig | None" = None,
    ):
        _check_sdk_available()

        self.bus = bus
        self.config = config
        self.channels_config = channels_config
        self.workspace = config.workspace_path
        self.model = config.agents.defaults.model
        self.context = ContextBuilder(self.workspace)
        self.cron_service = cron_service

        self._running = False
        self._active_tasks: dict[str, list[asyncio.Task]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._clients: dict[str, _SessionClient] = {}  # session_key → client

        _max = int(os.environ.get("NANOBOT_MAX_CONCURRENT_REQUESTS", "3"))
        self._concurrency_gate: asyncio.Semaphore | None = (
            asyncio.Semaphore(_max) if _max > 0 else None
        )

        # Build nanobot MCP server with custom tools
        self._nanobot_server = _build_nanobot_mcp_server(bus, cron_service)

        # Build merged MCP servers dict
        self._mcp_servers = _build_mcp_servers_dict(
            self._nanobot_server,
            config.tools.mcp_servers if config.tools.mcp_servers else None,
        )

    # ------------------------------------------------------------------
    # Options builder
    # ------------------------------------------------------------------

    def _build_options(self) -> Any:
        """Build ClaudeAgentOptions from nanobot config."""
        from claude_agent_sdk import ClaudeAgentOptions

        defaults = self.config.agents.defaults
        system_prompt = self.context.build_system_prompt()

        allowed_tools = [
            "Read", "Write", "Edit", "Bash", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "mcp__nanobot__*",
        ]
        for name in self._mcp_servers:
            if name != "nanobot":
                allowed_tools.append(f"mcp__{name}__*")

        options_kwargs: dict[str, Any] = {
            "system_prompt": system_prompt,
            "model": defaults.model,
            "max_tokens": defaults.max_tokens,
            "temperature": defaults.temperature,
            "max_turns": defaults.max_tool_iterations,
            "mcp_servers": self._mcp_servers,
            "allowed_tools": allowed_tools,
            "permission_mode": "bypassPermissions",
            "cwd": str(self.workspace),
        }

        if defaults.reasoning_effort:
            options_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": defaults.max_tokens,
            }

        return ClaudeAgentOptions(**options_kwargs)

    # ------------------------------------------------------------------
    # Per-session client lifecycle
    # ------------------------------------------------------------------

    async def _get_or_create_client(self, session_key: str) -> _SessionClient:
        """Return an existing client for *session_key*, or create a new one."""
        if session_key in self._clients:
            return self._clients[session_key]

        from claude_agent_sdk import ClaudeSDKClient

        options = self._build_options()
        client = ClaudeSDKClient(options=options)
        await client.__aenter__()

        sc = _SessionClient(client=client)
        self._clients[session_key] = sc
        logger.info("Created SDK client for session {}", session_key)
        return sc

    async def _close_client(self, session_key: str) -> None:
        """Close and remove the client for *session_key*."""
        sc = self._clients.pop(session_key, None)
        if sc and sc.client:
            try:
                await sc.client.__aexit__(None, None, None)
            except Exception:
                logger.debug("Error closing SDK client for {}", session_key)
        self._session_locks.pop(session_key, None)

    # ------------------------------------------------------------------
    # Slash command handling (SDK-aware replacements for legacy builtins)
    # ------------------------------------------------------------------

    async def _handle_slash_command(self, msg: InboundMessage) -> bool:
        """Handle slash commands that need SDK-specific logic.

        Returns True if the command was handled, False otherwise.
        """
        raw = msg.content.strip()

        if raw == "/new":
            await self._close_client(msg.session_key)
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content="New session started.",
            ))
            return True

        if raw == "/stop":
            sc = self._clients.get(msg.session_key)
            if sc and sc.client:
                try:
                    await sc.client.interrupt()
                except Exception:
                    pass
            tasks = self._active_tasks.pop(msg.session_key, [])
            cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            content = f"Stopped {cancelled} task(s)." if cancelled else "No active task to stop."
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content,
            ))
            return True

        if raw == "/status":
            n_sessions = len(self._clients)
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=(
                    f"nanobot v{__version__} | engine: sdk | model: {self.model} "
                    f"| sessions: {n_sessions}"
                ),
                metadata={"render_as": "text"},
            ))
            return True

        if raw == "/help":
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id,
                content=(
                    "nanobot commands:\n"
                    "/new \u2014 Start a new conversation\n"
                    "/stop \u2014 Stop the current task\n"
                    "/restart \u2014 Restart the bot\n"
                    "/status \u2014 Show bot status\n"
                    "/help \u2014 Show available commands"
                ),
                metadata={"render_as": "text"},
            ))
            return True

        if raw == "/restart":
            import sys

            async def _do_restart() -> None:
                await asyncio.sleep(1)
                os.execv(sys.executable, [sys.executable, "-m", "nanobot"] + sys.argv[1:])

            asyncio.create_task(_do_restart())
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content="Restarting...",
            ))
            return True

        return False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the agent loop, consuming messages from the bus."""
        self._running = True
        logger.info("SDK agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                if not self._running or asyncio.current_task().cancelling():
                    raise
                continue
            except Exception as e:
                logger.warning("Error consuming inbound message: {}, continuing...", e)
                continue

            # Handle slash commands (SDK-aware implementations)
            raw = msg.content.strip()
            if raw.startswith("/"):
                handled = await self._handle_slash_command(msg)
                if handled:
                    continue

            task = asyncio.create_task(self._dispatch(msg))
            self._active_tasks.setdefault(msg.session_key, []).append(task)
            task.add_done_callback(
                lambda t, k=msg.session_key: (
                    self._active_tasks[k].remove(t)
                    if k in self._active_tasks and t in self._active_tasks[k]
                    else None
                )
            )

    # ------------------------------------------------------------------
    # Per-message dispatch (serialized per session)
    # ------------------------------------------------------------------

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message with per-session serialization."""
        lock = self._session_locks.setdefault(msg.session_key, asyncio.Lock())
        gate = self._concurrency_gate or nullcontext()
        async with lock, gate:
            try:
                on_stream = on_stream_end = None
                if msg.metadata.get("_wants_stream"):

                    async def on_stream(delta: str) -> None:
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content=delta,
                                metadata={"_stream_delta": True},
                            )
                        )

                    async def on_stream_end(*, resuming: bool = False) -> None:
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                channel=msg.channel,
                                chat_id=msg.chat_id,
                                content="",
                                metadata={"_stream_end": True, "_resuming": resuming},
                            )
                        )

                response = await self._process_message(
                    msg, on_stream=on_stream, on_stream_end=on_stream_end
                )
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            metadata=msg.metadata or {},
                        )
                    )
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                    )
                )

    # ------------------------------------------------------------------
    # Core SDK interaction
    # ------------------------------------------------------------------

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message via ClaudeSDKClient."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

        key = session_key or msg.session_key

        # Set tool context for message/cron routing
        self._nanobot_server._nanobot_set_context(
            msg.channel, msg.chat_id, msg.metadata.get("message_id")
        )

        # Get or create persistent client for this session
        sc = await self._get_or_create_client(key)

        final_content = ""

        try:
            # Send prompt to the persistent client
            await sc.client.query(msg.content)

            # Stream response
            async for message in sc.client.receive_response():
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    sc.session_id = message.data.get("session_id")

                elif isinstance(message, AssistantMessage):
                    if on_stream and hasattr(message, "content"):
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text:
                                await on_stream(block.text)

                elif isinstance(message, ResultMessage):
                    if message.result:
                        final_content = message.result

        except Exception as e:
            logger.exception("SDK query failed for session {}", key)
            final_content = f"Sorry, I encountered an error: {e}"

        if on_stream and on_stream_end:
            await on_stream_end(resuming=False)

        # If the message tool sent a reply, don't duplicate
        if self._nanobot_server._nanobot_was_sent_in_turn():
            return None

        if not final_content:
            final_content = "I've completed processing but have no response to give."

        meta = dict(msg.metadata or {})
        if on_stream is not None:
            meta["_streamed"] = True

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=meta,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a message directly (for CLI single-message mode)."""
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        return await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            on_stream=on_stream,
            on_stream_end=on_stream_end,
        )

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("SDK agent loop stopping")

    async def close_mcp(self) -> None:
        """Close all persistent SDK clients."""
        for key in list(self._clients):
            await self._close_client(key)
