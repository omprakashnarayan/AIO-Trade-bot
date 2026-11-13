#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-portfolio-tracker:local}"
CONTAINER="${CONTAINER:-portfolio-tracker}"
APPDATA_DIR="${APPDATA_DIR:-/mnt/user/appdata/portfolio-tracker}"
PORT="${PORT:-5055}"
HOST_IP="${HOST_IP:-192.168.200.196}"
ADMIN_USERNAME="${PORTFOLIO_ADMIN_USERNAME:-}"
ADMIN_PASSWORD_HASH="${PORTFOLIO_ADMIN_PASSWORD_HASH:-}"
SESSION_SECRET="${PORTFOLIO_SESSION_SECRET:-}"
ALLOWED_CIDRS="${PORTFOLIO_ALLOWED_CIDRS:-192.168.200.0/24,127.0.0.1/32}"
MCP_API_TOKEN="${PORTFOLIO_MCP_API_TOKEN:-}"
PUBLIC_HTTPS="${PORTFOLIO_PUBLIC_HTTPS:-1}"
TRUSTED_HOSTS="${PORTFOLIO_TRUSTED_HOSTS:-findash.deepsort.in,192.168.200.196,127.0.0.1,localhost}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT_DIR"
mkdir -p "$APPDATA_DIR"

echo "Building $IMAGE..."
docker build -t "$IMAGE" .

echo "Replacing Unraid container $CONTAINER..."
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

env_args=()
[ -n "$ADMIN_USERNAME" ] && env_args+=(--env "PORTFOLIO_ADMIN_USERNAME=$ADMIN_USERNAME")
[ -n "$ADMIN_PASSWORD_HASH" ] && env_args+=(--env "PORTFOLIO_ADMIN_PASSWORD_HASH=$ADMIN_PASSWORD_HASH")
[ -n "$SESSION_SECRET" ] && env_args+=(--env "PORTFOLIO_SESSION_SECRET=$SESSION_SECRET")
[ -n "$MCP_API_TOKEN" ] && env_args+=(--env "PORTFOLIO_MCP_API_TOKEN=$MCP_API_TOKEN")
env_args+=(--env "PORTFOLIO_PUBLIC_HTTPS=$PUBLIC_HTTPS")
env_args+=(--env "PORTFOLIO_TRUSTED_HOSTS=$TRUSTED_HOSTS")

docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -p "${HOST_IP}:${PORT}:5055" \
  --env PORTFOLIO_TRACKER_HOME=/data \
  --env PORTFOLIO_ALLOWED_CIDRS="$ALLOWED_CIDRS" \
  "${env_args[@]}" \
  --mount "type=bind,src=${APPDATA_DIR},dst=/data" \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --read-only \
  --tmpfs /tmp:size=64m,noexec,nosuid,nodev \
  "$IMAGE" >/dev/null

echo "Waiting for health..."
for _ in $(seq 1 30); do
  status="$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || true)"
  case "$status" in
    healthy)
      echo "Unraid deployment healthy: http://127.0.0.1:${PORT}"
      docker inspect --format 'status={{.State.Status}} health={{.State.Health.Status}} mounts={{json .Mounts}}' "$CONTAINER"
      exit 0
      ;;
    unhealthy)
      docker logs "$CONTAINER" >&2 || true
      exit 1
      ;;
  esac
  sleep 2
done

echo "Container did not become healthy." >&2
docker logs "$CONTAINER" >&2 || true
exit 1
