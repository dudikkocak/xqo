# xqo-lite: core + office + Claude Agent SDK. No toolchain or docker-CLI stages (those plugins are not included).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    XQO_DATA=/data XQO_HOST=0.0.0.0 XQO_PORT=8000 \
    XQO_SANDBOX=local

# the Claude Agent SDK (and mcp) are regular dependencies of xqo-lite: agents run as Claude Code sessions
# WITH_NODE=1 adds Node.js, so local MCP servers started with npx work inside the container
ARG WITH_NODE=0
# VERSION labels the image; build it as xqo:<version> (see docs/OPERATIONS.md) so an upgrade can be rolled back
ARG VERSION=dev
LABEL org.opencontainers.image.title="xqo-lite" org.opencontainers.image.version="${VERSION}"
WORKDIR /app
COPY pyproject.toml README.md ./
COPY xqo ./xqo
# uv installs the app and creates the sandbox's per-task virtual environments (always uv, never python -m venv / pip)
COPY --from=astral/uv:0.9.30 /uv /uvx /usr/local/bin/
# Node comes from Debian
RUN apt-get update && apt-get install -y --no-install-recommends git \
      $( [ "$WITH_NODE" = "1" ] && echo nodejs npm ) \
 && rm -rf /var/lib/apt/lists/* \
 && uv pip install --system --no-cache . \
 && useradd --create-home --uid 1000 app && mkdir -p /data /exchange && chown app /data /exchange

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# strip Windows line endings (a checkout on Windows may add them), which would break the script
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && chmod 755 /usr/local/bin/docker-entrypoint.sh
# The entrypoint starts as root only to make /data writable, then runs xqo as the app user (see the script)
WORKDIR /data
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)" || exit 1
# Put your own agents.json in /data to override the packaged roster.
CMD ["xqo", "--config", "/data/agents.json", "serve"]
