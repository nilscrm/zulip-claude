import argparse
import json
import logging
import os

import anthropic

from .config import Config, load_env_file


def check_claude_auth() -> None:
    """Fail fast with a readable message if no Claude credentials are configured."""
    try:
        anthropic.Anthropic().models.list(limit=1)
    except TypeError as e:
        if "authentication" not in str(e):
            raise
        raise SystemExit(
            "No Claude credentials found: run `ant auth login`, set ANTHROPIC_API_KEY, "
            "or set the ANTHROPIC_FEDERATION_* variables (see .env.example)."
        )
    except anthropic.AuthenticationError as e:
        raise SystemExit(f"Claude rejected the credentials: {e.message}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="zulip-claude")
    parser.add_argument(
        "command", nargs="?", default="run", choices=["run", "setup"],
        help="run the bot (default), or create/update the Managed Agents agent, environment and vault",
    )
    args = parser.parse_args()
    load_env_file()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    check_claude_auth()
    if args.command == "setup":
        from .managed import setup

        print(json.dumps(setup(cfg), indent=2))
        print(f"Saved to {cfg.ids_file}. Start the bot with `zulip-claude`.")
        return

    from .bot import Bot

    Bot(cfg).run()


if __name__ == "__main__":
    main()
