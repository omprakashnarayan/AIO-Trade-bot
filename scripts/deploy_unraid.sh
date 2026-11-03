#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-portfolio-tracker:local}"
CONTAINER="${CONTAINER:-portfolio-tracker}"
APPDATA_DIR="${APPDATA_DIR:-/mnt/user/appdata/portfolio-tracker}"
PORT="${PORT:-5055}"
HOST_IP="${HOST_IP:-192.168.200.196}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$ROOT_DIR"
mkdir -p "$APPDATA_DIR"

echo "Building $IMAGE..."
docker build -t "$IMAGE" .

echo "Replacing Unraid container $CONTAINER..."
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  -p "${HOST_IP}:${PORT}:5055" \
  --env PORTFOLIO_TRACKER_HOME=/data \
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
