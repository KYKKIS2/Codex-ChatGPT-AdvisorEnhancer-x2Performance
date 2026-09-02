# Permanent Cloudflare Domain MCP

This is the explicit, high-trust alternative to the normal sanitized read-only
advisor connector. It keeps one stable URL, exposes one original Git checkout
with read/write/shell tools, and requires Cloudflare Access authentication before
the request reaches DevSpace.

It is intended for a trusted ChatGPT account and a deliberately selected
repository. It is not the default advisor-agent route.

## Architecture

```text
ChatGPT
  -> Cloudflare Access Managed OAuth
  -> named Cloudflare Tunnel
  -> mode-0600 gateway socket below /run/advisor-domain-mcp-<UID>/gateway
  -> JWT-validating gateway inside its own Bubblewrap sandbox
  -> read-only bind of the separate origin runtime
  -> private origin socket below /run/advisor-domain-mcp-<UID>/origin
  -> DevSpace inside a second Bubblewrap sandbox
  -> original checkout mounted read/write at /workspace
  -> each bash tool call inside a third, short-lived Bubblewrap sandbox
  -> systemd expiry timer stops the window after 60 minutes
```

The gateway validates:

- the `Cf-Access-Jwt-Assertion` RS256 signature against the Access team JWKS
- exact issuer and Access application audience
- expiry, not-before, and issue time
- an exact allowed email address

Cloudflare Access authenticates the configured Cloudflare identity; knowing the
email address or public hostname is not enough. The policy also requires that
identity to be an active member of the selected Cloudflare account and to
complete the audited phishing-resistant MFA method. After authorization,
ChatGPT stores the connector OAuth grant in the ChatGPT account. Anyone who can
use that same ChatGPT account can therefore use the already-authorized
connector; the MCP server cannot distinguish two people sharing one ChatGPT
login.

It strips cookies, OAuth headers, Cloudflare headers, forwarded headers, and
hop-by-hop headers before forwarding. DevSpace accepts only the private gateway
credential over a loopback/Unix-socket origin. The origin stages that credential
in its private runtime directory, loads it before opening the MCP socket, and
then unlinks it. The shell tool cannot see either service runtime, the gateway
configuration, the manager state, or the credential.

The DevSpace process has:

- the selected original checkout mounted read/write at `/workspace`
- a pinned Git commit plus content hashes for tracked modifications and
  non-ignored untracked paths in the clean or explicitly approved dirty state;
  ignored data/model trees remain live and visible without byte hashing
- a full boundary scan immediately before each mount; descendant mounts,
  hardlinks with any directory entry outside the exposed checkout, sockets,
  devices, FIFOs, and incomplete scans fail closed; hardlink groups entirely
  contained within intentionally exposed bulk roots are accepted, preventing
  aliases from bypassing path masks, and this native metadata pass does not read
  file contents
- recognized `.env`, HAR, advisor-state, credential-like, and other sensitive
  path names replaced by private empty read-only mounts before DevSpace starts;
  this is a path-only pass and does not classify ordinary file contents;
  intentionally exposed bulk roots such as `artifacts`, data/model/output trees,
  virtual environments, and `node_modules` are not enumerated for secret-name
  masking
- `.git` mounted read-only, including inside every shell command; local Git
  configuration and hooks are additionally hidden
- a private PID, IPC, UTS, cgroup, user, mount, and network namespace
- no host home directory, `.ssh`, `.codex`, browser profile, wallet directory,
  Cloudflare token, other repository, D-Bus socket, display socket, or host
  process namespace
- no network access from shell commands
- a cleared environment with no inherited API keys, tokens, or agent variables
- trusted host Python and Git executables plus hash-pinned manager, patch,
  gateway, server, shell helper, Node, Bubblewrap, every file in the executable
  DevSpace distribution, and DevSpace package metadata
- one fresh nested Bubblewrap sandbox for every `bash` tool call, with no
  service sockets, manager state, gateway files, origin files, or helper source
- up to eight authenticated MCP operations in parallel by default, each with
  independent shell isolation and the same exact `/workspace` boundary; the
  configurable gateway ceiling is 1 to 64
- finite command admission slots remain occupied until the upstream MCP
  operation ends, even if the ChatGPT-side HTTP client disconnects while a
  command is still running
- long-lived MCP GET event streams use a separate bounded pool and are closed
  at the origin as soon as the corresponding client disconnects, so reconnects
  cannot consume command capacity
