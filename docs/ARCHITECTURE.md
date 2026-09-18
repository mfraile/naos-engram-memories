# Architecture

## Boundaries

`naos-engram-memories` is a deployment toolkit, not a central memory service. It
owns a registry, client templates, policy, validation tooling, and operations
guidance. Engram owns local observation storage and its optional cloud-service
protocol. Repository sources remain the authority for code and governance.

Project identity is resolved in this order:

1. A registered explicit project requested by the client.
2. The checked-out Git `origin` remote, if it is approved in the registry.

When both are available they must resolve to the same canonical ID. Any missing
mapping, alias collision, or remote mismatch fails closed. Neither a checkout
folder name nor a hostname is evidence of project identity.

## Profiles

| Profile | Storage and replication | Intended use |
| --- | --- | --- |
| `local` | Per-user local SQLite only | Default; individual developer |
| `personal` | Local SQLite plus owner-approved local backup/restore | One user across owned machines |
| `team` | Local working copies plus organization-owned self-hosted Engram Cloud/Postgres | Approved shared project context |

No profile uses Git memory chunks. A `team` deployment is isolated by
organization/tenant; there is no global cross-organization consolidator.

## Team trust model

The server must authenticate each user and assign deny-by-default project
grants. Client-supplied project, user, machine, IDE, or operating-system fields
are provenance only and cannot authorize an operation. Store `actor_id` only
from the authenticated service identity and keep it in an access/audit layer,
not in agent-supplied memory content.

Engram's `scope` is a recall filter. It must not be described as tenant or
project authorization. Revoking a server grant blocks future service access;
it cannot remove an observation that a client already copied locally.

## Provenance envelope

When a deployment needs non-content provenance, keep a minimal,
non-identifying envelope adjacent to audit data:

```json
{
  "project_id": "canonical-project-id",
  "client_kind": "codex",
  "os_family": "macos",
  "device_pseudonym": "rotating-non-PII-id",
  "schema_version": 1,
  "created_at": "RFC3339 timestamp"
}
```

Do not place names, hostnames, e-mail addresses, paths, raw device identifiers,
tokens, or `actor_id` in the envelope. In `team`, service-side audit records may
associate a server-authenticated actor with this envelope.
