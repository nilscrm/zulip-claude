"""Zulip side: listen for mentions and DMs, run the agent, post the result."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import zulip

from .config import Config
from .managed import ManagedAgents, github_repo_url, repo_name
from .zulip_tools import ZulipReader

log = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 9500  # Zulip rejects messages over 10000 characters.
STATUS_EDIT_INTERVAL_S = 3.0
REPO_OVERRIDE = re.compile(r"(?:^|\s)repo:(\S+)")


@dataclass(frozen=True)
class Thread:
    """A Zulip conversation: a channel topic or a DM group."""

    key: str
    label: str
    channel: str | None = None
    topic: str | None = None
    dm_emails: tuple[str, ...] = ()

    @classmethod
    def from_message(cls, msg: dict[str, Any], bot_email: str) -> Thread:
        if msg["type"] == "stream":
            channel, topic = msg["display_recipient"], msg["subject"]
            return cls(
                key=f"channel:{msg['stream_id']}:{topic}",
                label=f"#{channel} > {topic}",
                channel=channel,
                topic=topic,
            )
        emails = tuple(sorted(u["email"] for u in msg["display_recipient"] if u["email"] != bot_email))
        return cls(key="dm:" + ",".join(emails), label="DM with " + ", ".join(emails), dm_emails=emails)

    def reply_to(self) -> dict[str, Any]:
        if self.channel is not None:
            return {"type": "stream", "to": self.channel, "topic": self.topic}
        return {"type": "private", "to": list(self.dm_emails)}

    def narrow(self) -> list[dict[str, Any]]:
        if self.channel is not None:
            return [
                {"operator": "stream", "operand": self.channel},
                {"operator": "topic", "operand": self.topic},
            ]
        return [{"operator": "dm", "operand": list(self.dm_emails)}]


class StateStore:
    """Agent session per thread, persisted so follow-ups go to the same session."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict[str, Any]] = json.loads(path.read_text()) if path.exists() else {}

    def get(self, key: str) -> dict[str, Any]:
        return self.data.get(key, {})

    def set(self, key: str, **values: Any) -> None:
        self.data.setdefault(key, {}).update(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)


class StatusMessage:
    """A message that shows live progress while the agent works, then a summary."""

    def __init__(self, bot: Bot, thread: Thread) -> None:
        self.bot = bot
        self.thread = thread
        self.id: int | None = None
        self.header = ""
        self.steps = 0
        self.last_edit = 0.0

    async def post(self, header: str) -> None:
        self.header = header
        res = await self.bot.call("send_message", {**self.thread.reply_to(), "content": f"⏳ {header}"})
        self.id = res.get("id")

    async def progress(self, line: str) -> None:
        self.steps += 1
        now = time.monotonic()
        if self.id is None or now - self.last_edit < STATUS_EDIT_INTERVAL_S:
            return
        self.last_edit = now
        await self.edit(f"⏳ {self.header}\n{line}")

    async def edit(self, content: str) -> None:
        if self.id is not None:
            await self.bot.call("update_message", {"message_id": self.id, "content": content})


def _fmt_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _chunks(text: str, size: int = MAX_MESSAGE_LEN) -> list[str]:
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks + [text]