- one exact checkout root; worktree, base-ref, and alternate-root opens fail
- shell commands that may run for the configured 5-minute to 8-hour exposure
  window; a command started later is stopped when the remaining window expires
- bounded tool output and systemd CPU, memory, process, core-dump, and per-file
  ceilings
- a 250-millisecond free-space watchdog that ends the whole exposure window before
  the configured filesystem reserve is crossed

This is still an original-checkout mode, not a sanitized copy. Secret masking is
defense in depth and cannot classify every confidential value. Any sensitive
content that the scan does not identify remains visible to ChatGPT. The
read-only Git object database remains visible so `git log`, `git diff`, and
history inspection work; secrets committed anywhere in reachable history may
therefore be disclosed even though `.git/config` and hooks are hidden. Generated
dependency/build trees and intentionally exposed bulk research roots remain
visible without secret-name enumeration. Use a sanitized workspace instead if
those disclosures are unacceptable. The
per-file limit and free-space reserve are reactive protections, not a hard
aggregate project quota; a hard quota requires a bounded disposable filesystem
instead of direct writes to the original checkout. Stop the connector when full
access is no longer required.

## 1. Configure Cloudflare Access

Provision the dedicated system-visible runtime once. The system `cloudflared`
service intentionally keeps `ProtectHome=true`, which hides `/run/user`; the
connector therefore uses a private directory directly below `/run` without
weakening that service sandbox:

```bash
UID_VALUE="$(id -u)"
GID_VALUE="$(id -g)"
RUNTIME="/run/advisor-domain-mcp-${UID_VALUE}"

sudo install -d -o "$UID_VALUE" -g "$GID_VALUE" -m 0700 "$RUNTIME"
printf 'd %s 0700 %s %s -\n' "$RUNTIME" "$UID_VALUE" "$GID_VALUE" |
  sudo tee "/etc/tmpfiles.d/advisor-domain-mcp-${UID_VALUE}.conf" >/dev/null
sudo systemd-tmpfiles --create \
  "/etc/tmpfiles.d/advisor-domain-mcp-${UID_VALUE}.conf"
```

First run `prepare` as shown below and copy its
`cloudflare_origin_service` value. In the named tunnel's **Published
application** route, use:

```text
Hostname: mcp.example.com
Service:  unix:/run/advisor-domain-mcp-<YOUR-UID>/gateway/cloudflare.sock
```

Do not use a loopback TCP port. An unused unprivileged TCP port could be claimed
by another local account while the timed gateway is off. The Unix socket's
parent is mode `0700`, the socket is mode `0600`, and the hardening audit checks
the tunnel configuration before startup.

Use a dedicated Cloudflare account member protected by a passkey and MFA. Do
not use email OTP for this full-access connector.

In **Cloudflare Zero Trust -> Settings -> Authentication -> Login methods**:

1. Enable the Cloudflare identity provider.
2. Enable **Restrict to account members**.
3. Do not offer One-Time PIN to this application.

In **Cloudflare Zero Trust -> Access controls -> Applications**:

1. Add an MCP server or self-hosted application for
   `mcp.example.com`.
2. Select only the restricted Cloudflare identity provider and enable automatic
   redirect to it.
3. Add exactly one **Allow** policy. Its Include rule is the dedicated email,
   its Require rule is **Cloudflare account member**, and its MFA setting allows
   only a security key and/or biometrics. Do not add `Everyone`, an email-domain
   rule, or a weaker fallback authenticator.
4. Set the application session to 5-15 minutes.
5. In **Advanced settings**, enable **Managed OAuth** and dynamic client
   registration.
6. Disable localhost and loopback clients.
7. Add only `https://chatgpt.com/connector/oauth/*` under **Allowed redirect
   URIs**. ChatGPT now generates a distinct callback below that path for each
   app. Do not broaden this to `https://chatgpt.com/*`, another hostname, or a
   localhost/loopback callback. The legacy fixed callback
   `https://chatgpt.com/connector_platform_oauth_redirect` is retained by the
   audit helper only for existing published connectors.
8. Set the OAuth access token to 5-15 minutes and the grant session to no more
   than 24 hours.
9. On the tunnel route, require Access validation with exactly this
   application's **AUD tag**. This adds validation inside `cloudflared` before
   the request reaches the local gateway.
10. Save the application and record the team domain, application **AUD tag**,
    and named tunnel ID.

The MCP origin validates the Access JWT itself. Do not use Managed OAuth in
front of an origin that has not been patched to validate that JWT.

