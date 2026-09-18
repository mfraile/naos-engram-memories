# MCP host acceptance matrix

[`config/mcp-host-acceptance.v1.json`](../config/mcp-host-acceptance.v1.json)
classifies every active entry in the MCP-host catalogue. Owner-excluded
products remain as validated terminal dispositions in the catalogue and do not
create an active acceptance lane. Its purpose is to preserve negative and
blocked cells, not to turn template parsing into a compatibility claim.

## Evidence classes and claim ceilings

| Evidence class | What actually ran | Maximum permitted claim |
| --- | --- | --- |
| `static_schema` | Checked-in catalogue/template parser | Document shape only |
| `mcp_protocol` | Managed wrapper with a synthetic MCP peer | Wrapper transport and fail-closed behavior only |
| `headless_extension_host` | Named IDE extension in a disposable extension-host profile | That exact extension/host/platform/version only; not the GUI shell |
| `real_host` | Named released host application or CLI spawning the wrapper | That exact host/platform/version/provider combination only |

A binary version probe or `mcp list` command is recorded as
`real_host_inventory`. It is deliberately not an acceptance evidence class and
cannot support a runtime claim. A successful schema parse, wrapper handshake,
or provider-only call must never be labelled host support.

The separate OpenCode startup runner is narrower and stronger than inventory:
it observes the real pinned host process spawn the managed wrapper and records
the synthetic peer's exact `initialize`, `notifications/initialized`, and
`tools/list` trace:

```bash
python3 tools/run_opencode_startup_acceptance.py \
  --host-executable /opt/homebrew/bin/opencode \
  --output reports/mcp-hosts/opencode-v1-darwin-arm64-startup.json
```

Its receipt conforms to
[`config/schemas/mcp-host-startup-receipt.v1.schema.json`](../config/schemas/mcp-host-startup-receipt.v1.schema.json).
It uses a disposable home, synthetic registry, and synthetic Engram peer; it
does not invoke a model provider or call `mem_current_project`. Therefore it
can establish real-host startup and tool discovery for the exact pinned binary,
but `runtime_support_claim` remains `false` until the separate user-assisted
tool call succeeds.

## Reproducible lanes

Run the complete macOS ARM64 classification and allow-listed local CLI
inventory from a checkout:

```bash
python3 tools/run_mcp_host_acceptance.py \
  --platform darwin_arm64 \
  --probe-local-hosts \
  --output reports/mcp-hosts/all-darwin-arm64.json
```

The probe discovers only catalogue-declared CLI names. Each command runs with
a new synthetic Git repository, `HOME`, `XDG_CONFIG_HOME`, and temporary
directory. The environment allow-list omits API keys, tokens, provider
credentials, and all client-specific variables. No live client configuration,
memory database, home directory, or `.engram` directory is copied or mounted.
Command output is discarded after extracting a version token and exit code.

The Ubuntu 24.04 AMD64 report runs in the pinned Docker test image:

```bash
docker build --platform linux/amd64 \
  -f tests/docker/Dockerfile.ubuntu-amd64 \
  -t naos-engram-test:ubuntu24.04-amd64-v1 .
docker run --rm --platform linux/amd64 \
  -v "$PWD:/work:ro" \
  -w /work \
  naos-engram-test:ubuntu24.04-amd64-v1 \
  python3 tools/run_mcp_host_acceptance.py \
    --platform ubuntu_24_04_amd64_container
```

The read-only mount means a report is emitted to stdout. A CI wrapper may
capture stdout as an artifact outside the container. Do not mount a live home,
provider credential directory, MCP settings directory, Engram database, or
memory-sync checkout. The image may contain approved host binaries/extensions
only when they were acquired and checksum-pinned as separate test assets; this
runner does not install them.

## Required deterministic cases

Every run uses checked-in synthetic project registrations and disposable Git
repositories to cover:

- registered canonical project;
- unregistered remote refusal;
- explicit-project/remote mismatch refusal;
- provider unavailable after identity resolution;
- two independently resolved projects with no folder-name fallback;
- instruction preview/install, repeated idempotent install, and malformed
  marker refusal without mutation.

