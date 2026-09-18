#!/usr/bin/env sh
# Foreground installer helper only; MCP wrappers never invoke it.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
EXPLICIT=""
PREVIOUS=""
for ARG in "$@"; do
  if [ "$PREVIOUS" = "--python" ]; then EXPLICIT=$ARG; break; fi
  case "$ARG" in --python=*) EXPLICIT=${ARG#--python=} ; break ;; esac
  PREVIOUS=$ARG
done
if [ -n "${ENGRAM_MEMORY_PYTHON:-}" ]; then
  printf '%s\n' 'Legacy ENGRAM_MEMORY_PYTHON detected; use NAOS_ENGRAM_MEMORY_PYTHON after explicit migration.' >&2
  exit 64
fi

try_python() {
  candidate=$1
  shift
  [ -n "$candidate" ] && [ -x "$candidate" ] || return 1
  "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1 || return 1
  exec "$candidate" "$SCRIPT_DIR/bootstrap.py" "$@"
}

[ -z "$EXPLICIT" ] || try_python "$EXPLICIT" "$@" || true
[ -z "${NAOS_ENGRAM_MEMORY_PYTHON:-}" ] || try_python "$NAOS_ENGRAM_MEMORY_PYTHON" "$@" || true
if command -v python3 >/dev/null 2>&1; then try_python "$(command -v python3)" "$@" || true; fi
if command -v python >/dev/null 2>&1; then try_python "$(command -v python)" "$@" || true; fi

HAS_INSTALL=0
HAS_YES=0
HAS_NONINTERACTIVE=0
for ARG in "$@"; do
  [ "$ARG" = "--install-python" ] && HAS_INSTALL=1
  [ "$ARG" = "--yes" ] && HAS_YES=1
  [ "$ARG" = "--non-interactive" ] && HAS_NONINTERACTIVE=1
done
if command -v brew >/dev/null 2>&1; then
  INSTALL_COMMAND='brew install python@3.12'
elif command -v apt-get >/dev/null 2>&1; then
  INSTALL_COMMAND='apt-get install python3'
elif command -v dnf >/dev/null 2>&1; then
  INSTALL_COMMAND='dnf install python3'
else
  INSTALL_COMMAND=''
fi
if [ "$HAS_NONINTERACTIVE" = 0 ] && [ "$HAS_INSTALL" = 1 ] && [ "$HAS_YES" = 1 ] && [ -n "$INSTALL_COMMAND" ]; then
  # This foreground script is deliberately the only no-Python path that can
  # invoke a package manager. It is never an MCP stdio entrypoint.
  if command -v brew >/dev/null 2>&1; then exec brew install python@3.12; fi
  if command -v apt-get >/dev/null 2>&1; then exec apt-get install python3; fi
  exec dnf install python3
fi
if [ -n "$INSTALL_COMMAND" ]; then
  printf '%s\n' "Python 3.10 or newer was not found. Offered command: $INSTALL_COMMAND" >&2
  if [ "$HAS_NONINTERACTIVE" = 1 ]; then
    printf '%s\n' 'Non-interactive bootstrap never runs a package manager; execute the offered command through your approved administration process.' >&2
    exit 2
  fi
  printf '%s\n' 'Review it, then rerun scripts/bootstrap.sh --install-python --yes. Or install Python 3.10+ manually and rerun with --python /absolute/path --list.' >&2
else
  printf '%s\n' 'Python 3.10+ and a supported package manager were not found. Install Python 3.10+ manually and rerun scripts/bootstrap.sh --python /absolute/path --list.' >&2
fi
exit 2