The OAuth access-token lifetime and the signed application assertion forwarded
in `Cf-Access-Jwt-Assertion` are different controls. Validate the assertion's
signature, exact issuer, exact application audience, current `exp`/`nbf`/`iat`
claims, subject, and allowed identity. Do not impose a short
`exp - iat` ceiling based on the OAuth access-token lifetime: Cloudflare can
issue the application assertion for the Access application or policy session,
while separately refreshing and re-evaluating the short opaque OAuth token.

Before starting the local origin, an unauthenticated request should receive the
Access OAuth challenge instead of reaching DevSpace:

```bash
curl -sS -D - -o /dev/null https://mcp.example.com/mcp
```

## 2. Prepare The Local Origin

Stop any existing permanent-domain origin before changing repositories:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py stop
```

Prepare the exact original checkout:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py prepare \
  --project-dir "/absolute/path/to/the/main-checkout" \
  --hostname "mcp.example.com"
```

`prepare` requires a clean checkout by default. If another Codex session has
deliberate uncommitted work, review that exact state first and opt in explicitly:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py prepare \
  --project-dir "/absolute/path/to/the/main-checkout" \
  --hostname "mcp.example.com" \
  --allow-dirty-checkout
```

This does not discard or edit the dirty work. It pins the exact commit plus
tracked and non-ignored untracked content, then fails closed if those inputs
change before startup. Ignored datasets remain visible and may change without
forcing `prepare` to read every dataset byte twice.
The complete tree still receives a native metadata-only boundary check, while
bulk data/artifact/model/output roots and virtual environments are excluded from
the Python secret-name plan. This keeps large research trees available in
`/workspace` without making startup proportional to their contents.

GPU access remains disabled by default. For CUDA work on an NVIDIA host, add
`--enable-nvidia` during `prepare`. The manager pins and revalidates only
`nvidiactl`, `nvidia-uvm`, optional NVIDIA support nodes, and the discovered
`nvidia[0-9]+` character devices, then passes those exact nodes through both
Bubblewrap layers. Driver libraries remain read-only through `/usr`; no DRI
device, host filesystem, credential path, or network namespace is added.
This grants the connected account use of the host GPU and its kernel-driver
attack surface, so enable it only for an explicitly trusted full-access window.
For training that must use all host CPU, RAM, swap, task, open-file, and
per-file-size capacity, add `--full-compute`. This removes those origin service
ceilings but does not weaken Bubblewrap isolation, the timed shutdown, or the
reactive minimum-free-space and inode reserve.

This command installs no Cloudflare credential and prints no secret. It patches
the pinned DevSpace package, creates owner-only local state, and installs the
origin and gateway services plus an expiry service/timer. It does not start the
public origin while Access configuration is incomplete. Review the reported
`sensitive_paths_masked` count locally; masked paths stay in the original
checkout but are not visible inside `/workspace`.

Add the three Access values:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py configure-access \
  --team-domain "YOUR-TEAM.cloudflareaccess.com" \
  --audience "YOUR-ACCESS-APPLICATION-AUD-TAG" \
  --email "YOUR-ALLOWED-LOGIN-EMAIL"
```

These identity values are stored in the owner-only config:

```text
~/.config/advisor-domain-mcp/config.json
```

The gateway/origin shared credential is generated locally at mode `0600` and is
never printed.

## 3. Audit Cloudflare Hardening

Create a short-lived API token with read-only access to:

- **Access: Apps and Policies Read**
- **Access: Organizations, Identity Providers, and Groups Read**
- **Cloudflare Tunnel Read** (or **Cloudflare One Connectors Read**)
- **Zone Read**
- **DNS Read**

Store it outside every repository at mode `0600`.

Pin the local `cloudflared` service to the exact named tunnel that will be
audited. The tunnel UUID is an identifier, not the tunnel token:

```bash
printf '%s\n' "YOUR-NAMED-TUNNEL-UUID" | \
  sudo install -o root -g root -m 0644 /dev/stdin \
  /etc/cloudflared/advisor-tunnel-id
```

Never put the tunnel token in this marker. Then run:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py \
  audit-cloudflare \
  --account-id "YOUR-CLOUDFLARE-ACCOUNT-ID" \
  --tunnel-id "YOUR-NAMED-TUNNEL-UUID" \
  --zone-id "YOUR-CLOUDFLARE-ZONE-ID" \
  --api-token-file "$HOME/.config/advisor-domain-mcp/cloudflare-audit-token" \
  --redirect-uri "https://chatgpt.com/connector/oauth/*"
