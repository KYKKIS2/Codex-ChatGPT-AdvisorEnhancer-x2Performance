#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-${G4F_MODEL:-gpt-5-thinking}}"
PROVIDER="${G4F_PROVIDER:-OpenaiAccount}"
PORT="${G4F_PORT:-8080}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
G4F="$ROOT/vendor/gpt4free"

if [[ ! -d "$G4F/g4f" ]]; then
  echo "gpt4free is not installed. Run ./setup.sh first." >&2
  exit 1
fi

export G4F_PROVIDER="$PROVIDER"
export G4F_MODEL="$MODEL"

echo "Starting g4f API on http://localhost:$PORT/v1"
echo "Provider: $PROVIDER"
echo "Model: $MODEL"

cd "$G4F"
python3 -m g4f api --port "$PORT" --debug

