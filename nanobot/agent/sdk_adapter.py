"""SDK adapter: thin dispatcher wrapping the Claude Agent SDK.

When config.agents.defaults.engine == "sdk", this module replaces the
legacy AgentLoop with the Claude Agent SDK's query() / ClaudeSDKClient.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.command import CommandContext, CommandRouter, register_builtin_commands

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, Config, ExecToolConfig, MCPServerConfig
    from nanobot.cron.service import CronService


def _check_sdk_available() -> None:
    """Raise a clear error if claude-agent-sdk is not installed."""
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        raise ImportError(
            "Claude Agent SDK not installed. Install it with: pip install nanobot-ai[sdk]"
        )


def _build_nanobot_mcp_server(
    bus: MessageBus,
    cron_service: CronService | None,
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


def _build_mcp_servers_dict(
    nanobot_server: Any,
    config_mcp_servers: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge in-process nanobot MCP server with config-defined external MCP servers."""
    servers: dict[str, Any] = {"nanobot": nanobot_server}
    if not config_mcp_servers:
        return servers

    for name, mcp_cfg in config_mcp_servers.items():
        # Convert nanobot MCPServerConfig to SDK dict format
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


class SDKAgentLoop:
    """Agent loop backed by the Claude Agent SDK.

    Drop-in replacement for AgentLoop when engine="sdk".
    Shares the same public interface: run(), stop(), process_direct(), close_mcp().
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
        self._session_ids: dict[str, str] = {}  # session_key → SDK session_id

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

        # Command router (for /stop, /new, etc.)
        self.commands = CommandRouter()
        register_builtin_commands(self.commands)

    def _build_options(
        self,
        *,
        resume_session_id: str | None = None,
    ) -> Any:
        """Build ClaudeAgentOptions from nanobot config."""
        from claude_agent_sdk import ClaudeAgentOptions

        defaults = self.config.agents.defaults
        system_prompt = self.context.build_system_prompt()

        allowed_tools = [
            "Read", "Write", "Edit", "Bash", "Glob", "Grep",
            "WebSearch", "WebFetch",
            "mcp__nanobot__*",
        ]
        # Allow all configured external MCP servers
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

        if resume_session_id:
            options_kwargs["resume"] = resume_session_id

        if defaults.reasoning_effort:
            options_kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": defaults.max_tokens,
            }

        return ClaudeAgentOptions(**options_kwargs)

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

            raw = msg.content.strip()
            if self.commands.is_priority(raw):
                ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, loop=self)
                result = await self.commands.dispatch_priority(ctx)
                if result:
                    await self.bus.publish_outbound(result)
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

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        on_stream: Callable[[str], Awaitable[None]] | None = None,
        on_stream_end: Callable[..., Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message via the Claude Agent SDK."""
        from claude_agent_sdk import query

        key = session_key or msg.session_key

        # Set tool context for message/cron routing
        self._nanobot_server._nanobot_set_context(
            msg.channel, msg.chat_id, msg.metadata.get("message_id")
        )

        # Resume existing session if available
        resume_id = self._session_ids.get(key)
        options = self._build_options(resume_session_id=resume_id)

        final_content = ""
        session_id = None

        try:
            async for message in query(prompt=msg.content, options=options):
                msg_type = type(message).__name__

                if msg_type == "AssistantMessage":
                    # Stream text content if streaming is enabled
                    if on_stream and hasattr(message, "content"):
                        for block in message.content:
                            if hasattr(block, "text") and block.text:
                                await on_stream(block.text)

                elif msg_type == "ResultMessage":
                    if hasattr(message, "result") and message.result:
                        final_content = message.result
                    if hasattr(message, "session_id") and message.session_id:
                        session_id = message.session_id
                        self._session_ids[key] = session_id

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
        """Cleanup (SDK manages its own MCP lifecycle)."""
        pass
