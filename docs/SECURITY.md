# Security and Privacy Boundaries

Never store credentials, tokens, passwords, API keys, connection strings,
personal data, customer data, regulated data, internal endpoints, or
governance-bypass instructions in memory. Treat content as copyable to every
local database and potentially to an authorized service in `team` mode.

The project registry accepts only credential-free `https://`,
`ssh://git@host/path`, or `git@host:path` identities. Managed installers refuse
local remotes, arbitrary remote usernames, URL query/fragment material, and
symbolic-link traversal at write boundaries.

The client wrapper enforces only project namespace resolution. It is not an IAM
layer. In a team deployment, project grants must be checked by the server using
an authenticated individual identity. `scope`, `client_kind`, `os_family`, and
`device_pseudonym` are never authorization inputs.

Inventory output is intentionally redacted and aggregate-only. Do not publish
wrapper logs, databases, backups, old `.engram` artifacts, or raw manifests.
For an incident, preserve the minimum necessary local evidence, control access,
and follow the organization’s incident and retention process.
