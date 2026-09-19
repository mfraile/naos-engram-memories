#!/usr/bin/env python3
"""Select Python 3.10+ for a foreground toolkit install; never used by MCP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import site
import subprocess
import sys
import sysconfig
from pathlib import Path


PINNED_PACKAGE = "naos-engram-memories==1.0.1"


def candidates(explicit: str | None = None) -> list[dict[str, object]]:
    if os.environ.get("ENGRAM_MEMORY_PYTHON"):
        raise ValueError(
            "legacy ENGRAM_MEMORY_PYTHON detected; use NAOS_ENGRAM_MEMORY_PYTHON after explicit migration"
        )
    raw = [explicit, os.environ.get("NAOS_ENGRAM_MEMORY_PYTHON"), "python3", "python"]
    if os.name == "nt":
        raw.append("py")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for candidate in raw:
        if not candidate:
            continue
        resolved = candidate if os.path.isabs(candidate) else shutil.which(candidate)
        if not resolved:
            continue
        resolved = str(Path(resolved).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        command = [resolved]
        if Path(resolved).name.casefold() in {"py", "py.exe"}:
            command.append("-3")
        probe = subprocess.run(
            command + ["-c", "import json,sys; print(json.dumps(list(sys.version_info[:3])))"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            version = json.loads(probe.stdout)
        except json.JSONDecodeError:
            version = []
        result.append({"path": resolved, "launcher_args": command[1:], "version": version, "supported": probe.returncode == 0 and tuple(version) >= (3, 10, 0)})
    return result


def python_install_offer() -> dict[str, object]:
    """Return one explicit, foreground-only package-manager offer.

    The command is never inferred from an MCP stdio process.  It is shown first
    and runs only when an operator invokes this bootstrap with both
    ``--install-python`` and ``--yes``.
    """
    if sys.platform == "darwin" and shutil.which("brew"):
        return {"available": True, "manager": "brew", "command": ["brew", "install", "python@3.12"]}
    if shutil.which("apt-get"):
        return {"available": True, "manager": "apt-get", "command": ["apt-get", "install", "python3"]}
    if shutil.which("dnf"):
        return {"available": True, "manager": "dnf", "command": ["dnf", "install", "python3"]}
    if shutil.which("winget"):
        return {"available": True, "manager": "winget", "command": ["winget", "install", "Python.Python.3.12"]}
    if shutil.which("choco"):
        return {"available": True, "manager": "choco", "command": ["choco", "install", "python", "-y"]}
    return {"available": False, "manager": None, "command": None}


def pipx_install_offer(selected: dict[str, object]) -> dict[str, object]:
    """Return one explicit pipx installation offer for foreground use."""
    if sys.platform == "darwin" and shutil.which("brew"):
        return {"available": True, "manager": "brew", "command": ["brew", "install", "pipx"]}
    if shutil.which("apt-get"):
        return {"available": True, "manager": "apt-get", "command": ["apt-get", "install", "pipx"]}
    if shutil.which("dnf"):
        return {"available": True, "manager": "dnf", "command": ["dnf", "install", "pipx"]}
    if os.name == "nt" and shutil.which("scoop"):
        return {"available": True, "manager": "scoop", "command": ["scoop", "install", "pipx"]}
    if os.name == "nt":
        return {
            "available": True,
            "manager": "python-user-install",
            "command": [
                str(selected["path"]),
                *[str(value) for value in selected["launcher_args"]],
                "-m",
                "pip",
                "install",
                "--user",
                "pipx",
            ],
        }
    return {"available": False, "manager": None, "command": None}


def select_python_candidate(
    supported: list[dict[str, object]],
    *,
    explicit: str | None,
    non_interactive: bool,
) -> dict[str, object]:
    """Select one displayed interpreter; never silently choose among several."""
    if explicit:
        resolved = explicit if os.path.isabs(explicit) else shutil.which(explicit)
        resolved = str(Path(resolved).resolve()) if resolved else None
        selected = next((item for item in supported if item["path"] == resolved), None)
        if not selected:
            raise ValueError("the explicit --python candidate is unavailable or older than Python 3.10")
        return selected
    if len(supported) == 1:
        return supported[0]
    if non_interactive or not sys.stdin.isatty():
        raise ValueError("multiple supported Python interpreters were detected; rerun with --python /absolute/path")
    print(json.dumps({"schema_version": 1, "supported_python_candidates": supported}, indent=2, sort_keys=True), file=sys.stderr)
    answer = input(f"Select Python candidate [1-{len(supported)}]: ").strip()
    try:
        selection = int(answer)
        if not 1 <= selection <= len(supported):
            raise ValueError
        selected = supported[selection - 1]
    except (ValueError, IndexError):
        raise ValueError("a valid Python candidate selection is required") from None
    return selected


def share_dirs() -> tuple[Path, ...]:
    """Return candidate packaged resource roots for every supported install scheme.

    This deliberately mirrors ``resource_data_roots`` in ``tools/engram_memory.py``.
    Bootstrap must stay importable before the package is installed, so it cannot
    import that module and keeps its own copy of the same ordered contract:
    packaged ``data_files`` only land under ``sys.prefix`` for a virtual
    environment or pipx, and a ``--user``, ``--target``, or ``--prefix`` install
    puts them elsewhere.
    """
    roots: list[Path] = []

    def remember(value: str | os.PathLike[str] | None) -> None:
        if not value:
            return
        path = Path(value)
        if path not in roots:
            roots.append(path)

    remember(sys.prefix)
    remember(sysconfig.get_path("data"))
    try:
        remember(site.getuserbase())
    except (AttributeError, OSError):  # pragma: no cover - defensive
        pass
    remember(sys.base_prefix)
    script_dir = Path(__file__).resolve().parent
    remember(script_dir.parent)
    return tuple(root / "share" / "naos-engram-memory" for root in roots)


def verify_asset(name: str) -> int:
    script_dir = Path(__file__).resolve().parent
    shares = share_dirs()
    manifest_candidates = (
        script_dir.parent / "config" / "bootstrap-assets.v1.json",
        *(share / "config" / "bootstrap-assets.v1.json" for share in shares),
    )
    manifest = next((path for path in manifest_candidates if path.exists()), manifest_candidates[0])
    try:
        expected = json.loads(manifest.read_text(encoding="utf-8"))["assets"][name]
    except (OSError, KeyError, json.JSONDecodeError):
        print("Bootstrap integrity manifest is unavailable or invalid.", file=sys.stderr)
        return 2
    target_candidates = (script_dir / name, *(share / "scripts" / name for share in shares))
    target = next((path for path in target_candidates if path.is_file()), target_candidates[0])
    if not target.is_file():
        print(f"Bootstrap asset is missing: {name}", file=sys.stderr)
        return 2
    actual = hashlib.sha256(target.read_bytes()).hexdigest()
    payload = {"asset": name, "algorithm": "sha256", "verified": actual == expected}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if actual == expected else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", help="explicit interpreter or launcher")
    parser.add_argument("--list", action="store_true", help="print detected candidates without installing")
    parser.add_argument("--yes", action="store_true", help="confirm package installation with the selected interpreter")
    parser.add_argument("--non-interactive", action="store_true")
    parser.add_argument(
        "--package",
        default=PINNED_PACKAGE,
        choices=[PINNED_PACKAGE],
        help="exact supported toolkit distribution (unpinned/latest values are refused)",
    )
    parser.add_argument("--pip-fallback", action="store_true", help="use interpreter pip instead of preferred pipx")
    parser.add_argument("--install-python", action="store_true", help="run one displayed OS package-manager Python install after explicit confirmation")
    parser.add_argument("--install-pipx", action="store_true", help="run one displayed official pipx installation command after explicit confirmation")
    parser.add_argument("--verify-asset", choices=["bootstrap.py", "bootstrap.sh", "bootstrap.ps1", "engram_mcp_wrapper.sh", "engram_mcp_wrapper.ps1"])
    args = parser.parse_args(argv)
    if args.verify_asset:
        return verify_asset(args.verify_asset)
    try:
        found = candidates(args.python)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    supported = [item for item in found if item["supported"]]
    offer = python_install_offer()
    if args.list:
        print(json.dumps({"schema_version": 1, "candidates": found, "network_accessed": False, "python_install_offer": offer}, indent=2, sort_keys=True))
        return 0 if supported else 2
    if not supported:
        if not args.install_python:
            print(json.dumps({"schema_version": 1, "candidates": found, "network_accessed": False, "python_install_offer": offer}, indent=2, sort_keys=True), file=sys.stderr)
            print("No Python 3.10+ interpreter found. Review the offered command, then rerun with --install-python --yes or install Python 3.10+ manually.", file=sys.stderr)
            return 2
        if not offer["available"]:
            print("No supported package manager was detected. Install Python 3.10+ manually, then rerun with --python /absolute/path.", file=sys.stderr)
            return 2
        if args.non_interactive:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "python_install_offer": offer,
                        "network_accessed": False,
                        "execution_allowed": False,
                        "remediation_command": offer["command"],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            print("Non-interactive bootstrap never runs a system package manager; execute the displayed command through your approved administration process.", file=sys.stderr)
            return 2
        if not args.yes:
            print(json.dumps({"schema_version": 1, "python_install_offer": offer, "network_accessed": "planned_after_confirmation"}, indent=2, sort_keys=True), file=sys.stderr)
            print("Package-manager installation requires --yes after reviewing the displayed command.", file=sys.stderr)
            return 2
        result = subprocess.run([str(value) for value in offer["command"]], check=False)
        print(json.dumps({"schema_version": 1, "python_install_offer": offer, "network_accessed": None, "network_access_status": "not_observed_by_bootstrap", "external_command_invoked": True, "command_may_access_network": True, "exit_code": result.returncode}, indent=2, sort_keys=True))
        return result.returncode
    if args.install_python:
        print("A supported Python 3.10+ interpreter is already available; refusing an unnecessary package-manager installation.", file=sys.stderr)
        return 2
    try:
        selected = select_python_candidate(
            supported,
            explicit=args.python,
            non_interactive=args.non_interactive,
        )
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "supported_python_candidates": supported,
                    "network_accessed": False,
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        print(str(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "schema_version": 1,
                "supported_python_candidates": supported,
                "selected_python": selected,
                "network_accessed": False,
            },
            indent=2,
            sort_keys=True,
        ),
        file=sys.stderr,
    )
    pipx = shutil.which("pipx")
    pipx_offer = pipx_install_offer(selected)
    if not pipx and not args.pip_fallback:
        if not args.install_pipx:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pipx_install_offer": pipx_offer,
                        "network_accessed": False,
                        "execution_allowed": False,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            print("pipx is required for the preferred isolated install. Review the offer, then rerun with --install-pipx --yes, install pipx through an approved process, or explicitly use --pip-fallback.", file=sys.stderr)
            return 2
        if not pipx_offer["available"]:
            print("No supported pipx installation method was detected. Install pipx manually from the official pipx instructions, then rerun this command.", file=sys.stderr)
            return 2
        if args.non_interactive:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pipx_install_offer": pipx_offer,
                        "network_accessed": False,
                        "execution_allowed": False,
                        "remediation_command": pipx_offer["command"],
                    },
                    indent=2,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            print("Non-interactive bootstrap never installs pipx; execute the displayed command through your approved administration process.", file=sys.stderr)
            return 2
        if not args.yes:
            print(json.dumps({"schema_version": 1, "pipx_install_offer": pipx_offer, "network_accessed": "planned_after_confirmation"}, indent=2, sort_keys=True), file=sys.stderr)
            print("pipx installation requires --yes after reviewing the displayed command.", file=sys.stderr)
            return 2
        result = subprocess.run([str(value) for value in pipx_offer["command"]], check=False)
        pipx = shutil.which("pipx")
        print(json.dumps({"schema_version": 1, "pipx_install_offer": pipx_offer, "network_accessed": None, "network_access_status": "not_observed_by_bootstrap", "external_command_invoked": True, "command_may_access_network": True, "exit_code": result.returncode, "pipx_detected_after_install": bool(pipx)}, indent=2, sort_keys=True))
        if result.returncode != 0:
            return result.returncode
        if not pipx:
            print("pipx installation completed but its executable is not on PATH. Run `pipx ensurepath`, restart the terminal, and rerun bootstrap.", file=sys.stderr)
            return 2
        print("pipx is now available. Rerun bootstrap to install the pinned toolkit package.", file=sys.stderr)
        return 0
    elif args.install_pipx:
        print("pipx is already available; refusing an unnecessary package-manager installation.", file=sys.stderr)
        return 2
    if not args.yes:
        if args.non_interactive or not sys.stdin.isatty():
            print(f"Package installation requires consent. Rerun with --python {selected['path']} --yes.", file=sys.stderr)
            return 2
        if input(f"Install {args.package} using {selected['path']}? [y/N] ").strip().casefold() not in {"y", "yes"}:
            print("Installation declined; no changes made.", file=sys.stderr)
            return 2
    if pipx and not args.pip_fallback:
        command = [str(Path(pipx).resolve()), "install", args.package]
    elif args.pip_fallback:
        command = [str(selected["path"]), *[str(value) for value in selected["launcher_args"]], "-m", "pip", "install", args.package]
    else:
        print("pipx is required for the preferred public install. Install pipx with an approved OS package manager, or explicitly pass --pip-fallback in a reviewed source environment.", file=sys.stderr)
        return 2
    result = subprocess.run(command, check=False)
    print(json.dumps({"schema_version": 1, "network_accessed": None, "network_access_status": "not_observed_by_bootstrap", "external_command_invoked": True, "command_may_access_network": True, "exit_code": result.returncode}, indent=2, sort_keys=True))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