```

The command performs read-only API calls and prints only boolean controls. It
does not print or persist the token, account ID, application ID, identity ID, or
callback; the already configured allowed email remains only in the owner-only
local config. It verifies the account's Access team domain, application,
restricted identity provider, exact policy, named tunnel identity, one active
connector, active zone, the single proxied CNAME to that tunnel, exact Unix
origin, final deny catch-all, tunnel-side Access validation, callback, and short
sessions. Every paginated Cloudflare inventory endpoint is consumed to its
reported `total_count`; missing, inconsistent, or changing pagination metadata
fails the audit. Cloudflare's dedicated tunnel-connections endpoint is validated
as its documented bounded single-page response, where `result_info` is optional.
The audit also binds the remote tunnel to both the root-owned local identity
marker and the exact active local connector reported by cloudflared's
loopback-only `/diag/tunnel` endpoint. A passing audit records only non-secret
identity/callback fingerprints for 24 hours.
Revoke the API token and delete its local file immediately afterward.

`start` fails closed without a current passing attestation. Changing the Access
issuer, audience, allowed email, hostname, runtime, or exposed repository
invalidates it. Ordinary edits inside the same repository do not invalidate the
remote Cloudflare attestation: `prepare` repins those Git changes, and `start`
verifies that exact checkout state and the current sensitive-path mask plan
independently before exposing it.
The attestation cannot detect a later Cloudflare dashboard change, so rerun the
audit immediately after every policy, identity-provider, OAuth, or session
change even if the 24-hour window has not expired.

## 4. Start And Verify

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py doctor
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py start
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py status
```

`start` fails closed unless the remote hardening audit is current, the named
tunnel is active, cloudflared uses a regular root-owned mode-`0600` token file,
has one explicit loopback-only diagnostics port, the local diagnostics tunnel
and connector IDs match the authenticated remote inventory, the root-owned
local identity marker matches the audited tunnel, the zone has
exactly one proxied CNAME to that tunnel, exactly one connector is active, the
audited tunnel targets the protected Unix socket and ends in a 404 catch-all,
and the public route presents the Cloudflare Access Managed OAuth challenge.
The gateway and origin are not enabled at login or boot and do not restart after
a failure. A one-shot timer and independent service runtime limits stop them
after 60 minutes.

The permanent connector does not expose the upstream synchronous `bash` tool.
All shell execution uses `exec_command`, which handles short commands directly
and returns within 30 seconds with a durable process-session handle when a
command is still running. This prevents a long build or training run from being
killed at the synchronous tool's former 90-second request boundary. The command
continues in the same repository, CUDA, network, and resource sandbox. Use
`write_stdin` to poll, interact, or send Ctrl-C. Stopping or expiring the
connector still terminates the process and its sandbox.

The gateway admits up to eight simultaneous authenticated operations by
default. The same `--max-concurrent` value caps active asynchronous process
sessions, so detached jobs cannot accumulate without bound. ChatGPT can still
run independent reads, checks, or commands concurrently inside the same
selected repository sandbox. Concurrent writes can conflict at the application
level, just as they can in two local terminals, so use separate paths or
explicit process coordination when commands mutate the same files.
Persistent MCP GET event streams are accounted separately from finite tool
operations and are torn down when their client disconnects.
Choose another bounded limit with `--max-concurrent`; accepted values are 1
through 64.

Exact matching `exec_command` retries reuse the active process or its retained
completion result, including after ChatGPT opens a fresh MCP session while the
same local origin remains alive. This prevents a lost HTTP response from
starting the same training twice. Reuse one explicit `executionKey` for retries.
Use a distinct key for each intentionally parallel identical command.
`allowConcurrentDuplicate=true` bypasses protection and should be reserved for
cases where every repeated submission is deliberate. Completed replay records
remain available for five minutes.

Choose another bounded window during `prepare`, from 5 minutes to 8 hours:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py prepare \
  --project-dir "/absolute/path/to/the/main-checkout" \
  --hostname "mcp.example.com" \
  --max-concurrent 8 \
  --session-minutes 30 \
  --enable-nvidia \
  --full-compute
