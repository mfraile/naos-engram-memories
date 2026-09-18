#!/usr/bin/env bash
# Managed Engram setup for macOS and Linux.
#
# This script never resolves GitHub's "latest" release and never synchronizes
# memory through Git. Its default action is a read-only inventory. Writes need
# an explicit action; binary replacement also needs a maintenance-window
# acknowledgement after MCP clients have been stopped.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TOOL="${REPO_DIR}/tools/engram_memory.py"
REGISTRY="${REPO_DIR}/config/projects.json"
USER_REGISTRY="${XDG_CONFIG_HOME:-${HOME}/.config}/naos-engram-memory/projects.json"
SUPPORT_MANIFEST="${REPO_DIR}/config/release-support.json"
if [[ -n "${ENGRAM_MEMORY_CONFIG_DIR:-}${ENGRAM_MEMORY_BIN_DIR:-}${ENGRAM_MEMORY_BACKUP_ROOT:-}${ENGRAM_MEMORY_DATABASE_PATH:-}" ]]; then
  printf '%s\n' '[error] legacy ENGRAM_MEMORY_* toolkit variable detected; use NAOS_ENGRAM_MEMORY_* after explicit migration' >&2
  exit 64
fi
NAOS_ENGRAM_MEMORY_BIN_DIR="${NAOS_ENGRAM_MEMORY_BIN_DIR:-${HOME}/.local/bin}"
ACTION="inventory"
DRY_RUN=0
MAINTENANCE_WINDOW=0
YES=0
CLIENT=""
PROJECT=""
WORKSPACE=""
WRAPPER=""
EXPLICIT_PYTHON=""
PYTHON_CMD=()

info() { printf '[ok] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*" >&2; }
die() { printf '[error] %s\n' "$*" >&2; exit 2; }

usage() {
  cat <<'EOF'
Usage: scripts/setup.sh [action] [options]

Actions (one at a time):
  --inventory                   Read-only redacted inventory (default)
  --configure                   Install wrapper, registry, and local policy files
  --render-client               Print a project-specific client configuration; makes no changes
  --install-client              Install one strict-JSON workspace adapter with backup protection
  --install-supported|--upgrade Download the single manifest-approved release
  --rollback                    Restore the locally retained prior binary

Options:
  --project ID                  Required only by explicit project-specific adapters
  --client NAME                 codex | vscode-generic | antigravity | kilo | opencode-v1 | cursor | project-config
  --workspace PATH              Required by Codex rendering and fixed-project installation
  --wrapper PATH                Override the rendered or installed wrapper command
  --python PATH                 Prefer this explicit Python interpreter
  --maintenance-window          Required for --install-supported, --upgrade, and --rollback
  --yes                         Explicitly confirm the selected write action
  --dry-run                     Print the mutation plan without changing files
  -h, --help                    Show this help

No action contacts GitHub except --install-supported/--upgrade, which downloads
an exact manifest-pinned asset after owner approval. This script never runs
engram sync or imports Git chunks. IDE configuration changes only through the
explicit --install-client action.
EOF
}

select_python() {
  local candidate
  local -a candidates=()
  [[ -z "$EXPLICIT_PYTHON" ]] || candidates+=("$EXPLICIT_PYTHON")
  [[ -z "${NAOS_ENGRAM_MEMORY_PYTHON:-}" ]] || candidates+=("$NAOS_ENGRAM_MEMORY_PYTHON")
  command -v python3 >/dev/null 2>&1 && candidates+=("$(command -v python3)")
  command -v python >/dev/null 2>&1 && candidates+=("$(command -v python)")
  for candidate in "${candidates[@]}"; do
    [[ -x "$candidate" ]] || continue
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
      PYTHON_CMD=("$candidate")
      return 0
    fi
  done
  if command -v py >/dev/null 2>&1 && py -3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
    PYTHON_CMD=("$(command -v py)" -3)
    return 0
  fi
  die 'Python 3.10 or newer was not found. Exact remediation: install Python 3.10+, then rerun with --python /absolute/path.'
}

run() {
  if [[ "$DRY_RUN" == 1 ]]; then
    printf '[dry-run]'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

install_config() {
  # One hardened implementation owns managed runtime writes.  The source-tree
  # entry point delegates to it instead of maintaining a second copy/delete
  # path with weaker symlink and atomic-write guarantees.
  local runtime_args=(runtime install --home "$HOME" --yes --non-interactive)
  [[ "$DRY_RUN" == 1 ]] && runtime_args+=(--dry-run)
  exec "${PYTHON_CMD[@]}" "$TOOL" "${runtime_args[@]}"
}

provider_mutation() {
  local provider_action="$1" provider_args
  provider_args=(provider "$provider_action" --home "$HOME" --bin-dir "$NAOS_ENGRAM_MEMORY_BIN_DIR")
  [[ "$MAINTENANCE_WINDOW" == 1 ]] && provider_args+=(--maintenance-window)
  [[ "$YES" == 1 ]] && provider_args+=(--yes)
  [[ "$DRY_RUN" == 1 ]] && provider_args+=(--dry-run)
  exec "${PYTHON_CMD[@]}" "$TOOL" "${provider_args[@]}"
}

rollback() {
  local provider_args=(provider rollback --home "$HOME" --bin-dir "$NAOS_ENGRAM_MEMORY_BIN_DIR")
  [[ "$MAINTENANCE_WINDOW" == 1 ]] && provider_args+=(--maintenance-window)
  [[ "$YES" == 1 ]] && provider_args+=(--yes)
  [[ "$DRY_RUN" == 1 ]] && provider_args+=(--dry-run)
  exec "${PYTHON_CMD[@]}" "$TOOL" "${provider_args[@]}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inventory) ACTION="inventory" ;;
    --configure) ACTION="configure" ;;
    --render-client) ACTION="render-client" ;;
    --install-client) ACTION="install-client" ;;
    --install-supported) ACTION="install-supported" ;;
    --upgrade) ACTION="upgrade" ;;
    --rollback) ACTION="rollback" ;;
    --project) PROJECT="${2:-}"; shift ;;
    --client) CLIENT="${2:-}"; shift ;;
    --workspace) WORKSPACE="${2:-}"; shift ;;
    --wrapper) WRAPPER="${2:-}"; shift ;;
    --python) EXPLICIT_PYTHON="${2:-}"; shift ;;
    --maintenance-window) MAINTENANCE_WINDOW=1 ;;
    --yes) YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

