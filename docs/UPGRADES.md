# Release Support and Upgrade Policy

The release manifest is an allowlist. A version is not installable because it
is upstream's latest, popular, or semver-compatible; it is installable only
when the manifest marks the release and exact platform asset `supported` and
contains a verified SHA-256.

## Candidate gate

For every upstream candidate, record the release notes and run isolated tests:

1. Create a disposable HOME and fixture repositories; never test against a
   live database first.
2. Verify canonical project resolution through an explicit ID, approved remote,
   `ENGRAM_PROJECT`, and `--project`; prove remote mismatch fails.
3. Exercise MCP read, save, session-summary, and project-scoped context paths.
4. Run ranking-sensitive fixture queries and assert required observations remain
   retrievable, not merely that the command exits successfully.
5. Run simultaneous fixture clients against one disposable database.
6. Test an offline database copy, binary backup, rollback, and unavailable
   service handling.
7. Record exact OS/architecture, binary SHA-256, commands, outputs, failures,
   and disposition. Promote only platforms with clean evidence.

v1.20.0 provider lifecycle support is approved for macOS ARM64, Ubuntu AMD64,
and Windows AMD64 after the recorded validation in
[`evidence/v1.20.0-darwin-arm64.json`](evidence/v1.20.0-darwin-arm64.json) and
[`evidence/v1.20.0-ubuntu-amd64-runtime.json`](evidence/v1.20.0-ubuntu-amd64-runtime.json),
plus [`evidence/v1.20.0-windows-amd64.json`](evidence/v1.20.0-windows-amd64.json).
That does not promote a named IDE or AI host; host support remains an
independent catalogue/acceptance decision. Other platforms remain experimental.
v1.15.1 remains unsupported for the observed MCP override defect.

## Rollout

Check upstream on demand. A maintainer evaluates a candidate and
commits the manifest/evidence change. A clone-free installation uses the same
manifest through the installed command:

```bash
naos-engram-memory provider status
naos-engram-memory provider install --maintenance-window --yes
naos-engram-memory provider upgrade --maintenance-window --yes
naos-engram-memory provider rollback --maintenance-window --yes
```

An adopted Homebrew or other external provider is deliberately outside these
mutation commands. The state provenance must prove `managed_install`, the
recorded absolute path must equal the toolkit-owned user binary, and its current
SHA-256 must match before and again after acquiring the maintenance lock.
Otherwise upgrade through the external owner and run `provider adopt` again.

Every mutation refuses an active Engram process or observable open SQLite
store, backs up the database plus present WAL/SHM sidecars, and retains the
previous binary for rollback. `--dry-run` performs neither download nor write.
An exclusive per-user maintenance lock and managed-wrapper client leases
serialize participating operations and close their two startup interleavings.
Unknown/stale lock artifacts fail closed for reviewed manual removal. Direct
unmanaged Engram starts do not participate, so process/store probes remain
point-in-time evidence and the owner must keep the maintenance window closed.
Binary publication and provider-state/rollback-record publication are treated
as one maintenance transaction. If any post-replacement metadata write fails,
the toolkit restores the exact prior binary, provider state, and rollback
record and removes the newly retained rollback candidate before releasing the
maintenance lock; it does not leave an orphan transaction to guess about.
No daemon, login task, or setup rerun may automatically update a binary. Windows
`setup.ps1 -InstallSupported`, `-Upgrade`, and `-Rollback` remain disabled and
perform no binary replacement or database maintenance. Use the installed
`naos-engram-memory provider ...` CLI for supported Windows AMD64 operations.

## Promoting a checksum-pinned platform

Three platforms ship with a verified SHA-256 but remain `experimental` because no
installed-runtime validation has run on their hardware: `darwin_amd64`,
`linux_arm64`, and `windows_arm64`. Each records
`promotion_blocked_on` in [`config/release-support.json`](../config/release-support.json)
and has an asset-identity receipt under `docs/evidence/`.

What is already established for them, from any machine:

- the published archive SHA-256 matches upstream's `checksums.txt` for the release;
- the archive contains exactly one safe `engram` / `engram.exe` member under the
  managed extraction rules, with no absolute, traversing, or linked member;
- the extracted binary's SHA-256 is recorded for later comparison.

What is not, and is what promotion requires:

- the binary has never been executed on that architecture;
- no MCP stdio `initialize` / `tools/list` / `mem_current_project` round trip;
- no managed wrapper start, runtime install, or fail-closed identity check there.

To promote one, on real hardware of that platform:

1. Obtain the provider for that platform and confirm its SHA-256 equals the pinned
   value in the manifest.
2. Run the installed-runtime probe:
   `python3 tools/validate_installed_runtime.py --engram-binary /absolute/path/to/engram`
   It must report `DISPOSITION: passed` with every check `passed` and
   `source_worktree_dirty: false`. A dirty worktree yields
   `candidate_passed_not_release_attestation`, which is not a promotion receipt.
3. Commit that receipt to `docs/evidence/`.
4. Change the platform's `status` to `supported`, cite the new receipt in its
   `evidence` string, and remove `promotion_blocked_on`.

Do not flip `status` on asset-identity evidence alone. That is the mistake the
`windows_amd64` entry records under `promotion.residual_gaps`, and it is why that
entry is labelled an explicit owner decision rather than an attestation.
