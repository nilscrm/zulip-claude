"""Custom tools that let the agent read other Zulip channels and topics.

The agent only sees the tool definitions; the bot runs each call against the Zulip
API, so the Zulip credentials never enter the sandbox. Every call is checked
against the people who will see the reply, so the bot can't be used to copy
messages to an audience that couldn't read them.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

GUEST_ROLE = 600
MAX_MESSAGES = 100
MAX_MESSAGE_CHARS = 2000
MAX_RESULT_CHARS = 60_000
MAX_TOPICS = 100

TOOLS: list[dict[str, Any]] = [
    {
        "type": "custom",
        "name": "zulip_list_topics",
        "description": "List the most recently active topics in a Zulip channel, newest first.",
        "input_schema": {
            "type": "object",
            "properties": {"channel": {"type": "string", "description": "Channel name, without the #."}},
            "required": ["channel"],
        },
    },
    {
        "type": "custom",
        "name": "zulip_read_topic",
        "description": (
            "Read messages from a topic in a Zulip channel, oldest first. Returns the latest "
            "messages; to page further back, pass the smallest message id you got as `before_id`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel name, without the #."},
                "topic": {"type": "string"},
                "limit": {"type": "integer", "description": f"Number of messages, at most {MAX_MESSAGES}. Default 50."},
                "before_id": {"type": "integer", "description": "Only return messages older than this message id."},
            },
            "required": ["channel", "topic"],
        },
    },
    {
        "type": "custom",
        "name": "zulip_search",
        "description": (
            "Full-text search over Zulip messages, newest matches returned (oldest first). "
            "Searches all public channels, or only `channel` if given."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords."},
                "channel": {"type": "string", "description": "Limit the search to this channel."},
                "limit": {"type": "integer", "description": f"Number of messages, at most {MAX_MESSAGES}. Default 20."},
            },
            "required": ["query"],
        },
    },
]

ZulipCall = Callable[..., Awaitable[dict[str, Any]]]


class ToolError(Exception):
    pass


def describe(name: str, inp: dict[str, Any]) -> str:
    """One line for the status message."""
    channel = inp.get("channel")
    if name == "zulip_list_topics":
        return f"Listing topics in #{channel}"
    if name == "zulip_read_topic":
        return f"Reading #{channel} > {inp.get('topic')}"
    return f"Searching Zulip for `{str(inp.get('query', ''))[:60]}`" + (f" in #{channel}" if channel else "")


def _limit(inp: dict[str, Any], default: int) -> int:
    try:
        return max(1, min(int(inp.get("limit") or default), MAX_MESSAGES))
    except (TypeError, ValueError):
        return default


class ZulipReader:
    """Runs the Zulip tools for one agent turn.

    Access rule, judged against the audience of the reply (the asker in a channel,
    every participant in a DM):
    - the current channel is always readable;
    - public channels are readable, except by guests who aren't subscribed;
    - private channels are readable only from a DM whose participants are all subscribed.
    DMs other than the current one are never readable.
    """

    def __init__(self, call: ZulipCall, msg: dict[str, Any], bot_user_id: int) -> None:
        self.call = call
        self.is_dm = msg["type"] == "private"
        self.current_channel_id: int | None = None if self.is_dm else msg["stream_id"]
        if self.is_dm:
            self.audience = [u["id"] for u in msg["display_recipient"] if u["id"] != bot_user_id]
        else:
            self.audience = [msg["sender_id"]]
        self._channels: dict[str, dict[str, Any]] | None = None
        self._roles: dict[int, int] = {}

    async def run(self, name: str, inp: dict[str, Any]) -> tuple[str, bool]:
        """Execute a tool call; return (result text, is_error)."""
        try:
            if name == "zulip_list_topics":
                return await self.list_topics(inp), False
            if name == "zulip_read_topic":
                return await self.read_topic(inp), False
            if name == "zulip_search":
                return await self.search(inp), False
            raise ToolError(f"Unknown tool {name!r}.")
        except ToolError as e:
            return str(e), True

    async def _zulip(self, method: str, *args: Any) -> dict[str, Any]:
        res = await self.call(method, *args)
        if res.get("result") != "success":
            raise ToolError(f"Zulip error: {res.get('msg', 'unknown error')}")
        return res

    async def _channel(self, name: Any) -> dict[str, Any]:
        """Look up a channel by name and check the audience may read it."""
        if self._channels is None:
            res = await self._zulip("get_streams")
            self._channels = {s["name"].casefold(): s for s in res["streams"]}
        name = str(name or "").strip().removeprefix("#")
        channel = self._channels.get(name.casefold())
        denied = ToolError(f"Channel #{name} doesn't exist or isn't readable from this conversation.")
        if channel is None:
            raise denied
        if channel["stream_id"] == self.current_channel_id:
            return channel
        if channel.get("invite_only"):
            if not self.is_dm or not await self._all_subscribed(channel):
                raise denied
        elif not await self._guests_subscribed(channel):
            raise denied
        return channel

    async def _subscribed(self, user_id: int, channel: dict[str, Any]) -> bool:
        res = await self._zulip("get_subscription_status", user_id, channel["stream_id"])
        return bool(res.get("is_subscribed"))

    async def _all_subscribed(self, channel: dict[str, Any]) -> bool:
        for user_id in self.audience:
            if not await self._subscribed(user_id, channel):
                return False
        return True

    async def _guests_subscribed(self, channel: dict[str, Any]) -> bool:
        for user_id in self.audience:
            if user_id not in self._roles:
                self._roles[user_id] = (await self._zulip("get_user_by_id", user_id))["user"]["role"]
            if self._roles[user_id] >= GUEST_ROLE and not await self._subscribed(user_id, channel):
                return False
        return True

    async def list_topics(self, inp: dict[str, Any]) -> str:
        channel = await self._channel(inp.get("channel"))
        res = await self._zulip("get_stream_topics", channel["stream_id"])
        topics = [t["name"] for t in res["topics"][:MAX_TOPICS]]
        if not topics:
            return f"#{channel['name']} has no topics."
        return f"Topics in #{channel['name']}, most recently active first:\n" + "\n".join(f"- {t}" for t in topics)

    async def read_topic(self, inp: dict[str, Any]) -> str:
        channel = await self._channel(inp.get("channel"))
        topic = str(inp.get("topic") or "")
        narrow = [
            {"operator": "stream", "operand": channel["stream_id"]},
            {"operator": "topic", "operand": topic},
        ]
        messages = await self._messages(narrow, _limit(inp, 50), inp.get("before_id"))
        if not messages:
            return f"No messages found in #{channel['name']} > {topic}."
        return f"#{channel['name']} > {topic}, oldest first:\n\n" + self._format(messages, with_location=False)

    async def search(self, inp: dict[str, Any]) -> str:
        query = str(inp.get("query") or "").strip()
        if not query:
            raise ToolError("`query` must not be empty.")
        narrow: list[dict[str, Any]] = [{"operator": "search", "operand": query}]
        if inp.get("channel"):
            channel = await self._channel(inp["channel"])
            narrow.append({"operator": "stream", "operand": channel["stream_id"]})
        else:
            narrow.append({"operator": "streams", "operand": "public"})
        messages = await self._messages(narrow, _limit(inp, 20), None)
        # Defense in depth: drop anything the audience may not read (e.g. guests).
        readable = []
        for m in messages:
            if m["type"] != "stream":
                continue
            try:
                await self._channel(m["display_recipient"])
            except ToolError:
                continue
            readable.append(m)
        if not readable:
            return f"No messages found for {query!r}."
        return f"Search results for {query!r}, oldest first:\n\n" + self._format(readable, with_location=True)

    async def _messages(self, narrow: list[dict[str, Any]], limit: int, before_id: Any) -> list[dict[str, Any]]:
        request: dict[str, Any] = {
            "anchor": "newest", "num_before": limit, "num_after": 0,
            "narrow": narrow, "apply_markdown": False,
        }
        if before_id is not None:
            try:
                request.update(anchor=int(before_id), include_anchor=False)
            except (TypeError, ValueError):
                raise ToolError("`before_id` must be a message id.")
        return (await self._zulip("get_messages", request))["messages"]

    def _format(self, messages: list[dict[str, Any]], with_location: bool) -> str:
        parts = []
        for m in messages:
            when = datetime.fromtimestamp(m["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M")
            where = f"#{m['display_recipient']} > {m['subject']} · " if with_location else ""
            content = m["content"]
            if len(content) > MAX_MESSAGE_CHARS:
                content = content[:MAX_MESSAGE_CHARS] + " […truncated]"
            parts.append(f"[{when} UTC] {where}{m['sender_full_name']} (message {m['id']}):\n{content}\n")
        text = "\n".join(parts)
        if len(text) > MAX_RESULT_CHARS:
            # Keep the newest messages; they're at the end.
            text = "[…older messages omitted; use a smaller limit or page with before_id]\n" + text[-MAX_RESULT_CHARS:]
        return text
