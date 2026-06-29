#!/usr/bin/env bash
#
# run-drevo.sh — pull and start the drevo-server container this MCP talks to.
#
# It is a thin convenience wrapper around `docker run` for people who do not
# want to use docker-compose. It:
#   1. pulls ice1x/drevo:<tag> from Docker Hub (https://hub.docker.com/r/ice1x/drevo),
#   2. creates a host data directory for the redb file,
#   3. runs the container as the current host user (so it can take redb's
#      write lock on the bind-mounted file),
#   4. waits until GET /health is green.
#
# Usage:
#   ./scripts/run-drevo.sh                 # start (tag=latest, port=8080, data=./data)
#   ./scripts/run-drevo.sh stop            # stop and remove the container
#   ./scripts/run-drevo.sh logs            # follow container logs
#
# Override defaults with env vars:
#   DREVO_TAG=0.1.0  DREVO_PORT=9090  DREVO_BOLT_PORT=7687 \
#   DREVO_DATA_DIR=~/drevo_data  DREVO_NAME=drevo  ./scripts/run-drevo.sh
#
set -euo pipefail

IMAGE="ice1x/drevo:${DREVO_TAG:-latest}"
NAME="${DREVO_NAME:-drevo}"
PORT="${DREVO_PORT:-8080}"
BOLT_PORT="${DREVO_BOLT_PORT:-7687}"
DATA_DIR="${DREVO_DATA_DIR:-./data}"

need_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker is not installed or not on PATH" >&2
    exit 1
  fi
}

cmd_stop() {
  need_docker
  echo "Stopping and removing container '$NAME'…"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  echo "Done. Host data dir '$DATA_DIR' was left untouched."
}

cmd_logs() {
  need_docker
  exec docker logs -f "$NAME"
}

cmd_start() {
  need_docker

  # Resolve the host data dir to an absolute path so the bind mount is
  # unambiguous regardless of where the script is invoked from.
  mkdir -p "$DATA_DIR"
  DATA_DIR="$(cd "$DATA_DIR" && pwd)"

  echo "Pulling $IMAGE …"
  docker pull "$IMAGE"

  # Replace any previous instance so re-running is idempotent.
  docker rm -f "$NAME" >/dev/null 2>&1 || true

  echo "Starting container '$NAME' on port $PORT (data: $DATA_DIR) …"
  docker run -d \
    --name "$NAME" \
    --restart unless-stopped \
    -p "${PORT}:8080" \
    -p "${BOLT_PORT}:7687" \
    --user "$(id -u):$(id -g)" \
    -e DREVO_HOST=0.0.0.0 \
    -e DREVO_PORT=8080 \
    -e DREVO_DATA_DIR=/data \
    -v "${DATA_DIR}:/data" \
    "$IMAGE" >/dev/null

  echo -n "Waiting for http://localhost:${PORT}/health "
  for _ in $(seq 1 30); do
    if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1; then
      echo "— up!"
      echo
      echo "  HTTP API : http://localhost:${PORT}"
      echo "  Web UI   : http://localhost:${PORT}/ui"
      echo "  Point the MCP at it with: export DREVO_HTTP_URL=http://localhost:${PORT}"
      exit 0
    fi
    echo -n "."
    sleep 1
  done

  echo >&2
  echo "error: server did not become healthy in time. Check 'docker logs $NAME'." >&2
  exit 1
}

case "${1:-start}" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  logs) cmd_logs ;;
  *)
    echo "usage: $0 [start|stop|logs]" >&2
    exit 2
    ;;
esac