These shared cases are wrapper/protocol and instruction-lifecycle evidence.
They are prerequisites for a host test, not substitutes for one.

## IDE, OS, and AI-provider combinations

The matrix expands each of 15 active catalogue entries across both OS lanes and five provider
profiles: synthetic offline, unavailable provider, host-native provider,
OpenRouter as a provider, and Ollama as a local model runtime. OpenRouter and
Ollama remain provider/runtime axes; neither is reclassified as an MCP host.
That yields 75 classified host/provider cells per platform. OpenCode V2 beta is
retained only as an `owner_excluded` catalogue disposition, so it has no cell,
probe, blocker, or promotion command in the active matrix.

Every host-provider cell is currently `blocked` or `not_applicable`. This is
intentional. A cell becomes eligible for promotion only after the exact named
host (or named headless extension host) spawns the wrapper in a disposable
profile and completes all identity, negative, provider, and instruction cases.
Results apply only to the observed host version, provider profile, operating
system, architecture, and execution mode. GUI evidence on macOS cannot certify
a Docker lane, and a headless Docker extension run cannot certify a desktop
GUI.

Each matrix row retains its blocker, exact runner command, and promotion gate.
GUI-only, account/subscription-gated, missing-version, unsupported-transport,
provider-role, and MCP-server-role entries are reported rather than silently
skipped.

Sixteen partial real-host observations are retained without promotion:

- One standalone Codex CLI case spawned the wrapper and called only
  `mem_current_project` for a registered synthetic project.
- One separate standalone Codex CLI case used a fresh disposable profile and
  selected a registered synthetic workspace with `-C`. The returned cwd
  matched the selected workspace and the source-bound wrapper/registry chain
  resolved it through the approved Git remote. The remaining dynamic-context
  contract is still required.
- One later standalone Codex CLI reliability case retained required-MCP
  startup refusal for unregistered, missing-remote, and unavailable-provider
  fixtures before model execution. Separate fresh processes resolved two
  same-basename repositories to different registered IDs, then resolved the
  first project again after restart. A linked worktree passed only the
  wrapper/MCP protocol layer, so the result remains partial and non-promoting.
- One Codex desktop-app diagnostic loaded an isolated Engram entry while the
  selected synthetic workspace was open, but its MCP child used filesystem
  root rather than that workspace. No memory tool was called and no memory
  content was accessed. This exact-version negative result rejects the current
  Desktop dynamic-context hypothesis without promoting or redesigning Codex.
- One later Codex desktop-app case used a project-local config with an explicit
  absolute cwd and required startup. The exact pinned Desktop application then
  called only `mem_current_project` and returned the registered canonical ID.
  The immediate clean-quit probe was not retained, so this remains partial.
- One Codex VS Code extension case used the same project-local config design.
  VS Code 1.133.0, extension 26.814.41407, and embedded Codex
  0.148.0-alpha.15 called only `mem_current_project`, returned the registered
  canonical ID, and completed point-in-time process, database-holder, and
  client-lease clean-quit probes. It remains part of host `codex`, not a
  separate `codex-vscode` promotion.
- One standalone Claude Code case resolved a registered synthetic project and
  called only `mem_current_project` under strict MCP and
  no-session-persistence flags. It still loaded existing user authentication,
  hooks, and plugins, so it is not a clean-profile result.
- One Claude desktop-app case used its embedded Claude Code engine to call only
  `mem_current_project` from a generated worktree. The returned canonical ID
  and clean-quit process/store/lease probes passed, while incoming workspace
  signal and wrapper resolution-source provenance were not retained.
- One Claude desktop-app diagnostic selected a sanitized unregistered Git
  fixture and exposed no Engram MCP server or memory tool. Clean-quit probes
  passed, but normal host logging did not retain wrapper invocation or resolver
  refusal, so the required unregistered-project case remains unpassed.
- One OpenCode V1 case completed isolated startup, MCP initialize, and
  `tools/list` against a synthetic provider; it listed but did not call
  `mem_current_project`.
