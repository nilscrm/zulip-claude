"""Configuration, read entirely from environment variables."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


def load_env_file(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines from a local .env file, for running outside Docker.

    Variables already set in the environment win. Empty values are skipped: an
    empty ANTHROPIC_API_KEY would otherwise shadow an `ant auth login` profile or
    workload identity federation.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()  # trailing comment
        if value and key not in os.environ:
            os.environ[key] = value


def _optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def _repo_list(value: str | list[str]) -> list[str]:
    """A repo spec from config: one `owner/name`, a comma-separated string, or a JSON list."""
    items = value.split(",") if isinstance(value, str) else value
    return [r.strip() for r in items if r.strip()]


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    data_dir: Path
    # Zulip channel name -> GitHub repos. Agent sessions in a mapped channel get them all mounted.
    repos: dict[str, list[str]]
    # Repos for DMs and unmapped channels. Empty means the agent gets an empty sandbox.
    default_repos: list[str]
    # Lowercased emails or user ids allowed to trigger the bot. Empty means everyone.
    allowed_users: frozenset[str]
    model: str
    agent_effort: str
    # GitHub token: mounts repos, lets the agent push branches and open PRs.
    github_token: str | None
    # "limited" (package registries + GitHub only) or "unrestricted".
    agent_network: str
    agent_web: bool
    # Hard spend cap per agent session (one session per topic), in USD.
    session_budget_usd: float | None
    run_timeout_s: int
    max_concurrent: int
    context_messages: int
    # Anthropic Console workspace for session trace links ("default" for the Default workspace).
    console_workspace: str

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def ids_file(self) -> Path:
        """IDs of the agent, environment and vault created by `zulip-claude setup`."""
        return self.data_dir / "managed.json"

    @classmethod
    def from_env(cls) -> Config:
        budget = _optional("SESSION_BUDGET_USD")
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR", "data")),
            repos={
                channel: _repo_list(spec)
                for channel, spec in json.loads(os.environ.get("ZULIP_CLAUDE_REPOS", "{}")).items()
            },
            default_repos=_repo_list(os.environ.get("DEFAULT_REPO", "")),
            allowed_users=frozenset(
                e.strip().lower()
                for e in os.environ.get("ALLOWED_USERS", "").split(",")
                if e.strip()
            ),
            model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
            agent_effort=os.environ.get("AGENT_EFFORT", "high"),
            github_token=_optional("GH_TOKEN") or _optional("GITHUB_TOKEN"),
            agent_network=os.environ.get("AGENT_NETWORK", "limited"),
            agent_web=_flag("AGENT_WEB"),
            session_budget_usd=float(budget) if budget else None,
            run_timeout_s=int(os.environ.get("AGENT_TIMEOUT_SECONDS", "3600")),
            max_concurrent=int(os.environ.get("MAX_CONCURRENT_RUNS", "3")),
            context_messages=int(os.environ.get("CONTEXT_MESSAGES", "50")),
            console_workspace=os.environ.get("CONSOLE_WORKSPACE", "default"),
        )
