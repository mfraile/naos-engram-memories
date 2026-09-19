# Operations and Recovery

## Validation prerequisites

The unit suite and `tools/validate_installed_runtime.py` build a wheel offline
with `--no-build-isolation`, so they use the ambient `setuptools`.
`setuptools>=70.1` is required; the 68.x shipped by Debian and Ubuntu cannot run
`bdist_wheel` in that mode and fails for every project, not only this one. The
validator now pre-checks this and names the remediation instead of surfacing a
pip traceback. Continuous integration pins `setuptools==80.9.0`.

The suite must pass on the whole declared `requires-python` range (3.10 to 3.13).

## Normal operation

The managed wrapper invokes `engram mcp --tools=agent`, so the exposed MCP
surface is the agent profile rather than the provider's administrative
delete/merge profile. It refuses non-empty cloud autosync/server/token, legacy
remote URL/token, database URL, JWT secret, and custom data-directory overrides
before provider startup. This release does not authorize cloud synchronization.

Run clients concurrently only for normal local reads/writes. SQLite WAL and
the Engram retry policy do not make upgrades, migrations, repair, import, or
service synchronization safe while clients are active.

When an MCP client cannot start, inspect its redacted wrapper stderr and run
the redacted inventory. The wrapper does not write a redirectable file log.
Do not expose raw client output in tickets because paths or environment details
may be sensitive.

## Maintenance window

1. Stop Codex, VS Code, Claude Code, and any other MCP client using the store.
2. Confirm no Engram MCP process is active.
3. Run the explicit operation with `--maintenance-window`.
4. Restart clients and verify `mem_current_project` before writing.

The managed wrapper holds a unique client lease for the lifetime of its Engram
child. Provider maintenance holds an exclusive per-user lock and scans every
lease after acquiring it. A stale or unfamiliar lock/lease is a fail-closed
condition: inspect its ownership and the machine's processes, then remove it
manually only after establishing that no client or maintenance operation is
alive. The toolkit does not delete ambiguous locks automatically.

This handshake covers managed wrappers. A direct or otherwise uncooperative
Engram invocation does not participate, and point-in-time process/store probes
cannot atomically prevent such a command from starting after the probe. The
maintenance-window instruction to close all clients therefore remains a real
owner responsibility, not merely a CLI flag.

The supported Unix upgrade script creates a local offline database backup and retains the
prior binary. `--rollback --maintenance-window` restores the newest retained
binary; it does not restore data automatically. Restore a database backup only
after a separately approved incident decision.

Windows binary/database maintenance actions in `setup.ps1` remain disabled;
their switches fail before any download, backup, replacement, or rollback.
Supported Windows AMD64 maintenance uses the installed
`naos-engram-memory provider ...` CLI under the same explicit maintenance-window
and backup requirements.
