#!/usr/bin/env python3
"""Deterministic, local administration for the Engram memory toolkit.

This program validates project identity, renders client snippets, installs
only explicit workspace-scoped strict-JSON adapters, reports redacted local
inventory, and reads the support manifest. It does not read memory
observations. The pure onboarding assessment does not execute Engram; explicit
diagnostic probes disclose that Engram may perform its own network check.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Callable


if __package__:
    # Installed-package imports remain package-relative and cannot collide with
    # an unrelated top-level ``tools`` package.
    from . import provider as provider_lifecycle
else:
    # Direct source and managed-runtime execution bind the reviewed sibling
    # file explicitly. The managed copy must never fall back to site-packages.
    _provider_path = Path(__file__).with_name("provider.py")
    _provider_spec = importlib.util.spec_from_file_location("naos_engram_managed_provider", _provider_path)
    if _provider_spec is None or _provider_spec.loader is None:
        raise ImportError(f"cannot load managed provider lifecycle module: {_provider_path}")
    provider_lifecycle = importlib.util.module_from_spec(_provider_spec)
    _provider_spec.loader.exec_module(provider_lifecycle)


class EngramMemoryError(ValueError):
    """An input or policy condition that must stop the caller safely."""


TOOLKIT_NAMESPACE = "naos-engram-memory"
TOOLKIT_VERSION = "1.0.1"
LEGACY_NAMESPACE = "engram-memory"
ROOT = Path(__file__).resolve().parents[1]
INSTALLED_DIR = Path(__file__).resolve().parent


def resource_data_roots() -> tuple[Path, ...]:
    """Return every install-scheme data root that may hold packaged resources.

    Packaged ``data_files`` land under the *active install scheme's* data root.
    That is ``sys.prefix`` only for a virtual environment or pipx; a ``--user``,
    ``--target``, or ``--prefix`` install places them somewhere else entirely.
    Assuming ``sys.prefix`` made every registry-backed command fail closed on
    those layouts, so each supported scheme is probed in a deterministic order.
    """
    candidates: list[Path] = []

    def remember(value: str | os.PathLike[str] | None) -> None:
        if not value:
            return
        path = Path(value)
        if path not in candidates:
            candidates.append(path)

    remember(sys.prefix)  # virtual environment, pipx
    remember(sysconfig.get_path("data"))  # distribution schemes such as /usr/local
    try:
        remember(site.getuserbase())  # pip install --user
    except (AttributeError, OSError):  # pragma: no cover - defensive
        pass
    remember(sys.base_prefix)
    remember(ROOT)  # pip install --target
    # A scheme root that contains site-packages as <root>/lib/pythonX.Y/site-packages.
    for ancestor in list(INSTALLED_DIR.parents)[:4]:
        remember(ancestor)
    return tuple(candidates)


def resolved_share_dir() -> Path:
    """Return the packaged resource root, preferring one that actually exists."""
    for root in resource_data_roots():
        candidate = root / "share" / TOOLKIT_NAMESPACE
        if (candidate / "config" / "projects.json").is_file():
            return candidate
    return Path(sys.prefix) / "share" / TOOLKIT_NAMESPACE


SHARE_DIR = resolved_share_dir()
RESOURCE_CONFIG_DIR = (
    INSTALLED_DIR
    if (INSTALLED_DIR / "projects.json").exists()
    else SHARE_DIR / "config"
    if (SHARE_DIR / "config" / "projects.json").exists()
    else ROOT / "config"
)
TEMPLATE_DIR = (
    INSTALLED_DIR / "clients"
    if (INSTALLED_DIR / "clients").exists()
    else SHARE_DIR / "clients"
    if (SHARE_DIR / "clients").exists()
    else ROOT / "clients"
)
RESOURCE_SCRIPT_DIR = (
    # The wheel also ships the scripts beside the package in purelib, so accept
    # that sibling directory the way the config and template roots already do.
    INSTALLED_DIR.parent / "scripts"
    if (INSTALLED_DIR.parent / "scripts" / "engram_mcp_wrapper.sh").is_file()
    else SHARE_DIR / "scripts"
    if (SHARE_DIR / "scripts").exists()
    else ROOT / "scripts"
)


def user_config_dir(home: Path | None = None) -> Path:
    """Return durable user-owned state; never store it inside a pipx venv."""
    default_home = home is None or home.expanduser() == Path.home().expanduser()
    resolved_home = Path.home() if home is None else home.expanduser()
    configured = os.environ.get("NAOS_ENGRAM_MEMORY_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") if default_home else None
        return Path(base or resolved_home / "AppData" / "Roaming") / TOOLKIT_NAMESPACE
    return Path(os.environ.get("XDG_CONFIG_HOME", str(resolved_home / ".config"))) / TOOLKIT_NAMESPACE


def legacy_user_config_dir(home: Path | None = None) -> Path:
    """Return the former namespace for detection only; never read or merge it."""
    default_home = home is None or home.expanduser() == Path.home().expanduser()
    resolved_home = Path.home() if home is None else home.expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") if default_home else None
        return Path(base or resolved_home / "AppData" / "Roaming") / LEGACY_NAMESPACE
    return Path(os.environ.get("XDG_CONFIG_HOME", str(resolved_home / ".config"))) / LEGACY_NAMESPACE


def legacy_runtime_state(home: Path | None = None, config_dir: Path | None = None) -> str:
    current = (config_dir or user_config_dir(home)).expanduser()
    legacy = legacy_user_config_dir(home).expanduser()
    if legacy == current or not legacy.exists():
        return "not_detected"
    return "separate_legacy_detected" if current.exists() else "migration_required"


def reject_legacy_environment() -> None:
    names = sorted(
        name
        for name in os.environ
        if name in {
            "ENGRAM_MEMORY_CONFIG_DIR",
            "ENGRAM_MEMORY_BIN_DIR",
            "ENGRAM_MEMORY_TOOL",
            "ENGRAM_MEMORY_REGISTRY",
            "ENGRAM_MEMORY_LOG",
            "ENGRAM_MEMORY_PYTHON",
        }
    )
    if names:
        raise EngramMemoryError(
            "legacy toolkit environment variable(s) detected: "
            + ", ".join(names)
            + "; use the NAOS_ENGRAM_MEMORY_* namespace after an explicit reviewed migration"
        )


def user_bin_dir(home: Path | None = None) -> Path:
    default_home = home is None or home.expanduser() == Path.home().expanduser()
    resolved_home = Path.home() if home is None else home.expanduser()
    configured = os.environ.get("NAOS_ENGRAM_MEMORY_BIN_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        # A general-purpose "~\bin" is a common ad hoc PATH location for other
        # personal tools; namespace the managed wrapper under the toolkit's own
        # config tree instead of colliding with an unrelated existing binary.
        base = os.environ.get("APPDATA") if default_home else None
        return Path(base or resolved_home / "AppData" / "Roaming") / "naos-engram-memory" / "bin"
    return resolved_home / ".local/bin"


def user_wrapper_path(home: Path | None = None) -> Path:
    return user_bin_dir(home) / ("engram-mcp-wrapper.cmd" if os.name == "nt" else "engram-mcp-wrapper")


DEFAULT_REGISTRY = user_config_dir() / "projects.json"
DEFAULT_SUPPORT = user_config_dir() / "release-support.json"
DEFAULT_HOST_CATALOGUE = user_config_dir() / "mcp-hosts.v1.json"
INSTRUCTION_TEMPLATE = (
    INSTALLED_DIR / "instructions" / "engram-project-lifecycle.md"
    if (INSTALLED_DIR / "instructions").exists()
    else SHARE_DIR / "instructions" / "engram-project-lifecycle.md"
    if (SHARE_DIR / "instructions" / "engram-project-lifecycle.md").exists()
    else ROOT / "instructions" / "engram-project-lifecycle.md"
)
PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{1,127}$")
MCP_REFERENCE = re.compile(r"(?:engram|mem_current_project|mem_context|engram-mcp-wrapper)", re.IGNORECASE)

# Each installation target is intentionally workspace-scoped. Global client
# configuration could launch a fixed canonical project from the wrong checkout.
# The tuple is (workspace-relative destination, JSON path to the server entry).
WORKSPACE_INSTALL_TARGETS: dict[str, tuple[Path, tuple[str, ...]]] = {
    "vscode-generic": (Path(".vscode") / "mcp.json", ("servers", "engram")),
    "antigravity": (Path(".agents") / "mcp_config.json", ("mcpServers", "engram")),
    "kilo": (Path(".kilo") / "kilo.jsonc", ("mcp", "engram")),
    "opencode": (Path("opencode.json"), ("mcp", "engram")),
    "opencode-v1": (Path("opencode.json"), ("mcp", "engram")),
    "cursor": (Path(".cursor") / "mcp.json", ("mcpServers", "engram")),
    "project-config": (Path(".engram") / "config.json", ()),
}

OPENCODE_EXECUTABLES = {
    "opencode-v1": "opencode",
}

INSTRUCTION_TARGETS: dict[str, Path] = {
    "codex": Path("AGENTS.md"),
    "claude-code": Path("CLAUDE.md"),
    "opencode-v1": Path("AGENTS.md"),
    "cursor": Path("AGENTS.md"),
    "vscode-generic": Path("AGENTS.md"),
    "antigravity": Path("AGENTS.md"),
    "kilo": Path("AGENTS.md"),
}
INSTRUCTION_BEGIN = "<!-- NAOS-ENGRAM-MEMORY:BEGIN -->"
INSTRUCTION_END = "<!-- NAOS-ENGRAM-MEMORY:END -->"
INSTRUCTION_CONFLICT = re.compile(
    r"(?im)^\s*(?:always\s+save|save\s+every|automatically\s+sync|auto[- ]sync|.*sync\s+--all)\b"
)

# Muse Code starts its stdio servers from the explicitly selected workspace, as
# validated in an isolated macOS test. Its user settings can therefore use the
# wrapper's remote-attested dynamic resolution without pinning one project.
USER_INSTALL_TARGETS: dict[str, tuple[Path, tuple[str, ...]]] = {}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EngramMemoryError(f"required file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise EngramMemoryError(f"invalid JSON in {path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise EngramMemoryError(f"{path} must contain a JSON object")
    return value


def bundled_config_path(name: str) -> Path:
    path = RESOURCE_CONFIG_DIR / name
    if not path.exists():
        # Name the roots that were probed. The usual cause is an install whose
        # data scheme differs from every searched root, so the operator needs to
        # see where the toolkit looked rather than only which file was absent.
        searched = ", ".join(str(root / "share" / TOOLKIT_NAMESPACE) for root in resource_data_roots())
        raise EngramMemoryError(
            f"bundled toolkit resource is missing: {name}; "
            f"resolved resource directory {RESOURCE_CONFIG_DIR} does not contain it. "
            f"Searched packaged roots: {searched}. "
            "Reinstall the exact toolkit package with 'pipx install naos-engram-memories' "
            "or 'python3 -m pip install --user naos-engram-memories'"
        )
    return path


def read_config(path: Path, *, default_name: str | None = None) -> dict[str, Any]:
    """Read user state when present, otherwise the immutable bundled seed.

    Read-only commands must remain usable immediately after ``pipx install``.
    A mutating command copies the seed into the durable user config directory
    before it writes anything.
    """
    # Configuration paths are operator/user controlled.  Reject the complete
    # lexical chain before probing existence so a symlink cannot redirect the
    # read to an unrelated registry or catalogue (including through a deeper
    # ancestor several levels above the endpoint).
    reject_symlink_components(path, label="configuration path")
    if path.exists():
        return load_json(path)
    if default_name:
        return load_json(bundled_config_path(default_name))
    return load_json(path)


# The symlink refusal is deliberate and stays unchanged, but two of its labels
# describe environment-derived paths the operator can actually correct. Name the
# remedy there so a symlinked home or configuration root is recoverable instead
# of being an unexplained dead end.
SYMLINK_REMEDIATION = {
    "user home directory": (
        '; rerun with HOME set to its resolved physical path, for example '
        'HOME="$(cd "$HOME" && pwd -P)"'
    ),
    # Verified distinction: NAOS_ENGRAM_MEMORY_CONFIG_DIR recovers a symlinked
    # configuration directory under a real home, but it cannot recover a
    # symlinked home, because the home check runs independently of it.
    "configuration path": (
        "; if only the configuration directory is a symbolic link, set "
        "NAOS_ENGRAM_MEMORY_CONFIG_DIR to a path whose every component is real; if the home "
        'directory itself is a symbolic link, rerun with HOME set to its resolved physical '
        'path, for example HOME="$(cd "$HOME" && pwd -P)"'
    ),
}


def reject_symlink(path: Path, *, label: str) -> None:
    """Fail closed for existing or broken symbolic links before any read/write."""
    if path.is_symlink():
        raise EngramMemoryError(
            f"{label} must not be a symbolic link{SYMLINK_REMEDIATION.get(label, '')}"
        )


def reject_symlinks_beneath(base: Path, target: Path, *, label: str) -> None:
    """Reject every target component controlled below a trusted lexical base."""
    base = Path(os.path.abspath(base))
    target = Path(os.path.abspath(target))
    try:
        relative = target.relative_to(base)
    except ValueError as exc:
        raise EngramMemoryError(f"{label} escapes its authorized directory") from exc
    current = base
    reject_symlink(current, label=label)
    for part in relative.parts:
        current /= part
        reject_symlink(current, label=label)


def reject_symlink_components(path: Path, *, label: str) -> None:
    """Reject symlinks in every lexical component from the filesystem anchor."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        # Darwin defines /var as a root-owned compatibility alias for
        # /private/var.  It is outside every caller-controlled directory and
        # is required for ordinary tempfile paths.  No environment-derived or
        # nested symlink receives this exception.
        if (
            sys.platform == "darwin"
            and current == Path("/var")
            and current.is_symlink()
            and current.resolve() == Path("/private/var")
        ):
            continue
        reject_symlink(current, label=label)


