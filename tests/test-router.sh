#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROUTER="$ROOT/codex-skill/external-advisor/scripts/router.py"
PROJECT="$(mktemp -d)"
FAKE_BIN="$(mktemp -d)"
trap 'rm -rf "$PROJECT" "$FAKE_BIN"' EXIT

printf '#!/usr/bin/env bash\necho fake devspace\n' > "$FAKE_BIN/devspace"
chmod +x "$FAKE_BIN/devspace"

assert_route() {
  local expected="$1"
  local expected_kind="${2:-}"
  if [[ "$#" -gt 1 ]]; then
    shift
  fi
  shift
  local data route kind
  data="$(python3 "$ROUTER" --project-dir "$PROJECT" --json "$@")"
  route="$(printf '%s' "$data" | python3 -c 'import json,sys; print(json.load(sys.stdin)["route"])')"
  if [[ "$route" != "$expected" ]]; then
    echo "Expected route '$expected' but got '$route' for args: $*" >&2
    exit 1
  fi
  if [[ -n "$expected_kind" ]]; then
    kind="$(printf '%s' "$data" | python3 -c 'import json,sys; print(json.load(sys.stdin)["command_kind"])')"
    if [[ "$kind" != "$expected_kind" ]]; then
      echo "Expected command kind '$expected_kind' but got '$kind' for args: $*" >&2
      exit 1
    fi
  fi
  echo "Route OK: $expected"
}

assert_route_mode() {
  local expected_route="$1"
  local expected_mode="$2"
  shift 2
  local data route mode reasons
  data="$(python3 "$ROUTER" --project-dir "$PROJECT" --json "$@")"
  route="$(printf '%s' "$data" | python3 -c 'import json,sys; print(json.load(sys.stdin)["route"])')"
  mode="$(printf '%s' "$data" | python3 -c 'import json,sys; print(json.load(sys.stdin)["mode"])')"
  reasons="$(printf '%s' "$data" | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["reasons"]))')"
  if [[ "$route" != "$expected_route" || "$mode" != "$expected_mode" ]]; then
    echo "Expected route/mode '$expected_route/$expected_mode' but got '$route/$mode' for args: $*" >&2
    exit 1
  fi
  if [[ "$reasons" == *"security/privacy topic terms"* ]]; then
    echo "Prompt topic words unexpectedly selected a security route: $reasons" >&2
    exit 1
  fi
  echo "Route mode OK: $expected_route/$expected_mode"
}

assert_route "no-advisor" "" --prompt "fix typo in README"
assert_route "single-advisor" "" --prompt "Decide the architecture for advisor memory"
assert_route "single-advisor" "" --prompt "Review security and privacy risks for token storage"
assert_route "single-advisor" "" --allow-sensitive-advisor --prompt "Review security and privacy risks for token storage"
assert_route "single-advisor" "" --prompt "Prepare-goal planning review for a Shopify theme using the owner's authoritative annotated PDF requirements."
assert_route "no-advisor" "" --prompt "Give a concise recommendation for a world-class homepage."
assert_route "verifier" "verifier-loop" --failed-tests --prompt "pytest failed after the patch"
assert_route "conclave" "" --prompt "Which model or framework should I use for training?"
assert_route_mode "conclave" "model-choice" --prompt-only --prompt "Review sequence model training with ordered event tokens and a frozen HGB residual Transformer comparison."
assert_route "single-advisor" "" --before-final --draft "Draft answer" --prompt "Review before final"
assert_route "machine-json-verifier" "verifier-loop" --machine-verify --prompt "Verify this patch"
assert_route "single-advisor" "advisor" --agent-allowed-root "$PROJECT" --agent-bridge-executable "$FAKE_BIN/devspace" --prompt "Decide the architecture for advisor memory"
assert_route "single-advisor" "" --prompt-only --agent-allowed-root "$PROJECT" --agent-bridge-executable "$FAKE_BIN/devspace" --prompt "Decide the architecture for advisor memory"
assert_route "single-advisor" "" --agent-allowed-root "$PROJECT" --agent-bridge-executable "$FAKE_BIN/missing-devspace" --prompt "Decide the architecture for advisor memory"

if python3 "$ROUTER" \
  --project-dir "$PROJECT" \
  --agent-allow-shell \
  --prompt "Review this repository." >/tmp/advisor-router-shell-out.txt 2>/tmp/advisor-router-shell-err.txt; then
  echo "Expected the deprecated repo-aware shell flag to be rejected." >&2
  exit 1
fi
grep -q "mechanically read-only" /tmp/advisor-router-shell-err.txt
