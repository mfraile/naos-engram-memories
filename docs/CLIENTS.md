# Client Manual

For a clone-free pipx installation, run `naos-engram-memory runtime install
--yes --non-interactive` first. It installs the user-owned registry, policy,
templates, and absolute-interpreter wrapper below the platform user config/bin
directories; it never writes inside the pipx environment. Source operators can
instead run `scripts/setup.sh --configure` (or `setup.ps1 -Configure`). Both
paths install the local wrapper and policy only. There are two deliberate adapter
modes: an explicit canonical project for a single workspace, or dynamic
remote-attested resolution for a client that passes the active workspace to
the wrapper.

Runtime health is content-addressed, not existence-based. `doctor` reports a
complete runtime only when `managed-runtime.v1.json`, the toolkit version, all
managed asset hashes, and the executable wrapper agree. The user-owned project
registry is deliberately outside the replaceable managed-asset set.

`client install` is the managed write path. It accepts only documented
strict-JSON configuration targets, validates fixed-project workspace remotes,
creates a sibling backup before changing an existing file, and refuses a
different `engram` entry. It deliberately refuses comment-bearing JSONC. Run
`--dry-run` first; if a client file is JSONC with comments, use
`render-client` and make a reviewed manual merge instead of stripping comments.
`client repair` is narrower than arbitrary replacement: it updates only an
entry whose executable is the managed `engram-mcp-wrapper`, whose canonical
project does not conflict, and whose fields are within the known adapter shape.
It creates a unique backup and refuses all other entries.

## Multiple projects on one machine

Register each repository once with a unique canonical project ID, then install
the desired client adapter from that repository's directory. The registry is
machine-wide, while client configuration is normally workspace-local:

```powershell
Set-Location C:\work\project-a
naos-engram-memory project register --id project-a --from-remote .
naos-engram-memory client install --client cursor `
  --project project-a --workspace . --yes

Set-Location C:\work\project-b
naos-engram-memory project register --id project-b --from-remote .
naos-engram-memory client install --client cursor `
  --project project-b --workspace . --yes
```

Each workspace then owns its own `.cursor/mcp.json`, `.vscode/mcp.json`,
`.agents/mcp_config.json`, or other documented project configuration. The
wrapper validates the active workspace's approved Git remote before selecting
the project, so two repositories can use the same IDE and client installation
without sharing memory accidentally. Worktrees with the same approved remote
may resolve to the same canonical project ID.

Do not put a fixed `ENGRAM_PROJECT` in a global client configuration. A global
fixed project can be launched from an unrelated workspace. For dynamic clients
such as Claude Code and Muse, use the rendered adapter without a fixed project
and let the host workspace determine the remote.

The upstream Engram MCP server currently advertises broad proactive-save
guidance during its initialization. That guidance does not relax this
toolkit's durable-only and no-sensitive-data policy. A client that cannot obey
the managed protocol is not a supported client; do not rely on an upstream
server prompt as a policy control.

## Codex desktop and CLI

Codex has one render-only project adapter. It emits a project-local
`.codex/config.toml` stanza with the managed wrapper, an explicit absolute
workspace `cwd`, `required = true`, and a bounded startup timeout. It never
emits `ENGRAM_PROJECT` and never writes or merges TOML automatically. Render it
for each registered workspace or worktree, then manually reconcile the stanza
inside that trusted project:

```bash
naos-engram-memory client render --client codex \
  --project example-product \
  --workspace /absolute/path/to/registered-workspace \
  --wrapper /absolute/path/to/engram-mcp-wrapper
```

The renderer validates that the explicit project and workspace Git remote
resolve to the same registry entry. Copying the rendered absolute `cwd` to a
different workspace is unsafe; re-render instead. No `codex mcp add` command
embedding a global fixed `ENGRAM_PROJECT` is provided.