```

Then create or update the ChatGPT custom app:

```text
https://mcp.example.com/mcp
```

Select OAuth. Cloudflare, not DevSpace, handles the login, so this permanent
connector has no DevSpace owner password to paste. Set this ChatGPT app to
**Any changes** / ask before writes. DevSpace marks `write`, `edit`, and `bash`
as destructive, non-read-only tools so the client can require approval. This
approval mode is a ChatGPT-side control and cannot be forced by the MCP server.

Tell ChatGPT to open:

```text
/workspace
```

The tools include `open_workspace`, `read`, `write`, `edit`, `grep`, `glob`,
`ls`, `bash`, `exec_command`, and `write_stdin`. Shell commands run offline
inside the namespace.

Stop access:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py stop
```

## Repoint The Stable URL

One permanent URL exposes one exact repository at a time:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py stop
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py prepare \
  --project-dir "/absolute/path/to/another/main-checkout" \
  --hostname "mcp.example.com"
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py \
  audit-cloudflare \
  --account-id "YOUR-CLOUDFLARE-ACCOUNT-ID" \
  --tunnel-id "YOUR-NAMED-TUNNEL-UUID" \
  --zone-id "YOUR-CLOUDFLARE-ZONE-ID" \
  --api-token-file "$HOME/.config/advisor-domain-mcp/cloudflare-audit-token" \
  --redirect-uri "https://chatgpt.com/connector/oauth/*"
python3 ~/.codex/skills/external-advisor/scripts/advisor_domain_mcp.py start
```

The project root itself must be the main checkout with a real `.git` directory,
not a symlink or linked worktree.

## Tunnel Token Hardening

A remotely managed Cloudflare Tunnel token can run that tunnel. Rotate the
current token if it was ever placed directly in a readable systemd unit or
terminal history.

After rotation, store it in a root-only file and use Cloudflare's
`--token-file` option (supported by cloudflared 2025.4.0 and newer):

```text
/etc/cloudflared/tunnel-token  mode 0600, root:root
```

The service command should be equivalent to:

```text
/usr/local/bin/cloudflared --no-autoupdate tunnel \
  --metrics 127.0.0.1:60123 run \
  --token-file /etc/cloudflared/tunnel-token
```

Choose a dedicated unused loopback port. Do not bind the metrics/diagnostics
listener to `0.0.0.0`, `::`, a LAN address, or a public interface. The manager
reads only `http://127.0.0.1:60123/diag/tunnel` and never prints the returned
tunnel or connector identifiers.

After changing the root service, run `sudo systemctl daemon-reload` and
`sudo systemctl restart cloudflared`. A restart creates a new connector
identity, so rerun `audit-cloudflare` before `start`.

Do not paste the token into this repository, ChatGPT, Codex output, an
environment file, or a shell command that will remain in history.

## Validation

Local regression tests:

```bash
node tests/test-cloudflare-access-gateway.mjs
python3 tests/test-domain-mcp.py
```

The tests use a disposable Git fixture. They verify JWT claims and signatures,
current JWT validity and identity claims, header stripping, full MCP tool registration,
Unix-socket origin authentication, exact-root enforcement, original-mount write
propagation, sensitive-path masking, read-only Git metadata, per-command shell
isolation, environment clearing, home isolation, network isolation, split
origin/gateway Bubblewrap sandboxes, systemd resource/timer units, private
Cloudflare-origin socket health, runtime-integrity pinning, disk-reserve
enforcement, tracked/untracked content pinning, ignored-data live visibility,
pre-sandbox Git-helper suppression, descendant-mount rejection, contained
bulk-root hardlink acceptance, external and ordinary-path hardlink rejection,
asynchronous process completion, reconnect replay deduplication, intentional
parallel duplicates, active-process limits, complete Cloudflare pagination,
loopback diagnostics parsing, and strict
Cloudflare/DNS/local-connector/tunnel policy-audit behavior.

## Cloudflare References

- [Managed OAuth](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/)
- [Validate Access JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)
- [Tunnel tokens](https://developers.cloudflare.com/tunnel/advanced/tunnel-tokens/)
- [`cloudflared` run parameters](https://developers.cloudflare.com/tunnel/advanced/run-parameters/)
- [Unix-socket tunnel origins](https://developers.cloudflare.com/tunnel/advanced/local-management/configuration-file/#services)
- [Read a remotely managed tunnel configuration](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/subresources/configurations/methods/get/)
- [List active tunnel connections](https://developers.cloudflare.com/api/resources/zero_trust/subresources/tunnels/subresources/cloudflared/subresources/connections/methods/get/)
- [Tunnel diagnostics and local connector identity](https://developers.cloudflare.com/tunnel/monitoring/)
- [List DNS records](https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/list/)
