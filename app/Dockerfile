FROM python:3.13-slim

# Docker CLI + compose v2 plugin, from the official static binaries.
# The app brings the stack up itself, so it needs a client.
# Debian packages are not used: the CLI package name changes between releases.
ARG DOCKER_VERSION=27.5.1
ARG COMPOSE_VERSION=v2.32.4
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" \
      | tar -xz -C /tmp docker/docker \
 && mv /tmp/docker/docker /usr/local/bin/docker \
 && rm -rf /tmp/docker \
 && mkdir -p /usr/local/lib/docker/cli-plugins \
 && curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose \
 && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose \
 && docker --version && docker compose version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV STATE_PATH=/data/config.yaml \
    DATA_DIR=/data \
    HOST_SYS=/host/sys \
    PORT=8000

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD curl -fsS http://localhost:8000/healthz || exit 1
CMD ["python", "-u", "app.py"]
