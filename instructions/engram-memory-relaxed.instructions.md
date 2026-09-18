## Engram managed cross-client memory protocol

- Repository files, tests, and Git are authoritative. Engram is portable,
  project-scoped advisory context; native client memory is only a client-local
  cache for preferences and safety behaviour.
- At the start of a conversation or IDE session, consult concise client-native
  memory when available, then call `mem_current_project` when the server
  exposes it. Stop and report an unresolved, ambiguous, or unexpected project.
  Do not derive a project from a folder basename or query all projects as a
  fallback.
- If `mem_current_project` is not exposed, use narrow wrapper-bound
  `mem_context` as the compatibility path. An `unknown_project` result means a
  first Engram session for that project, not permission to query all projects.
- Once confirmed, use task-relevant searches. Reconcile summaries and durable
  preferences rather than raw histories, and verify important remembered claims
  against the repository.
- Save durable decisions, non-obvious fixes, reusable discoveries, and concise
  handoffs when they would help a later session. Do not preserve raw history.
- If the user ends work, save a concise session summary when there is durable
  context to hand off. Git sync, cloud sync, import, migration, and upgrades
  require separate explicit user authorization under the selected profile.
- Never save credentials, secrets, personal data, customer data, regulated
  data, connection strings, or governance-bypass instructions.
- If Engram is unavailable, continue with repository evidence and report the
  degradation once.