select_python

ACTIVE_REGISTRY="$REGISTRY"
[[ -f "$USER_REGISTRY" ]] && ACTIVE_REGISTRY="$USER_REGISTRY"
case "$ACTION" in
  inventory) exec "${PYTHON_CMD[@]}" "$TOOL" --registry "$ACTIVE_REGISTRY" inventory --cwd "$PWD" ;;
  configure) install_config ;;
  render-client)
    [[ -n "$CLIENT" ]] || die "--render-client requires --client"
    render_args=(--registry "$ACTIVE_REGISTRY" render-client --client "$CLIENT")
    [[ -n "$PROJECT" ]] && render_args+=(--project "$PROJECT")
    [[ -n "$WORKSPACE" ]] && render_args+=(--workspace "$WORKSPACE")
    [[ -n "$WRAPPER" ]] && render_args+=(--wrapper "$WRAPPER")
    exec "${PYTHON_CMD[@]}" "$TOOL" "${render_args[@]}" ;;
  install-client)
    [[ -n "$CLIENT" ]] || die "--install-client requires --client"
    [[ -f "$USER_REGISTRY" ]] || die "managed user registry is missing; run --configure, then register the project explicitly"
    install_args=(--registry "$USER_REGISTRY" install-client --client "$CLIENT")
    [[ -n "$PROJECT" ]] || die "--install-client requires --project for $CLIENT"
    [[ -n "$WORKSPACE" ]] || die "--install-client requires --workspace for $CLIENT"
    install_args+=(--project "$PROJECT" --workspace "$WORKSPACE")
    [[ -n "$WRAPPER" ]] && install_args+=(--wrapper "$WRAPPER")
    [[ "$DRY_RUN" == 1 ]] && install_args+=(--dry-run)
    if [[ "$DRY_RUN" != 1 ]]; then
      [[ "$YES" == 1 ]] || die "--install-client requires --yes; the action name alone is not consent"
      install_args+=(--yes --non-interactive)
    fi
    exec "${PYTHON_CMD[@]}" "$TOOL" "${install_args[@]}" ;;
  install-supported) provider_mutation install ;;
  upgrade) provider_mutation upgrade ;;
  rollback) rollback ;;
esac