Three exact-version standalone Codex CLI observations, one exact-version Codex
desktop negative observation, and positive Desktop and VS Code extension
project-config observations are retained. The older
case passed a registered-project `mem_current_project` call without dynamic
workspace provenance. The newer isolated case selected a registered synthetic
workspace with `-C`; the returned cwd matched that workspace, the source-bound
wrapper used process cwd, and the same registry resolved the workspace through
its approved Git remote. The latest CLI reliability case retained required-MCP
startup refusal for unregistered, missing-remote, and unavailable-provider
fixtures before model execution; two same-basename repositories resolved to
their distinct registered IDs in separate processes; and the first project
resolved correctly again after a fresh restart. A linked worktree also passed
the wrapper/MCP protocol check, but did not run through the Codex host. In
contrast, the pinned desktop application loaded
the isolated Engram entry while the expected workspace was open but launched
the MCP child from filesystem root. No memory tool was called. That result
rejects the no-cwd Desktop configuration for the pinned version. The later
project-local configuration supplied an explicit absolute cwd and required
startup; the same Desktop version then returned the registered project. The
positive and negative observations are both retained because they test
different configurations. They do not promote Codex: the named-host
linked-worktree path, credential canaries, instruction following, the
managed-adapter mismatch disposition, equivalent Desktop and IDE-extension
negative/lifecycle cases, and other-platform evidence remain open. The
standalone CLI receipt does not transfer to those other execution modes.

## Codex IDE extension

Codex CLI and the Codex IDE extension share the Codex `config.toml` MCP
surface. `codex-vscode` is therefore not a separate adapter and is refused.
Use the render-only `codex` entry. A `.vscode/mcp.json` entry belongs to the
generic VS Code/Copilot host below, not to Codex. Visual Studio Code 1.133.0,
the OpenAI Codex extension 26.814.41407, and embedded Codex
0.148.0-alpha.15 completed one exact project-config `mem_current_project`
call and clean quit. This is partial exact-version evidence under host `codex`,
not an independent `codex-vscode` promotion.

## Generic VS Code/Copilot MCP

VS Code's global user-profile `mcp.json` is not a safe dynamic client for
Engram: it can launch outside the active repository. Render
`vscode-generic` **with** `--project CANONICAL_ID`, then place the result as
`.vscode/mcp.json` in that registered workspace. Do not place it in the VS Code
user-profile `mcp.json` or `~/.copilot/mcp-config.json`.

VS Code gives a workspace server that workspace as its default working
directory and forwards that workspace configuration to its Agent Host. The
explicit canonical project and the observed Git remote must agree; a remote
mismatch, unregistered workspace, or missing project mapping stops rather than
guessing. The installed wrapper must be discoverable by the VS Code GUI
process; use the managed command name only when its installation directory is
on that process's `PATH`, otherwise use that host's absolute local wrapper path
without committing it.

The safe installer writes this project-scoped surface when it is strict JSON.
This is documented and syntax-validated, not a live host-runtime claim:

```bash
bash scripts/setup.sh --install-client --client vscode-generic \
  --project example-product --workspace /path/to/registered-workspace --dry-run
```

## Claude Code adapter and desktop-app execution mode

Render `claude-code` **without** `--project` for a Claude Code user or local
MCP configuration. The same adapter applies when the Claude desktop app runs
its embedded Claude Code engine: that is a distinct evidence execution mode,
not a second adapter or broad support for Claude chat/work. The wrapper uses
`CLAUDE_PROJECT_DIR` when the host supplies it and otherwise uses process cwd,
then resolves the selected Git remote through the approved registry. The same
JSON can be project-scoped when a team intentionally wants a reviewed shared
tool definition.

