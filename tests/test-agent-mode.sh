#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPTS="$ROOT/codex-skill/external-advisor/scripts"
PROJECT_ROOT="$(mktemp -d)"
FAKE_BIN="$(mktemp -d)"
OUTSIDE="$(mktemp -d)"
DEVSPACE_FIXTURE="$(mktemp -d)"
CONFIG="$PROJECT_ROOT/agent-config.json"
SANITIZED_CONFIG="$PROJECT_ROOT/agent-config-sanitized.json"
WORKSPACES="$PROJECT_ROOT/workspaces"
FAKE_PORT="$(python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
cleanup() {
  python3 - "$PROJECT_ROOT" <<'PY'
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
if root.exists():
    for path in root.rglob("*"):
        try:
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        except OSError:
            pass
PY
  rm -rf "$PROJECT_ROOT" "$FAKE_BIN" "$OUTSIDE" "$DEVSPACE_FIXTURE"
}
trap cleanup EXIT

unset ADVISOR_AGENT_ALLOWED_ROOTS
unset ADVISOR_AGENT_BRIDGE_EXECUTABLE
unset ADVISOR_AGENT_SANITIZED_WORKSPACE
unset ADVISOR_AGENT_WORKSPACE_ROOT
unset DEVSPACE_ALLOWED_ROOTS

