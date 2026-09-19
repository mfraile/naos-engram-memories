# NAOS Engram Memories

An Engram-backed, client-agnostic toolkit for local-first, project-scoped AI
coding memory. It is not a memory store, a hosted service, or an authorization
system.

## Operating model

- Each user owns a local SQLite database. Repository files, tests, and Git are
  authoritative; Engram is portable, advisory context.
- A canonical project ID comes from [`config/projects.json`](config/projects.json):
  an approved Git remote or an explicitly registered project ID. The checked-in
  registry is synthetic; replace it with reviewed organization entries. Folder
  names are never used as identity.
- Git stores only this toolkit's source, templates, policy, and documentation.
  It never stores `.engram` chunks, databases, manifests, or memory exports.
- `local` is the default profile. `personal` permits an explicitly approved
  local backup/restore procedure. `team` is an organization-owned deployment
  with authenticated, individual users and server-enforced project grants.

This release exposes only the `local` profile. The `personal` and `team`
sections are architecture guidance, not enabled transport, cloud, or service
features; no command in this release configures replication or team access.

This repository was published from a fresh history after the sanitization
procedure passed and a human review approved the staged contents. Re-run
`python3 tools/sanitize_public_tree.py` before any further publication step. The
toolkit source is under the [MIT License](LICENSE); it does not bundle an
upstream Engram binary or source. See [NOTICE.md](NOTICE.md). Because the
history was rewritten for publication, the checked-in evidence files are not
reproducible from this tree; see
[`docs/evidence/README.md`](docs/evidence/README.md) for exactly what each
record does and does not attest.

## Friendly local onboarding

The intended public no-clone path, once version `1.0.1` has actually been
published, is:

```bash
pipx install "naos-engram-memories==1.0.1"
naos-engram-memory onboard --project /path/to/registered-workspace
naos-engram-memory runtime install --yes --non-interactive
naos-engram-memory provider status
```

This repository does **not** claim that package is currently published. For a
reviewed local checkout, `python3 -m pip install .` is the source-install path.
`pipx` is preferred, but `python3 -m pip install --user`, `--target`, and
`--prefix` are supported: packaged resources are located through every install
scheme's data root rather than assuming `sys.prefix`. On a distribution whose
Python is externally managed (PEP 668), prefer `pipx` or a virtual environment
over `--break-system-packages`.
If Python selection is uncertain, the foreground bootstrap enumerates explicit
`--python`, configured interpreter, `python3`/`python`, and Windows `py -3`
candidates, verifies Python 3.10+, and asks before package installation:

```bash
scripts/bootstrap.sh --list
scripts/bootstrap.sh --python /absolute/path/to/python --yes
```

Bootstrap displays the supported candidates and the selected interpreter. If
more than one supported interpreter is detected, the operator must select one
interactively or pass its absolute path with `--python`; non-interactive mode
fails with that exact remediation.

