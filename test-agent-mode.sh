#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$ROOT/codex-skill/external-advisor/scripts"
PROJECT_ROOT="$(mktemp -d)"
FAKE_BIN="$(mktemp -d)"
OUTSIDE="$(mktemp -d)"
CONFIG="$PROJECT_ROOT/agent-config.json"
SANITIZED_CONFIG="$PROJECT_ROOT/agent-config-sanitized.json"
WORKSPACES="$PROJECT_ROOT/workspaces"
trap 'rm -rf "$PROJECT_ROOT" "$FAKE_BIN" "$OUTSIDE"' EXIT

PROJECT="$PROJECT_ROOT/project with spaces"
mkdir -p "$PROJECT"
git -C "$PROJECT" init -q -b main
git -C "$PROJECT" config user.email "advisor-test@example.invalid"
git -C "$PROJECT" config user.name "Advisor Test"
printf 'safe\n' > "$PROJECT/README.md"
git -C "$PROJECT" add README.md
git -C "$PROJECT" commit -m "initial" >/dev/null

printf '#!/usr/bin/env bash\necho fake devspace\n' > "$FAKE_BIN/devspace"
chmod +x "$FAKE_BIN/devspace"

python3 "$SCRIPTS/agent_mode.py" \
  --doctor \
  --project-dir "$PROJECT" \
  --allowed-root "$PROJECT_ROOT" \
  --bridge-executable "$FAKE_BIN/devspace" >/tmp/advisor-agent-doctor.txt

grep -q "available: yes" /tmp/advisor-agent-doctor.txt
grep -q "dry_run: no tunnel opened" /tmp/advisor-agent-doctor.txt

python3 "$SCRIPTS/agent_mode.py" \
  --print-handoff \
  --project-dir "$PROJECT" \
  --allowed-root "$PROJECT_ROOT" \
  --bridge-executable "$FAKE_BIN/devspace" \
  --task "Review the architecture." >/tmp/advisor-agent-handoff.txt

grep -q "Review-only default" /tmp/advisor-agent-handoff.txt
grep -q "Open exactly one workspace" /tmp/advisor-agent-handoff.txt
grep -q "Review the architecture." /tmp/advisor-agent-handoff.txt
grep -q '"mode": "worktree"' /tmp/advisor-agent-handoff.txt

PYTHONPATH="$SCRIPTS" python3 - "$PROJECT" "$PROJECT_ROOT" "$FAKE_BIN/devspace" "$OUTSIDE" <<'PY'
import os
import sys
from pathlib import Path

import agent_mode

project = Path(sys.argv[1])
root = Path(sys.argv[2])
bridge = Path(sys.argv[3])
outside = Path(sys.argv[4])

def must_fail(result, label):
    if result.ok:
        raise SystemExit(f"{label} unexpectedly passed: {result}")

def must_pass(result, label):
    if not result.ok:
        raise SystemExit(f"{label} unexpectedly failed: {result.errors}")

must_fail(agent_mode.validate_allowed_root(Path.home()), "home root")
must_fail(agent_mode.validate_allowed_root(Path("/")), "filesystem root")

for name in [".ssh", ".codex-advisor", "har_and_cookies", "wallets", "google-chrome"]:
    path = root / name
    path.mkdir(exist_ok=True)
    must_fail(agent_mode.validate_allowed_root(path), name)

for name in [".env.local", "chat.har", "auth_openaichat.json", "my-private-key.pem"]:
    path = root / name
    path.write_text("secret", encoding="utf-8")
    must_fail(agent_mode.validate_project_under_allowed_root(path, root), name)
    path.unlink()

must_pass(agent_mode.validate_project_under_allowed_root(project, root), "project with spaces")

scan = agent_mode.scan_project_secrets(project)
if not scan.ok:
    raise SystemExit(f"clean project failed secret scan: {scan.to_dict()}")

route_dir = project / ".codex-advisor" / "routes"
route_dir.mkdir(parents=True)
(project / ".codex-advisor" / "latest-route.json").write_text("{}", encoding="utf-8")
(route_dir / "safe-route.json").write_text("{}", encoding="utf-8")
scan = agent_mode.scan_project_secrets(project)
if not scan.ok:
    raise SystemExit(f"route-only advisor state failed secret scan: {scan.to_dict()}")
(project / ".codex-advisor" / "latest-response.md").write_text("advisor response", encoding="utf-8")
scan = agent_mode.scan_project_secrets(project)
if scan.ok or not any(".codex-advisor/latest-response.md" in finding.path for finding in scan.findings):
    raise SystemExit("advisor transcript/state was not blocked")
(project / ".codex-advisor" / "latest-response.md").unlink()

secret_file = project / ".env.local"
secret_file.write_text("PASSWORD=definitely-secret-value", encoding="utf-8")
scan = agent_mode.scan_project_secrets(project)
if scan.ok or not any(finding.path == ".env.local" for finding in scan.findings):
    raise SystemExit(".env.local was not blocked by secret scan")
