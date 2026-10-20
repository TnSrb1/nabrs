#!/usr/bin/env bash
# A wrapper over the image shots:local — screenshot + measurements of the invariants.
#
#   scripts/shots/shot.sh --url https://tendersrb.com/plans --lang sr \
#       --out /out/plans_sr.png --json /out/plans_sr.json
#
# The PNG and JSON are written to the host directory SHOTS_OUT (default ~/shots),
# inside the container it is mounted as /out.
#
# The first run (and after any docker cleanup) builds the image itself.
set -euo pipefail

IMAGE="${SHOTS_IMAGE:-shots:local}"
OUT_DIR="${SHOTS_OUT:-$HOME/shots}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$OUT_DIR"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "образ $IMAGE не найден — собираю из $HERE" >&2
  docker build -t "$IMAGE" "$HERE" >&2
fi

# --network host: production is hit by its external address, the local stand by
# 127.0.0.1, both schemes work without changing the command.
exec docker run --rm --network host \
  -v "$OUT_DIR:/out" \
  "$IMAGE" "$@"