Windows uses `scripts/bootstrap.ps1 -List`, or the installed
`naos-engram-memory-bootstrap` command. Bootstrap is never called by an MCP
stdio wrapper. If Python is absent it displays exactly one detected package
manager command. An interactive foreground operator may run it only with
`--install-python --yes` (PowerShell: `-InstallPython -Yes`); non-interactive
mode only emits exact remediation and never invokes a system package manager.
After an external install command is invoked, bootstrap reports that the
command may use the network; it does not claim to have observed whether it did.
The public install uses pipx. `--pip-fallback` is an explicit
source-environment fallback, not the default public path.
If Python is available but pipx is missing, bootstrap displays one official
platform-specific installation offer. Interactive execution requires
`--install-pipx --yes` (PowerShell: `-InstallPipx -Yes`); non-interactive mode prints the exact remediation and
never runs it. After pipx installation, run `pipx ensurepath` if the executable
is not yet on `PATH`, restart the terminal, and rerun bootstrap.
See the [official pipx installation guidance](https://pipx.pypa.io/latest/how-to/install-pipx.html)
before approving a platform package-manager command.
`bootstrap.py --verify-asset <name>` checks a delivered script against
[`config/bootstrap-assets.v1.json`](config/bootstrap-assets.v1.json). Exact
SHA-256 integrity is mandatory. A detached release signature is optional and
used only when the selected profile or an auditor requires it; this toolkit
does not treat signature absence as a baseline failure or claim to validate a
signature merely because an entry is present.

The managed MCP wrapper performs canonical-project resolution and starts
Engram directly. It does not run `engram version`, `doctor`, release checks, or
other diagnostic probes during ordinary client startup.

Provider installation is a separate owner-approved action and also needs no
clone:

```bash
naos-engram-memory provider install --maintenance-window --yes --dry-run
naos-engram-memory provider install --maintenance-window --yes
```

### Choosing how to obtain the provider

`provider install` downloads the one allowlisted release asset. That is not always
the best route, and on two platforms it is the worse one:

- **macOS and Linux — prefer Homebrew, then adopt.** Upstream's recommended path is
  `brew install gentleman-programming/tap/engram`, and its release automation
  publishes that formula. Installing the asset separately competes with Homebrew
  and can leave two binaries on `PATH`. If you use Homebrew, let it own the
  binary and bind it here instead: `provider adopt` resolves the Homebrew symlink
  once and records the real executable path and hash. Upgrade with Homebrew, then
  re-adopt.
- **macOS binaries are ad-hoc signed only.** Upstream signs with
  `codesign --force --sign -`, not a Developer ID, so Gatekeeper may still object
  on first run. That is an upstream distribution property, not something this
  toolkit can resolve.
- **Windows — the prebuilt binary is known to trip antivirus.** Upstream documents
  Windows Defender, ESET, and others flagging its prebuilt releases as
  `Trojan:Script/Wacatac.H!ml` or similar, identifies it as a heuristic false
  positive on unsigned Go binaries, and states it will not purchase a code-signing
  certificate. Upstream's own advice to technical users is to build locally with
  `go install`. This toolkit cannot verify a locally built binary against a release
  checksum, but it can still bind one:

  ```powershell
  go install github.com/Gentleman-Programming/engram/cmd/engram@latest
  naos-engram-memory provider adopt --path "$env:USERPROFILE\go\bin\engram.exe" --dry-run
  naos-engram-memory provider adopt --path "$env:USERPROFILE\go\bin\engram.exe" --yes
  ```

  Adoption still enforces the approved version and records the SHA-256 the managed
  wrapper verifies before every start. What it cannot do is prove provenance from
  a signed release, so it reports `network_access_status: not_observed` and marks
  the provenance external.

Use `provider upgrade` or `provider rollback` with the same two confirmations.
The dry-run reads only the packaged allowlist and makes no network request or
write. A real install/upgrade downloads the one exact release asset, verifies
the captured SHA-256, backs up the database plus present WAL/SHM sidecars, and
retains an existing binary for rollback. It refuses while any Engram process
or observable provider-store user is active. Managed MCP wrappers also hold a
per-client lease for their provider lifetime; maintenance takes an exclusive
per-user lock and refuses any lease. Unknown or stale locks/leases remain for
manual owner review and are never removed automatically. This cooperative
protocol closes races with managed wrappers only: a direct, uncooperative
`engram` start cannot be atomically excluded by a point-in-time process/store
probe, so the owner must still close all clients for the maintenance window.
Windows AMD64 provider lifecycle operations are supported through the installed
`naos-engram-memory provider ...` CLI for the exact allowlisted v1.20.0 ZIP.

To keep an already installed supported provider, preview and then explicitly
adopt it instead of copying or replacing it:

```bash
naos-engram-memory provider adopt --path "$(command -v engram)" --dry-run
naos-engram-memory provider adopt --path "$(command -v engram)" --yes
```

Adoption resolves Homebrew or `~/bin` symlinks once, validates the exact
approved version, and records the real absolute executable path, SHA-256, and
external provenance. The managed wrapper binds and verifies that exact path
and hash before every start. Because the version probe is an external command,
its network behavior is reported as `not_observed`, not falsely as offline.
The toolkit never upgrades or rolls back an adopted external executable:
upgrade it with its owner (for example Homebrew), then explicitly re-adopt it.
`ENGRAM_DATA_DIR` is accepted when it names the store this release manages
(`~/.engram`, which is also the path NAOS governance declares) and refused with
exit 64 when it names a different one. A custom store stays unsupported because
maintenance would otherwise back up or probe a database Engram does not open;
because the effective store cannot vary, restating it is now harmless rather than
a refusal. The comparison is lexical on the normalized absolute path — resolving
symlinks to force a match would accept a path the managed symlink policy refuses.

The wrapper starts the provider with an explicit tool allowlist rather than a
profile name:

```
mcp --tools=mem_current_project,mem_context,mem_search,mem_get_observation,mem_save,mem_session_summary
```

Those six are the intersection of the provider's agent profile with the memory
tool vocabulary NAOS governance recognises, so the exposed surface is a
deterministic boundary instead of a profile whose membership can change between
provider releases. The provider's administrative delete/merge tools are excluded,
as is cross-project enumeration: upstream added `mem_list_projects` to the agent
profile after v1.20.0, and this toolkit's project-isolation protocol forbids it.
The wrapper also refuses the verified cloud autosync/server/token, legacy
remote/token, database-URL, and JWT-secret environment variables before provider
startup.
Cloud synchronization remains outside this locked local-only release.

`onboard` defaults to the `local` profile. Its initial assessment is read-only,
does not execute the Engram provider, makes no network request, accepts an
absolute or relative project path, and emits a
machine-readable, non-sensitive JSON summary. It does not edit a registry,
client, instruction file, database, binary, MCP setting, Git transport, or
memory. NAOS may launch it only after the user explicitly selects the toolkit;
the toolkit does not silently activate itself.

Add `--guided` for a foreground choice between `local`, `use-existing`,
`defer`, and `decline`. `local` remains a preview unless explicitly confirmed
with `--yes`; confirmation installs only the durable per-user policy, registry,
templates, and absolute-interpreter wrapper. It does not install Engram or edit
an MCP host. The later registry/client/instruction commands confirm each write.

The installed registry and wrapper runtime live in the user configuration
directory (`$XDG_CONFIG_HOME/naos-engram-memory` or platform equivalent), never
inside the pipx virtual environment. The live Engram provider database remains
`~/.engram/engram.db`.

The former `engram-memory` config namespace and `ENGRAM_MEMORY_*` toolkit
variables are detected but never read, merged, or migrated automatically. If
found before first runtime installation, the command stops with an explicit
migration message. The provider-owned `ENGRAM_PROJECT` and `ENGRAM_BIN`
variables are separate and remain supported.

The guided follow-on commands remain local and narrow:

```bash
naos-engram-memory doctor --project /path/to/workspace
naos-engram-memory project register --id example-product \
  --remote https://github.com/example-org/example-product.git --dry-run
naos-engram-memory project register --from-remote /path/to/workspace --dry-run
naos-engram-memory client list
naos-engram-memory client render --client cursor --project example-product
naos-engram-memory client render --client codex --project example-product \
  --workspace /absolute/path/to/workspace \
  --wrapper /absolute/path/to/engram-mcp-wrapper
naos-engram-memory client install --client cursor --project example-product \
  --workspace /path/to/workspace --dry-run
naos-engram-memory client verify --client cursor --project example-product \
  --workspace /path/to/workspace
naos-engram-memory instruction render
naos-engram-memory instruction install --host codex \
  --project /path/to/workspace --dry-run
```

Every mutating CLI command requires an interactive per-write confirmation or
an explicit `--yes`. In non-interactive use, omission is an error. `--dry-run`
validates and reports the plan without writing. Client verification checks
only syntax and the expected managed entry; it never claims the host runtime
or memory call worked.

`project register --from-remote` reads only the explicit workspace's origin
and proposes the repository component as the canonical ID; it never uses the
folder basename. Review the dry run before confirming the registry write.

## Start safely on every machine

The legacy checkout setup path remains available for source operators. Its
first command is read-only and emits no
memory content, absolute paths, hostnames, tokens, or user names.

```bash
bash scripts/setup.sh --inventory
```

Every source setup action selects and verifies one Python 3.10+ interpreter in
this order: explicit `--python`, `NAOS_ENGRAM_MEMORY_PYTHON`, `python3`,
`python`, then `py -3`. The selected interpreter is reused for the action. If
none qualifies, setup stops with an exact manual remediation and performs no
toolkit action. PowerShell provides the equivalent `-Python` option.

Windows:

```powershell
.\scripts\setup.ps1 -Inventory
```

Windows `setup.ps1` configuration and inventory remain available, but its
binary install, upgrade, database-backup, and rollback switches are explicitly
disabled. They fail before any maintenance action; use the installed
`naos-engram-memory provider ...` CLI for supported Windows AMD64 lifecycle
operations instead.

The inventory shows registered projects, aliases, current-repository
resolution, toolkit-managed provider path/checksum state, and non-content
client MCP reference states. It never executes upstream Engram commands;
database health and provider project counts are reported as `not_probed`. A
detected reference is **unvalidated**: it does not prove that a client can
start the server or that the configuration is correct. Run it separately on
every machine; local database state is not assumed to match.

## Configure a client deliberately

First install only the managed wrapper and policy files. This does **not** edit
VS Code, Codex, Claude Code, or any other client configuration.

```bash
bash scripts/setup.sh --configure --dry-run
bash scripts/setup.sh --configure
```

Source configuration delegates to the same fail-closed runtime installer as the
clone-free command; it does not maintain a second copy/delete implementation.
The user project registry is retained. `doctor` verifies the toolkit-owned
runtime against a versioned hash manifest. If those assets are pre-manifest or
locally changed, review them and run
`naos-engram-memory runtime repair --yes --non-interactive`; repair uniquely
backs up and atomically refreshes only toolkit-owned assets. `--configure` uses
plain `runtime install` and therefore fails closed on unreviewed drift; repair
is never inferred from configure. Configuration does not touch
the database; a database backup is reserved for an explicit maintenance-window
upgrade.

For a managed fixed-project workspace adapter, use the explicit installer. It
checks the workspace remote against the registry, refuses a conflicting existing
Engram entry, and is idempotent. `client repair` can replace only a provably
toolkit-managed stale Engram entry after a unique backup; unrelated entries
remain a hard refusal. It only rewrites strict JSON; it refuses comment-bearing JSONC
instead of risking a client configuration.

```bash
bash scripts/setup.sh --install-client --client vscode-generic \
  --project example-product --workspace /path/to/registered-workspace --yes
```

Use `--dry-run` first to see the canonical target without writing; it does not
require `--yes`. The actual setup action requires `--yes` and forwards a
non-interactive confirmation to the managed installer.
`codex` and `claude-code` retain render-only configuration because their safe
surfaces require a reviewed manual merge. The Codex renderer validates the
registered workspace remote and emits an absolute project `cwd`; it never
writes `.codex/config.toml` or emits a fixed `ENGRAM_PROJECT`. Muse is also
render-only because this release does not edit actual user settings.

The versioned classifications and blockers are authoritative in
[`config/mcp-hosts.v1.json`](config/mcp-hosts.v1.json). The adapter inventory is:

| Adapter | Intended client/configuration |
| --- | --- |
| `codex` | Render-only project `.codex/config.toml` with a registered absolute workspace `cwd`; manual merge only |
| `codex-vscode` | Not a separate surface; Codex IDE and CLI share `config.toml` |
| `vscode-generic` | Explicit-project VS Code workspace `.vscode/mcp.json` for Copilot Chat and Agent Host |
| `claude-code` | Dynamic Claude Code MCP configuration |
| `antigravity` | Experimental explicit-project `.agents/mcp_config.json`; exact CLI and app registered-project calls retained without promotion, with app MCP cwd `/` limitation |
| `kilo` | Experimental explicit-project `.kilo/kilo.jsonc`; Kilo Code 7.4.22 in VS Code 1.133.0 completed one registered-project call and clean quit without promotion |
| `opencode-v1` | Stable OpenCode project adapter; requires the `opencode` executable and a locally ignored repository-root `opencode.json` |
| `cursor` | Experimental project `.cursor/mcp.json`; one exact-version registered-project call retained without promotion, remaining runtime gates incomplete |
| `muse` | Render-only preview; exact-version registered-project call retained without promotion, automatic clean quit failed |
| LM Studio / AnythingLLM | Experimental preview-only prospects; direct host and identity boundaries unproven |
| Open WebUI | Unsupported: native MCP is HTTP while this toolkit provides local stdio |
| `project-config` | Repository `.engram/config.json` project default |

See [docs/CLIENTS.md](docs/CLIENTS.md) for exact installation locations and
the distinction between those clients and chat products. Ad-hoc snippets for
clients outside this table remain examples only; they are not managed or
compatibility-certified.

OpenCode V2 beta is terminally `owner_excluded` from this release. No V2
adapter is packaged, installed, probed, or supported; a pre-existing V2-shaped
configuration is refused for explicit manual archive or removal.

The dedicated `tools/run_opencode_startup_acceptance.py` lane executes the
exact pinned stable host in a disposable profile and retains initialize/tool
discovery evidence without invoking a model or reading production memory. It
does not promote runtime support. One separate user-assisted
`mem_current_project` call returned the expected registered project on the
exact pinned macOS binary, but the required negative, isolation,
instruction-lifecycle, restart, and second-platform cases remain incomplete.

`claude-code` and `muse` are dynamic adapters. They have no `ENGRAM_PROJECT`
value and resolve the active workspace through the approved Git remote at
server start. The experimental adapters use a fixed, registered canonical
project for one workspace. All adapters stop on an unregistered or conflicting
repository; none infer a folder name.

## Cross-client memory protocol

The managed instruction templates require clients to:

1. Call `mem_current_project` first and stop on an unresolved or conflicting
   project instead of guessing.
2. Use narrow, project-scoped `mem_context` and relevance searches; never load
   unrelated project histories.
3. Verify recalled claims against repository sources before relying on them.
4. Save durable decisions, non-obvious fixes, and handoffs only. Never save
   secrets, credentials, personal data, regulated data, or bypass instructions.
5. Treat native client memory as a local preference/cache layer, not as an
   authoritative replacement for the repository or Engram.

When Engram is unavailable, work continues with repository evidence and the
client reports that degradation once. The normal session-close operation is a
project-scoped summary; no automatic Git or cloud synchronization occurs.

## Versions and upgrades

[`config/release-support.json`](config/release-support.json) records the only
versions the installer may use. The installer never queries or installs
`latest`.

- v1.15.1 is unsupported because the observed MCP project override was not
  reliable.
- v1.20.0 provider lifecycle support covers validated macOS ARM64, Ubuntu
  AMD64, and Windows AMD64 assets. This does not promote any named IDE/AI host.
- Release checks occur on demand. An owner explicitly requests every
  supported upgrade during a maintenance window; no background update exists.

### Platform envelope

Platform support is an allowlist, so an unlisted platform fails closed rather
than downloading an unvalidated asset. The toolkit CLI itself is pure Python and
runs anywhere Python 3.10+ runs; the table describes the *provider* lifecycle.

Upstream publishes six assets for every release. This release pins a verified
SHA-256 for all six — each one checked against upstream's `checksums.txt` — but a
platform is promoted to `supported` only after an installed-runtime validation has
actually run on that platform's hardware. Three platforms are therefore
checksum-pinned and still `experimental`.

| Platform | `provider install`/`upgrade`/`rollback` | `provider adopt` |
| --- | --- | --- |
| macOS ARM64 (Apple Silicon) | supported | supported |
| Linux x86-64 | supported | supported |
| Windows x86-64 | supported | supported |
| macOS x86-64 (Intel) | refused — checksum pinned, validation pending | `--allow-unvalidated-platform` |
| Linux ARM64 / aarch64 | refused — checksum pinned, validation pending | `--allow-unvalidated-platform` |
| Windows ARM64 | refused — checksum pinned, validation pending | `--allow-unvalidated-platform` |
| anything else | refused — no published asset | `--allow-unvalidated-platform` |

For the three pending platforms the archive identity *has* been verified — the
published checksum matches and the archive contains exactly one safe binary
member — but the binary has never been executed on that architecture, and no MCP
stdio round trip or wrapper start has been exercised there. See
`docs/evidence/v1.20.0-asset-identity-*.json` for exactly what each receipt does
and does not establish, and [docs/UPGRADES.md](docs/UPGRADES.md) for the one-step
promotion once you can run the validator on that hardware.

On a platform with no allowlisted asset you can still bind an `engram` you
installed yourself. This is an explicit owner decision, never a default:

```bash
naos-engram-memory provider adopt --path "$(command -v engram)" --dry-run
naos-engram-memory provider adopt --path "$(command -v engram)" \
  --allow-unvalidated-platform --yes
```

Only the platform-asset allowlist is waived. The approved version check and the
recorded SHA-256 path binding still apply, the managed wrapper still verifies
that exact path and hash before every start, and the adoption is recorded as
`platform_validation: unvalidated_owner_approved`. Toolkit-driven install,
upgrade, and rollback remain unavailable there, because no verified download
exists to install. Windows ARM64 is detected as `windows_arm64` and is refused
rather than silently receiving the x86-64 asset.

After a release is promoted on a supported platform, use the clone-free
`naos-engram-memory provider ...` commands above. The legacy source-tree
`--install-supported --maintenance-window` entry point remains available to
source operators. See [docs/UPGRADES.md](docs/UPGRADES.md).

The exact PyPI name `naos-engram-memories` returned 404 from the official JSON
endpoint on 2026-08-13, so it appeared unregistered at that check. This is not
a reservation or publication claim; release automation must recheck the
official registry immediately before publishing.

`python3 tools/check_upstream.py` reports the latest upstream release without
changing the manifest. Normal pull-request/main CI may run read-only policy
checks; a maintainer must still perform compatibility validation and commit a
reviewed promotion.

## Team deployments

Engram `scope` filters recall; it is not access control. A project ID is also
not access control. For `team`, use a self-hosted, organization-owned service
and Postgres with individual authenticated identities and deny-by-default
project grants. Do not use a shared token, a global consolidator, or client
metadata as an authorization decision. Revoking a grant stops future service
access; it cannot erase local copies already held by a client.

The toolkit provides architecture and operating guidance, not a hosted service
or an enterprise IAM product. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
and [docs/SECURITY.md](docs/SECURITY.md).

## Repository layout

| Path | Purpose |
| --- | --- |
| `config/projects.json` | Canonical project registry and approved remotes |
| `config/release-support.json` | Exact release status and platform support policy |
| `config/mcp-hosts.v1.json` | Versioned host, instruction-surface, evidence, and blocker catalogue |
| `config/mcp-host-observations.v1.json` | Append-only exact-version real-host observations with bounded claims |
| `clients/` | Explicit managed client templates |
| `instructions/` | Cross-client memory protocol templates |
| `scripts/` | Explicit setup, upgrade, rollback, and MCP wrapper scripts |
| `tools/engram_memory.py` | Inventory, resolution, guided runtime, registry, client, and instruction command implementation |
| `tools/validate_installed_runtime.py` | Synthetic clean-wheel and stdio MCP acceptance probe |
| `tools/run_opencode_startup_acceptance.py` | Pinned real-OpenCode startup and tool-discovery receipt runner |
| `tools/sanitize_public_tree.py` | Fail-closed public-release staging scan |
| `docs/` | Architecture, administration, migration, operations, security, and release manuals |
| `docs/evidence/README.md` | What each retained validation receipt does and does not attest |

## Validation

The test suite and the installed-runtime probe build a wheel offline with
`--no-build-isolation`, so they use whatever `setuptools` is already importable.
**`setuptools>=70.1` is a prerequisite.** The `setuptools` 68.x that Debian and
Ubuntu ship cannot run `bdist_wheel` in that mode at all, which is a
distribution packaging defect rather than a fault in this toolkit. Continuous
integration pins `setuptools==80.9.0`; do the same locally, ideally in a virtual
environment:

```bash
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install "setuptools==80.9.0"
```

```bash
python3 -m unittest discover -s tests -v
bash -n scripts/setup.sh scripts/engram_mcp_wrapper.sh scripts/engram-memory
python3 tools/engram_memory.py release-status
python3 tools/validate_installed_runtime.py --engram-binary /absolute/path/to/engram
```

The suite is expected to pass on every interpreter in the declared
`requires-python` range (3.10 through 3.13); CI runs all four on Ubuntu and
macOS.

The installed-runtime probe builds and installs the wheel offline, uses a
synthetic home, config root, Engram data root, and Git repositories, and checks
the real stdio initialize/tools/list/`mem_current_project` path. Candidate
binary validation is intentionally isolated from real memory data; follow
[docs/UPGRADES.md](docs/UPGRADES.md) before changing release status.

### Environment constraints

Managed writes refuse symbolic links anywhere in a runtime or workspace path, so
two environments need an explicit adjustment:

- A symlinked home directory: rerun with `HOME` set to its resolved physical
  path, for example `HOME="$(cd "$HOME" && pwd -P)"`.
- A real home whose configuration directory is a symlink: set
  `NAOS_ENGRAM_MEMORY_CONFIG_DIR` to a path whose every component is real.

`NAOS_ENGRAM_MEMORY_CONFIG_DIR` cannot substitute for the first case, because the
home check runs independently of it. See
[docs/ADMINISTRATION.md](docs/ADMINISTRATION.md).