override_scan = agent_mode.scan_project_secrets(project, allow_sensitive_project=True)
if not override_scan.ok or not override_scan.warnings:
    raise SystemExit("sensitive-project override did not allow findings with warnings")
secret_file.unlink()

content_secret = project / "config.json"
content_secret.write_text('{"api_key":"abcdefghijklmnopqrstuvwxyz123456"}', encoding="utf-8")
scan = agent_mode.scan_project_secrets(project)
if scan.ok or not any(finding.kind == "content" for finding in scan.findings):
    raise SystemExit("secret-looking content was not blocked")
content_secret.unlink()

outside_project = outside / "repo"
outside_project.mkdir()
link = root / "linked-repo"
try:
    link.symlink_to(outside_project, target_is_directory=True)
except (OSError, NotImplementedError):
    link = None
if link is not None:
    must_fail(agent_mode.validate_project_under_allowed_root(link, root), "symlink escape")
secret_target = outside / "id_rsa"
secret_target.write_text("secret", encoding="utf-8")
secret_link = project / "linked-secret"
try:
    secret_link.symlink_to(secret_target)
except (OSError, NotImplementedError):
    secret_link = None
if secret_link is not None:
    scan = agent_mode.scan_project_secrets(project)
    if scan.ok or not any(finding.kind == "symlink" for finding in scan.findings):
        raise SystemExit("symlink escape to secret was not blocked")
    secret_link.unlink()

confusing_root = root / "parent"
confusing_child = root / "parent-other" / "repo"
confusing_root.mkdir()
confusing_child.mkdir(parents=True)
must_fail(agent_mode.validate_project_under_allowed_root(confusing_child, confusing_root), "parent child confusion")

if not agent_mode.path_is_same_or_child(Path("/Tmp/Allowed/Repo"), Path("/tmp/allowed"), case_insensitive=True):
    raise SystemExit("case-insensitive containment failed")
if agent_mode.path_is_same_or_child(Path("/tmp/allowed-other/repo"), Path("/tmp/allowed"), case_insensitive=True):
    raise SystemExit("case-insensitive parent/child confusion passed")

project_bridge = project / "devspace"
project_bridge.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
project_bridge.chmod(0o700)
bad_bridge = agent_mode.check_bridge_executable(str(project_bridge), project_dir=project)
if bad_bridge.ok:
    raise SystemExit("project-local bridge shim was accepted")
good_bridge = agent_mode.check_bridge_executable(str(bridge), project_dir=project)
if not good_bridge.ok:
    raise SystemExit(f"trusted fake bridge was rejected: {good_bridge.errors}")
PY

printf 'TOKEN=abcdefghijklmnopqrstuvwxyz123456\n' > "$PROJECT/.env.local"
python3 "$SCRIPTS/agent_mode.py" \
  --print-handoff \
  --project-dir "$PROJECT" \
  --allowed-root "$PROJECT_ROOT" \
  --bridge-executable "$FAKE_BIN/devspace" \
  --workspace-root "$WORKSPACES" \
  --task "Review with sanitized copy." >/tmp/advisor-agent-sanitized-handoff.txt
grep -q '"mode": "sanitized_copy"' /tmp/advisor-agent-sanitized-handoff.txt
grep -q "sanitized_copy" /tmp/advisor-agent-sanitized-handoff.txt
router_json="$(python3 "$SCRIPTS/router.py" \
  --project-dir "$PROJECT" \
  --json \
  --agent-allowed-root "$PROJECT_ROOT" \
  --agent-bridge-executable "$FAKE_BIN/devspace" \
  --agent-workspace-root "$WORKSPACES" \
  --prompt "Decide the architecture for advisor memory")"
route="$(printf '%s' "$router_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["route"])')"
if [[ "$route" != "agent-mode" ]]; then
  echo "Expected sanitized agent-mode route, got $route" >&2
  exit 1
fi
sanitized_used="$(printf '%s' "$router_json" | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data["agent_mode"]["sanitized_workspace"]["used"])')"
if [[ "$sanitized_used" != "True" ]]; then
  echo "Expected sanitized workspace to be used" >&2
  exit 1
fi
sanitized_dir="$(printf '%s' "$router_json" | python3 -c 'import json,sys; data=json.load(sys.stdin); print(data["agent_mode"]["sanitized_workspace"]["workspace_dir"])')"
if [[ ! -d "$sanitized_dir" ]]; then
  echo "Expected sanitized workspace directory to exist: $sanitized_dir" >&2
  exit 1
fi
if [[ -e "$sanitized_dir/.env.local" ]]; then
  echo "Sanitized workspace copied .env.local" >&2
  exit 1
fi
grep -q "Advisor Sanitized Workspace" "$sanitized_dir/ADVISOR_SANITIZED_WORKSPACE.md"
test -f "$sanitized_dir/SANITIZED_WORKSPACE_MANIFEST.json"