- One separate OpenCode V1 case used the exact pinned binary and generated
  registered worktree to call only `mem_current_project`; it returned
  `naos-engram-memories`, and the post-quit process, database-holder, and
  client-lease probes were zero. The displayed model label was retained as an
  unverified label only; no LLM provider or account identity was inferred.
- One Muse Code `0.1.0-R708.1` case used a generated registered worktree to
  call only `mem_current_project`; it returned `naos-engram-memories`. The
  database-holder probe was zero, but automatic clean quit failed because one
  empty stale client lease required exact manual cleanup. Five older August 11
  Muse probes also contaminated the first process-count check and were retained
  as pre-existing cleanup evidence rather than attributed to this session.
- One Cursor `3.16.17` desktop-app case called only `mem_current_project` after
  the owner explicitly enabled its fixed-project workspace server.
- One Antigravity CLI `1.1.13` case called only `mem_current_project` through a
  fixed-project workspace entry and exited cleanly.
- One Antigravity `2.3.1` desktop-app case called only
  `mem_current_project` through a fixed-project workspace entry; its MCP child
  used the filesystem root as process cwd, so it is not dynamic-context proof.
- One Kilo Code `7.4.22` in Visual Studio Code `1.133.0` case called only
  `mem_current_project` through a fixed-project workspace entry and exited
  cleanly.

None of these observations satisfies the full mode-specific negative,
linked-worktree, credential, instruction-following, second-platform, and
GUI/extension-host matrix. The
sanitized historical reports remain under `docs/evidence/`; the closed,
append-only structured register is
[`config/mcp-host-observations.v1.json`](../config/mcp-host-observations.v1.json),
validated against
[`config/schemas/mcp-host-observations.v1.schema.json`](../config/schemas/mcp-host-observations.v1.schema.json).
The Codex and Claude catalogue rows remain `manual`; OpenCode V1 remains
`experimental`. None is recommended or supported from these observations.

Exact host, embedded-engine, provider, operating-system, and toolkit versions
belong in observation records. Stable catalogue and client contracts remain
version-neutral. A version change neither proves incompatibility nor inherits
the earlier runtime result; re-run the affected behavior gate before making a
current-version claim. The register is packaged for audit/reporting but is not
copied into the managed runtime, so app-version evidence updates do not trigger
an operational runtime repair. Referenced sanitized receipts remain in the
repository and source distribution at their register-relative `docs/evidence/`
paths; they are intentionally omitted from the wheel to keep installation data
portable across platform path limits. The register retains their exact SHA-256
bindings for audit against the source tree.

The sanitized matrix summaries are dated pre-exclusion evidence with the prior
16-host/80-cell topology:
`docs/evidence/mcp-host-matrix-darwin-arm64-20260813.json` and
`docs/evidence/mcp-host-matrix-ubuntu-amd64-20260813.json`. They retain every
blocked/ineligible combination count and explicitly record zero runtime-support
promotions.

## Cross-OS transport limitation

The Docker cross-OS memory-transport experiment is separate from host
acceptance and remains non-shipping. Its basic bidirectional and project
isolation path passed, but the strengthened real-concurrent-writer case exposed
an unresolved conflict: after the expected stale push rejection,
`git pull --rebase` cannot automatically reconcile concurrent
`.engram/manifest.json` commits. Do not weaken the concurrent case, discard the
negative result, or treat this acceptance matrix as promotion evidence for
that transport. The exact negative report is retained at
`docs/evidence/cross-os-trusted-replica-20260813.json`.

## Report contract

Reports conform to
[`config/schemas/mcp-host-acceptance-report.v1.schema.json`](../config/schemas/mcp-host-acceptance-report.v1.schema.json).
The report records catalogue coverage, shared scenario results, every selected
host, every selected host/provider cell, exact blockers and promotion gates,
the privacy boundary, any validated observation references, and a canonical
SHA-256 digest of the exact observation register used. The digest detects
evidence drift; it is not a signature or reviewer-identity control.
Exact-version observations remain partial inputs rather than generated
synthetic results.
`runtime_support_claim` remains `false` throughout this runner because it does
not automate or import a real-host session.

Use `--generated-at` with an RFC 3339 value when byte-for-byte deterministic
fixture output is needed. Otherwise the runner records the current UTC time.
