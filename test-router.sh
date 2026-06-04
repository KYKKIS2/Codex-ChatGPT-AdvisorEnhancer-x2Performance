#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROUTER="$ROOT/codex-skill/external-advisor/scripts/router.py"

assert_route() {
  local expected="$1"
  local expected_kind="${2:-}"
  if [[ "$#" -gt 1 ]]; then
    shift
  fi
  shift
  local data route kind
  data="$(python3 "$ROUTER" --json "$@")"
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

assert_route "no-advisor" "" --prompt "fix typo in README"
assert_route "single-advisor" "" --prompt "Decide the architecture for advisor memory"
assert_route "conclave" "" --prompt "Review security and privacy risks for token storage"
assert_route "verifier" "verifier-loop" --failed-tests --prompt "pytest failed after the patch"
assert_route "conclave" "" --prompt "Which model or framework should I use for training?"
assert_route "single-advisor" "" --before-final --draft "Draft answer" --prompt "Review before final"
assert_route "machine-json-verifier" "verifier-loop" --machine-verify --prompt "Verify this patch"