python3 "$SCRIPTS/advisor_agent_setup.py" \
  --project-dir "$PROJECT" \
  --allowed-root "$PROJECT" \
  --bridge-executable "$FAKE_BIN/devspace" \
  --workspace-root "$WORKSPACES" \
  --config-path "$SANITIZED_CONFIG" >/tmp/advisor-agent-setup-sanitized.txt
grep -q "wrote_config: yes" /tmp/advisor-agent-setup-sanitized.txt
grep -q "sanitized_workspace_used: yes" /tmp/advisor-agent-setup-sanitized.txt

router_json="$(python3 "$SCRIPTS/router.py" \
  --project-dir "$PROJECT" \
  --json \
  --agent-sanitized-workspace off \
  --agent-allowed-root "$PROJECT_ROOT" \
  --agent-bridge-executable "$FAKE_BIN/devspace" \
  --prompt "Decide the architecture for advisor memory")"
route="$(printf '%s' "$router_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["route"])')"
if [[ "$route" != "single-advisor" ]]; then
  echo "Expected secret preflight fallback route when sanitization is off, got $route" >&2
  exit 1
fi
rm "$PROJECT/.env.local"

python3 "$SCRIPTS/advisor_agent_setup.py" \
  --project-dir "$PROJECT" \
  --allowed-root "$PROJECT" \
  --bridge-executable "$FAKE_BIN/devspace" \
  --config-path "$CONFIG" >/tmp/advisor-agent-setup.txt
grep -q "wrote_config: yes" /tmp/advisor-agent-setup.txt

router_json="$(python3 "$SCRIPTS/router.py" \
  --project-dir "$PROJECT" \
  --json \
  --agent-config-path "$CONFIG" \
  --agent-bridge-executable "$FAKE_BIN/devspace" \
  --prompt "Decide the architecture for advisor memory")"
route="$(printf '%s' "$router_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["route"])')"
if [[ "$route" != "agent-mode" ]]; then
  echo "Expected config-driven agent-mode route, got $route" >&2
  exit 1
fi

if python3 "$SCRIPTS/advisor_agent_setup.py" \
  --project-dir "$PROJECT" \
  --allowed-root "$PROJECT" \
  --bridge-executable "$FAKE_BIN/devspace" \
  --config-path "$PROJECT/agent-config.json" >/tmp/advisor-agent-setup-bad.txt 2>&1; then
  echo "Expected project-local config path to be rejected" >&2
  exit 1
fi
grep -q "agent config path must live outside" /tmp/advisor-agent-setup-bad.txt

if ADVISOR_AGENT_CONFIG="$PROJECT/agent-config.json" python3 "$SCRIPTS/agent_mode.py" \
  --doctor \
  --project-dir "$PROJECT" \
  --allowed-root "$PROJECT" \
  --bridge-executable "$FAKE_BIN/devspace" >/tmp/advisor-agent-doctor-bad-config.txt 2>&1; then
  echo "Expected project-local ADVISOR_AGENT_CONFIG to be rejected" >&2
  exit 1
fi
grep -q "agent config path must live outside" /tmp/advisor-agent-doctor-bad-config.txt

router_json="$(python3 "$SCRIPTS/router.py" \
  --project-dir "$PROJECT" \
  --json \
  --agent-allowed-root "$PROJECT_ROOT" \
  --agent-bridge-executable "$FAKE_BIN/devspace" \
  --prompt "Decide the architecture for advisor memory")"
route="$(printf '%s' "$router_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["route"])')"
if [[ "$route" != "agent-mode" ]]; then
  echo "Expected agent-mode route, got $route" >&2
  exit 1
fi

router_json="$(python3 "$SCRIPTS/router.py" \
  --project-dir "$PROJECT" \
  --json \
  --prompt-only \
  --agent-allowed-root "$PROJECT_ROOT" \
  --agent-bridge-executable "$FAKE_BIN/devspace" \
  --prompt "Decide the architecture for advisor memory")"
route="$(printf '%s' "$router_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["route"])')"
if [[ "$route" != "single-advisor" ]]; then
  echo "Expected prompt-only fallback route, got $route" >&2
  exit 1
fi

router_json="$(python3 "$SCRIPTS/router.py" \
  --project-dir "$PROJECT" \
  --json \
  --agent-allowed-root "$PROJECT_ROOT" \
  --agent-bridge-executable "$FAKE_BIN/missing-devspace" \
  --prompt "Decide the architecture for advisor memory")"
route="$(printf '%s' "$router_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["route"])')"
if [[ "$route" != "single-advisor" ]]; then
  echo "Expected missing bridge fallback route, got $route" >&2
  exit 1
fi

python3 "$SCRIPTS/router.py" \
  --project-dir "$PROJECT" \
  --execute \
  --agent-allowed-root "$PROJECT_ROOT" \
  --agent-bridge-executable "$FAKE_BIN/devspace" \
  --prompt "Decide the architecture for advisor memory" >/tmp/advisor-agent-router-exec.txt
grep -q "Advisor Agent-Mode Handoff" /tmp/advisor-agent-router-exec.txt

echo "Agent-mode tests passed."
