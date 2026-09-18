#!/usr/bin/env bash
# Fail-closed stdio wrapper for registered Engram projects.
set -euo pipefail

if [[ -n "${ENGRAM_CLOUD_AUTOSYNC:-}${ENGRAM_CLOUD_SERVER:-}${ENGRAM_CLOUD_TOKEN:-}${ENGRAM_REMOTE_URL:-}${ENGRAM_TOKEN:-}${ENGRAM_DATABASE_URL:-}${ENGRAM_JWT_SECRET:-}" ]]; then
  printf '%s\n' 'ERROR cloud/autosync environment is unsupported by this locked local-only wrapper' >&2
  exit 64
fi

DEFAULT_CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/naos-engram-memory"
LEGACY_CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/engram-memory"
if [[ -n "${ENGRAM_MEMORY_CONFIG_DIR:-}${ENGRAM_MEMORY_TOOL:-}${ENGRAM_MEMORY_REGISTRY:-}${ENGRAM_MEMORY_LOG:-}${ENGRAM_MEMORY_PYTHON:-}${NAOS_ENGRAM_MEMORY_CONFIG_DIR:-}${NAOS_ENGRAM_MEMORY_TOOL:-}${NAOS_ENGRAM_MEMORY_REGISTRY:-}${NAOS_ENGRAM_MEMORY_LOG:-}${NAOS_ENGRAM_MEMORY_PYTHON:-}" ]]; then
  printf '%s\n' 'ERROR toolkit path overrides are not accepted by the installed MCP wrapper' >&2
  exit 64
fi
if [[ -n "${ENGRAM_DATA_DIR:-}" ]]; then
  printf '%s\n' 'ERROR custom ENGRAM_DATA_DIR is unsupported by the managed wrapper in this release' >&2
  exit 64
fi
NAOS_ENGRAM_MEMORY_CONFIG_DIR=__NAOS_ENGRAM_MEMORY_CONFIG_DIR__
if [[ "$NAOS_ENGRAM_MEMORY_CONFIG_DIR" == "__NAOS""_ENGRAM_MEMORY_CONFIG_DIR__" ]]; then
  NAOS_ENGRAM_MEMORY_CONFIG_DIR="$DEFAULT_CONFIG_DIR"
fi
NAOS_ENGRAM_MEMORY_TOOL="${NAOS_ENGRAM_MEMORY_CONFIG_DIR}/engram_memory.py"
NAOS_ENGRAM_MEMORY_REGISTRY="${NAOS_ENGRAM_MEMORY_CONFIG_DIR}/projects.json"
PROVIDER_MAINTENANCE_LOCK="${NAOS_ENGRAM_MEMORY_CONFIG_DIR}/.provider-maintenance.lock"
PROVIDER_CLIENT_LEASE_ROOT="${NAOS_ENGRAM_MEMORY_CONFIG_DIR}/.provider-client-leases"
PYTHON_EXECUTABLE=__PYTHON_EXECUTABLE__
if [[ "$PYTHON_EXECUTABLE" == "__PYTHON""_EXECUTABLE__" ]]; then
  PYTHON_EXECUTABLE="$(command -v python3 || true)"
fi

