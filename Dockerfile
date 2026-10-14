FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY zulip_claude ./zulip_claude
RUN pip install --no-cache-dir .

# The agent's work runs in Anthropic-hosted sandboxes, so this container is only a relay
# between Zulip and the Claude API. /data keeps the agent IDs and topic -> session map.
RUN useradd --create-home --uid 1000 bot && mkdir -p /data && chown bot:bot /data
USER bot
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data

CMD ["zulip-claude"]
