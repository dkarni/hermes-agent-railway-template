FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git ffmpeg && \
    rm -rf /var/lib/apt/lists/*

RUN apt-get update && \
    apt-get install -y --no-install-recommends gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_24.x nodistro main" > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get purge -y --auto-remove gnupg && \
    rm -rf /var/lib/apt/lists/*

RUN node --version && npm --version

# Browser automation for Hermes browser tool (Browserbase + local headless)
RUN npm install -g agent-browser && agent-browser --version

# Pin hermes-agent to a specific commit so rebuilds are reproducible and can be
# rolled back. Bump HERMES_REF to update. 22c5048d (2026-07-03) includes the
# Telegram streamed-reply overflow/continuation and rich-format fixes.
ARG HERMES_REF=22c5048d9c6a3d6e3d6c786ef014a0998ca2a0c3
RUN mkdir -p /tmp/hermes-agent && cd /tmp/hermes-agent && \
    git init -q && \
    git remote add origin https://github.com/NousResearch/hermes-agent.git && \
    git fetch --depth 1 origin "${HERMES_REF}" && \
    git checkout -q FETCH_HEAD && \
    uv pip install --system --no-cache -e ".[all]" && \
    rm -rf /tmp/hermes-agent/.git

COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

RUN mkdir -p /data/.hermes

COPY server.py /app/server.py
COPY templates/ /app/templates/
COPY start.sh /app/start.sh
COPY bootstrap_marco.py /app/bootstrap_marco.py
COPY bootstrap_max.py /app/bootstrap_max.py
COPY bootstrap_files/ /app/bootstrap_files/
RUN chmod +x /app/start.sh

ENV HOME=/data
ENV HERMES_HOME=/data/.hermes

CMD ["/app/start.sh"]