The append-only observation register retains exact-version standalone and
desktop-app-embedded positive cases plus one desktop diagnostic from an
unregistered fixture. In the positive desktop case, the canonical project was
returned from a generated worktree and clean-quit process, database-holder,
and client-lease probes passed. In the diagnostic case, no Engram MCP server
or memory tool was exposed and the same clean-quit probes passed, but normal
host logging did not retain whether the wrapper started or refused the remote.
It therefore does not satisfy the unregistered-project acceptance case. The
pre-observability wrapper also did not retain whether `CLAUDE_PROJECT_DIR` or
process cwd supplied the positive case's workspace. None of these observations
promotes the adapter or transfers automatically to a new version. Version
changes do not by themselves prove incompatibility; repeat the behavior gate
before making a current-version runtime claim.

## Antigravity 2.0 and Antigravity CLI (experimental)

Do not add a new Gemini CLI or Gemini Code Assist configuration. Google's
consumer Gemini service transition means those products are not a viable new
target for this toolkit. Antigravity is the replacement target, but this
adapter remains **experimental** until it has passed the same live
`mem_current_project` validation on a supported version and platform.

```bash
bash scripts/setup.sh --install-client --client antigravity \
  --project example-product --workspace /path/to/registered-workspace --dry-run
```

The installer targets `.agents/mcp_config.json`. Do not configure the global
`~/.gemini/config/mcp_config.json`: a fixed canonical project in a global file
could be launched from an unrelated workspace. Restart the IDE or CLI, call
`mem_current_project`, and retain only a configuration that returns the
expected canonical ID. The toolkit does not install or upgrade Antigravity.

Exact-version partial observations now retain one successful fixed-project
call from Antigravity CLI 1.1.13 and one from Antigravity 2.3.1 with its
embedded language server on macOS ARM64. The CLI wrapper used the expected
workspace cwd. The desktop-app MCP child used `/` as its process cwd and still
returned the configured canonical ID through `process_override`; this proves
only the explicit project binding, not workspace-derived resolution. Neither
case promotes runtime support, and the required negative, isolation,
instruction-lifecycle, restart, account-boundary, and other-platform cases
remain incomplete.

## Kilo Code (experimental)

```bash
bash scripts/setup.sh --install-client --client kilo \
  --project example-product --workspace /path/to/registered-workspace --dry-run
```

The installer targets `.kilo/kilo.jsonc`. Kilo's configuration uses a
top-level `mcp` map and accepts JSONC, while the generated snippet is strict
JSON. Kilo no longer falls back to OpenCode configuration. Do **not** move an
active OpenCode directory into Kilo: the applications have different ownership
and configuration semantics. Kilo Code 7.4.22 in Visual Studio Code 1.133.0 on
macOS ARM64 has completed one owner-assisted fixed-project
`mem_current_project` call and one clean quit. The adapter remains experimental:
comment-bearing JSONC and the required negative, isolation,
instruction-lifecycle, restart, and other-platform cases remain incomplete.

## Stable OpenCode V1 (experimental)

```bash
naos-engram-memory client install --client opencode-v1 \
  --host-executable "$(command -v opencode)" \
  --project example-product --workspace /path/to/registered-workspace \
  --wrapper /absolute/path/to/engram-mcp-wrapper --dry-run
```

The installer targets repository-root `opencode.json`, not shared user
configuration. V1 runs as `opencode`, nests the server under `mcp.engram`, and
uses `enabled: true`. The installer selects this adapter by the exact
`opencode` executable identity, never by semantic-major guessing. Because this
file contains an absolute machine-local wrapper path, installation refuses a
tracked file and requires both `/opencode.json` and its generated backup names
to be ignored locally (prefer a reviewed `.git/info/exclude`). Verification
applies the same Git boundary. OpenCode V2 beta is `owner_excluded`: no V2
adapter is packaged or installed, and a retained `mcp.servers` shape is refused
without mutation so the owner can archive or remove it explicitly. The exact
OpenCode V1 macOS binary has completed isolated startup/tool discovery and one
user-assisted registered-project `mem_current_project` call. V1 remains
experimental because the negative, multi-project, isolation,
instruction-lifecycle, restart, and second-platform gates are incomplete.

