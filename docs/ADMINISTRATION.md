# Administrator Manual

## Registry change control

Edit `config/projects.json` only through reviewed source control. Each entry
needs one canonical ID, one or more verified Git remotes, and optional aliases.
The resolver rejects duplicate aliases and remotes. Do not insert unverified
projects discovered from another machine's database; run the read-only
inventory on that machine first.

Approved remote identity forms are deliberately narrow: credential-free
`https://`, `ssh://git@host/path`, or `git@host:path`. Local paths, `file://`,
arbitrary SSH usernames, URL queries/fragments, and embedded credentials are
refused before registry persistence.

Managed configuration writes reject symbolic links in workspace and runtime
targets. They allocate unpredictable same-directory temporary files
exclusively and atomically replace the reviewed target only after rendering.

## Profiles

- Use `local` unless a shared use case has an approved owner and data policy.
- Use `personal` only for explicitly approved owner backup/restore. Backups are
  local files and must never be committed.
- Use `team` only with an organization-owned service, individual authenticated
  users, project grants, incident ownership, retention, backup, and recovery
  procedures. A shared token is not a team identity model.

Before enabling `team`, confirm the selected Engram server version's exact
authentication, grant, audit, and revocation semantics in its released source
and deployment documentation. If they do not satisfy the organization’s IAM
requirements, place an approved identity-aware access layer in front of the
service or do not deploy the profile.

## First machine command

Run the inventory on every host:

```bash
naos-engram-memory inventory
```

Inventory, `doctor`, and the initial `onboard` assessment do not execute
Engram. They report only toolkit-managed provider state, the recorded provider
path/checksum relationship, registry resolution, and non-content client
references. Database health and provider project counts are `not_probed`.
These read-only flows therefore perform no provider network or store activity.
Run any upstream diagnostic only as a separate, explicitly approved operation;
Engram releases may perform their own update check or store initialization.
Ordinary MCP wrapper startup also skips version and diagnostic probes.

Provider installation is also available from the pipx-installed command; a
source clone is not required. First run `naos-engram-memory provider status`.
Only after stopping every Engram process and store user may an owner run
`provider install`, `provider upgrade`, or `provider rollback` with both
`--maintenance-window` and `--yes`. Add `--dry-run` first: it resolves only the
locally packaged allowlist and performs no network access or write. The real
install/upgrade downloads exactly the manifest URL, verifies its captured
SHA-256 before extraction, and never resolves `latest`.

`provider adopt --path <engram> --dry-run` previews use of an existing provider;
`--yes` records its resolved real path, checksum, semantic version, and
`adopted_existing` provenance, then refreshes the integrity-managed wrapper.
The version probe executes the selected provider, so network access is
`not_observed` and may be possible. Wrapper startup never executes a diagnostic
probe; it verifies the recorded path and checksum locally before spawning MCP.
Any path retargeting or byte change is refused. Adopted binaries remain owned
by their external installer and cannot be upgraded or rolled back by this
toolkit; upgrade externally and re-adopt.

This release supports only the provider's default `~/.engram` data directory.
Any non-empty `ENGRAM_DATA_DIR` causes adoption, provider mutation, and managed
wrapper startup to fail before download, state change, binary change, database
backup, or provider spawn. Custom stores require a future independently tested
profile that resolves, probes, and backs up the same explicit location.

Provider mutation acquires an exclusive per-user maintenance lock before its
activity probes and retains it through backup, download, verification, binary
replacement, and state recording. Managed MCP wrappers create a unique client
lease, recheck the maintenance lock, and retain the lease until the provider
exits. Either side therefore refuses when it loses the startup race. Any
unknown/stale lock or lease blocks maintenance and requires reviewed manual
removal; the toolkit never guesses that it is stale. This protocol coordinates
managed wrappers. It cannot atomically stop an uncooperative direct `engram`
command started after a point-in-time probe, so closing all Engram clients is
still an owner obligation of `--maintenance-window`.

It reports no observations, paths, or raw configuration contents. Its
`client_mcp_reference_status` covers Codex, legacy VS Code profile settings,
Copilot Agent Host workspace/user configuration, Claude Code, Antigravity,
Kilo, OpenCode, Cursor, LM Studio, AnythingLLM, Open WebUI, and Muse Code.
Values are deliberately conservative:
`not_detected` means no known configuration file was found;
`configuration_present_without_reference` means a known file has no
Engram-related reference; `reference_detected_unvalidated` means that a known
file contains one; and `configuration_unreadable` means inspection could not
complete. None is a health check or proof of a working registration. Attach
only its redacted JSON to a migration or support record.

## Reusable clean-system test bases

The pinned Ubuntu AMD64 test base is declared in
`tests/docker/Dockerfile.ubuntu-amd64`. Build it with an explicit platform and
run the host-acceptance matrix with networking disabled and the toolkit mounted
read-only. An operator may archive that image with `docker save` and retain a
checksum plus the image ID for later clean-system regressions. Never bake a
live home directory, credentials, client settings, Engram database, or real
`.engram` data into a reusable image.

Docker validates Linux CLI, protocol, static configuration, and eligible
headless extension-host lanes. It cannot certify a native macOS/Windows GUI,
subscription-gated desktop client, or IDE extension unless that named released
host actually runs the test. The catalogue records every blocked combination
instead of silently treating it as passed.

The final synthetic installed-wheel results are retained in
`docs/evidence/v0.1.0-installed-runtime-darwin-arm64.json` and
`docs/evidence/v1.20.0-ubuntu-amd64-runtime.json`. They validate the provider
and installed stdio wrapper only; they do not promote a named IDE or AI host.

## Runtime namespace and legacy state

Persistent toolkit policy, registry, templates, logs, and wrapper state live
under the platform user config directory named `naos-engram-memory`. Package
resources inside a pipx environment are immutable seeds, never the persistent
registry. The former `engram-memory` directory and `ENGRAM_MEMORY_*` toolkit
variables are detection-only: runtime installation and both wrappers stop when
an unmigrated legacy namespace is the only state. They never merge it. Review,
archive, or migrate it explicitly, then rerun the dry-run and install commands.
