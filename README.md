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
  session with the channel's repos mounted (`ZULIP_CLAUDE_REPOS`), each at `/workspace/<name>`.
  Map a channel to several repos (e.g. backend + frontend) and the agent can change them
  together, opening a PR in each. Add `repo:owner/name` to a message to use a different repo
  (repeat it, or comma-separate, for several). Follow-ups in the topic go to the same session, so
  "now add a test too" continues on the same branch.
- **Reads other channels and topics.** The agent can list topics, read a topic and search
  messages ("what did we decide in #infra > deploys?"). The bot runs these calls itself, so
  the Zulip API key stays on your server. Each call is checked against who will see the reply:
  the current channel and public channels are readable (guests: only channels they're
  subscribed to); private channels only from a DM where everyone in it is subscribed; other
  DMs never.
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
3. **Configure:** `cp .env.example .env` and fill in the Zulip credentials, Claude
   authentication (see below), `GH_TOKEN` (a fine-grained token with *Contents* and *Pull
   requests* read/write on the repos), and the channel → repo map. Managed Agents is in beta,
   so your Anthropic organization needs access to it.

**Claude authentication.** Use exactly one of these:
- **`ant auth login`** (easiest locally): log in once and leave the Anthropic variables out.
- **`ANTHROPIC_API_KEY`**.
- **Workload identity federation** (for servers, e.g. on AWS): set the `ANTHROPIC_FEDERATION_*`
  variables from `.env.example`.

Leave the others out entirely. Even an empty `ANTHROPIC_API_KEY=` line takes priority and
breaks the other two.

Only run **one copy of the bot per Zulip bot account**, or every mention gets answered twice.
Use a separate Zulip bot (and test channel) for local testing.

### Run locally

Needs [uv](https://docs.astral.sh/uv/). The bot reads `.env` from the current directory and
keeps its state in `./data`.

```bash
uv sync
uv run zulip-claude setup
uv run zulip-claude
```

The bot only answers while it's running. Mentions sent while it's stopped, or while your
laptop sleeps, are not picked up later.

### Run on a server (Docker)

```bash
docker compose run --rm zulip-claude zulip-claude setup
docker compose up -d --build
```

State lives in the `data` volume; logs: `docker compose logs -f`.

Run `setup` once, and again after changing the model, effort, network or web settings,
rotating `GH_TOKEN`, or upgrading the bot. It updates the existing agent in place.

On **AWS**, any always-on single instance works, for example a small EC2 instance with Docker
or an ECS service with one task. The `/data` directory must survive restarts, so on ECS
mount an EFS volume there, and keep secrets in Secrets Manager or SSM instead of a `.env` file.

## Configuration

| Variable | Default | |
|---|---|---|
| `ZULIP_SITE`, `ZULIP_EMAIL`, `ZULIP_API_KEY` | – | Bot credentials (or `ZULIP_CONFIG` pointing at a zuliprc). |
| `ANTHROPIC_API_KEY` | – | Or `ant auth login`, or the federation variables; see above. |
| `GH_TOKEN` | – | Mounts repos, lets the agent push branches and open PRs. |
| `ZULIP_CLAUDE_REPOS` | `{}` | JSON map of channel name → `owner/name`, or a list of them (GitHub only). |
| `DEFAULT_REPO` | none | Repo(s) for DMs and unmapped channels, comma-separated; empty = empty sandbox. |
| `ALLOWED_USERS` | everyone | Comma-separated emails or Zulip user ids. |
| `CLAUDE_MODEL` | `claude-opus-5-5` | |
| `AGENT_EFFORT` | `high` | `low` … `max`. |
| `AGENT_NETWORK` | `limited` | `limited` allows package registries and GitHub; `unrestricted` allows everything. |
| `AGENT_WEB` | off | Let the agent use web search and fetch. |
| `SESSION_BUDGET_USD` | none | Hard spend cap per topic's agent session. |
| `AGENT_TIMEOUT_SECONDS` | `3600` | Interrupt a run that takes longer. |
| `MAX_CONCURRENT_RUNS` | `3` | Agent runs in progress at once. |
| `DATA_DIR` | `./data` (`/data` in Docker) | Agent IDs and the topic → session map. |
| `CONTEXT_MESSAGES` | `50` | History given to a new session. |
| `CONSOLE_WORKSPACE` | `default` | Workspace ID used in trace links. |

`CLAUDE_MODEL`, `AGENT_EFFORT`, `AGENT_NETWORK`, `AGENT_WEB` and `GH_TOKEN` are stored on the
Managed Agents objects, so run `zulip-claude setup` again after changing them.

## Security

- Give `GH_TOKEN` access only to the repos the bot should touch, and protect default branches
  so nothing merges without review.
- Set `ALLOWED_USERS` unless everyone in your Zulip org should be able to drive it.
- Set `SESSION_BUDGET_USD` to cap spend per topic.
- Anything in a topic, another channel the agent reads, or a repo can try to steer the agent
  (prompt injection). Don't map channels where outsiders post to repos with powerful tokens.
