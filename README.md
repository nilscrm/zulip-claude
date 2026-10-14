# zulip-claude

Mention Claude in a Zulip topic and every message goes to a Claude agent running on
[Claude Managed Agents](https://platform.claude.com/docs/en/managed-agents), with your repo
checked out. It answers questions, changes code and opens PRs, like Claude in Slack.

```
you:     @**Claude** what's the difference between our two retry helpers?
Claude:  `retry()` backs off exponentially; `retry_once()` is a single immediate retry…

you:     @**Claude** the signup form 500s when the email has a plus sign, can you fix it?
Claude:  ✅ Done in 3m 12s · 41 steps · $0.84 · trace
Claude:  Fixed: the email validator rejected `+`. Added a test and opened
         https://github.com/your-org/backend/pull/123
```

## How it works

```
Zulip ──mention──▶ this bot (your server) ──▶ Managed Agents session for the topic
                                               (Anthropic's sandbox, repo checked out)
      ◀──────────── reply + PR link ─────────────────────┘
```

- **Agent work** runs on Anthropic's infrastructure. Each topic gets its own long-lived
  session with the channel's repo mounted (`ZULIP_CLAUDE_REPOS`). Add `repo:owner/name` to a
  message to use a different repo. Follow-ups in the topic go to the same session, so
  "now add a test too" continues on the same branch.
- **Credentials never enter the sandbox.** Git pushes and the GitHub `create_pull_request`
  tool are authenticated by Anthropic-side proxies.
- **Feedback:** 🛠️ on your message while working; a status message shows the agent's current
  step ("Running `pytest`") and ends with time, steps, cost and a link to the session trace
  in the Anthropic Console.
- **Unattended safety:** built-in tools use the `auto` permission policy. Safe calls run,
  high-risk ones are denied, and anything that would wait for a human is denied, with
  a note asking the agent to explain in its reply instead.
- **Your server** only relays messages. It needs no toolchains and very little CPU or RAM.

## Setup

1. **Create the bot in Zulip:** *Personal settings → Bots → Add a new bot*, type **Generic
   bot**, named e.g. "Claude". Copy its email and API key.
2. **Subscribe the bot to the channels** where it should respond. A bot only receives
   mentions in channels it's subscribed to.
3. **Configure:** `cp .env.example .env` and fill in the Zulip credentials, `ANTHROPIC_API_KEY`
   (Managed Agents is in beta; the key's organization needs access), `GH_TOKEN` (a fine-grained
   token with *Contents* and *Pull requests* read/write on the repos), and the channel → repo map.
4. **Create the agent, environment and vault** (once, and again after changing model, effort,
   network or web settings, or rotating `GH_TOKEN`):

   ```bash
   docker compose run --rm zulip-claude zulip-claude setup
   ```

   This stores their IDs in the `data` volume. Re-running it updates them in place.
5. **Run:**

   ```bash
   docker compose up -d --build
   ```

   Logs: `docker compose logs -f`.

## Configuration

| Variable | Default | |
|---|---|---|
| `ZULIP_SITE`, `ZULIP_EMAIL`, `ZULIP_API_KEY` | – | Bot credentials (or `ZULIP_CONFIG` pointing at a zuliprc). |
| `ANTHROPIC_API_KEY` | – | Creates and drives the agent sessions. |
| `GH_TOKEN` | – | Mounts repos, lets the agent push branches and open PRs. |
| `ZULIP_CLAUDE_REPOS` | `{}` | JSON map of channel name → `owner/name` (GitHub only). |
| `DEFAULT_REPO` | none | Repo for DMs and unmapped channels; empty = empty sandbox. |
| `ALLOWED_USERS` | everyone | Comma-separated emails or Zulip user ids. |
| `CLAUDE_MODEL` | `claude-opus-5-5` | |
| `AGENT_EFFORT` | `high` | `low` … `max`. |
| `AGENT_NETWORK` | `limited` | `limited` allows package registries and GitHub; `unrestricted` allows everything. |
| `AGENT_WEB` | off | Let the agent use web search and fetch. |
| `SESSION_BUDGET_USD` | none | Hard spend cap per topic's agent session. |
| `AGENT_TIMEOUT_SECONDS` | `3600` | Interrupt a run that takes longer. |
| `MAX_CONCURRENT_RUNS` | `3` | Agent runs in progress at once. |
| `CONTEXT_MESSAGES` | `50` | History given to a new session. |
| `CONSOLE_WORKSPACE` | `default` | Workspace ID used in trace links. |

`CLAUDE_MODEL`, `AGENT_EFFORT`, `AGENT_NETWORK`, `AGENT_WEB` and `GH_TOKEN` are stored on the
Managed Agents objects, so run `zulip-claude setup` again after changing them.

## Security

- Give `GH_TOKEN` access only to the repos the bot should touch, and protect default branches
  so nothing merges without review.
- Set `ALLOWED_USERS` unless everyone in your Zulip org should be able to drive it.
- Set `SESSION_BUDGET_USD` to cap spend per topic.
- Anything in a topic or a repo can try to steer the agent (prompt injection). Don't map
  channels where outsiders post to repos with powerful tokens.

## Local development

```bash
uv venv --python 3.12 && uv pip install -e .
set -a; source .env; set +a
DATA_DIR=./data zulip-claude setup
DATA_DIR=./data zulip-claude
```
