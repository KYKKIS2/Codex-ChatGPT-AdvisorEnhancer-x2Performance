# Optional ChatGPT Cloud GUI

The advisor includes a small local interface for conversations from ChatGPT
Projects already bound through `.codex-advisor/project.json`. It uses the same
g4f/HAR transport as the command-line advisor; it does not add another account,
hosted service, database, or conversation system.

The interface lists conversations only from Projects registered by exact local
directory. Opening a chat fetches the latest 80 visible user/assistant messages
plus their recent user-visible activity summaries into that page's memory and
continues the original cloud conversation from its current parent message.
Reloading or closing the page discards those browser message bodies. ChatGPT
remains authoritative for the complete conversation, cloud compaction, Project
membership, and connector attachment.

## Start

Linux:

```bash
./start-advisor-gui.sh --project-dir /absolute/path/to/a/bound/repository
```

Windows:

```powershell
.\start-advisor-gui.ps1 -ProjectDir C:\absolute\path\to\a\bound\repository
```

Open `http://127.0.0.1:8088/chat/`. Select the bound Project and cloud
conversation in the sidebar. The normal advisor supervisor on
`127.0.0.1:8080` can run at the same time.

The desktop sidebar can be collapsed from its header to widen the transcript;
the menu button restores it. ChatGPT-style `\\(...\\)` and `\\[...\\]`
equations render locally as native MathML, with visible source fallback for
unsupported notation. Markdown tables render as semantic HTML tables inside a
keyboard-scrollable container on narrow screens. Neither preference nor
rendered chat content is stored in browser persistence.

Use the `+` button to attach images, or paste an image directly into the
message box. The GUI accepts up to four JPEG, PNG, WebP, or GIF images per
turn, with an 8 MiB per-image and 20 MiB combined limit. A text prompt remains
required so interrupted-turn recovery can correlate the exact cloud turn.
Animated images are rejected; accepted static images are normalized before
upload.

The browser shell is served entirely from this repository. It does not load the
upstream g4f page, Cake Baker, analytics, CDN assets, or any other third-party
browser code. Chat requests still use the existing server-side g4f/HAR route to
contact ChatGPT.

During a turn, the browser holds one streamed response open while g4f consumes
ChatGPT's conversation WebSocket. User-visible activity summaries such as
`Inspected...` and `Updated...` appear in the transcript as they arrive. A
same-origin read-only check supplements the stream every five seconds because
the g4f stream does not always include those summaries. Each check makes one
cloud request; an HTTP 429 pauses this optional polling for at least 60 seconds
and honors a longer numeric `Retry-After` instead of retrying server-side.
Hidden reasoning tokens,
tool arguments, tool results, request/response payloads, debug logs, and private
cloud identifiers are discarded by the bridge before browser delivery.
After the provider stream finishes, the server checks ChatGPT's authoritative
stream status before completing its local journal. If ChatGPT is still working,
the page switches to low-rate read-only observation and keeps rendering the
sanitized cloud timeline, including after a page reload. Once ChatGPT reports
completion, reconciliation requires the exact streamed message or the
journal-bound user node to appear on the active cloud branch. A lagging cloud
GET cannot replace the provisional answer or regress the next parent message.
If ChatGPT terminates on a tool node without a final narrative, the GUI
preserves that exact current node as the next parent so a follow-up can continue
with the complete cloud branch instead of branching from an older assistant
message.

The current directory is registered automatically when it contains a valid
`.codex-advisor/project.json`. Additional directories must be registered
explicitly:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_gui.py register \
  --project-dir /absolute/path/to/repository
```

Registration does not scan sibling directories and does not change the
repository binding. One ChatGPT Project may be registered from multiple local
repositories without creating duplicate cloud Projects.

## Security And Recovery

- The GUI server binds only to `127.0.0.1` and rejects non-loopback Host and
  client addresses.
- A strict Content Security Policy allows browser scripts, styles, and network
  requests only from the loopback GUI origin. Unrelated g4f browser routes are
  unavailable on this process.
- The HAR, bearer token, raw Project ids, raw conversation ids, and parent
  message ids are never returned to browser JavaScript.
- Imported message bodies stay in page memory only. The interface does not use
  IndexedDB, local storage, or session storage.
- Image bodies are validated from their actual bytes, bounded by count, encoded
  size, per-image pixels, and total decoded pixels. Static images are decoded and
  re-encoded without original EXIF/XMP/trailing metadata; animation is rejected.
  Original local filenames are replaced with generic names and are not written
  to the advisor catalog. g4f stages uploads in the operating system's temporary
  directory and normally removes them when the response stream closes; ChatGPT
  retains the normalized image as part of the cloud conversation.
- During an active turn, the page supplements the provider stream with one
  same-origin, read-only cloud-history check every five seconds. A rate-limited
  check backs off for at least 60 seconds without an internal retry cascade.
  Only ChatGPT's
  user-visible activity summaries are returned; hidden reasoning, raw tool
  payloads, and raw message ids are omitted. Live submission checks are bound to
  the exact pending private journal. Reload and externally-started-turn
  observation is read-only and limited to the selected account-bound opaque
  conversation handle.
- A private catalog under `${CODEX_HOME:-~/.codex}/advisor-gui` maps opaque
  browser handles to cloud ids. Directories use owner-only permissions and
  catalog files use mode `0600` where the platform supports it.
- The catalog is bound to stable claims in the active ChatGPT authentication
  and fails closed if another account is used.
- Cloud sends share the advisor's machine-wide FIFO and conversation locks.
  The bridge provides at-most-once automatic submission: it makes at most one
  local POST attempt and never retries an ambiguous POST.
- Before provider iteration, the private journal durably records the prior
  parent and a SHA-256 fingerprint of the exact prompt. Before g4f advances to
  its network POST, the journal also binds the provider-generated cloud user
  node id. Recovery must find that exact user descendant and matching text. A
  normal turn also recovers its completed assistant response; a turn that ends
  on a terminal tool node keeps that node as its exact continuation parent.
  Unrelated or same-text advancement from another tab cannot clear the journal.
- If the browser disconnects, or the provider emits its finish marker while
  ChatGPT still reports the turn as running, the conversation remains blocked
  from another send. The GUI automatically waits on low-rate, idempotent
  status/history reads, displays sanitized progress, and restores the final
  cloud graph when ChatGPT finishes. It never replays the turn POST.
- A completed stream also remains blocked from another send until its exact
  branch advancement is visible in cloud history. If persistence is delayed,
  the streamed timeline remains on screen while automatic or manual Refresh
  uses GET-only reconciliation without replaying the turn. A terminal tool
  parent is surfaced as a missing-final warning but remains a valid same-branch
  continuation point.
- Only the supported Thinking Max and Pro Extended cloud routes are accepted.
  The browser cannot select arbitrary g4f providers or add provider options to
  the request.
- Reusing the exact ChatGPT conversation preserves its cloud conversation
  state. A HAR refresh authenticates the route but does not attach a DevSpace
  connector to a different chat.

The ChatGPT web backend is a private, changing interface. The local shell is
versioned with this repository, but a future ChatGPT or g4f transport change may
still require updating the bridge.

To clear only the optional GUI mappings, while preserving every repository's
`.codex-advisor` binding:

```bash
python3 ~/.codex/skills/external-advisor/scripts/advisor_gui.py reset
```
