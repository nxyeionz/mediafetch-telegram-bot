FROM brainicism/bgutil-ytdlp-pot-provider:latest AS pot_provider
FROM node:22-slim AS node_runtime

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    YTDLP_POT_PROVIDER_URL=http://127.0.0.1:4416

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node_runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=pot_provider /app /pot_server

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mediafetch ./mediafetch

RUN mkdir -p /data/jobs

CMD ["python", "-m", "mediafetch.main"]