assert_no_symlink_chain() {
  local path="$1" current='' part
  [[ "$path" == /* ]] || return 1
  IFS='/' read -r -a parts <<< "${path#/}"
  for part in "${parts[@]}"; do
    [[ -n "$part" ]] || continue
    current="${current}/${part}"
    if [[ -L "$current" ]]; then
      if [[ "$(uname -s)" == Darwin && "$current" == /var && "$(readlink "$current")" == private/var ]]; then
        continue
      fi
      return 1
    fi
  done
}

for managed_path in "$NAOS_ENGRAM_MEMORY_CONFIG_DIR" "$LEGACY_CONFIG_DIR" "$NAOS_ENGRAM_MEMORY_TOOL" "$NAOS_ENGRAM_MEMORY_REGISTRY" "$PROVIDER_MAINTENANCE_LOCK" "$PROVIDER_CLIENT_LEASE_ROOT" "$PYTHON_EXECUTABLE"; do
  assert_no_symlink_chain "$managed_path" || { printf '%s\n' 'ERROR managed wrapper path crosses a symbolic link' >&2; exit 64; }
done
[[ -n "$PYTHON_EXECUTABLE" && -x "$PYTHON_EXECUTABLE" ]] || exit 127
if [[ ! -d "$NAOS_ENGRAM_MEMORY_CONFIG_DIR" && -d "$LEGACY_CONFIG_DIR" ]]; then
  printf 'ERROR legacy toolkit config detected at %s; no files were read or merged\n' "$LEGACY_CONFIG_DIR" >&2
  exit 64
fi

if [[ -n "${ENGRAM_BIN:-}" ]]; then
  ENGRAM_EXECUTABLE="$ENGRAM_BIN"
elif [[ -x __NAOS_ENGRAM_PROVIDER_BIN__ ]]; then
  ENGRAM_EXECUTABLE=__NAOS_ENGRAM_PROVIDER_BIN__
elif command -v engram >/dev/null 2>&1; then
  ENGRAM_EXECUTABLE="$(command -v engram)"
elif [[ -x "${HOME}/bin/engram" ]]; then
  ENGRAM_EXECUTABLE="${HOME}/bin/engram"
else
  ENGRAM_EXECUTABLE=""
fi
[[ -z "$ENGRAM_EXECUTABLE" ]] || assert_no_symlink_chain "$ENGRAM_EXECUTABLE" || { printf '%s\n' 'ERROR provider path crosses a symbolic link' >&2; exit 64; }

timestamp() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { printf '[%s] %s\n' "$(timestamp)" "$*" >&2; }

[[ -f "$NAOS_ENGRAM_MEMORY_TOOL" ]] || { log 'ERROR managed registry tool is not installed'; exit 64; }
[[ -f "$NAOS_ENGRAM_MEMORY_REGISTRY" ]] || { log 'ERROR managed project registry is not installed'; exit 64; }

REQUESTED_PROJECT="${ENGRAM_PROJECT:-${1:-}}"
if [[ -n "$REQUESTED_PROJECT" ]]; then
  INCOMING_PROJECT_SIGNAL=present
else
  INCOMING_PROJECT_SIGNAL=absent
fi
if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
  PROJECT_CWD="$CLAUDE_PROJECT_DIR"
  WORKSPACE_SIGNAL=claude_project_dir
else
  PROJECT_CWD="$PWD"
  WORKSPACE_SIGNAL=process_cwd
fi
RESOLUTION_ARGS=(--registry "$NAOS_ENGRAM_MEMORY_REGISTRY" resolve --cwd "$PROJECT_CWD")
[[ -n "$REQUESTED_PROJECT" ]] && RESOLUTION_ARGS+=(--project "$REQUESTED_PROJECT")
if ! RESOLUTION="$("$PYTHON_EXECUTABLE" "$NAOS_ENGRAM_MEMORY_TOOL" "${RESOLUTION_ARGS[@]}" 2>&1)"; then
  log "ERROR canonical-project-resolution-failed; inspect a redacted inventory"
  exit 64
fi
CANONICAL_PROJECT="$("$PYTHON_EXECUTABLE" -c 'import json,sys; print(json.load(sys.stdin)["project"])' <<<"$RESOLUTION")"
RESOLUTION_SOURCE="$("$PYTHON_EXECUTABLE" -c 'import json,sys; print(json.load(sys.stdin)["source"])' <<<"$RESOLUTION")"
case "$RESOLUTION_SOURCE" in
  approved_git_remote|registered_explicit_project) ;;
  *) log 'ERROR canonical-project-resolution-returned-unsupported-source'; exit 64 ;;
esac

if ! command -v "$ENGRAM_EXECUTABLE" >/dev/null 2>&1 && [[ ! -x "$ENGRAM_EXECUTABLE" ]]; then
  log 'ERROR Engram binary is not installed or executable'
  exit 127
fi
# The lease/maintenance protocol closes both startup races:
# - maintenance wins first, so this client refuses before creating a lease;
# - the client wins first, so maintenance sees the lease and refuses.
# A second maintenance check after the exclusive lease creation handles the
# remaining interleaving. Unknown/stale leases are never removed here.
if [[ -e "$PROVIDER_MAINTENANCE_LOCK" || -L "$PROVIDER_MAINTENANCE_LOCK" ]]; then
  log 'ERROR provider maintenance is active or requires manual lock review'
  exit 75
fi
mkdir -p -- "$PROVIDER_CLIENT_LEASE_ROOT"
assert_no_symlink_chain "$PROVIDER_CLIENT_LEASE_ROOT" || { log 'ERROR provider client lease path crosses a symbolic link'; exit 64; }
CLIENT_LEASE_DIR="$(mktemp -d "${PROVIDER_CLIENT_LEASE_ROOT}/client.$$.XXXXXXXX")"
CHILD_PID=''
cleanup_client_lease() {
  if [[ -n "$CLIENT_LEASE_DIR" && -d "$CLIENT_LEASE_DIR" ]]; then
    rmdir -- "$CLIENT_LEASE_DIR" 2>/dev/null || true
  fi
}
forward_signal() {
  local signal="$1"
  if [[ -n "$CHILD_PID" ]]; then
    kill -s "$signal" "$CHILD_PID" 2>/dev/null || true
  fi
}
trap cleanup_client_lease EXIT
trap 'forward_signal INT' INT
trap 'forward_signal TERM' TERM
assert_no_symlink_chain "$CLIENT_LEASE_DIR" || { log 'ERROR provider client lease crosses a symbolic link'; exit 64; }
if [[ -e "$PROVIDER_MAINTENANCE_LOCK" || -L "$PROVIDER_MAINTENANCE_LOCK" ]]; then
  log 'ERROR provider maintenance started during MCP client startup'
  exit 75
fi
if ! "$PYTHON_EXECUTABLE" "$NAOS_ENGRAM_MEMORY_TOOL" provider verify-bound --home "$HOME" --config-dir "$NAOS_ENGRAM_MEMORY_CONFIG_DIR" --path "$ENGRAM_EXECUTABLE" >/dev/null; then
  log 'ERROR Engram provider integrity verification failed'
  exit 64
fi
log "spawn project=${CANONICAL_PROJECT} workspace_signal=${WORKSPACE_SIGNAL} resolution_source=${RESOLUTION_SOURCE} incoming_project_signal=${INCOMING_PROJECT_SIGNAL} provider_probe=not_run client_lease=active"

# OpenCode and several IDE hosts inherit their complete process environment into
# local MCP children. Start Engram with a small, explicit local-runtime
# environment so arbitrary client/API credentials cannot cross this boundary.
/usr/bin/env -i \
  HOME="$HOME" \
  PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
  TMPDIR="/tmp" \
  LANG="C" \
  LC_ALL="C" \
  NO_COLOR="1" \
  ENGRAM_PROJECT="$CANONICAL_PROJECT" \
  "$ENGRAM_EXECUTABLE" mcp --tools=agent --project="$CANONICAL_PROJECT" <&0 &
CHILD_PID=$!
set +e
wait "$CHILD_PID"
STATUS=$?
set -e
CHILD_PID=''
exit "$STATUS"