def reject_runtime_path(home: Path, target: Path, *, label: str) -> None:
    """Reject symlink traversal for default user paths and explicit endpoints."""
    lexical_home = Path(os.path.abspath(home))
    lexical_target = Path(os.path.abspath(target))
    try:
        lexical_target.relative_to(lexical_home)
    except ValueError:
        reject_symlink_components(lexical_target, label=label)
    else:
        reject_symlinks_beneath(lexical_home, lexical_target, label=label)


def atomic_write_text(target: Path, text: str, *, encoding: str) -> None:
    """Write through an exclusively allocated same-directory file, never a fixed path."""
    reject_symlink(target, label="managed write target")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.engram-memory-",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def safe_copy_file(source: Path, target: Path) -> None:
    reject_symlink(target, label="managed runtime target")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.engram-memory-",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        shutil.copystat(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_path(path: Path) -> str:
    """Hash one regular file or directory tree without following links."""
    reject_symlink(path, label="managed runtime asset")
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    if not path.is_dir():
        raise EngramMemoryError("managed runtime asset is missing or not a regular file/directory")
    for candidate in sorted(path.rglob("*")):
        reject_symlink(candidate, label="managed runtime asset")
        if candidate.is_dir():
            continue
        relative = candidate.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(bytes.fromhex(sha256_path(candidate)))
    return digest.hexdigest()


def create_unique_directory_backup(target: Path) -> Path:
    """Atomically move a managed directory to a collision-safe backup name."""
    reject_symlink(target, label="backup source")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for counter in range(1000):
        suffix = f"-{counter}" if counter else ""
        candidate = target.with_name(f"{target.name}.engram-backup-{timestamp}{suffix}")
        try:
            target.rename(candidate)
        except FileExistsError:
            continue
        return candidate
    raise EngramMemoryError("could not allocate a unique directory backup; manual review required")


def replace_directory(source: Path, target: Path) -> bool:
    """Stage a directory beside its target and atomically publish the staged tree."""
    reject_symlink(target, label="managed runtime target")
    temporary = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{target.name}.engram-memory-"))
    try:
        shutil.copytree(source, temporary, dirs_exist_ok=True)
        if target.exists():
            create_unique_directory_backup(target)
        os.replace(temporary, target)
        return True
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def rendered_wrapper_assets(
    config_dir: Path, bin_dir: Path, *, provider_path_override: Path | None = None
) -> dict[str, tuple[Path, str]]:
    wrapper_name = "engram-mcp-wrapper.ps1" if os.name == "nt" else "engram-mcp-wrapper"
    wrapper_source = RESOURCE_SCRIPT_DIR / ("engram_mcp_wrapper.ps1" if os.name == "nt" else "engram_mcp_wrapper.sh")
    if not wrapper_source.exists():
        raise EngramMemoryError("bundled MCP wrapper asset is missing; reinstall the exact toolkit package")
    # Bind the executable itself, not a launcher symlink that the installed
    # wrapper must correctly reject as an unsafe managed path.
    python_text = os.path.realpath(sys.executable)
    config_text = os.path.abspath(os.fspath(config_dir))
    provider_name = "engram.exe" if os.name == "nt" else "engram"
    default_provider = Path(os.path.abspath(os.path.join(os.fspath(bin_dir), provider_name)))
    if provider_path_override is not None:
        provider_text = os.path.realpath(os.fspath(provider_path_override.expanduser()))
    else:
        try:
            provider_text = os.fspath(provider_lifecycle.configured_provider_path(config_dir, default_provider))
        except provider_lifecycle.ProviderError as exc:
            raise EngramMemoryError(str(exc)) from exc
    if any(ord(character) < 32 or ord(character) == 127 for value in (python_text, config_text, provider_text) for character in value):
        raise EngramMemoryError("managed wrapper paths must not contain control characters")
    content = wrapper_source.read_text(encoding="utf-8")
    if os.name == "nt":
        ps_literal = lambda value: "'" + value.replace("'", "''") + "'"
        content = content.replace("__PYTHON_EXECUTABLE__", ps_literal(python_text))
        content = content.replace("__NAOS_ENGRAM_MEMORY_CONFIG_DIR__", ps_literal(config_text))
        content = content.replace("__NAOS_ENGRAM_PROVIDER_BIN__", ps_literal(provider_text))
    else:
        content = content.replace("__PYTHON_EXECUTABLE__", shlex.quote(python_text))
        content = content.replace("__NAOS_ENGRAM_MEMORY_CONFIG_DIR__", shlex.quote(config_text))
        content = content.replace("__NAOS_ENGRAM_PROVIDER_BIN__", shlex.quote(provider_text))
    # Preserve the caller's concrete path flavour. Tests exercise Windows
    # rendering on Unix without allowing a mocked os.name to create WindowsPath.
    path_type = type(config_dir)
    target_path = path_type(os.path.join(os.fspath(bin_dir), wrapper_name))
    assets = {wrapper_name: (target_path, content)}
    if os.name == "nt":
        assets["engram-mcp-wrapper.cmd"] = (
            path_type(os.path.join(os.fspath(bin_dir), "engram-mcp-wrapper.cmd")),
            '@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0engram-mcp-wrapper.ps1" %*\r\n',
        )
    return assets


