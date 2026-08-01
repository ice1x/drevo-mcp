#!/usr/bin/env bash
#
# restart-drevo.sh — restart the local drevo container in ONE command.
#
# Sibling of run-drevo.sh, with two deliberate differences:
#   1. it never fetches a new image — it runs whatever image tag is present
#      LOCALLY, so a freshly `docker build`-ed image is not clobbered by an
#      older published one (run-drevo.sh always fetches the latest);
#   2. it force-recreates the container (`docker rm -f` then `docker run`).
# The redb data lives on the host bind mount and is never touched by the swap.
#
# Same env-var conventions as run-drevo.sh:
#   DREVO_NAME       container name           (default: drevo)
#   DREVO_IMAGE      full image ref           (default: ice1x/drevo:$DREVO_TAG)
#   DREVO_TAG        tag when DREVO_IMAGE unset(default: latest)
#   DREVO_PORT       host port -> HTTP 8080   (default: 8080)
#   DREVO_BOLT_PORT  host port -> Bolt 7687   (default: 7687)
#   DREVO_DATA_DIR   host dir bind-mounted    (default: ./data)
#
# IMPORTANT: DREVO_BOLT_PORT must match the host port your MCP client's
# DREVO_BOLT_URL points at. If the MCP connects to bolt://localhost:7688, run:
#   DREVO_BOLT_PORT=7688 DREVO_DATA_DIR=~/drevo_data ./scripts/restart-drevo.sh
#
# Persistent config file (so a normal restart is a SINGLE bare command): all of
# the above vars — plus the embeddings-proxy config and its API key — can live
# in ~/.drevo.env (see .drevo.env.example; chmod 600 it). It is sourced first,
# so `./scripts/restart-drevo.sh` alone brings drevo up with your config.
# Override the path with DREVO_ENV_FILE.
#
set -euo pipefail

# Load ~/.drevo.env (or $DREVO_ENV_FILE) if present, before defaults are applied.
ENV_FILE="${DREVO_ENV_FILE:-$HOME/.drevo.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

NAME="${DREVO_NAME:-drevo}"
IMAGE="${DREVO_IMAGE:-ice1x/drevo:${DREVO_TAG:-latest}}"
PORT="${DREVO_PORT:-8080}"
BOLT_PORT="${DREVO_BOLT_PORT:-7687}"
DATA_DIR="${DREVO_DATA_DIR:-./data}"

command -v docker >/dev/null 2>&1 || { echo "restart-drevo: docker not found on PATH" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "restart-drevo: docker daemon unreachable — is Docker running?" >&2; exit 1; }

mkdir -p "$DATA_DIR"
DATA_DIR="$(cd "$DATA_DIR" && pwd)"

# Forward the embeddings-proxy config (issue #217) into the container when set,
# so POST /v1/embeddings works. No-op (endpoint stays 503) when unset. Requires
# an image built with the `embeddings-proxy` feature — the deploy image ships it
# by default. The upstream is taken ONLY from config, never from a request
# (SSRF boundary — OWASP A10).
EMB_ENV=()
for _v in DREVO_EMBEDDINGS_UPSTREAM DREVO_EMBEDDINGS_API_KEY DREVO_EMBEDDINGS_MODEL; do
  [ -n "${!_v:-}" ] && EMB_ENV+=(-e "${_v}=${!_v}")
done
[ "${#EMB_ENV[@]}" -gt 0 ] && echo "embeddings proxy: forwarding ${DREVO_EMBEDDINGS_UPSTREAM:-<upstream unset>}"

echo "Recreating '$NAME' from $IMAGE  (HTTP :$PORT, Bolt :$BOLT_PORT, data $DATA_DIR) …"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  --restart unless-stopped \
  -p "${PORT}:8080" -p "${BOLT_PORT}:7687" \
  --user "$(id -u):$(id -g)" \
  -e DREVO_HOST=0.0.0.0 \
  -e DREVO_PORT=8080 \
  -e DREVO_BOLT_PORT=7687 \
  -e DREVO_DATA_DIR=/data \
  ${EMB_ENV[@]+"${EMB_ENV[@]}"} \
  -v "${DATA_DIR}:/data" \
  "$IMAGE" >/dev/null

echo -n "Waiting for http://localhost:${PORT}/health "
for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    echo "OK"
    echo "  HTTP : http://localhost:${PORT}"
    echo "  Bolt : bolt://localhost:${BOLT_PORT}   (point the MCP's DREVO_BOLT_URL here)"
    echo "  Data : ${DATA_DIR}"
    exit 0
  fi
  echo -n "."
  sleep 1
done
echo
echo "restart-drevo: container did not become healthy within 30s — check: docker logs ${NAME}" >&2
exit 1