class Bot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # Credentials come from ZULIP_EMAIL / ZULIP_API_KEY / ZULIP_SITE or ZULIP_CONFIG.
        try:
            self.client = zulip.Client(client="zulip-claude")
        except zulip.ConfigNotFoundError:
            raise SystemExit("Zulip credentials missing: set ZULIP_SITE, ZULIP_EMAIL and ZULIP_API_KEY (e.g. in .env).")
        profile = self.client.get_profile()
        if profile.get("result") != "success":
            raise SystemExit(f"Could not authenticate with Zulip: {profile.get('msg')}")
        self.user_id: int = profile["user_id"]
        self.email: str = profile["email"]
        self.name: str = profile["full_name"]
        self.state = StateStore(cfg.state_file)
        self.agents = ManagedAgents(cfg)
        self.locks: dict[str, asyncio.Lock] = {}
        self.slots = asyncio.Semaphore(cfg.max_concurrent)

    async def call(self, method: str, *args: Any) -> dict[str, Any]:
        """Call a (blocking) Zulip client method without blocking the event loop."""
        res = await asyncio.to_thread(getattr(self.client, method), *args)
        if res.get("result") != "success":
            log.warning("zulip %s failed: %s", method, res.get("msg"))
        return res

    async def react(self, message_id: int, emoji: str, add: bool = True) -> None:
        method = "add_reaction" if add else "remove_reaction"
        await self.call(method, {"message_id": message_id, "emoji_name": emoji})

    async def reply(self, thread: Thread, text: str) -> None:
        for chunk in _chunks(text):
            await self.call("send_message", {**thread.reply_to(), "content": chunk})

    def run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        loop = asyncio.get_running_loop()
        listener = zulip.Client(client="zulip-claude")

        def on_event(event: dict[str, Any]) -> None:
            if event["type"] == "message":
                asyncio.run_coroutine_threadsafe(
                    self.on_message(event["message"], event.get("flags", [])), loop
                )

        log.info("listening as %s (%s)", self.name, self.email)
        # Blocks forever, long-polling Zulip's event queue in a worker thread.
        await asyncio.to_thread(
            listener.call_on_each_event, on_event, ["message"], None, apply_markdown=False
        )

    async def on_message(self, msg: dict[str, Any], flags: list[str]) -> None:
        try:
            await self._on_message(msg, flags)
        except Exception:
            log.exception("failed to handle message %s", msg.get("id"))

    def _is_allowed(self, msg: dict[str, Any]) -> bool:
        allowed = self.cfg.allowed_users
        return not allowed or msg["sender_email"].lower() in allowed or str(msg["sender_id"]) in allowed

    async def _on_message(self, msg: dict[str, Any], flags: list[str]) -> None:
        if msg["sender_id"] == self.user_id:
            return
        if msg["type"] == "stream" and "mentioned" not in flags:
            return
        thread = Thread.from_message(msg, self.email)
        if msg["type"] == "private" and not thread.dm_emails:
            return
        if not self._is_allowed(msg):
            await self.reply(thread, "Sorry, you're not on the list of people who can use me.")
            return

        lock = self.locks.setdefault(thread.key, asyncio.Lock())
        queued = lock.locked()
        if queued:
            await self.react(msg["id"], "hourglass")
        async with lock, self.slots:
            if queued:
                await self.react(msg["id"], "hourglass", add=False)
            await self.react(msg["id"], "working_on_it")
            ok = False
            try:
                ok = await self.handle(msg, thread)
            except Exception as e:
                log.exception("failed in %s", thread.label)
                detail = e.message if isinstance(e, anthropic.APIStatusError) else type(e).__name__
                await self.reply(thread, f"❌ Something went wrong: {detail}. Check the bot's logs.")
            finally:
                await self.react(msg["id"], "working_on_it", add=False)
                await self.react(msg["id"], "check" if ok else "cross_mark")

    def pick_repo(self, content: str, thread: Thread) -> str | None:
        override = REPO_OVERRIDE.search(content)
        repo = override.group(1) if override else self.cfg.repos.get(thread.channel or "") or self.cfg.default_repo
        return github_repo_url(repo) if repo else None

    async def fetch_history(self, msg: dict[str, Any], thread: Thread,
                            after_id: int | None = None) -> list[dict[str, Any]]:
        """Messages in the thread up to `msg`: the recent window, or everything after `after_id`."""
        if after_id is None:
            window = {"anchor": msg["id"], "num_before": self.cfg.context_messages, "num_after": 0}
        else:
            window = {"anchor": after_id, "num_before": 0, "num_after": 200, "include_anchor": False}
        res = await self.call("get_messages", {**window, "narrow": thread.narrow(), "apply_markdown": False})
        history = [m for m in res.get("messages", []) if m["id"] <= msg["id"]]
        if not any(m["id"] == msg["id"] for m in history):
            history.append(msg)
        return history

    def format_conversation(self, history: list[dict[str, Any]]) -> str:
        lines = []
        for m in history:
            when = datetime.fromtimestamp(m["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M")
            who = m["sender_full_name"] + (" (you)" if m["sender_id"] == self.user_id else "")
            lines.append(f"[{when} UTC] {who}:\n{m['content']}\n")
        return "<conversation>\n" + "\n".join(lines) + "</conversation>"

    async def handle(self, msg: dict[str, Any], thread: Thread) -> bool:
        """Send the message to this topic's agent session. Returns whether it succeeded."""
        try:
            repo = self.pick_repo(msg["content"], thread)
        except ValueError as e:
            await self.reply(thread, str(e))
            return False
        state_key = f"{thread.key}|{repo or ''}"
        saved = self.state.get(state_key)
        if repo and not self.cfg.github_token:
            await self.reply(thread, "I need a GitHub token (`GH_TOKEN`) to work on repositories.")
            return False

        started = time.monotonic()
        status = StatusMessage(self, thread)
        await status.post("Working on it" + (f" in `{repo_name(repo)}`" if repo else "") + "…")
        sender = msg["sender_full_name"]

        session_id = saved.get("session_id")
        if session_id and await self.agents.is_usable(session_id):
            # The session remembers everything up to its last turn; send only what's new.
            history = await self.fetch_history(msg, thread, after_id=saved.get("last_message_id"))
            history = [m for m in history if m["sender_id"] != self.user_id]
            prompt = (
                f"New messages in {thread.label} since your last reply:\n\n"
                f"{self.format_conversation(history)}\n\n"
                f"Respond to the last message, from {sender}."
            )
            cost_before = saved.get("cost_usd") or 0.0
        else:
            session_id = await self.agents.create_session(
                title=thread.label, repo_url=repo, metadata={"zulip_thread": thread.key[:512]},
            )
            where = (
                f"The repository {repo} is checked out at /workspace/{repo_name(repo)}."
                if repo else "No repository is attached; you have an empty sandbox."
            )
            history = await self.fetch_history(msg, thread)
            prompt = (
                f"You were mentioned on Zulip in {thread.label} by {sender}. {where}\n\n"
                f"Recent conversation, oldest first:\n\n{self.format_conversation(history)}\n\n"
                f"Respond to the last message, from {sender}."
            )
            cost_before = 0.0

        reader = ZulipReader(self.call, msg, self.user_id)
        result = await self.agents.run_turn(session_id, prompt, status.progress, reader.run)
        cost = await self.agents.list_cost_usd(session_id)
        self.state.set(state_key, session_id=session_id, last_message_id=msg["id"], cost_usd=cost)

        await self.reply(thread, result.text)
        summary = (
            f"{'⚠️ Stopped' if result.is_error else '✅ Done'} in "
            f"{_fmt_duration(time.monotonic() - started)} · {status.steps} step{'' if status.steps == 1 else 's'}"
        )
        if cost is not None:
            summary += f" · ${cost - cost_before:.2f}"
        summary += f" · [trace]({self.agents.trace_url(session_id)})"
        await status.edit(summary)
        return not result.is_error
