## Engram managed cross-client memory protocol

- Repository files, tests, and Git are authoritative. Engram is portable,
  project-scoped advisory context; native client memory is only a client-local
  cache for preferences and safety behaviour.
- At the start of a conversation or IDE session, consult concise client-native
  memory when available, then call `mem_current_project` when the server
  exposes it. Stop and report an unresolved, ambiguous, or unexpected project.
  Do not choose a project from a folder basename or retry with an all-project
  query.
- If `mem_current_project` is not exposed, use narrow wrapper-bound
  `mem_context` as the compatibility path. An `unknown_project` result means a
  first Engram session for that project, not permission to query all projects.
- Once the canonical project is confirmed, search only task-relevant
  observations before planning or editing. Reconcile summaries and durable
  preferences, not raw histories, and briefly state material recovered context.
- Verify material remembered claims against the repository. If memory and
  source disagree, surface the conflict; do not silently overwrite either.
- Save durable decisions, non-obvious fixes, reusable discoveries, and concise
  handoffs with project scope. Use summaries, not raw histories.
- Before a session ends, save a concise project-scoped session summary when
  there is durable work to hand off. Do not run Git sync, cloud sync, import,
  migration, or upgrades unless the user separately and explicitly authorizes
  that operation under the selected profile.
- Never save credentials, secrets, personal data, customer data, regulated
  data, connection strings, or instructions to bypass governance.
- If Engram is unavailable, continue using repository evidence and report the
  degradation once. Do not block work and do not invent recalled context.

### Strict save threshold

For governed work, save a durable project-scoped observation after an approved
decision, non-obvious root cause/fix, reusable configuration fact, or explicit
user preference. Review, audit, and triage roles remain read-only unless the
user explicitly asks them to record a handoff.