cat >"$DEVSPACE_FIXTURE/cli.js" <<'EOF'
// DevSpace patch fixture.
EOF
cat >"$DEVSPACE_FIXTURE/config.js" <<'EOF'
function parseToolMode(mode) {
    if (mode === "minimal" || mode === "full" || mode === "codex")
        return mode;
}
EOF
cat >"$DEVSPACE_FIXTURE/server.js" <<'EOF'
function serverInstructions(config) {
    return "default";
}
function tools(config, server, toolNames) {
    if (config.toolMode !== "codex") {
        registerAppTool(server, toolNames.write, {
    if (config.toolMode === "full") {
        registerAppTool(server, toolNames.grep, {
    if (config.toolMode !== "codex") {
        registerAppTool(server, toolNames.shell, {
}
EOF
python3 "$SCRIPTS/devspace_readonly_patch.py" --executable "$DEVSPACE_FIXTURE/cli.js" >/tmp/devspace-readonly-patch.txt
python3 "$SCRIPTS/devspace_readonly_patch.py" --check --executable "$DEVSPACE_FIXTURE/cli.js" >/tmp/devspace-readonly-check.txt
grep -q 'mode === "readonly"' "$DEVSPACE_FIXTURE/config.js"
grep -q 'config.toolMode !== "readonly"' "$DEVSPACE_FIXTURE/server.js"

PROJECT="$PROJECT_ROOT/project with spaces"
mkdir -p "$PROJECT"
git -C "$PROJECT" init -q -b main
git -C "$PROJECT" config user.email "advisor-test@example.invalid"
git -C "$PROJECT" config user.name "Advisor Test"
printf 'safe\n' > "$PROJECT/README.md"
git -C "$PROJECT" add README.md
git -C "$PROJECT" commit -m "initial" >/dev/null

cat > "$FAKE_BIN/fake_devspace_server.py" <<'PY'
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/mcp":
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer resource_metadata="http://127.0.0.1/.well-known/oauth-protected-resource"')
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        return


ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), Handler).serve_forever()
PY

cat > "$FAKE_BIN/devspace" <<EOF
#!/usr/bin/env bash
set -euo pipefail
if [[ "\${1:-}" == "serve" ]]; then
  printf '%s\n' \
    "DEVSPACE_TOOL_MODE=\${DEVSPACE_TOOL_MODE:-}" \
    "DEVSPACE_SKILLS=\${DEVSPACE_SKILLS:-}" \
    "DEVSPACE_SUBAGENTS=\${DEVSPACE_SUBAGENTS:-}" \
    "DEVSPACE_LOG_REQUESTS=\${DEVSPACE_LOG_REQUESTS:-}" \
    "DEVSPACE_LOG_TOOL_CALLS=\${DEVSPACE_LOG_TOOL_CALLS:-}" \
    "DEVSPACE_LOG_SHELL_COMMANDS=\${DEVSPACE_LOG_SHELL_COMMANDS:-}" \
    "DEVSPACE_TRUST_PROXY=\${DEVSPACE_TRUST_PROXY:-}" \
    > "$FAKE_BIN/devspace-env.txt"
  exec python3 "$FAKE_BIN/fake_devspace_server.py"
elif [[ "\${1:-}" == "config" && "\${2:-}" == "get" && "\${3:-}" == "publicBaseUrl" ]]; then
  echo "null"
elif [[ "\${1:-}" == "config" && "\${2:-}" == "get" ]]; then
  printf '{"host":"127.0.0.1","port":%s,"publicBaseUrl":null}\n' "\${PORT:-$FAKE_PORT}"
else
  echo fake devspace
fi
EOF
chmod +x "$FAKE_BIN/devspace"

cat > "$FAKE_BIN/cloudflared" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "https://fake-devspace.trycloudflare.com"
sleep 300
EOF
chmod +x "$FAKE_BIN/cloudflared"

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
grep -q '"mode": "sanitized_copy"' /tmp/advisor-agent-handoff.txt

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
(project / ".codex-advisor" / "latest-response.md").write_text("advisor response", encoding="utf-8")
scan = agent_mode.scan_project_secrets(project)
if scan.ok or not any(".codex-advisor" in finding.path for finding in scan.findings):
    raise SystemExit("advisor state was not blocked")
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
printf 'api_key = "abcdefghijklmnopqrstuvwxyz123456"\n' > "$PROJECT/secret-fixture.py"
printf '\0binary-secret-fixture\n' > "$PROJECT/binary-secret-fixture.bin"
python3 - "$PROJECT/large-secret-fixture.txt" <<'PY'
import sys
from pathlib import Path

Path(sys.argv[1]).write_text("x" * 262145, encoding="utf-8")
PY
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
if grep -q "abcdefghijklmnopqrstuvwxyz123456" "$sanitized_dir/secret-fixture.py"; then
  echo "Sanitized workspace did not redact a tracked text fixture." >&2
  exit 1
fi
if [[ -e "$sanitized_dir/large-secret-fixture.txt" ]]; then
  echo "Sanitized workspace copied an unscanned oversized text file." >&2
  exit 1
fi
if [[ -e "$sanitized_dir/binary-secret-fixture.bin" ]]; then
  echo "Sanitized workspace copied an uninspectable binary file." >&2
  exit 1
fi
if [[ -w "$sanitized_dir/README.md" ]]; then
  echo "Sanitized workspace files were not frozen read-only." >&2
  exit 1
fi
grep -q "Advisor Sanitized Workspace" "$sanitized_dir/ADVISOR_SANITIZED_WORKSPACE.md"
test -f "$sanitized_dir/SANITIZED_WORKSPACE_MANIFEST.json"
python3 - "$sanitized_dir/SANITIZED_WORKSPACE_MANIFEST.json" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if manifest.get("redacted_files", 0) < 1:
    raise SystemExit("sanitized manifest did not record redacted source files")
skipped = "\n".join(manifest.get("skipped_paths") or [])
for expected in ("large-secret-fixture.txt", "binary-secret-fixture.bin"):
    if expected not in skipped:
        raise SystemExit(f"sanitized manifest did not record the omission for {expected}")
PY

PYTHONPATH="$SCRIPTS" python3 - "$PROJECT" "$PROJECT_ROOT" "$FAKE_BIN/devspace" "$WORKSPACES" <<'PY'
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import agent_mode

project = Path(sys.argv[1])
allowed_root = Path(sys.argv[2])
bridge = Path(sys.argv[3])
workspaces = Path(sys.argv[4])
script = Path(agent_mode.__file__)
agent_mode.make_tree_writable_for_cleanup(workspaces)
shutil.rmtree(workspaces, ignore_errors=True)
workspaces.mkdir(parents=True, exist_ok=True)
command = [
    sys.executable,
    str(script),
    "--doctor",
    "--json",
    "--project-dir",
    str(project),
    "--allowed-root",
    str(allowed_root),
    "--bridge-executable",
    str(bridge),
    "--workspace-root",
    str(workspaces),
]

def run():
    return subprocess.run(command, text=True, capture_output=True, timeout=30)

with ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(lambda _item: run(), range(2)))
if any(result.returncode != 0 for result in results):
    raise SystemExit("concurrent sanitized workspace generation failed:\n" + "\n".join(result.stderr for result in results))
payloads = [json.loads(result.stdout) for result in results]
paths = {payload["agent_mode"]["sanitized_workspace"]["workspace_dir"] for payload in payloads}
if len(paths) != 1:
    raise SystemExit(f"concurrent callers selected different workspace generations: {paths}")
if not any(payload["agent_mode"]["sanitized_workspace"]["reused"] for payload in payloads):
    raise SystemExit("expected at least one concurrent caller to reuse the published generation")
PY

PYTHONPATH="$SCRIPTS" python3 - <<'PY'
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import advisor_agent_connect


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()

    def log_message(self, *_args):
        return


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    live = advisor_agent_connect.probe_mcp_url(f"http://127.0.0.1:{server.server_port}/mcp")
    stale = advisor_agent_connect.probe_mcp_url("https://advisor-stale.invalid/mcp", timeout=0.5)
finally:
    server.shutdown()
if not live["ready"]:
    raise SystemExit(f"live MCP challenge was not accepted: {live}")
if stale["ready"]:
    raise SystemExit("stale public URL was accepted as connector-ready")
PY

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
  --agent-dry-run \
  --agent-allowed-root "$PROJECT_ROOT" \
  --agent-bridge-executable "$FAKE_BIN/devspace" \
  --prompt "Decide the architecture for advisor memory" >/tmp/advisor-agent-router-exec.txt
grep -q "Advisor agent dry run saved" /tmp/advisor-agent-router-exec.txt

python3 "$SCRIPTS/advisor_agent_connect.py" \
  prepare \
  --project-dir "$PROJECT" \
  --allowed-root "$PROJECT" \
  --bridge-executable "$FAKE_BIN/devspace" \
  --config-path "$CONFIG" \
  --workspace-root "$WORKSPACES" \
  --public-base-url "https://manual-devspace.trycloudflare.com" \
  --task "Review via connected advisor." >/tmp/advisor-agent-connect-prepare.txt
grep -q "chatgpt_connector_url: https://manual-devspace.trycloudflare.com/mcp" /tmp/advisor-agent-connect-prepare.txt
grep -q "Advisor Agent-Mode Handoff" /tmp/advisor-agent-connect-prepare.txt

if python3 "$SCRIPTS/advisor_agent_connect.py" \
  prepare \
  --project-dir "$PROJECT" \
  --allowed-root "$PROJECT" \
  --bridge-executable "$FAKE_BIN/devspace" \
  --config-path "$CONFIG" \
  --public-base-url "http://localhost:8080" >/tmp/advisor-agent-connect-bad-url.txt 2>&1; then
  echo "Expected insecure ChatGPT connector URL to be rejected" >&2
  exit 1
fi
grep -q "must use https" /tmp/advisor-agent-connect-bad-url.txt

PORT="$FAKE_PORT" python3 "$SCRIPTS/advisor_agent_connect.py" \
  serve \
  --project-dir "$PROJECT" \
  --allowed-root "$PROJECT" \
  --bridge-executable "$FAKE_BIN/devspace" \
  --cloudflared-executable "$FAKE_BIN/cloudflared" \
  --config-path "$CONFIG" \
  --workspace-root "$WORKSPACES" \
  --runtime-root "$PROJECT_ROOT/runtime" \
  --timeout 5 \
  --allow-unpatched-devspace \
  --skip-public-probe \
  --task "Review via running bridge." >/tmp/advisor-agent-connect-serve.txt
grep -q "chatgpt_connector_url: https://fake-devspace.trycloudflare.com/mcp" /tmp/advisor-agent-connect-serve.txt
grep -q "devspace_pid:" /tmp/advisor-agent-connect-serve.txt
grep -q "tunnel_pid:" /tmp/advisor-agent-connect-serve.txt
grep -q "connector_ready: yes" /tmp/advisor-agent-connect-serve.txt
grep -q '^DEVSPACE_TOOL_MODE=readonly$' "$FAKE_BIN/devspace-env.txt"
grep -q '^DEVSPACE_SKILLS=false$' "$FAKE_BIN/devspace-env.txt"
grep -q '^DEVSPACE_SUBAGENTS=false$' "$FAKE_BIN/devspace-env.txt"
grep -q '^DEVSPACE_LOG_REQUESTS=false$' "$FAKE_BIN/devspace-env.txt"
grep -q '^DEVSPACE_LOG_TOOL_CALLS=true$' "$FAKE_BIN/devspace-env.txt"
grep -q '^DEVSPACE_LOG_SHELL_COMMANDS=false$' "$FAKE_BIN/devspace-env.txt"
grep -q '^DEVSPACE_TRUST_PROXY=false$' "$FAKE_BIN/devspace-env.txt"

python3 "$SCRIPTS/advisor_agent_connect.py" \
  status \
  --project-dir "$PROJECT" \
  --skip-public-probe \
  --runtime-root "$PROJECT_ROOT/runtime" >/tmp/advisor-agent-connect-status.json
python3 - <<'PY' /tmp/advisor-agent-connect-status.json
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
if data.get("mcp_url") != "https://fake-devspace.trycloudflare.com/mcp":
    raise SystemExit("wrong mcp_url in status")
if not data.get("connector_ready") or not data.get("devspace_running") or not data.get("tunnel_running"):
    raise SystemExit("expected fake connector processes to be ready")
if not data.get("readonly_tool_mode") or data.get("tool_mode") != "readonly":
    raise SystemExit("connector readiness did not require the read-only DevSpace tool mode")
PY

python3 - /tmp/advisor-agent-connect-status.json <<'PY'
import json
import sys
from pathlib import Path

status = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
state_path = Path(status["state_path"])
state = json.loads(state_path.read_text(encoding="utf-8"))
state["tool_mode"] = "full"
state_path.write_text(json.dumps(state), encoding="utf-8")
PY
python3 "$SCRIPTS/advisor_agent_connect.py" \
  status \
  --project-dir "$PROJECT" \
  --skip-public-probe \
  --runtime-root "$PROJECT_ROOT/runtime" >/tmp/advisor-agent-connect-full-status.json
python3 - /tmp/advisor-agent-connect-full-status.json <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
if data.get("connector_ready") or data.get("readonly_tool_mode"):
    raise SystemExit("a pre-patch/full-mode connector was incorrectly accepted as ready")
PY
python3 - /tmp/advisor-agent-connect-status.json <<'PY'
import json
import sys
from pathlib import Path

status = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
state_path = Path(status["state_path"])
state = json.loads(state_path.read_text(encoding="utf-8"))
state["tool_mode"] = "readonly"
state_path.write_text(json.dumps(state), encoding="utf-8")
PY

python3 "$SCRIPTS/advisor_agent_connect.py" \
  stop \
  --project-dir "$PROJECT" \
  --runtime-root "$PROJECT_ROOT/runtime" >/tmp/advisor-agent-connect-stop.txt
grep -q "devspace_stopped: yes" /tmp/advisor-agent-connect-stop.txt
grep -q "tunnel_stopped: yes" /tmp/advisor-agent-connect-stop.txt

echo "Agent-mode tests passed."