## Cursor and other experimental prospects

Cursor documents project MCP configuration at `.cursor/mcp.json` and supports
local stdio commands. The toolkit can preview, explicitly install, and
syntax-verify that project entry with backup/collision protection. Cursor
3.16.17 on macOS ARM64 completed one owner-assisted fixed-project
`mem_current_project` call through its embedded MCP extension after the owner
enabled the initially disabled project server. This exact-version observation
is partial evidence only: negative, isolation, instruction-lifecycle, restart,
and other-platform cases remain incomplete, and `runtime_support_claim` stays
false.

LM Studio 0.3.17+ and AnythingLLM's
`anythingllm_mcp_servers.json` are documented MCP surfaces but remain
experimental and preview-only. LM Studio must first prove direct Engram hosting;
AnythingLLM must prove workspace/tenant identity and safe local-process
ownership. Open
WebUI is unsupported because its native MCP integration is HTTP while this
toolkit manages a local stdio server.

## Muse Code (dynamic; partial real-host evidence)

Muse Code's native `mcp_servers` setting accepts a local stdio command. An
isolated macOS validation started the real Engram wrapper with the offline echo
provider and resolved the active registered Git workspace. A separate
user-assisted Muse Code `0.1.0-R708.1` run called only
`mem_current_project` and returned the registered canonical ID. No memory was
read or saved. Automatic clean quit did not pass because the session left one
empty stale client lease; exact manual cleanup restored quiescence. This is
partial, non-promoting evidence rather than full supported-host evidence.

```bash
naos-engram-memory client render --client muse
```

Review and merge manually; the toolkit does not edit Muse user settings.
Restart Muse, open a registered repository, and call `mem_current_project`
before using memory. Do not add an
`ENGRAM_PROJECT` value: the wrapper resolves the selected workspace through
its approved Git remote. The managed installer records the verified host-local
absolute local wrapper path; GUI and CLI hosts must not depend on an
interactive shell `PATH`.

## Grok CLI and Grok Bot (scope only)

Grok CLI is catalogued as an experimental prospect, but no exact executable
identity or official local stdio MCP configuration contract has been verified
on Windows. Do not guess a configuration file or install an adapter until the
released CLI and its MCP host behavior are documented and pass the acceptance
matrix. Grok Bot is a model/chat surface rather than an independent local MCP
host, so it does not receive a direct Engram adapter. Configure Engram in the
actual MCP-capable coding agent that uses Grok as its model provider.

## Not independent Engram client targets

- **OpenRouter** is a model provider rather than a local MCP client host. It
  can be used through a coding agent that supports MCP, but it does not receive
  a direct Engram adapter.
- **Open Design** exposes its own MCP server to supported coding agents. It is
  not another host for Engram; configure Engram in the underlying agent.
- **Panes** is excluded by project decision.

## Every client

Restart the client and call `mem_current_project` before reading or saving
memory. For explicit entries, compare the returned canonical ID with the
configured project; for dynamic entries, compare it with the active repository
you intended to open. If wrapper stderr reports a resolution failure, run
`naos-engram-memory inventory` from the checkout and add a reviewed registry entry
instead of bypassing the wrapper.

## Instruction surfaces

MCP configuration alone is incomplete. `instruction render` previews the
managed lifecycle policy: resolve at start, recall narrowly, verify repository
authority during work, save durable context only, summarize substantive work
at end, degrade to repository evidence, and never sync without approval.

`instruction install --host HOST --project PATH` supports project-owned
`AGENTS.md` or `CLAUDE.md`. It preserves existing content and writes only one
`NAOS-ENGRAM-MEMORY` marker block. It backs up an existing file and refuses
malformed or duplicate markers for manual review. User personalization,
custom instructions, rules, skills, agents, and workflows are catalogue
classifications and previews only; this release does not edit actual user
platform settings.