def expected_runtime_manifest(
    config_dir: Path, bin_dir: Path, *, provider_path_override: Path | None = None
) -> tuple[dict[str, Any], dict[str, tuple[str, Path | str, Path]]]:
    """Return content-addressed managed assets; the project registry is user state."""
    assets: dict[str, tuple[str, Path | str, Path]] = {
        "release-support.json": ("file", bundled_config_path("release-support.json"), config_dir / "release-support.json"),
        "mcp-hosts.v1.json": ("file", bundled_config_path("mcp-hosts.v1.json"), config_dir / "mcp-hosts.v1.json"),
        "engram_memory.py": ("file", Path(__file__), config_dir / "engram_memory.py"),
        "provider.py": ("file", Path(provider_lifecycle.__file__), config_dir / "provider.py"),
        "clients": ("directory", TEMPLATE_DIR, config_dir / "clients"),
        "instructions": ("directory", INSTRUCTION_TEMPLATE.parent, config_dir / "instructions"),
    }
    expected_hashes: dict[str, str] = {}
    for name, (kind, source, _target) in assets.items():
        assert isinstance(source, Path)
        expected_hashes[name] = sha256_path(source)
    for name, (target, content) in rendered_wrapper_assets(
        config_dir, bin_dir, provider_path_override=provider_path_override
    ).items():
        assets[name] = ("text", content, target)
        expected_hashes[name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    manifest = {
        "schema_version": 1,
        "toolkit_version": TOOLKIT_VERSION,
        "assets": expected_hashes,
    }
    return manifest, assets


def ensure_user_runtime(
    *, home: Path, config_dir: Path | None = None, bin_dir: Path | None = None,
    dry_run: bool = False, repair: bool = False,
    provider_path_override: Path | None = None,
) -> dict[str, Any]:
    """Install only toolkit policy/runtime files into durable user locations.

    This does not install Engram, edit an MCP host, read memory, or contact the
    network.  The wrapper records this interpreter's absolute path so GUI hosts
    do not depend on shell PATH discovery.
    """
    home = Path(os.path.abspath(home.expanduser()))
    reject_symlink_components(home, label="user home directory")
    config_dir = (config_dir or user_config_dir(home)).expanduser()
    bin_dir = (bin_dir or user_bin_dir(home)).expanduser()
    reject_runtime_path(home, config_dir, label="user configuration directory")
    reject_runtime_path(home, bin_dir, label="user binary directory")
    reject_legacy_environment()
    legacy_state = legacy_runtime_state(home, config_dir)
    if legacy_state == "migration_required":
        raise EngramMemoryError(
            f"legacy toolkit configuration detected at {legacy_user_config_dir(home)}; "
            "it was not read or merged—move or archive it only after explicit review"
        )
    registry_target = config_dir / "projects.json"
    manifest_target = config_dir / "managed-runtime.v1.json"
    expected_manifest, assets = expected_runtime_manifest(
        config_dir, bin_dir, provider_path_override=provider_path_override
    )
    all_targets = [registry_target, manifest_target, *[item[2] for item in assets.values()]]
    for target in [registry_target, manifest_target, *[item[2] for item in assets.values()]]:
        assert isinstance(target, Path)
        reject_runtime_path(home, target, label="managed runtime target")
    if registry_target.exists():
        validate_registry(read_config(registry_target))

    prior_manifest: dict[str, Any] | None = None
    if manifest_target.exists():
        prior_manifest = load_json(manifest_target)
        if prior_manifest.get("schema_version") != 1 or not isinstance(prior_manifest.get("assets"), dict):
            raise EngramMemoryError("managed runtime manifest is invalid; use explicit runtime repair after review")
        drift: list[str] = []
        for name, expected_hash in prior_manifest["assets"].items():
            asset = assets.get(name)
            if asset is None:
                drift.append(name)
                continue
            target = asset[2]
            assert isinstance(target, Path)
            if not target.exists() or sha256_path(target) != expected_hash:
                drift.append(name)
        if drift and not repair:
            raise EngramMemoryError(
                "managed runtime assets were locally changed; run runtime repair only after reviewing backups"
            )
    elif any(Path(item[2]).exists() for item in assets.values()):
        if not repair:
            raise EngramMemoryError(
                "pre-manifest managed runtime detected; run runtime repair after reviewing the existing files"
            )

    manifest_text = json.dumps(expected_manifest, indent=2, sort_keys=True) + "\n"
    would_change: list[str] = []
    would_back_up: list[str] = []
    if not registry_target.exists():
        would_change.append(registry_target.name)
    for name, (_kind, _source, target) in assets.items():
        assert isinstance(target, Path)
        if not target.exists() or sha256_path(target) != expected_manifest["assets"][name]:
            would_change.append(name)
            if target.exists():
                would_back_up.append(name)
    if not manifest_target.exists() or manifest_target.read_text(encoding="utf-8") != manifest_text:
        would_change.append(manifest_target.name)
        if manifest_target.exists():
            would_back_up.append(manifest_target.name)

    if dry_run:
        return {
            "status": "dry_run",
            "changed": bool(would_change),
            "would_change": would_change,
            "would_back_up": would_back_up,
            "config_scope": "user",
            "legacy_state": legacy_state,
            "repair": repair,
            "planned": [str(target) for target in all_targets if isinstance(target, Path)],
        }

    # No mutation occurs before every existing path, manifest, registry, and
    # drift condition has passed. Publication is additionally transactional:
    # an exception after any asset replacement restores every managed target
    # (including modes), removes targets that were absent, and removes only
    # backups created by this attempt.
    parent_existence = {
        path: path.exists()
        for root in (config_dir, bin_dir)
        for path in (root, *root.parents)
        if path == home or home in path.parents
    }
    transaction_root = Path(tempfile.mkdtemp(prefix="naos-engram-runtime-transaction-"))
    snapshots: dict[Path, tuple[str, Path, int] | None] = {}
    backup_inventory: dict[Path, set[Path]] = {}
    unique_targets = list(dict.fromkeys(target for target in all_targets if isinstance(target, Path)))
    try:
        for index, target in enumerate(unique_targets):
            backup_inventory[target] = set(target.parent.glob(f"{target.name}.engram-backup-*")) if target.parent.exists() else set()
            if not target.exists():
                snapshots[target] = None
                continue
            snapshot_path = transaction_root / str(index)
            mode = target.stat().st_mode & 0o7777
            if target.is_dir():
                shutil.copytree(target, snapshot_path)
                snapshots[target] = ("directory", snapshot_path, mode)
            elif target.is_file():
                shutil.copy2(target, snapshot_path)
                snapshots[target] = ("file", snapshot_path, mode)
            else:
                raise EngramMemoryError("managed runtime target is not a regular file or directory")

        config_dir.mkdir(parents=True, exist_ok=True)
        bin_dir.mkdir(parents=True, exist_ok=True)
        if not registry_target.exists():
            safe_copy_file(bundled_config_path("projects.json"), registry_target)

        changed: list[str] = []
        backed_up: list[str] = []
        for name, (kind, source, target) in assets.items():
            assert isinstance(target, Path)
            expected_hash = expected_manifest["assets"][name]
            if target.exists() and sha256_path(target) == expected_hash:
                continue
            if target.exists():
                if kind == "directory":
                    create_unique_directory_backup(target)
                else:
                    create_unique_backup(target)
                backed_up.append(name)
            if kind == "file":
                assert isinstance(source, Path)
                safe_copy_file(source, target)
            elif kind == "directory":
                assert isinstance(source, Path)
                replace_directory(source, target)
            else:
                assert isinstance(source, str)
                atomic_write_text(target, source, encoding="utf-8" if not target.name.endswith(".cmd") else "ascii")
            if target.name.startswith("engram-mcp-wrapper") and os.name != "nt":
                target.chmod(0o755)
            changed.append(name)
        if not manifest_target.exists() or manifest_target.read_text(encoding="utf-8") != manifest_text:
            if manifest_target.exists():
                create_unique_backup(manifest_target)
                backed_up.append(manifest_target.name)
            atomic_write_text(manifest_target, manifest_text, encoding="utf-8")
            changed.append(manifest_target.name)
    except BaseException:
        for target in reversed(unique_targets):
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
            elif target.is_dir():
                shutil.rmtree(target)
            snapshot_entry = snapshots.get(target)
            if snapshot_entry is None:
                continue
            kind, source, mode = snapshot_entry
            target.parent.mkdir(parents=True, exist_ok=True)
            if kind == "directory":
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
            target.chmod(mode)
        for target, before in backup_inventory.items():
            if not target.parent.exists():
                continue
            for backup in set(target.parent.glob(f"{target.name}.engram-backup-*")) - before:
                if backup.is_dir() and not backup.is_symlink():
                    shutil.rmtree(backup)
                else:
                    backup.unlink(missing_ok=True)
        for path, existed in sorted(parent_existence.items(), key=lambda item: len(item[0].parts), reverse=True):
            if not existed:
                try:
                    path.rmdir()
                except (FileNotFoundError, OSError):
                    pass
        raise
    finally:
        shutil.rmtree(transaction_root, ignore_errors=True)
    return {
        "status": "repaired" if repair and changed else "updated" if prior_manifest and changed else "installed" if changed else "already_configured",
        "changed": bool(changed),
        "config_scope": "user",
        "legacy_state": legacy_state,
        "runtime_files": changed,
        "backups_created": backed_up,
        "wrapper": "user_bin",
    }


def runtime_configuration_state(home: Path) -> dict[str, Any]:
    home = Path(os.path.abspath(home.expanduser()))
    reject_symlink_components(home, label="user home directory")
    config_dir = user_config_dir(home)
    wrapper = user_wrapper_path(home)
    required_files = (
        "projects.json",
        "release-support.json",
        "mcp-hosts.v1.json",
        "engram_memory.py",
        "provider.py",
        "managed-runtime.v1.json",
    )
    required_directories = ("clients", "instructions")
    missing = [name for name in required_files if not (config_dir / name).is_file()]
    missing.extend(name for name in required_directories if not (config_dir / name).is_dir())
    wrapper_ready = wrapper.is_file() and (os.name == "nt" or os.access(wrapper, os.X_OK))
    if not wrapper_ready:
        missing.append("user_wrapper")
    integrity = "unverified"
    manifest_path = config_dir / "managed-runtime.v1.json"
    if not missing and manifest_path.is_file():
        try:
            manifest = load_json(manifest_path)
            expected, assets = expected_runtime_manifest(config_dir, user_bin_dir(home))
            actual_ok = manifest.get("assets") == expected.get("assets")
            if actual_ok:
                actual_ok = all(
                    Path(item[2]).exists() and sha256_path(Path(item[2])) == manifest["assets"][name]
                    for name, item in assets.items()
                )
            integrity = "verified_current" if actual_ok and manifest.get("toolkit_version") == TOOLKIT_VERSION else "stale_or_modified"
            if integrity != "verified_current":
                missing.append("runtime_integrity")
        except (EngramMemoryError, OSError, ValueError):
            integrity = "invalid_manifest"
            missing.append("runtime_integrity")
    return {
        "config_scope": "user",
        "configured": not missing,
        "status": "complete" if not missing else "absent" if not config_dir.exists() else "partial",
        "missing_components": missing,
        "integrity": integrity,
        "legacy_state": legacy_runtime_state(home),
    }


def normalized_remote(value: str) -> str:
    """Normalise only transport spelling; do not infer a project from a path."""
    value = value.strip().rstrip("/")
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        identity = f"{host.casefold()}/{path}"
    elif "://" in value:
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise EngramMemoryError("approved remote has an invalid port") from exc
        scheme = parsed.scheme.casefold()
        default_port = 443 if scheme == "https" else 22 if scheme == "ssh" else None
        authority = (parsed.hostname or "").casefold()
        if port is not None and port != default_port:
            authority = f"{scheme}+{authority}:{port}"
        identity = f"{authority}/{parsed.path.lstrip('/')}"
    else:
        identity = value
    if identity.endswith(".git"):
        identity = identity[:-4]
    return identity


def validate_credential_free_remote(value: str) -> str:
    """Accept only credential-free, non-local Git remote identity forms."""
    remote = value.strip()
    if not remote or any(character in remote for character in ("\r", "\n", "\0", "\t", " ")):
        raise EngramMemoryError("invalid approved remote")
    if "?" in remote:
        raise EngramMemoryError("approved remote must not contain a URL query")
    if "#" in remote:
        raise EngramMemoryError("approved remote must not contain a URL fragment")

    if "://" not in remote:
        scp = re.fullmatch(r"(?P<user>[^@/:]+)@(?P<host>[^@/:]+):(?P<path>.+)", remote)
        if scp is None or scp.group("user").casefold() != "git":
            raise EngramMemoryError(
                "approved remote must use credential-free https, ssh://git, or git@host:path form"
            )
        return remote

    parsed = urlsplit(remote)
    scheme = parsed.scheme.casefold()
    try:
        parsed.port
    except ValueError as exc:
        raise EngramMemoryError("approved remote has an invalid port") from exc
    if scheme not in {"https", "ssh"} or not parsed.hostname or not parsed.path.strip("/"):
        raise EngramMemoryError(
            "approved remote must use credential-free https, ssh://git, or git@host:path form"
        )
    if parsed.password is not None:
        raise EngramMemoryError("approved remote contains embedded credentials; use a credential-free remote")
    if scheme == "https" and parsed.username is not None:
        raise EngramMemoryError("approved remote contains embedded credentials; use a credential-free remote")
    if scheme == "ssh" and parsed.username != "git":
        raise EngramMemoryError("approved SSH remote must use the non-personal git account")
    return remote


def validate_registry(registry: dict[str, Any]) -> None:
    if registry.get("schema_version") != 1:
        raise EngramMemoryError("unsupported project registry schema")
    projects = registry.get("projects")
    if not isinstance(projects, list):
        raise EngramMemoryError("project registry projects must be a list")

    identifiers: dict[str, str] = {}
    remotes: dict[str, str] = {}
    for project in projects:
        if not isinstance(project, dict):
            raise EngramMemoryError("every project registry entry must be an object")
        canonical = project.get("id")
        if not isinstance(canonical, str) or not PROJECT_ID.fullmatch(canonical):
            raise EngramMemoryError(f"invalid canonical project ID: {canonical!r}")
        aliases = project.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
            raise EngramMemoryError(f"project aliases must be a list of strings for {canonical!r}")
        for identifier in [canonical, *aliases]:
            if not isinstance(identifier, str) or not PROJECT_ID.fullmatch(identifier):
                raise EngramMemoryError(f"invalid project alias: {identifier!r}")
            key = identifier.casefold()
            if key in identifiers:
                raise EngramMemoryError(f"duplicate or colliding project identifier: {identifier!r}")
            identifiers[key] = canonical
        project_remotes = project.get("remotes")
        if not isinstance(project_remotes, list) or not project_remotes:
            raise EngramMemoryError(f"{canonical!r} must declare at least one approved remote")
        for remote in project_remotes:
            if not isinstance(remote, str) or not remote.strip():
                raise EngramMemoryError(f"invalid approved remote for {canonical!r}")
            key = normalized_remote(validate_credential_free_remote(remote))
            if key in remotes:
                raise EngramMemoryError(f"duplicate or colliding approved remote: {remote!r}")
            remotes[key] = canonical


def project_by_identifier(registry: dict[str, Any], identifier: str) -> dict[str, Any]:
    validate_registry(registry)
    lookup = identifier.casefold()
    matches = [
        project
        for project in registry["projects"]
        if lookup == project["id"].casefold()
        or lookup in {alias.casefold() for alias in project.get("aliases", [])}
    ]
    if not matches:
        raise EngramMemoryError(f"unregistered project identifier: {identifier!r}")
    if len(matches) != 1:  # validate_registry normally makes this impossible.
        raise EngramMemoryError(f"ambiguous project identifier: {identifier!r}")
    return matches[0]


def project_by_remote(registry: dict[str, Any], remote: str) -> dict[str, Any]:
    validate_registry(registry)
    normal = normalized_remote(validate_credential_free_remote(remote))
    matches = [
        project
        for project in registry["projects"]
        if normal in {normalized_remote(candidate) for candidate in project["remotes"]}
    ]
    if not matches:
        raise EngramMemoryError("Git remote is not approved by the project registry")
    if len(matches) != 1:
        raise EngramMemoryError("approved Git remote maps to more than one project")
    return matches[0]


def git_origin(cwd: Path, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> str | None:
    result = runner(
        ["git", "-C", str(cwd), "config", "--get", "remote.origin.url"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def resolve_project(
    registry: dict[str, Any], *, requested: str | None, remote: str | None
) -> tuple[str, str]:
    """Return canonical project ID and its evidence source.

    An explicit project is allowed only when it is registered and, if a Git
    remote is observable, agrees with the remote. This prevents a stale client
    environment from writing one repository's memory under another project.
    """
    requested_project = project_by_identifier(registry, requested) if requested else None
    remote_project = project_by_remote(registry, remote) if remote else None
    if requested_project and remote_project and requested_project["id"] != remote_project["id"]:
        raise EngramMemoryError(
            "explicit project and checked-out Git remote resolve to different canonical projects"
        )
    if requested_project:
        return requested_project["id"], "registered_explicit_project"
    if remote_project:
        return remote_project["id"], "approved_git_remote"
    raise EngramMemoryError("no explicit project or approved Git remote is available")


def parse_codex_mcp_toml(document: str) -> dict[str, Any]:
    """Parse the exact dependency-free TOML subset emitted for Codex."""
    lines = document.splitlines()
    expected_keys = ("command", "cwd", "enabled", "required", "startup_timeout_sec")
    if len(lines) != 1 + len(expected_keys) or lines[0] != "[mcp_servers.engram]":
        raise EngramMemoryError("bundled Codex client template has an invalid TOML structure")
    values: dict[str, Any] = {}
    for line, key in zip(lines[1:], expected_keys):
        prefix = f"{key} = "
        if not line.startswith(prefix):
            raise EngramMemoryError("bundled Codex client template has an invalid TOML structure")
        raw = line[len(prefix) :]
        if key in {"command", "cwd"}:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise EngramMemoryError("bundled Codex client template has an invalid TOML string") from exc
            if not isinstance(value, str) or not value or any(
                ord(character) < 32 or ord(character) == 127 for character in value
            ):
                raise EngramMemoryError("bundled Codex client template has an unsafe TOML path")
            if not Path(value).is_absolute():
                raise EngramMemoryError("bundled Codex client template paths must be absolute")
        elif key in {"enabled", "required"}:
            if raw != "true":
                raise EngramMemoryError("bundled Codex client template must enable required startup")
            value = True
        else:
            if raw != "30":
                raise EngramMemoryError("bundled Codex client template has an invalid startup timeout")
            value = 30
        values[key] = value
    return {"mcp_servers": {"engram": values}}


def client_template(
    client: str,
    project: str | None = None,
    *,
    wrapper_command: str | None = None,
    host_version: str | None = None,
    workspace_path: str | Path | None = None,
) -> str:
    if client == "codex-vscode":
        raise EngramMemoryError(
            "codex-vscode is not an independent configuration surface; use codex or vscode-generic"
        )
    client = select_opencode_adapter(client, host_version=host_version)
    templates = {
        "codex": TEMPLATE_DIR / "codex.mcp.toml",
        "vscode-generic": TEMPLATE_DIR / "vscode-generic.mcp.json",
        "claude-code": TEMPLATE_DIR / "claude-code.mcp.json",
        "antigravity": TEMPLATE_DIR / "antigravity.mcp.json",
        "kilo": TEMPLATE_DIR / "kilo.mcp.json",
        "opencode-v1": TEMPLATE_DIR / "opencode.mcp.json",
        "cursor": TEMPLATE_DIR / "cursor.mcp.json",
        "muse": TEMPLATE_DIR / "muse.mcp.json",
        "project-config": TEMPLATE_DIR / "project-engram-config.json",
    }
    try:
        template_path = templates[client]
        template = template_path.read_text(encoding="utf-8")
    except KeyError as exc:
        raise EngramMemoryError(f"unsupported client adapter: {client!r}") from exc
    if "__ENGRAM_PROJECT__" in template:
        if not project:
            raise EngramMemoryError(f"client adapter {client!r} requires an explicit registered project")
    if "__ENGRAM_WORKSPACE__" in template:
        if workspace_path is None or not Path(workspace_path).is_absolute():
            raise EngramMemoryError(
                f"client adapter {client!r} requires an absolute registered workspace path"
            )
    if client == "codex" and (
        wrapper_command is None or not Path(wrapper_command).is_absolute()
    ):
        raise EngramMemoryError("Codex render requires an absolute managed wrapper path")
    replacements = {
        "__ENGRAM_PROJECT__": project or "",
        "__ENGRAM_MCP_WRAPPER__": wrapper_command or "engram-mcp-wrapper",
        "__ENGRAM_WORKSPACE__": str(workspace_path) if workspace_path is not None else "",
    }
    if template_path.suffix == ".json":
        try:
            document = json.loads(template)
        except json.JSONDecodeError as exc:
            raise EngramMemoryError(f"bundled client template is invalid JSON: {client}") from exc

        def replace_strings(value: Any) -> Any:
            if isinstance(value, str):
                return replacements.get(value, value)
            if isinstance(value, list):
                return [replace_strings(item) for item in value]
            if isinstance(value, dict):
                return {key: replace_strings(item) for key, item in value.items()}
            return value

        return json.dumps(replace_strings(document), indent=2) + "\n"
    if template_path.suffix == ".toml":
        for marker, value in replacements.items():
            template = template.replace(f'"{marker}"', json.dumps(value))
        parse_codex_mcp_toml(template)
        return template
    raise EngramMemoryError(f"unsupported client template format: {template_path.suffix}")


def render_client_adapter(
    registry: dict[str, Any],
    *,
    client: str,
    project: str | None = None,
    workspace: Path | None = None,
    wrapper_command: str | None = None,
    host_version: str | None = None,
) -> str:
    """Render without writing, after validating any workspace-bound identity."""
    canonical = project_by_identifier(registry, project)["id"] if project else None
    rendered_workspace: Path | None = None
    if client == "codex":
        if not project or workspace is None:
            raise EngramMemoryError(
                "Codex render requires both an explicit registered project and absolute workspace"
            )
        if wrapper_command is None or not Path(wrapper_command).is_absolute():
            raise EngramMemoryError("Codex render requires an absolute managed wrapper path")
        if not workspace.is_absolute():
            raise EngramMemoryError("Codex render requires an absolute workspace path")
        try:
            rendered_workspace = workspace.resolve(strict=True)
        except OSError as exc:
            raise EngramMemoryError("workspace must be an existing directory") from exc
        if not rendered_workspace.is_dir():
            raise EngramMemoryError("workspace must be an existing directory")
        remote = git_origin(rendered_workspace)
        if remote is None:
            raise EngramMemoryError(
                "Codex render requires the workspace to expose an approved Git remote"
            )
        canonical, _ = resolve_project(
            registry,
            requested=project,
            remote=remote,
        )
    elif workspace is not None:
        raise EngramMemoryError("--workspace is currently supported only by the Codex renderer")
    return client_template(
        client,
        canonical,
        wrapper_command=wrapper_command,
        host_version=host_version,
        workspace_path=rendered_workspace,
    )


def select_opencode_adapter(client: str, *, host_version: str | None = None) -> str:
    """Select OpenCode by product identity, never by an unrelated semver."""
    if client == "opencode":
        raise EngramMemoryError(
            "OpenCode is ambiguous; select the stable opencode-v1 (opencode) adapter explicitly"
        )
    if client == "opencode-v2":
        raise EngramMemoryError(
            "OpenCode V2 beta is owner-excluded from this release and no adapter is shipped"
        )
    if client not in OPENCODE_EXECUTABLES:
        return client
    if host_version:
        raise EngramMemoryError(
            "OpenCode adapter identity is not selected by semantic version; omit --host-version "
            "and provide the stable opencode executable when installing"
        )
    return client


def validate_opencode_executable(client: str, host_executable: Path | None) -> Path | None:
    """Bind managed OpenCode writes to its distinct local executable identity."""
    if client not in OPENCODE_EXECUTABLES:
        return None
    expected = OPENCODE_EXECUTABLES[client]
    candidate = host_executable
    if candidate is None:
        discovered = shutil.which(expected)
        candidate = Path(discovered) if discovered else None
    if candidate is None:
        raise EngramMemoryError(
            f"{client} installation requires the {expected!r} executable; pass --host-executable"
        )
    candidate = candidate.expanduser().resolve()
    accepted_names = {expected}
    if os.name == "nt":
        accepted_names.update({f"{expected}.cmd", f"{expected}.bat", f"{expected}.ps1"})
    if candidate.name not in accepted_names or not candidate.is_file() or (os.name != "nt" and not os.access(candidate, os.X_OK)):
        raise EngramMemoryError(
            f"{client} requires an executable named {expected!r}; refusing a different OpenCode product"
        )
    return candidate


def _nested_value(document: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = document
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _set_nested_value(document: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    if not path:
        if not isinstance(value, dict):
            raise EngramMemoryError("managed adapter must render a JSON object")
        document.clear()
        document.update(value)
        return
    current: dict[str, Any] = document
    for key in path[:-1]:
        child = current.get(key)
        if child is None:
            child = {}
            current[key] = child
        if not isinstance(child, dict):
            raise EngramMemoryError(
                f"existing client configuration has a non-object value at {key!r}; refusing to overwrite it"
            )
        current = child
    current[path[-1]] = value


def _load_install_document(target: Path) -> dict[str, Any]:
    reject_symlink(target, label="client configuration target")
    if not target.exists():
        return {}
    try:
        parsed = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngramMemoryError(
            "existing client configuration is not strict JSON or cannot be read; "
            "refusing to rewrite it—use render-client and perform a reviewed manual merge"
        ) from exc
    if not isinstance(parsed, dict):
        raise EngramMemoryError("existing client configuration must be a JSON object")
    return parsed


def reject_opencode_v2_shape(document: dict[str, Any]) -> None:
    """Refuse the removed beta schema instead of creating a mixed document."""
    mcp = document.get("mcp")
    if isinstance(mcp, dict) and "servers" in mcp:
        raise EngramMemoryError(
            "OpenCode configuration contains the owner-excluded V2 beta mcp.servers shape; "
            "remove or archive it before installing the stable V1 adapter"
        )


def _run_boundary_git(workspace: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise EngramMemoryError("OpenCode installation requires Git")
    environment = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        return subprocess.run(
            [git, "-C", str(workspace), *arguments],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngramMemoryError("OpenCode Git boundary could not be established") from exc


def require_opencode_ignored_path(workspace: Path, path: Path, *, label: str) -> None:
    try:
        relative = path.relative_to(workspace).as_posix()
    except ValueError as exc:
        raise EngramMemoryError(f"{label} must stay inside the exact Git workspace") from exc
    ignored = _run_boundary_git(
        workspace,
        ["check-ignore", "--no-index", "-q", "--", relative],
    )
    if ignored.returncode == 1:
        raise EngramMemoryError(f"{label} must be locally ignored before installation")
    if ignored.returncode != 0:
        raise EngramMemoryError(f"{label} ignore status could not be established")


def require_opencode_local_git_hygiene(workspace: Path, target: Path) -> None:
    """Keep machine-specific OpenCode configuration out of repository history."""
    root_result = _run_boundary_git(workspace, ["rev-parse", "--show-toplevel"])
    if root_result.returncode != 0:
        raise EngramMemoryError("OpenCode installation requires a Git workspace root")
    observed_root = Path(root_result.stdout.strip()).resolve()
    if observed_root != workspace.resolve():
        raise EngramMemoryError("OpenCode configuration must be installed at the exact Git workspace root")
    relative = target.relative_to(workspace).as_posix()
    tracked = _run_boundary_git(
        workspace,
        ["ls-files", "--error-unmatch", "--", relative],
    )
    if tracked.returncode == 0:
        raise EngramMemoryError("OpenCode configuration is tracked; refusing a machine-specific managed write")
    if tracked.returncode != 1:
        raise EngramMemoryError("OpenCode tracked-file status could not be established")
    require_opencode_ignored_path(workspace, target, label="OpenCode configuration")


def create_unique_backup(
    target: Path, *, before_create: Callable[[Path], None] | None = None
) -> Path:
    """Create a collision-safe sibling backup without overwriting prior evidence."""
    reject_symlink(target, label="backup source")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for counter in range(1000):
        suffix = f"-{counter}" if counter else ""
        candidate = target.with_name(f"{target.name}.engram-backup-{timestamp}{suffix}")
        if before_create is not None:
            before_create(candidate)
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(descriptor)
        try:
            shutil.copyfile(target, candidate)
            shutil.copystat(target, candidate)
        except Exception:
            candidate.unlink(missing_ok=True)
            raise
        return candidate
    raise EngramMemoryError("could not allocate a unique backup path; manual review required")


def install_client(
    registry: dict[str, Any],
    *,
    client: str,
    project: str | None = None,
    workspace: Path | None = None,
    home: Path | None = None,
    wrapper_path: Path | None = None,
    host_executable: Path | None = None,
    host_version: str | None = None,
    dry_run: bool = False,
    repair: bool = False,
    _allow_unmanaged_wrapper: bool = False,
) -> dict[str, str | bool]:
    """Install one explicit adapter without guessing the workspace identity."""
    if client == "codex-vscode":
        raise EngramMemoryError("codex-vscode is not installable; use codex or vscode-generic")
    client = select_opencode_adapter(client, host_version=host_version)
    validate_opencode_executable(client, host_executable)
    resolved_home = Path(os.path.abspath(Path.home() if home is None else home))
    reject_symlink_components(resolved_home, label="user home directory")
    if client in WORKSPACE_INSTALL_TARGETS:
        if not project or workspace is None:
            raise EngramMemoryError("workspace client installation requires both a project and workspace")
        relative_target, entry_path = WORKSPACE_INSTALL_TARGETS[client]
        workspace = Path(os.path.abspath(workspace.expanduser()))
        reject_symlink_components(workspace, label="workspace root")
        if not workspace.is_dir():
            raise EngramMemoryError("workspace must be an existing directory")
        canonical, _ = resolve_project(registry, requested=project, remote=git_origin(workspace))
        target = workspace / relative_target
        reject_symlinks_beneath(workspace, target, label="client configuration target")
        if client == "opencode-v1":
            require_opencode_local_git_hygiene(workspace, target)
    elif client in USER_INSTALL_TARGETS:
        if project or workspace is not None:
            raise EngramMemoryError("the dynamic Muse adapter does not accept a fixed project or workspace")
        relative_target, entry_path = USER_INSTALL_TARGETS[client]
        canonical = None
        target = resolved_home / relative_target
    else:
        raise EngramMemoryError(
            f"client adapter {client!r} has no safe managed installer; use render-client"
        )
    wrapper = Path(os.path.abspath((wrapper_path or user_wrapper_path(resolved_home)).expanduser()))
    reject_symlink_components(wrapper, label="managed client wrapper")
    if not wrapper.is_file() or (os.name != "nt" and not os.access(wrapper, os.X_OK)):
        raise EngramMemoryError(
            "managed client installation requires the durable user Engram wrapper; "
            "run naos-engram-memory runtime install first"
        )
    if not _allow_unmanaged_wrapper:
        expected_wrapper = Path(os.path.abspath(user_wrapper_path(resolved_home)))
        state = runtime_configuration_state(resolved_home)
        if wrapper != expected_wrapper or state["integrity"] != "verified_current":
            raise EngramMemoryError(
                "managed client installation requires the exact integrity-verified user runtime wrapper; "
                "custom wrappers are render-only"
            )
    rendered = client_template(client, canonical, wrapper_command=str(wrapper), host_version=host_version)
    try:
        managed_document = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise EngramMemoryError(f"managed adapter {client!r} is invalid JSON") from exc
    document = _load_install_document(target)
    if client == "opencode-v1":
        reject_opencode_v2_shape(document)
    managed_entry = _nested_value(managed_document, entry_path)
    existing_entry = _nested_value(document, entry_path)
    if existing_entry is not None:
        if existing_entry != managed_entry:
            legacy_document = json.loads(client_template(client, canonical, host_version=host_version))
            legacy_entry = _nested_value(legacy_document, entry_path)
            if existing_entry != legacy_entry and not repair:
                raise EngramMemoryError(
                    "existing engram client entry differs from the managed explicit-project adapter; "
                    "refusing to overwrite it"
                )
            if repair and not is_repairable_engram_entry(client, existing_entry, canonical):
                raise EngramMemoryError(
                    "existing engram entry is not provably toolkit-managed; refusing repair"
                )
        else:
            return {
                "client": client,
                "target": relative_target.as_posix(),
                "changed": False,
                "status": "already_configured",
                **({"project": canonical} if canonical else {}),
            }
    if not isinstance(managed_entry, dict):
        raise EngramMemoryError("managed adapter does not contain the expected server entry")
    _set_nested_value(document, entry_path, managed_entry)
    if dry_run:
        return {
            "client": client,
            "target": relative_target.as_posix(),
            "changed": True,
            "status": "dry_run",
            **({"project": canonical} if canonical else {}),
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_created = False
    if target.exists():
        backup_validator: Callable[[Path], None] | None = None
        if client == "opencode-v1":
            assert workspace is not None
            backup_validator = lambda candidate: require_opencode_ignored_path(
                workspace, candidate, label="OpenCode configuration backup"
            )
        create_unique_backup(
            target,
            before_create=backup_validator,
        )
        backup_created = True
    atomic_write_text(target, json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "client": client,
        "target": relative_target.as_posix(),
        "changed": True,
        "backup_created": backup_created,
        "status": "repaired" if repair else "installed",
        **({"project": canonical} if canonical else {}),
    }


def is_repairable_engram_entry(client: str, entry: Any, project: str | None) -> bool:
    """Recognize only stale variants of this toolkit's local Engram wrapper."""
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    if isinstance(command, list):
        executable = command[0] if command else ""
    else:
        executable = command
    if not isinstance(executable, str) or Path(executable).name not in {
        "engram-mcp-wrapper", "engram-mcp-wrapper.cmd",
    }:
        return False
    environment = entry.get("env", entry.get("environment", {}))
    if not isinstance(environment, dict):
        return False
    if project is not None and environment.get("ENGRAM_PROJECT") not in {None, project}:
        return False
    allowed = {
        "type", "command", "args", "env", "environment", "enabled", "disabled",
        "timeout", "codemode",
    }
    return set(entry) <= allowed


def redact(value: str) -> str:
    """Keep inventory metadata useful without emitting local identities or paths."""
    home = str(Path.home())
    replacements = [(home, "[HOME]")]
    for original, replacement in replacements:
        value = value.replace(original, replacement)
    value = re.sub(r"/(?:Users|home)/[^/\s]+", "/[USER]", value)
    value = re.sub(r"[A-Za-z]:\\\\Users\\\\[^\\\s]+", r"[USER]", value)
    value = re.sub(r"(?i)(token|secret|password|api[_-]?key)\s*[=:]\s*[^\s,]+", r"\1=[REDACTED]", value)
    return value


def vscode_user_config_paths(home: Path, platform: str | None = None) -> tuple[Path, ...]:
    """Return known VS Code user MCP locations without exposing them in reports."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return (home / "Library" / "Application Support" / "Code" / "User" / "mcp.json",)
    if platform == "win32":
        appdata = os.environ.get("APPDATA")
        paths = [home / "AppData" / "Roaming" / "Code" / "User" / "mcp.json"]
        if appdata:
            paths.insert(0, Path(appdata) / "Code" / "User" / "mcp.json")
        return tuple(paths)
    return (home / ".config" / "Code" / "User" / "mcp.json",)


def mcp_reference_status(paths: tuple[Path, ...]) -> str:
    """Report a conservative client configuration state without outputting its content."""
    configuration_found = False
    unreadable = False
    for path in paths:
        try:
            reject_symlink_components(path, label="client configuration path")
        except EngramMemoryError:
            unreadable = True
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            unreadable = True
            continue
        configuration_found = True
        if MCP_REFERENCE.search(text):
            return "reference_detected_unvalidated"
    if unreadable:
        return "configuration_unreadable"
    if configuration_found:
        return "configuration_present_without_reference"
    return "not_detected"


def client_mcp_reference_status(cwd: Path, home: Path) -> dict[str, str]:
    """Return possible MCP reference states, never an assertion that a client works."""
    return {
        "codex": mcp_reference_status((home / ".codex" / "config.toml",)),
        "vscode_user": mcp_reference_status(vscode_user_config_paths(home)),
        "vscode_workspace": mcp_reference_status((cwd / ".vscode" / "mcp.json",)),
        "copilot_agent_host_user": mcp_reference_status(
            (home / ".copilot" / "mcp-config.json",)
        ),
        "claude_code": mcp_reference_status(
            (cwd / ".mcp.json", home / ".claude.json", home / ".claude" / "settings.json")
        ),
        "antigravity_workspace": mcp_reference_status((cwd / ".agents" / "mcp_config.json",)),
        "antigravity_global": mcp_reference_status(
            (
                home / ".gemini" / "config" / "mcp_config.json",
                home / ".gemini" / "antigravity-cli" / "mcp_config.json",
            )
        ),
        "kilo": mcp_reference_status(
            (cwd / ".kilo" / "kilo.jsonc", home / ".config" / "kilo" / "kilo.jsonc")
        ),
        "opencode": mcp_reference_status(
            (
                cwd / "opencode.json",
                home / ".config" / "opencode" / "opencode.json",
                home / ".config" / "opencode" / "opencode.jsonc",
            )
        ),
        "cursor": mcp_reference_status((cwd / ".cursor" / "mcp.json", home / ".cursor" / "mcp.json")),
        "lm_studio": mcp_reference_status(
            (
                home / ".lmstudio" / "mcp.json",
                home / ".cache" / "lm-studio" / "mcp.json",
            )
        ),
        "anythingllm": mcp_reference_status(
            (home / ".anythingllm" / "anythingllm_mcp_servers.json",)
        ),
        "open_webui": "unsupported_native_http_transport",
        "muse": mcp_reference_status((home / ".config" / "muse" / "settings.json",)),
    }


def _binary_is_available(command: str) -> bool:
    if os.path.isabs(command):
        return Path(command).is_file() and os.access(command, os.X_OK)
    return shutil.which(command) is not None


def inventory(
    registry: dict[str, Any],
    cwd: Path,
    engram_binary: str,
    home: Path | None = None,
) -> dict[str, Any]:
    validate_registry(registry)
    home = Path.home() if home is None else home
    remote = git_origin(cwd)
    try:
        canonical, source = resolve_project(registry, requested=None, remote=remote)
        current = {"status": "resolved", "project": canonical, "source": source}
    except EngramMemoryError:
        current = {"status": "unresolved"}

    # Do not execute the upstream binary here. Even nominally diagnostic
    # commands may run update checks, migrations, or store initialization.
    # Managed provider state and the recorded binary checksum are sufficient
    # for a truthful local inventory; database health and project counts remain
    # explicitly unknown until a separately approved provider operation.
    try:
        provider_state = provider_lifecycle.provider_status(
            read_config(DEFAULT_SUPPORT, default_name="release-support.json"),
            home=home,
            config_dir=user_config_dir(home),
        )
    except provider_lifecycle.ProviderError as exc:
        raise EngramMemoryError(str(exc)) from exc
    external_candidate = _binary_is_available(engram_binary)
    managed_integrity = provider_state.get("binary_integrity") == "state_verified"
    binary_available = bool(managed_integrity or external_candidate)
    discovery_status = (
        "managed_state_verified"
        if managed_integrity
        else "external_candidate_detected_unverified"
        if external_candidate
        else "not_detected"
    )
    return {
        "schema_version": 5,
        "read_only": True,
        "provider_probe": {
            "enabled": True,
            "mode": "managed_state_and_checksum_only",
            "external_commands_invoked": False,
            "external_commands_may_access_network": False,
            "network_accessed": False,
            "network_access_status": "not_accessed",
        },
        "current_repository": current,
        "registry_projects": [
            {"id": project["id"], "aliases": project.get("aliases", [])}
            for project in registry["projects"]
        ],
        "engram": {
            "available": binary_available,
            "discovery_status": discovery_status,
            "version": provider_state.get("installed_version"),
            "provider_state": provider_state,
            "database": {
                "detected": None,
                "bytes": None,
                "doctor": "not_probed",
            },
            "project_inventory": {"status": "not_probed"},
        },
        "client_mcp_reference_status": client_mcp_reference_status(cwd, home),
    }


def release_status(support: dict[str, Any]) -> dict[str, Any]:
    if support.get("schema_version") != 1:
        raise EngramMemoryError("unsupported release support schema")
    releases = support.get("releases")
    if not isinstance(releases, list):
        raise EngramMemoryError("release support manifest must include releases")
    return {
        "schema_version": 1,
        "rollout_policy": support.get("rollout", {}).get("policy"),
        "releases": [
            {"version": release.get("version"), "status": release.get("status")}
            for release in releases
        ],
    }


def host_catalogue(catalogue: dict[str, Any]) -> dict[str, Any]:
    if catalogue.get("schema_version") != 1 or not isinstance(catalogue.get("hosts"), list):
        raise EngramMemoryError("unsupported MCP-host catalogue schema")
    terminal_dispositions = catalogue.get("terminal_dispositions", [])
    if not isinstance(terminal_dispositions, list):
        raise EngramMemoryError("MCP-host terminal dispositions must be a list")
    required = {
        "id", "host_type", "supported_platform_version", "schema_evidence", "scope",
        "project_mode", "config_target", "detection_command", "support_tier",
        "risk_note", "repair_path", "verification_instruction", "instruction_surfaces",
        "instruction_install", "evidence",
    }
    evidence_required = {"source_url", "source_type", "checked_on", "platforms_tested", "host_version_tested", "fixture_or_report"}
    string_fields = required - {"instruction_surfaces", "evidence"}
    allowed_tiers = {"manual", "experimental", "ineligible"}
    seen_ids: set[str] = set()
    for host in catalogue["hosts"]:
        if not isinstance(host, dict) or required - set(host):
            raise EngramMemoryError("MCP-host catalogue entry is missing required fields")
        if any(not isinstance(host[field], str) or not host[field] for field in string_fields):
            raise EngramMemoryError("MCP-host catalogue string fields must be non-empty strings")
        host_id = host["id"]
        if not PROJECT_ID.fullmatch(host_id) or host_id.casefold() in seen_ids:
            raise EngramMemoryError(f"duplicate or invalid MCP-host catalogue ID: {host_id!r}")
        seen_ids.add(host_id.casefold())
        if host["support_tier"] not in allowed_tiers:
            raise EngramMemoryError(f"unsupported MCP-host support tier: {host['support_tier']!r}")
        surfaces = host["instruction_surfaces"]
        if not isinstance(surfaces, list) or any(not isinstance(item, str) or not item for item in surfaces):
            raise EngramMemoryError(f"instruction_surfaces must be a list of strings for {host_id!r}")
        evidence = host["evidence"]
        if not isinstance(evidence, dict) or evidence_required - set(evidence):
            raise EngramMemoryError(f"MCP-host catalogue evidence is incomplete for {host.get('id', 'unknown')!r}")
        if any(evidence[field] is not None and not isinstance(evidence[field], str) for field in ("source_url", "host_version_tested")):
            raise EngramMemoryError(f"MCP-host catalogue evidence has invalid nullable strings for {host_id!r}")
        for field in ("source_type", "checked_on", "fixture_or_report"):
            if not isinstance(evidence[field], str) or not evidence[field]:
                raise EngramMemoryError(f"MCP-host catalogue evidence field {field!r} is invalid for {host_id!r}")
        platforms = evidence["platforms_tested"]
        if not isinstance(platforms, list) or any(not isinstance(item, str) or not item for item in platforms):
            raise EngramMemoryError(f"platforms_tested must be a list of strings for {host_id!r}")
    terminal_ids: set[str] = set()
    for terminal in terminal_dispositions:
        if not isinstance(terminal, dict) or set(terminal) != {"id", "disposition", "reason"}:
            raise EngramMemoryError("MCP-host terminal disposition must contain only id, disposition, and reason")
        terminal_id = terminal["id"]
        if not isinstance(terminal_id, str) or not PROJECT_ID.fullmatch(terminal_id):
            raise EngramMemoryError("MCP-host terminal disposition has an invalid ID")
        folded = terminal_id.casefold()
        if folded in seen_ids or folded in terminal_ids:
            raise EngramMemoryError(f"active and terminal MCP-host IDs must be disjoint: {terminal_id!r}")
        if terminal["disposition"] != "owner_excluded":
            raise EngramMemoryError("unsupported MCP-host terminal disposition")
        if not isinstance(terminal["reason"], str) or not terminal["reason"]:
            raise EngramMemoryError("MCP-host terminal disposition reason must be non-empty")
        terminal_ids.add(folded)
    return catalogue


def discover_python(candidates: list[str] | None = None) -> str:
    """Resolve one Python 3.10+ interpreter to an absolute path deterministically."""
    candidates = candidates or [sys.executable, "python3", "python"]
    seen: set[str] = set()
    for candidate in candidates:
        resolved = shutil.which(candidate) if not os.path.isabs(candidate) else candidate
        if not resolved:
            continue
        resolved = str(Path(resolved).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        result = subprocess.run(
            [resolved, "-c", "import sys; print(int(sys.version_info >= (3, 10)))"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip() == "1":
            return resolved
    raise EngramMemoryError("Python 3.10 or newer was not found")


def instruction_state(path: Path) -> str:
    reject_symlink(path, label="instruction target")
    if not path.exists():
        return "absent"
    text = path.read_text(encoding="utf-8")
    begins, ends = text.count(INSTRUCTION_BEGIN), text.count(INSTRUCTION_END)
    if begins == ends == 0:
        return "present_unmanaged"
    if begins == ends == 1 and text.find(INSTRUCTION_BEGIN) < text.find(INSTRUCTION_END):
        return "managed"
    return "conflicting_markers"


def render_instruction() -> str:
    text = INSTRUCTION_TEMPLATE.read_text(encoding="utf-8")
    if text.count(INSTRUCTION_BEGIN) != 1 or text.count(INSTRUCTION_END) != 1:
        raise EngramMemoryError("managed instruction template markers are invalid")
    return text


def install_instruction(workspace: Path, host: str, *, dry_run: bool = False) -> dict[str, Any]:
    if host not in INSTRUCTION_TARGETS:
        raise EngramMemoryError(f"host {host!r} has no managed project instruction installer")
    workspace = Path(os.path.abspath(workspace.expanduser()))
    reject_symlink_components(workspace, label="workspace root")
    if not workspace.is_dir():
        raise EngramMemoryError("project must be an existing directory")
    target = workspace / INSTRUCTION_TARGETS[host]
    reject_symlinks_beneath(workspace, target, label="instruction target")
    state = instruction_state(target)
    if state == "conflicting_markers":
        raise EngramMemoryError("instruction file has conflicting managed markers; manual review required")
    managed = render_instruction().rstrip() + "\n"
    existing_full = target.read_text(encoding="utf-8") if target.exists() else ""
    unmanaged = existing_full
    if state == "managed":
        start = existing_full.index(INSTRUCTION_BEGIN)
        end = existing_full.index(INSTRUCTION_END) + len(INSTRUCTION_END)
        unmanaged = existing_full[:start] + existing_full[end:]
    if INSTRUCTION_CONFLICT.search(unmanaged):
        raise EngramMemoryError("existing instructions conflict with the managed lifecycle; manual review required")
    if state == "managed":
        existing = existing_full
        start = existing.index(INSTRUCTION_BEGIN)
        end = existing.index(INSTRUCTION_END) + len(INSTRUCTION_END)
        proposed = existing[:start] + managed.rstrip("\n") + existing[end:]
        if proposed == existing:
            return {"host": host, "target": target.name, "status": "already_configured", "changed": False}
    else:
        existing = existing_full
        separator = "" if not existing or existing.endswith("\n\n") else "\n" if existing.endswith("\n") else "\n\n"
        proposed = existing + separator + managed
    if dry_run:
        return {"host": host, "target": target.name, "status": "dry_run", "changed": True}
    target.parent.mkdir(parents=True, exist_ok=True)
    backup_created = False
    if target.exists():
        create_unique_backup(target)
        backup_created = True
    atomic_write_text(target, proposed, encoding="utf-8")
    return {"host": host, "target": target.name, "status": "installed", "changed": True, "backup_created": backup_created}


def register_project(
    registry_path: Path,
    *,
    project_id: str,
    remote: str,
    alias: list[str] | None = None,
    dry_run: bool = False,
    authorized_base: Path | None = None,
) -> dict[str, Any]:
    reject_symlink_components(registry_path, label="project registry")
    if authorized_base is not None:
        reject_symlinks_beneath(authorized_base, registry_path, label="project registry")
    registry = load_json(registry_path)
    validate_registry(registry)
    if any(project_id.casefold() == p["id"].casefold() for p in registry["projects"]):
        raise EngramMemoryError(f"project {project_id!r} is already registered")
    candidate = {"id": project_id, "aliases": alias or [], "remotes": [remote]}
    proposed = json.loads(json.dumps(registry))
    proposed["projects"].append(candidate)
    validate_registry(proposed)
    if dry_run:
        return {"project": project_id, "status": "dry_run", "changed": True}
    reject_symlink_components(registry_path, label="project registry")
    create_unique_backup(registry_path)
    reject_symlink_components(registry_path, label="project registry")
    atomic_write_text(registry_path, json.dumps(proposed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"project": project_id, "status": "registered", "changed": True}


def propose_project_from_remote(workspace: Path) -> dict[str, str]:
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise EngramMemoryError("--from-remote must identify an existing workspace")
    remote = git_origin(workspace)
    if not remote:
        raise EngramMemoryError("workspace has no origin remote")
    validate_credential_free_remote(remote)
    normalized = normalized_remote(remote)
    candidate = normalized.rsplit("/", 1)[-1]
    if not PROJECT_ID.fullmatch(candidate):
        raise EngramMemoryError("origin repository name cannot form a valid canonical project ID; pass --id")
    return {"project": candidate, "remote": remote, "source": "git_origin_repository_name"}


def onboarding_summary(
    registry: dict[str, Any],
    catalogue: dict[str, Any],
    project: Path,
    engram_binary: str,
    home: Path,
    *,
    operation: str = "onboard",
) -> dict[str, Any]:
    project = project.expanduser().resolve()
    if not project.is_dir():
        raise EngramMemoryError("--project must identify an existing directory")
    report = inventory(
        registry,
        project,
        engram_binary,
        home=home,
    )
    python = discover_python()
    probe = report["provider_probe"]
    local_command_categories = ["git_local_config", "python_version_probe"]
    return {
        "schema_version": 1,
        "operation": operation,
        "profile": "local",
        "read_only": True,
        "network_accessed": probe["network_accessed"],
        "network_access_status": probe["network_access_status"],
        "external_commands_invoked": True,
        "external_commands_may_access_network": probe["external_commands_may_access_network"],
        "provider_commands_invoked": probe["external_commands_invoked"],
        "external_command_categories": local_command_categories,
        "runtime_state": runtime_configuration_state(home),
        "project": report["current_repository"],
        "python": {"available": True, "version_supported": True},
        "engram": report["engram"],
        "clients": host_catalogue(catalogue)["hosts"],
        "client_mcp_reference_status": report["client_mcp_reference_status"],
        "instruction_state": {
            host: instruction_state(project / target)
            for host, target in sorted(INSTRUCTION_TARGETS.items())
        },
        "next_actions": [
            "register the project if unresolved",
            "run provider status and explicitly install the supported local provider if absent",
            "preview one client and instruction surface",
            "confirm each requested local write",
            "restart the selected host and verify mem_current_project",
        ],
    }


def confirm_write(description: str, *, yes: bool, non_interactive: bool, dry_run: bool) -> bool:
    if dry_run:
        return False
    if yes:
        return True
    if non_interactive or not sys.stdin.isatty():
        raise EngramMemoryError(f"write requires explicit confirmation: {description}; pass --yes or --dry-run")
    answer = input(f"{description}? [y/N] ").strip().casefold()
    if answer not in {"y", "yes"}:
        raise EngramMemoryError("write was not confirmed")
    return True


def guided_onboarding_choice(choice: str | None, *, non_interactive: bool) -> dict[str, Any]:
    allowed = {"local", "use-existing", "defer", "decline"}
    if choice:
        if choice not in allowed:
            raise EngramMemoryError("onboarding choice must be local, use-existing, defer, or decline")
    elif non_interactive or not sys.stdin.isatty():
        raise EngramMemoryError("guided onboarding requires --choice in non-interactive mode")
    else:
        print("Choose: [1] local (default), [2] use-existing, [3] defer, [4] decline", file=sys.stderr)
        response = input("Choice [1]: ").strip() or "1"
        choice = {"1": "local", "2": "use-existing", "3": "defer", "4": "decline"}.get(response)
        if not choice:
            raise EngramMemoryError("invalid onboarding choice")
    previews = {
        "local": ["inspect the local project", "inspect provider status", "preview project registration if unresolved", "preview one client and instruction surface"],
        "use-existing": ["resolve and validate the selected existing Engram executable", "preview or record its exact real path, hash, version, and provenance", "bind the managed wrapper to that verified real path"],
        "defer": ["record no toolkit or client change"],
        "decline": ["stop without a write"],
    }
    return {"choice": choice, "preview_only": True, "writes_performed": False, "action_preview": previews[choice]}


def verify_client(
    registry: dict[str, Any], *, client: str, project: str, workspace: Path, wrapper: Path,
    host_version: str | None, host_executable: Path | None = None, home: Path | None = None,
    _allow_unmanaged_wrapper: bool = False,
) -> dict[str, Any]:
    workspace = Path(os.path.abspath(workspace.expanduser()))
    reject_symlink_components(workspace, label="workspace root")
    selected = select_opencode_adapter(client, host_version=host_version)
    validate_opencode_executable(selected, host_executable)
    if selected not in WORKSPACE_INSTALL_TARGETS:
        raise EngramMemoryError(f"client {client!r} has no workspace verification surface")
    relative, entry_path = WORKSPACE_INSTALL_TARGETS[selected]
    canonical, _ = resolve_project(registry, requested=project, remote=git_origin(workspace))
    resolved_home = Path(os.path.abspath(Path.home() if home is None else home))
    reject_symlink_components(resolved_home, label="user home directory")
    wrapper = Path(os.path.abspath(wrapper.expanduser()))
    reject_symlink_components(wrapper, label="managed client wrapper")
    if not _allow_unmanaged_wrapper:
        expected_wrapper = Path(os.path.abspath(user_wrapper_path(resolved_home)))
        state = runtime_configuration_state(resolved_home)
        if wrapper != expected_wrapper or state["integrity"] != "verified_current":
            raise EngramMemoryError(
                "client verification requires the exact integrity-verified user runtime wrapper"
            )
    expected = json.loads(client_template(selected, canonical, wrapper_command=str(wrapper), host_version=host_version))
    target = workspace / relative
    reject_symlinks_beneath(workspace, target, label="client verification target")
    if not target.exists():
        return {"client": selected, "status": "not_configured", "configuration_valid": False}
    if selected == "opencode-v1":
        require_opencode_local_git_hygiene(workspace, target)
    document = _load_install_document(target)
    if selected == "opencode-v1":
        reject_opencode_v2_shape(document)
    valid = _nested_value(document, entry_path) == _nested_value(expected, entry_path)
    return {
        "client": selected,
        "status": "syntax_and_managed_entry_valid" if valid else "configuration_differs",
        "configuration_valid": valid,
        "runtime_verified": False,
        "project": canonical,
    }


def supported_asset(support: dict[str, Any], platform: str) -> dict[str, str]:
    """Return the one explicitly promoted asset for a platform, or fail closed."""
    if support.get("schema_version") != 1:
        raise EngramMemoryError("unsupported release support schema")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for release in support.get("releases", []):
        platform_entry = release.get("platforms", {}).get(platform)
        if (
            release.get("status") == "supported"
            and isinstance(platform_entry, dict)
            and platform_entry.get("status") == "supported"
        ):
            matches.append((release, platform_entry))
    if len(matches) != 1:
        raise EngramMemoryError(f"no single supported Engram release is approved for {platform}")
    release, asset = matches[0]
    if not asset.get("sha256"):
        raise EngramMemoryError(f"approved release is missing a verified {platform} asset checksum")
    return {
        "version": release["version"],
        "asset": asset["asset"],
        "sha256": asset["sha256"],
        "url": f"https://github.com/Gentleman-Programming/engram/releases/download/v{release['version']}/{asset['asset']}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--catalogue", type=Path, default=DEFAULT_HOST_CATALOGUE)
    subparsers = parser.add_subparsers(dest="command", required=True)

    onboard_parser = subparsers.add_parser("onboard", help="local-profile onboarding assessment; read-only unless an explicit local choice is confirmed")
    onboard_parser.add_argument("--project", type=Path, required=True)
    onboard_parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    onboard_parser.add_argument("--engram-bin", default="engram", help=argparse.SUPPRESS)
    onboard_parser.add_argument("--guided", action="store_true", help="choose and preview a local onboarding posture")
    onboard_parser.add_argument("--choice", choices=["local", "use-existing", "defer", "decline"])
    onboard_parser.add_argument("--yes", action="store_true", help="confirm the local runtime files for --guided --choice local")
    onboard_parser.add_argument("--dry-run", action="store_true", help="preview local runtime files without writing")
    onboard_parser.add_argument("--non-interactive", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="read-only local diagnostics")
    doctor_parser.add_argument("--project", type=Path, required=True)
    doctor_parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    doctor_parser.add_argument("--engram-bin", default="engram", help=argparse.SUPPRESS)

    runtime_parser = subparsers.add_parser("runtime", help="install or preview durable local toolkit runtime files")
    runtime_commands = runtime_parser.add_subparsers(dest="runtime_command", required=True)
    runtime_install = runtime_commands.add_parser("install", help="install local registry, policy, templates, and MCP wrapper")
    runtime_install.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    runtime_install.add_argument("--dry-run", action="store_true")
    runtime_install.add_argument("--yes", action="store_true")
    runtime_install.add_argument("--non-interactive", action="store_true")
    runtime_repair = runtime_commands.add_parser("repair", help="back up and refresh toolkit-owned runtime assets")
    runtime_repair.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    runtime_repair.add_argument("--dry-run", action="store_true")
    runtime_repair.add_argument("--yes", action="store_true")
    runtime_repair.add_argument("--non-interactive", action="store_true")

    provider_parser = subparsers.add_parser("provider", help="status, adopt, install, upgrade, or roll back an explicitly verified Engram provider")
    provider_commands = provider_parser.add_subparsers(dest="provider_command", required=True)
    provider_status_parser = provider_commands.add_parser("status", help="read-only provider integrity and support status")
    provider_status_parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    provider_status_parser.add_argument("--platform", help=argparse.SUPPRESS)
    provider_adopt_parser = provider_commands.add_parser("adopt", help="explicitly adopt an existing supported Engram executable")
    provider_adopt_parser.add_argument("--path", required=True)
    provider_adopt_parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    provider_adopt_parser.add_argument("--platform", help=argparse.SUPPRESS)
    provider_adopt_parser.add_argument(
        "--allow-unvalidated-platform",
        action="store_true",
        help=(
            "bind an already-installed provider on a platform with no allowlisted asset; "
            "the approved version and SHA-256 binding still apply and provider install, "
            "upgrade, and rollback remain unavailable there"
        ),
    )
    provider_adopt_parser.add_argument("--yes", action="store_true")
    provider_adopt_parser.add_argument("--dry-run", action="store_true")
    provider_verify_parser = provider_commands.add_parser("verify-bound", help=argparse.SUPPRESS)
    provider_verify_parser.add_argument("--path", type=Path, required=True)
    provider_verify_parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    provider_verify_parser.add_argument("--config-dir", type=Path, required=True, help=argparse.SUPPRESS)
    for provider_action in ("install", "upgrade", "rollback"):
        provider_action_parser = provider_commands.add_parser(provider_action, help=f"explicitly {provider_action} the manifest-approved provider")
        provider_action_parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
        provider_action_parser.add_argument("--bin-dir", type=Path, help=argparse.SUPPRESS)
        provider_action_parser.add_argument("--platform", help=argparse.SUPPRESS)
        provider_action_parser.add_argument("--maintenance-window", action="store_true")
        provider_action_parser.add_argument("--yes", action="store_true")
        provider_action_parser.add_argument("--dry-run", action="store_true")

    project_parser = subparsers.add_parser("project", help="manage the canonical project registry")
    project_commands = project_parser.add_subparsers(dest="project_command", required=True)
    register_parser = project_commands.add_parser("register", help="register an explicit project and remote")
    register_parser.add_argument("--id", help="canonical project ID to register (lowercase, dot/dash/underscore)")
    register_parser.add_argument("--remote", help="approved credential-free Git remote to associate with --id")
    register_parser.add_argument(
        "--from-remote",
        type=Path,
        help=(
            "read this workspace's Git origin and propose the repository component as the ID; "
            "never the folder name. An already-registered remote keeps its existing project ID"
        ),
    )
    register_parser.add_argument("--alias", action="append", default=[], help="additional alias for the project (repeatable)")
    register_parser.add_argument("--dry-run", action="store_true")
    register_parser.add_argument("--yes", action="store_true")
    register_parser.add_argument("--non-interactive", action="store_true")

    client_parser = subparsers.add_parser("client", help="list, render, install, verify, or repair clients")
    client_commands = client_parser.add_subparsers(dest="client_command", required=True)
    client_commands.add_parser("list", help="show the versioned host catalogue")
    client_render = client_commands.add_parser("render", help="render a client adapter without writing")
    client_render.add_argument("--client", required=True)
    client_render.add_argument("--project")
    client_render.add_argument("--workspace", type=Path)
    client_render.add_argument("--host-version")
    client_render.add_argument("--wrapper")
    for action in ("install", "repair"):
        target_parser = client_commands.add_parser(action, help=f"{action} a reviewed managed client entry")
        target_parser.add_argument("--client", required=True)
        target_parser.add_argument("--project")
        target_parser.add_argument("--workspace", type=Path)
        target_parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
        target_parser.add_argument("--wrapper", type=Path)
        target_parser.add_argument("--host-executable", type=Path)
        target_parser.add_argument("--host-version")
        target_parser.add_argument("--dry-run", action="store_true")
        target_parser.add_argument("--yes", action="store_true")
        target_parser.add_argument("--non-interactive", action="store_true")
    client_verify = client_commands.add_parser("verify", help="verify syntax and managed entry without host-runtime claims")
    client_verify.add_argument("--client", required=True)
    client_verify.add_argument("--project", required=True)
    client_verify.add_argument("--workspace", type=Path, required=True)
    client_verify.add_argument("--wrapper", type=Path)
    client_verify.add_argument("--host-executable", type=Path)
    client_verify.add_argument("--host-version")

    instruction_parser = subparsers.add_parser("instruction", help="render or install project lifecycle instructions")
    instruction_commands = instruction_parser.add_subparsers(dest="instruction_command", required=True)
    instruction_commands.add_parser("render", help="render lifecycle instructions without writing")
    instruction_install = instruction_commands.add_parser("install", help="merge within managed markers")
    instruction_install.add_argument("--host", required=True)
    instruction_install.add_argument("--project", type=Path, required=True)
    instruction_install.add_argument("--dry-run", action="store_true")
    instruction_install.add_argument("--yes", action="store_true")
    instruction_install.add_argument("--non-interactive", action="store_true")

    resolve_parser = subparsers.add_parser("resolve", help="resolve one canonical project without writing")
    resolve_parser.add_argument(
        "--project",
        help="registered canonical project ID or alias (not a filesystem path, unlike 'onboard --project')",
    )
    resolve_parser.add_argument("--remote", help="approved Git remote URL to resolve instead of reading a workspace")
    resolve_parser.add_argument(
        "--cwd",
        type=Path,
        # Without a default, a bare "resolve" inspected no directory at all and
        # still reported that no approved Git remote was available. "inventory"
        # already defaults to the process directory; match it.
        default=Path.cwd(),
        help="workspace whose Git origin is read (default: the current directory)",
    )

    render_parser = subparsers.add_parser("render-client", help="render a managed client adapter")
    render_parser.add_argument(
        "--client",
        required=True,
        choices=[
            "codex",
            "codex-vscode",
            "vscode-generic",
            "claude-code",
            "antigravity",
            "kilo",
            "opencode",
            "opencode-v1",
            "cursor",
            "muse",
            "project-config",
        ],
    )
    render_parser.add_argument("--project", help="required only by adapters with an explicit project placeholder")
    render_parser.add_argument("--workspace", type=Path, help="absolute registered workspace for Codex")
    render_parser.add_argument("--wrapper", help="override the rendered wrapper command")
    render_parser.add_argument("--host-version")

    install_parser = subparsers.add_parser(
        "install-client",
        help="install a strict-JSON workspace adapter with backup and collision protection",
    )
    install_parser.add_argument(
        "--client",
        required=True,
        choices=sorted((*WORKSPACE_INSTALL_TARGETS, *USER_INSTALL_TARGETS)),
    )
    install_parser.add_argument("--project", help="required for fixed-project workspace adapters")
    install_parser.add_argument("--workspace", type=Path, help="required for fixed-project workspace adapters")
    install_parser.add_argument("--home", type=Path, help="test-only override for a user-scoped adapter")
    install_parser.add_argument(
        "--wrapper",
        type=Path,
        help="override the verified local wrapper path (defaults to ~/bin/engram-mcp-wrapper)",
    )
    install_parser.add_argument("--host-executable", type=Path)
    install_parser.add_argument("--dry-run", action="store_true", help="validate and report without writing")
    install_parser.add_argument("--host-version")
    install_parser.add_argument("--yes", action="store_true")
    install_parser.add_argument("--non-interactive", action="store_true")

    inventory_parser = subparsers.add_parser("inventory", help="emit a redacted, read-only local inventory")
    inventory_parser.add_argument("--cwd", type=Path, default=Path.cwd())
    inventory_parser.add_argument("--engram-bin", default="engram")
    inventory_parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)

    release_parser = subparsers.add_parser("release-status", help="show local support states without checking upstream")
    release_parser.add_argument("--support-manifest", type=Path, default=DEFAULT_SUPPORT)

    asset_parser = subparsers.add_parser("supported-asset", help="return the one approved platform asset")
    asset_parser.add_argument("--support-manifest", type=Path, default=DEFAULT_SUPPORT)
    asset_parser.add_argument("--platform", required=True)

    # argparse does not honour argparse.SUPPRESS for a subparser help string; it
    # renders the sentinel literally in --help. Omit the kwarg instead.
    validate_path_parser = subparsers.add_parser("validate-path")
    validate_path_parser.add_argument("--path", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate-path":
            reject_symlink_components(args.path, label="managed filesystem path")
            print(json.dumps({"status": "safe_lexical_path", "read_only": True}, sort_keys=True))
            return 0
        registry = read_config(args.registry, default_name="projects.json" if args.registry == DEFAULT_REGISTRY else None)
        if args.command in {"onboard", "doctor"}:
            result = onboarding_summary(
                registry,
                read_config(args.catalogue, default_name="mcp-hosts.v1.json" if args.catalogue == DEFAULT_HOST_CATALOGUE else None),
                args.project,
                args.engram_bin,
                args.home,
                operation=args.command,
            )
            if args.command == "onboard" and (args.guided or args.choice):
                result["guided"] = guided_onboarding_choice(args.choice, non_interactive=args.non_interactive)
                if result["guided"]["choice"] == "local":
                    confirm_write(
                        "install durable local toolkit runtime files",
                        yes=args.yes,
                        non_interactive=args.non_interactive,
                        dry_run=args.dry_run,
                    )
                    result["runtime"] = ensure_user_runtime(home=args.home, dry_run=args.dry_run)
                    wrote_runtime = not args.dry_run and bool(result["runtime"].get("changed"))
                    result["read_only"] = not wrote_runtime
                    result["guided"]["preview_only"] = args.dry_run
                    result["guided"]["writes_performed"] = wrote_runtime
                    result["runtime_state"] = runtime_configuration_state(args.home)
                elif result["guided"]["choice"] == "use-existing":
                    confirm_write(
                        "adopt the selected existing Engram provider and bind the managed wrapper",
                        yes=args.yes,
                        non_interactive=args.non_interactive,
                        dry_run=args.dry_run,
                    )
                    support = read_config(DEFAULT_SUPPORT, default_name="release-support.json")
                    adoption_preview = provider_lifecycle.adopt_existing_provider(
                        support,
                        candidate=args.engram_bin,
                        home=args.home,
                        config_dir=user_config_dir(args.home),
                        yes=False,
                        dry_run=True,
                    )
                    selected_provider = Path(adoption_preview["provider_path"])
                    current_runtime = runtime_configuration_state(args.home)
                    if current_runtime["status"] == "partial" or current_runtime["integrity"] in {"stale_or_modified", "invalid_manifest"}:
                        raise EngramMemoryError(
                            "use-existing refuses partial or drifted runtime; run and review runtime repair separately"
                        )
                    runtime_preview = ensure_user_runtime(
                        home=args.home,
                        dry_run=True,
                        provider_path_override=selected_provider,
                    )
                    result["provider_adoption"] = adoption_preview
                    result["runtime"] = runtime_preview
                    if not args.dry_run:
                        state_path = user_config_dir(args.home) / "provider-state.v1.json"
                        prior_state = state_path.read_bytes() if state_path.exists() else None
                        result["provider_adoption"] = provider_lifecycle.adopt_existing_provider(
                            support,
                            candidate=args.engram_bin,
                            home=args.home,
                            config_dir=user_config_dir(args.home),
                            yes=args.yes,
                            dry_run=False,
                        )
                        try:
                            result["runtime"] = ensure_user_runtime(
                                home=args.home,
                                provider_path_override=selected_provider,
                            )
                        except BaseException:
                            provider_lifecycle.restore_provider_state(
                                config_dir=user_config_dir(args.home), prior=prior_state
                            )
                            raise
                    wrote = not args.dry_run and (
                        bool(result["provider_adoption"].get("changed"))
                        or bool(result["runtime"].get("changed"))
                    )
                    result["read_only"] = not wrote
                    result["guided"]["preview_only"] = args.dry_run
                    result["guided"]["writes_performed"] = wrote
                    result["runtime_state"] = runtime_configuration_state(args.home)
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "runtime" and args.runtime_command in {"install", "repair"}:
            confirm_write(
                f"{args.runtime_command} durable local toolkit runtime files",
                yes=args.yes,
                non_interactive=args.non_interactive,
                dry_run=args.dry_run,
            )
            print(json.dumps(ensure_user_runtime(
                home=args.home, dry_run=args.dry_run,
                repair=args.runtime_command == "repair",
            ), indent=2, sort_keys=True))
        elif args.command == "provider":
            support = read_config(DEFAULT_SUPPORT, default_name="release-support.json")
            selected_bin_dir = getattr(args, "bin_dir", None) or user_bin_dir(args.home)
            if args.provider_command == "status":
                provider_result = provider_lifecycle.provider_status(
                    support, home=args.home, bin_dir=selected_bin_dir,
                    config_dir=user_config_dir(args.home),
                    selected_platform=args.platform,
                )
            elif args.provider_command == "adopt":
                adoption_preview = provider_lifecycle.adopt_existing_provider(
                    support,
                    candidate=args.path,
                    home=args.home,
                    config_dir=user_config_dir(args.home),
                    selected_platform=args.platform,
                    yes=False,
                    dry_run=True,
                    allow_unvalidated_platform=args.allow_unvalidated_platform,
                )
                selected_provider = Path(adoption_preview["provider_path"])
                current_runtime = runtime_configuration_state(args.home)
                if current_runtime["status"] == "partial" or current_runtime["integrity"] in {"stale_or_modified", "invalid_manifest"}:
                    raise EngramMemoryError(
                        "provider adopt refuses partial or drifted runtime; run and review runtime repair separately"
                    )
                runtime_preview = ensure_user_runtime(
                    home=args.home, dry_run=True,
                    provider_path_override=selected_provider,
                )
                provider_result = adoption_preview
                provider_result["runtime"] = runtime_preview
                if not args.dry_run:
                    state_path = user_config_dir(args.home) / "provider-state.v1.json"
                    prior_state = state_path.read_bytes() if state_path.exists() else None
                    provider_result = provider_lifecycle.adopt_existing_provider(
                        support,
                        candidate=args.path,
                        home=args.home,
                        config_dir=user_config_dir(args.home),
                        selected_platform=args.platform,
                        yes=args.yes,
                        dry_run=False,
                        allow_unvalidated_platform=args.allow_unvalidated_platform,
                    )
                    try:
                        provider_result["runtime"] = ensure_user_runtime(
                            home=args.home,
                            provider_path_override=selected_provider,
                        )
                    except BaseException:
                        provider_lifecycle.restore_provider_state(
                            config_dir=user_config_dir(args.home), prior=prior_state
                        )
                        raise
            elif args.provider_command == "verify-bound":
                provider_result = provider_lifecycle.verify_bound_provider(
                    config_dir=args.config_dir,
                    expected_path=args.path,
                )
            else:
                provider_result = provider_lifecycle.mutate_provider(
                    args.provider_command, support, home=args.home,
                    bin_dir=selected_bin_dir, config_dir=user_config_dir(args.home),
                    selected_platform=args.platform,
                    maintenance_window=args.maintenance_window,
                    yes=args.yes, dry_run=args.dry_run,
                )
            print(json.dumps(provider_result, indent=2, sort_keys=True))
        elif args.command == "project" and args.project_command == "register":
            if args.from_remote:
                proposal = propose_project_from_remote(args.from_remote)
                try:
                    existing = project_by_remote(registry, proposal["remote"])
                except EngramMemoryError:
                    existing = None
                if existing:
                    # A remote legitimately maps to exactly one canonical project,
                    # so this stays idempotent and successful. Report explicitly
                    # when a requested --id was not the one recorded, instead of
                    # returning a different project as a bare success.
                    payload = {
                        "project": existing["id"],
                        "status": "already_registered",
                        "changed": False,
                        "proposal_source": "approved_git_remote",
                    }
                    requested = args.id or proposal["project"]
                    if requested.casefold() != existing["id"].casefold():
                        payload["requested_id"] = requested
                        payload["requested_id_honoured"] = False
                        print(
                            f"naos-engram-memory: this remote is already registered as "
                            f"{existing['id']!r}; the requested identifier {requested!r} was not "
                            "applied because one approved remote maps to one canonical project",
                            file=sys.stderr,
                        )
                    print(json.dumps(payload, indent=2, sort_keys=True))
                    return 0
                project_id = args.id or proposal["project"]
                remote = args.remote or proposal["remote"]
            else:
                if not args.remote or not args.id:
                    raise EngramMemoryError("explicit project register requires both --id and --remote")
                project_id, remote = args.id, args.remote
            confirm_write("update the local project registry", yes=args.yes, non_interactive=args.non_interactive, dry_run=args.dry_run)
            registry_path = args.registry
            if registry_path == DEFAULT_REGISTRY and not registry_path.exists() and not args.dry_run:
                ensure_user_runtime(home=Path.home())
            if registry_path == DEFAULT_REGISTRY and not registry_path.exists() and args.dry_run:
                registry_path = bundled_config_path("projects.json")
            result = register_project(
                registry_path,
                project_id=project_id,
                remote=remote,
                alias=args.alias,
                dry_run=args.dry_run,
                authorized_base=args.registry.parent,
            )
            result["proposal_source"] = "git_origin_repository_name" if args.from_remote else "explicit"
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "client" and args.client_command == "list":
            print(json.dumps(host_catalogue(read_config(args.catalogue, default_name="mcp-hosts.v1.json" if args.catalogue == DEFAULT_HOST_CATALOGUE else None)), indent=2, sort_keys=True))
        elif args.command == "client" and args.client_command == "render":
            print(render_client_adapter(
                registry,
                client=args.client,
                project=args.project,
                workspace=args.workspace,
                wrapper_command=args.wrapper,
                host_version=args.host_version,
            ), end="")
        elif args.command == "client" and args.client_command in {"install", "repair"}:
            confirm_write(f"{args.client_command} the local {args.client} project configuration", yes=args.yes, non_interactive=args.non_interactive, dry_run=args.dry_run)
            wrapper = args.wrapper or user_wrapper_path()
            result = install_client(
                registry, client=args.client, project=args.project, workspace=args.workspace,
                home=args.home,
                wrapper_path=wrapper, host_executable=args.host_executable,
                host_version=args.host_version, dry_run=args.dry_run,
                repair=args.client_command == "repair",
            )
            print(json.dumps(result, indent=2, sort_keys=True))
        elif args.command == "client" and args.client_command == "verify":
            wrapper = args.wrapper or user_wrapper_path()
            print(json.dumps(verify_client(
                registry, client=args.client, project=args.project, workspace=args.workspace,
                wrapper=wrapper, host_version=args.host_version,
                host_executable=args.host_executable,
            ), indent=2, sort_keys=True))
        elif args.command == "instruction" and args.instruction_command == "render":
            print(render_instruction(), end="")
        elif args.command == "instruction" and args.instruction_command == "install":
            confirm_write(f"merge managed lifecycle instructions into {args.host} project instructions", yes=args.yes, non_interactive=args.non_interactive, dry_run=args.dry_run)
            print(json.dumps(install_instruction(args.project, args.host, dry_run=args.dry_run), indent=2, sort_keys=True))
        elif args.command == "resolve":
            remote = args.remote or (git_origin(args.cwd) if args.cwd else None)
            project, source = resolve_project(registry, requested=args.project, remote=remote)
            print(json.dumps({"project": project, "source": source}, sort_keys=True))
        elif args.command == "render-client":
            print(render_client_adapter(
                registry,
                client=args.client,
                project=args.project,
                workspace=args.workspace,
                wrapper_command=args.wrapper,
                host_version=args.host_version,
            ), end="")
        elif args.command == "install-client":
            confirm_write(f"install the local {args.client} configuration", yes=args.yes, non_interactive=args.non_interactive, dry_run=args.dry_run)
            print(
                json.dumps(
                    install_client(
                        registry,
                        client=args.client,
                        project=args.project,
                        workspace=args.workspace,
                        home=args.home,
                        wrapper_path=args.wrapper,
                        host_executable=args.host_executable,
                        host_version=args.host_version,
                        dry_run=args.dry_run,
                    ),
                    indent=2,
                    sort_keys=True,
                )
            )
        elif args.command == "inventory":
            print(json.dumps(inventory(registry, args.cwd, args.engram_bin, home=args.home), indent=2, sort_keys=True))
        elif args.command == "release-status":
            print(json.dumps(release_status(read_config(args.support_manifest, default_name="release-support.json" if args.support_manifest == DEFAULT_SUPPORT else None)), indent=2, sort_keys=True))
        elif args.command == "supported-asset":
            print(json.dumps(supported_asset(read_config(args.support_manifest, default_name="release-support.json" if args.support_manifest == DEFAULT_SUPPORT else None), args.platform), sort_keys=True))
        return 0
    except (EngramMemoryError, provider_lifecycle.ProviderError) as exc:
        print(f"naos-engram-memory: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
