# The tutorial's demo server, as a container.
#
# Two things about an OPC UA server in a container are easy to get wrong, and both are
# invisible until a client cannot connect: which address the server binds, and which
# address it tells clients about. This image binds 0.0.0.0 (so the published port works)
# and lets the client use the address it already has. Chapter 31 explains why the
# announced URL is not something a client should connect to.

FROM python:3.13-slim

# uv resolves from uv.lock, so the image installs exactly what the repo tested with.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# Dependencies first: a change to the source must not throw away the installed packages.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

# A server that talks to a plant should not run as root, and does not need to. /data is
# created here rather than left to the container runtime so a mounted volume is writable by
# the same user - a volume that arrives root-owned is a history database that never opens.
RUN useradd --create-home --shell /usr/sbin/nologin opcuactl && \
    mkdir -p /data && \
    chown -R opcuactl:opcuactl /app /opt/venv /data
USER opcuactl

EXPOSE 48401

# The healthcheck is a real OPC UA request, not a port probe: a server can accept a TCP
# connection and still have failed to build its address space.
HEALTHCHECK --interval=15s --timeout=10s --start-period=10s --retries=3 \
    CMD ["opcua-toolkit", "discover", "--endpoint", "opc.tcp://127.0.0.1:48401", "--timeout", "5"]

CMD ["opcua-tutorial-server", "--host", "0.0.0.0", "--port", "48401"]
