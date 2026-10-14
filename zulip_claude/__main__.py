import argparse
import json
import logging
import os

from .config import Config


def main() -> None:
    parser = argparse.ArgumentParser(prog="zulip-claude")
    parser.add_argument(
        "command", nargs="?", default="run", choices=["run", "setup"],
        help="run the bot (default), or create/update the Managed Agents agent, environment and vault",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.from_env()
    if args.command == "setup":
        from .managed import setup

        print(json.dumps(setup(cfg), indent=2))
        print(f"Saved to {cfg.ids_file}. Start the bot with `zulip-claude`.")
        return

    from .bot import Bot

    Bot(cfg).run()


if __name__ == "__main__":
    main()
