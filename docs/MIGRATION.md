# Migration Guide

1. Stop all MCP clients on the machine. Do not migrate, import, synchronize, or
   upgrade an active SQLite store.
2. Run the read-only inventory and record only its redacted output.
3. Review database project names against the registry. Existing checkout names
   do not need renaming. Add only verified remote-to-canonical mappings.
4. Run `setup --configure --dry-run`, review the planned local files, then run
   `setup --configure`. Source setup delegates to fail-closed `runtime install`:
   it preserves `projects.json`, installs reviewed toolkit assets, and records
   `managed-runtime.v1.json`, but refuses a pre-manifest or locally modified
   managed runtime. Use `runtime repair` only as a separate explicit command
   after reviewing its backup/refresh plan.
5. For a supported fixed-project workspace adapter, run
   `setup --install-client --client NAME --project CANONICAL_ID --workspace PATH --dry-run`,
   review the result, then rerun with `--yes` and without `--dry-run`. The installer validates
   the Git remote, backs up strict JSON before writing, and refuses collisions
   or comment-bearing JSONC. Use `render-client` for dynamic/user-scoped
   adapters or a client surface not covered by the safe installer.
6. Restart one client at a time and verify `mem_current_project`. Do not save
   until the returned canonical project is correct.
7. Retire Git memory-sync aliases and routines. Existing private Git history
   stays private; do not copy its `.engram` artifacts into the new workflow.

Provider migration is a distinct, explicit maintenance action. The installed
tool can preview it without a clone:

```bash
naos-engram-memory provider status
naos-engram-memory provider upgrade --maintenance-window --yes --dry-run
```

For an existing Homebrew or `~/bin` symlink installation, use explicit
`provider adopt --path <candidate> --dry-run` followed by `--yes`. The toolkit
records the resolved real executable rather than the mutable launcher symlink.
After an external upgrade, re-adopt so the wrapper receives the new verified
path/hash. Never point toolkit upgrade/rollback at an external installation.

After reviewing the preview and closing all Engram processes, omit `--dry-run`.
The command retains the previous binary and creates a collision-safe local
database/sidecar backup. If validation fails, do not delete those backups; use
`provider rollback --maintenance-window --yes` or restore through the reviewed
manual recovery procedure.

If a historical session has a directory/project mismatch, preserve it as a
review finding. Do not merge project namespaces merely because names are
similar.
