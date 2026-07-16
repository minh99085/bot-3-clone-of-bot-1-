# Hermes v2 Paper — production image (bot + dashboard)
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HERMES_PAPER_ONLY=1 \
    HERMES_LIVE=0 \
    HERMES_CAPITAL=2000 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl \
      ca-certificates \
      gosu \
      git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY hermes/ hermes/
COPY connectors/ connectors/
COPY strategy/ strategy/
COPY autonomy/ autonomy/
COPY risk/ risk/
COPY models/ models/
COPY backtest/ backtest/
COPY paper_trader/ paper_trader/
COPY utils/ utils/
COPY config/ config/
COPY knowledge/ knowledge/
COPY dashboard.py .
COPY .streamlit/ .streamlit/
COPY scripts/docker_entrypoint_bot.sh scripts/docker_entrypoint_dashboard.sh scripts/
COPY scripts/docker_entrypoint_wrapper.sh scripts/
COPY scripts/healthcheck_bot.sh scripts/

RUN chmod +x scripts/*.sh \
    && mkdir -p data/paper data/live data/handoff logs \
    && useradd --create-home --uid 10001 hermes \
    && chown -R hermes:hermes /app

ENTRYPOINT ["scripts/docker_entrypoint_wrapper.sh"]

EXPOSE 8501 8080

# Default: bot overnight paper loop (overridden by compose)
CMD ["scripts/docker_entrypoint_bot.sh"]
