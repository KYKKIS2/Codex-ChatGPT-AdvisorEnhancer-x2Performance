#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-${G4F_MODEL:-gpt-5-5-thinking}}"
PROVIDER="${G4F_PROVIDER:-OpenaiAccount}"
PORT="${G4F_PORT:-8080}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
G4F="$ROOT/vendor/gpt4free"
PY="$G4F/.venv/bin/python"

if [[ ! -d "$G4F/g4f" ]]; then
  SETUP="$ROOT/setup.sh"
  if [[ ! -f "$SETUP" ]]; then
    echo "gpt4free is not installed and setup.sh was not found." >&2
    exit 1
  fi
  echo "gpt4free is not installed. Running setup.sh first..."
  "$SETUP"
  if [[ ! -d "$G4F/g4f" ]]; then
    echo "setup.sh completed but gpt4free is still missing at $G4F" >&2
    exit 1
  fi
fi

export G4F_PROVIDER="$PROVIDER"
export G4F_MODEL="$MODEL"

if [[ ! -x "$PY" ]]; then
  PY="python3"
fi

if ! "$PY" - "$PORT" <<'PY'
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("127.0.0.1", port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
PY
then
  echo "Port $PORT is already in use. Stop the existing g4f server or set G4F_PORT to another port." >&2
  exit 1
fi

echo "Starting g4f API on http://127.0.0.1:$PORT/v1"
echo "Provider: $PROVIDER"
echo "Model: $MODEL"

cd "$G4F"
ARGS=(-m g4f api --port "$PORT")
if [[ "${G4F_DEBUG:-}" =~ ^(1|true|yes|on)$ ]]; then
  ARGS+=(--debug)
fi
"$PY" "${ARGS[@]}"
