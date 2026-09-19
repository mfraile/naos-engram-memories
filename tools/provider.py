"""Explicit, checksum-pinned Engram provider lifecycle operations.

The public CLI calls this module only after explicit owner confirmation.  It
never resolves a latest release, reads memory content, or mutates MCP client
configuration.  Tests can inject the downloader and process probe so no live
provider, network, database, or client process is touched.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import platform as host_platform
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from typing import Any, Callable
from urllib.request import Request, urlopen


class ProviderError(ValueError):
    """A provider lifecycle safety or policy condition failed closed."""


Downloader = Callable[[str, Path], None]
ActiveProbe = Callable[[Path], bool]


def _reject_custom_data_dir() -> None:
    if os.environ.get("ENGRAM_DATA_DIR"):
        raise ProviderError(
            "custom ENGRAM_DATA_DIR is unsupported for managed provider maintenance in this release"
        )


def _reject_link(path: Path, *, label: str) -> None:
    if path.is_symlink():
        raise ProviderError(f"{label} must not be a symbolic link")


def _reject_link_chain(path: Path, *, label: str) -> None:
    absolute = Path(os.path.abspath(path.expanduser()))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        # /var is a root-owned compatibility alias on macOS.  No nested or
        # environment-selected alias receives this exception.
        if (
            sys.platform == "darwin"
            and current == Path("/var")
            and current.is_symlink()
            and current.resolve() == Path("/private/var")
        ):
            continue
        _reject_link(current, label=label)


def _sha256(path: Path) -> str:
    _reject_link_chain(path, label="provider file")
    if not path.is_file():
        raise ProviderError("provider file must be a regular file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_provider_state(paths: dict[str, Path]) -> dict[str, Any] | None:
    state_path = paths["state"]
    _reject_link_chain(state_path, label="provider state")
    if not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProviderError("provider state is unreadable; run repair only after manual review") from exc
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ProviderError("provider state has an unsupported schema")
    required_strings = ("version", "binary_sha256", "provider_path")
    if any(not isinstance(state.get(name), str) or not state[name] for name in required_strings):
        raise ProviderError("provider state is malformed; manual review required")
    if not os.path.isabs(state["provider_path"]):
        raise ProviderError("provider state has no valid absolute provider path")
    checksum = state["binary_sha256"]
    if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum.lower()):
        raise ProviderError("provider state has no valid binary checksum")
    provenance = state.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("kind") not in {"managed_install", "adopted_existing"}:
        raise ProviderError("provider state has invalid provenance; manual review required")
    return state


def _require_managed_ownership(paths: dict[str, Path], binary: Path) -> dict[str, Any]:
    state = _read_provider_state(paths)
    if state is None:
        raise ProviderError("provider is not toolkit-owned; install it through the toolkit or adopt it as external")
    provenance = state["provenance"]
    recorded_path = state["provider_path"]
    if (
        provenance.get("kind") != "managed_install"
        or os.path.realpath(recorded_path) != os.path.realpath(binary)
        or not binary.is_file()
        or _sha256(binary) != state["binary_sha256"]
    ):
        raise ProviderError(
            "adopted external providers are not toolkit-owned; upgrade externally and re-adopt"
        )
    return state


def configured_provider_path(config_dir: Path, default: Path) -> Path:
    """Return the recorded real provider path, or the managed-install default."""
    state_path = config_dir / "provider-state.v1.json"
    _reject_link_chain(state_path, label="provider state")
    state = None
    if state_path.exists():
        try:
            value = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProviderError("provider state is unreadable; manual review required") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ProviderError("provider state has an unsupported schema")
        state = value
    recorded = state.get("provider_path") if state else None
    if recorded is None:
        return Path(os.path.abspath(default.expanduser()))
    if not isinstance(recorded, str) or not os.path.isabs(recorded):
        raise ProviderError("provider state has no valid absolute provider path")
    return Path(recorded)


def verify_bound_provider(*, config_dir: Path, expected_path: Path) -> dict[str, Any]:
    """Verify path and bytes without executing the provider or reading memory."""
    if not config_dir.is_absolute():
        raise ProviderError("verified provider configuration directory must be absolute")
    _reject_link_chain(config_dir, label="provider configuration directory")
    try:
        expected = Path(os.path.abspath(expected_path.expanduser())).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProviderError("verified provider executable could not be resolved safely") from exc
    paths = provider_paths(Path.home(), expected.parent, config_dir)
    state = _read_provider_state(paths)
    if state is None:
        raise ProviderError("provider is not verified; install or explicitly adopt an existing provider")
    recorded = state.get("provider_path")
    if not isinstance(recorded, str) or not os.path.isabs(recorded):
        raise ProviderError("managed wrapper provider path does not match verified provider state")
    try:
        recorded_real = Path(recorded).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProviderError("verified provider executable could not be resolved safely") from exc
    if recorded_real != expected:
        raise ProviderError("managed wrapper provider path does not match verified provider state")
    _reject_link_chain(expected, label="verified provider executable")
    if not expected.is_file() or not os.access(expected, os.X_OK):
        raise ProviderError("verified provider executable is missing or not executable")
    if _sha256(expected) != state.get("binary_sha256"):
        raise ProviderError("verified provider executable changed after installation or adoption")
    return {
        "schema_version": 1,
        "status": "verified",
        "provider_path": str(expected),
        "version": state.get("version"),
        "provider_executed": False,
        "memory_content_read": False,
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _reject_link_chain(path, label="provider state")
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_link_chain(path.parent, label="provider state directory")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.provider-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _reject_link_chain(path, label="provider state")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalized_architecture(raw: str) -> str:
    value = raw.lower()
    if value in {"arm64", "aarch64"}:
        return "arm64"
    if value in {"x86_64", "amd64"}:
        return "amd64"
    return value


def platform_key() -> str:
    if os.name == "nt":
        # Windows must report its real architecture like every other platform.
        # Returning windows_amd64 unconditionally made an ARM64 host silently
        # select the x86_64 asset, which no allowlist entry covers. Under WOW64
        # PROCESSOR_ARCHITECTURE reports the emulated architecture, so prefer
        # PROCESSOR_ARCHITEW6432 when the process is itself emulated.
        raw = (
            os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE")
            or host_platform.machine()
        )
        return f"windows_{_normalized_architecture(raw)}"
    operating_system = sys.platform
    operating_system = "darwin" if operating_system == "darwin" else "linux" if operating_system.startswith("linux") else operating_system
    return f"{operating_system}_{_normalized_architecture(host_platform.machine())}"


def approved_asset(support: dict[str, Any], selected_platform: str) -> dict[str, str]:
    if support.get("schema_version") != 1:
        raise ProviderError("unsupported release support schema")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for release in support.get("releases", []):
        platform_entry = release.get("platforms", {}).get(selected_platform)
        if (
            release.get("status") == "supported"
            and isinstance(platform_entry, dict)
            and platform_entry.get("status") == "supported"
        ):
            matches.append((release, platform_entry))
    if len(matches) != 1:
        raise ProviderError(f"no single supported Engram release is approved for {selected_platform}")
    release, asset = matches[0]
    checksum = asset.get("sha256")
    filename = asset.get("asset")
    version = release.get("version")
    if not (
        isinstance(checksum, str) and checksum
        and isinstance(filename, str) and filename
        and isinstance(version, str) and version
    ):
        raise ProviderError(f"approved release is incomplete for {selected_platform}")
    if not len(checksum) == 64 or any(character not in "0123456789abcdef" for character in checksum.lower()):
        raise ProviderError("approved release checksum is not a SHA-256 digest")
    return {
        "version": version,
        "asset": filename,
        "sha256": checksum.lower(),
        "url": f"https://github.com/Gentleman-Programming/engram/releases/download/v{version}/{filename}",
        "platform": selected_platform,
    }


def validated_platforms(support: dict[str, Any]) -> list[str]:
    """Return every platform with an allowlisted asset, for exact error text."""
    names: set[str] = set()
    for release in support.get("releases", []):
        if release.get("status") != "supported":
            continue
        for name, entry in (release.get("platforms") or {}).items():
            if isinstance(entry, dict) and entry.get("status") == "supported":
                names.add(name)
    return sorted(names)


def supported_release_version(support: dict[str, Any]) -> str:
    """Return the single supported release version, ignoring platform assets.

    Adoption binds a provider the owner already has, so it needs the approved
    *version* but not a platform asset or its checksum. This keeps the version
    pin intact on a platform that has no allowlisted download.
    """
    if support.get("schema_version") != 1:
        raise ProviderError("unsupported release support schema")
    versions = [
        release.get("version")
        for release in support.get("releases", [])
        if release.get("status") == "supported"
    ]
    exact = [version for version in versions if isinstance(version, str) and version]
    if len(exact) != 1:
        raise ProviderError("no single supported Engram release version is approved")
    return exact[0]


def _user_config_dir(home: Path) -> Path:
    configured = os.environ.get("NAOS_ENGRAM_MEMORY_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        default_home = Path.home().expanduser()
        base = os.environ.get("APPDATA") if home.expanduser() == default_home else None
        return Path(base or home / "AppData" / "Roaming") / "naos-engram-memory"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))) / "naos-engram-memory"


def provider_paths(
    home: Path, bin_dir: Path | None = None, config_dir: Path | None = None
) -> dict[str, Path]:
    home = Path(os.path.abspath(home.expanduser()))
    binary_directory = Path(os.path.abspath((bin_dir or home / ".local" / "bin").expanduser()))
    configuration_directory = Path(
        os.path.abspath((config_dir or _user_config_dir(home)).expanduser())
    )
    return {
        "binary": binary_directory / ("engram.exe" if os.name == "nt" else "engram"),
        "rollback_root": binary_directory / ".naos-engram-memory-rollbacks",
        "database": home / ".engram" / "engram.db",
        "database_backup_root": home / ".engram" / "backups",
        "state": configuration_directory / "provider-state.v1.json",
        "maintenance_lock": configuration_directory / ".provider-maintenance.lock",
        "client_leases": configuration_directory / ".provider-client-leases",
    }


def _active_client_leases(paths: dict[str, Path]) -> list[str]:
    """Return opaque lease names without inspecting or deleting them.

    A lease may outlive a crashed client.  Automatic stale-lease deletion would
    turn an ambiguous state into permission to replace a live provider, so any
    entry blocks maintenance until an owner reviews it manually.
    """
    root = paths["client_leases"]
    _reject_link_chain(root, label="provider client lease directory")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ProviderError("provider client lease path is not a directory")
    try:
        return sorted(entry.name for entry in root.iterdir())
    except OSError as exc:
        raise ProviderError("provider client leases could not be inspected") from exc


@contextmanager
def _exclusive_maintenance(paths: dict[str, Path]):
    """Acquire the per-user maintenance lock and release only our own token."""
    lock = paths["maintenance_lock"]
    parent = lock.parent
    _reject_link_chain(parent, label="provider maintenance directory")
    parent.mkdir(parents=True, exist_ok=True)
    _reject_link_chain(parent, label="provider maintenance directory")
    token = secrets.token_hex(16)
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ProviderError(
            "provider maintenance is already active or requires manual lock review"
        ) from exc
    except OSError as exc:
        raise ProviderError("provider maintenance lock could not be acquired") from exc

    owner = lock / "owner.json"
    acquired = False
    try:
        descriptor = os.open(owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            json.dump({
                "schema_version": 1,
                "token": token,
                "pid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat(),
            }, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        acquired = True
        yield
    finally:
        # Never recursively remove a lock or a lock whose ownership changed.
        # Ambiguous artifacts deliberately remain fail-closed for manual review.
        if acquired:
            try:
                value = json.loads(owner.read_text(encoding="utf-8"))
                if value.get("token") == token and set(lock.iterdir()) == {owner}:
                    owner.unlink()
                    lock.rmdir()
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
        else:
            try:
                if lock.is_dir() and not any(lock.iterdir()):
                    lock.rmdir()
            except OSError:
                pass


def provider_status(
    support: dict[str, Any], *, home: Path, bin_dir: Path | None = None,
    config_dir: Path | None = None, selected_platform: str | None = None,
) -> dict[str, Any]:
    selected_platform = selected_platform or platform_key()
    asset: dict[str, str] | None = None
    if selected_platform.startswith("windows_"):
        try:
            asset = approved_asset(support, selected_platform)
        except ProviderError:
            windows_paths = provider_paths(home, bin_dir, config_dir)
            windows_state = _read_provider_state(windows_paths)
            windows_binary = windows_paths["binary"]
            return {
                "schema_version": 1,
                "platform": selected_platform,
                "status": "experimental_mutation_disabled",
                "binary_detected": windows_binary.is_file(),
                "installed_version": windows_state.get("version") if windows_state else None,
                "read_only": True,
                "network_accessed": False,
            }
    asset = asset or approved_asset(support, selected_platform)
    paths = provider_paths(home, bin_dir, config_dir)
    for path in paths.values():
        _reject_link_chain(path, label="provider lifecycle path")
    state = _read_provider_state(paths)
    recorded_path = state.get("provider_path") if state else None
    if recorded_path is not None and (not isinstance(recorded_path, str) or not os.path.isabs(recorded_path)):
        raise ProviderError("provider state has no valid absolute provider path")
    binary = Path(recorded_path) if isinstance(recorded_path, str) else paths["binary"]
    _reject_link_chain(binary, label="provider executable")
    detected = binary.is_file()
    executable = detected and os.access(binary, os.X_OK)
    observed_sha256 = _sha256(binary) if detected else None
    state_checksum = state.get("binary_sha256") if state else None
    state_consistent = bool(executable and state_checksum and observed_sha256 == state_checksum)
    managed_owned = bool(
        state_consistent
        and state
        and state["provenance"].get("kind") == "managed_install"
        and os.path.realpath(state["provider_path"]) == os.path.realpath(paths["binary"])
    )
    return {
        "schema_version": 1,
        "platform": selected_platform,
        "approved_version": asset["version"],
        "binary_detected": detected,
        "binary_executable": executable,
        "installed_version": state.get("version") if state_consistent and state else None,
        "provider_provenance": state.get("provenance", {}).get("kind") if state_consistent and state else None,
        "binary_integrity": "state_verified" if state_consistent else "unmanaged_or_changed" if detected else "absent",
        "upgrade_available": bool(managed_owned and state and state.get("version") != asset["version"]),
        "rollback_available": bool(managed_owned and _latest_rollback(paths["rollback_root"]) is not None),
        "upgrade_owner": "toolkit" if managed_owned else "external" if state_consistent else None,
        "toolkit_mutation_available": managed_owned,
        "read_only": True,
        "provider_executed": False,
        "memory_content_read": False,
        "network_accessed": False,
        "network_access_status": "not_accessed",
        "external_commands_may_access_network": False,
    }


def _resolve_existing_candidate(candidate: str | Path) -> tuple[Path, str, bool]:
    raw = os.fspath(candidate)
    if not raw or any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise ProviderError("existing provider candidate is empty or contains control characters")
    source = "explicit_path" if os.path.isabs(os.path.expanduser(raw)) or os.sep in raw else "path_lookup"
    located = os.path.expanduser(raw) if source == "explicit_path" else shutil.which(raw)
    if not located:
        raise ProviderError("existing provider candidate was not found")
    lexical = Path(os.path.abspath(located))
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProviderError("existing provider candidate could not be resolved safely") from exc
    _reject_link_chain(resolved, label="resolved existing provider")
    if resolved.name not in {"engram", "engram.exe"}:
        raise ProviderError("existing provider must resolve to an executable named engram")
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ProviderError("existing provider must resolve to a regular executable")
    return resolved, source, lexical != resolved


def adopt_existing_provider(
    support: dict[str, Any],
    *,
    candidate: str | Path,
    home: Path,
    config_dir: Path | None = None,
    selected_platform: str | None = None,
    yes: bool,
    dry_run: bool,
    allow_unvalidated_platform: bool = False,
) -> dict[str, Any]:
    """Explicitly adopt a local provider without copying or replacing it."""
    _reject_custom_data_dir()
    selected_platform = selected_platform or platform_key()
    if not yes and not dry_run:
        raise ProviderError("existing provider adoption requires explicit --yes confirmation")
    # Adoption binds an executable the owner already installed, so a platform
    # without an allowlisted download asset can still be bound on explicit
    # request. Only the platform-asset allowlist is bypassed: the approved
    # version check and the SHA-256 path binding below are unchanged, and
    # install/upgrade/rollback stay refused because they need a real asset.
    platform_validation = "allowlisted_asset"
    try:
        approved_version = approved_asset(support, selected_platform)["version"]
    except ProviderError:
        if not allow_unvalidated_platform:
            raise ProviderError(
                f"no single supported Engram release is approved for {selected_platform}; "
                f"validated platforms are {', '.join(validated_platforms(support))}. "
                "To bind a provider you already installed on this platform, rerun "
                "'provider adopt' with --allow-unvalidated-platform --yes; toolkit "
                "install, upgrade, and rollback remain unavailable there"
            ) from None
        approved_version = supported_release_version(support)
        platform_validation = "unvalidated_owner_approved"
    paths = provider_paths(home, config_dir=config_dir)
    resolved, source, followed_symlink = _resolve_existing_candidate(candidate)
    before_path = resolved
    before_hash = _sha256(resolved)
    observed_version = _binary_version(resolved, approved_version)
    # Detect candidate retargeting or byte replacement across validation.
    resolved_after, _source_after, _followed_after = _resolve_existing_candidate(candidate)
    if resolved_after != before_path or _sha256(resolved_after) != before_hash:
        raise ProviderError("existing provider changed while it was being validated")
    state = {
        "schema_version": 1,
        "version": observed_version,
        "binary_sha256": before_hash,
        "provider_path": str(resolved),
        "provenance": {
            "kind": "adopted_existing",
            "candidate_source": source,
            "symlink_resolved": followed_symlink,
            "platform": selected_platform,
            "platform_validation": platform_validation,
        },
    }
    result = {
        "schema_version": 1,
        "operation": "adopt",
        "status": "dry_run" if dry_run else "adopted",
        "changed": not dry_run,
        "dry_run": dry_run,
        "installed_version": observed_version,
        "provider_path": str(resolved),
        "provider_provenance": "adopted_existing",
        "symlink_resolved": followed_symlink,
        "platform": selected_platform,
        "platform_validation": platform_validation,
        "provider_executed": True,
        "memory_content_read": False,
        "network_accessed": None,
        "network_access_status": "not_observed",
        "external_commands_may_access_network": True,
    }
    if not dry_run:
        with _exclusive_maintenance(paths):
            if _active_client_leases(paths):
                raise ProviderError(
                    "an Engram MCP client lease exists; close clients or manually review a stale lease"
                )
            final_path, _final_source, _final_followed = _resolve_existing_candidate(candidate)
            if final_path != before_path or _sha256(final_path) != before_hash:
                raise ProviderError("existing provider changed before adoption state was recorded")
            _atomic_json(paths["state"], state)
    return result


def restore_provider_state(*, config_dir: Path, prior: bytes | None) -> None:
    """Restore exact pre-adoption state after a failed wrapper publication."""
    path = config_dir / "provider-state.v1.json"
    _reject_link_chain(path, label="provider state rollback")
    if prior is None:
        path.unlink(missing_ok=True)
        return
    descriptor, temporary_name = tempfile.mkstemp(
        dir=config_dir, prefix=".provider-state-rollback-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(prior)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_regular_bytes(path: Path, prior: bytes | None, *, mode: int = 0o755) -> None:
    """Atomically restore exact bytes, or remove a newly-created target."""
    _reject_link_chain(path, label="provider transaction restoration")
    if prior is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.restore-", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(prior)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _windows_process_running(image_name: str) -> bool:
    """Return whether a process with this exact image name is running (Windows only)."""
    tasklist = shutil.which("tasklist")
    if not tasklist:
        raise ProviderError("could not prove Engram process inactivity; tasklist is unavailable")
    result = subprocess.run(
        [tasklist, "/FI", f"IMAGENAME eq {image_name}", "/NH"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
    )
    if result.returncode != 0:
        raise ProviderError("could not prove Engram process inactivity; tasklist failed")
    return image_name.lower() in result.stdout.lower()


def _windows_file_locked(path: Path) -> bool:
    """Best-effort exclusive-open probe for Windows database lock detection."""
    if not path.exists():
        return False
    try:
        descriptor = os.open(str(path), os.O_RDWR)
    except OSError:
        return True
    else:
        os.close(descriptor)
        return False


def active_engram_use(database: Path) -> bool:
    """Conservatively detect any Engram process or open provider store.

    A maintenance operation is rare, so false-positive refusal is safer than
    replacing a binary or copying SQLite files while serve, cloud, TUI, sync,
    an MCP host, or another Engram writer is active.
    """
    if os.name == "nt":
        if _windows_process_running("engram.exe"):
            return True
        for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
            _reject_link_chain(candidate, label="Engram database activity probe")
            if _windows_file_locked(candidate):
                return True
        return False
    pgrep = shutil.which("pgrep")
    if pgrep:
        result = subprocess.run(
            [pgrep, "-f", r"(^|[/[:space:]])engram([[:space:]]|$)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode != 1:
            raise ProviderError("could not prove Engram process inactivity; pgrep failed")
    else:
        raise ProviderError("could not prove Engram process inactivity; pgrep is unavailable")
    lsof = shutil.which("lsof")
    if not lsof:
        if any(candidate.exists() for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm"))):
            raise ProviderError("could not prove provider-store inactivity; lsof is unavailable")
        return False
    for candidate in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        _reject_link_chain(candidate, label="Engram database activity probe")
        if not candidate.exists():
            continue
        opened = subprocess.run(
            [lsof, "-t", "--", str(candidate)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if opened.returncode == 0:
            return True
        if opened.returncode != 1:
            raise ProviderError("could not prove provider-store inactivity; lsof failed")
    return False


def _download_https(url: str, target: Path) -> None:
    if not url.startswith("https://"):
        raise ProviderError("provider downloads require an exact HTTPS release URL")
    request = Request(url, headers={"User-Agent": "naos-engram-memories/0.1"})
    with urlopen(request, timeout=60) as response, target.open("wb") as output:  # noqa: S310 - exact manifest URL
        if getattr(response, "status", 200) != 200:
            raise ProviderError("provider release download returned a non-success status")
        shutil.copyfileobj(response, output)


def _allocate_directory(parent: Path, prefix: str) -> Path:
    _reject_link_chain(parent, label="backup directory")
    parent.mkdir(parents=True, exist_ok=True)
    _reject_link_chain(parent, label="backup directory")
    return Path(tempfile.mkdtemp(dir=parent, prefix=prefix))


def _copy_regular(source: Path, target: Path) -> None:
    _reject_link_chain(source, label="backup source")
    if not source.is_file():
        raise ProviderError("backup source must be a regular file")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if _sha256(source) != _sha256(target):
        target.unlink(missing_ok=True)
        raise ProviderError("backup checksum verification failed")


def _backup_database(paths: dict[str, Path]) -> str | None:
    database = paths["database"]
    _reject_link_chain(database, label="Engram database")
    candidates = [database, Path(f"{database}-wal"), Path(f"{database}-shm")]
    for candidate in candidates:
        _reject_link_chain(candidate, label="Engram database backup source")
    if not database.exists():
        return None
    backup = _allocate_directory(
        paths["database_backup_root"],
        "provider-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-")
    )
    copied: list[dict[str, str]] = []
    try:
        for source in candidates:
            if source.exists():
                destination = backup / source.name
                _copy_regular(source, destination)
                copied.append({"file": source.name, "sha256": _sha256(destination)})
        if not copied or copied[0]["file"] != database.name:
            raise ProviderError("database backup is incomplete")
        _atomic_json(backup / "backup-record.json", {
            "schema_version": 1,
            "files": copied,
            "complete": True,
        })
    except BaseException:
        shutil.rmtree(backup, ignore_errors=True)
        raise
    return backup.name


def _atomic_replace(source: Path, target: Path) -> None:
    _reject_link_chain(source, label="staged provider binary")
    _reject_link_chain(target, label="provider binary target")
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_link_chain(target.parent, label="provider binary directory")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=".engram-install-", suffix=".tmp"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        temporary.chmod(0o755)
        if _sha256(temporary) != _sha256(source):
            raise ProviderError("staged provider binary checksum verification failed")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _binary_version(binary: Path, expected: str | None = None) -> str:
    try:
        result = subprocess.run(
            [str(binary), "version"], capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderError("installed provider did not complete its version check") from exc
    if result.returncode != 0:
        raise ProviderError("installed provider version check failed")
    matches = [
        match.group(1)
        for line in result.stdout.splitlines()
        if (match := re.fullmatch(r"engram\s+v?(\d+\.\d+\.\d+)\s*", line))
    ]
    if len(matches) != 1:
        raise ProviderError(
            "installed provider must report exactly one canonical version line on stdout"
        )
    observed = matches[0]
    if expected and observed != expected:
        raise ProviderError("installed provider did not report the manifest-approved version")
    return observed


def _extract_binary_zip(archive: Path, destination: Path) -> Path:
    try:
        with zipfile.ZipFile(archive) as bundle:
            matches = []
            for info in bundle.infolist():
                member_path = Path(info.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ProviderError("approved provider archive contains an unsafe member")
                if not info.is_dir() and member_path.name == "engram.exe":
                    matches.append(info)
            if len(matches) != 1:
                raise ProviderError("approved provider archive must contain exactly one regular engram.exe binary")
            target = destination / "engram.exe"
            with bundle.open(matches[0]) as stream:
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
                with os.fdopen(descriptor, "wb") as output:
                    shutil.copyfileobj(stream, output)
                    output.flush()
                    os.fsync(output.fileno())
            target.chmod(0o755)
            return target
    except zipfile.BadZipFile as exc:
        raise ProviderError("approved provider asset is not a valid zip archive") from exc


def _extract_binary(archive: Path, destination: Path) -> Path:
    if archive.suffix.lower() == ".zip":
        return _extract_binary_zip(archive, destination)
    try:
        with tarfile.open(archive, mode="r:gz") as bundle:
            matches = []
            for member in bundle.getmembers():
                member_path = Path(member.name)
                if member.issym() or member.islnk() or member_path.is_absolute() or ".." in member_path.parts:
                    raise ProviderError("approved provider archive contains an unsafe member")
                if member.isfile() and member_path.name == "engram":
                    matches.append(member)
            if len(matches) != 1:
                raise ProviderError("approved provider archive must contain exactly one regular engram binary")
            stream = bundle.extractfile(matches[0])
            if stream is None:
                raise ProviderError("approved provider binary could not be read")
            target = destination / "engram"
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            with stream, os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(stream, output)
                output.flush()
                os.fsync(output.fileno())
            target.chmod(0o755)
            return target
    except tarfile.TarError as exc:
        raise ProviderError("approved provider asset is not a valid tar.gz archive") from exc


def _retain_binary(binary: Path, rollback_root: Path, *, version: str | None) -> Path:
    backup = _allocate_directory(
        rollback_root,
        "binary-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ-")
    )
    retained = backup / "engram"
    try:
        _copy_regular(binary, retained)
        retained.chmod(0o500)
        _atomic_json(backup / "record.json", {
            "schema_version": 1,
            "status": "available",
            "version": version,
            "binary_sha256": _sha256(retained),
        })
    except BaseException:
        shutil.rmtree(backup, ignore_errors=True)
        raise
    return backup


def _latest_rollback(rollback_root: Path) -> Path | None:
    _reject_link_chain(rollback_root, label="provider rollback directory")
    if not rollback_root.exists():
        return None
    candidates: list[Path] = []
    for record in rollback_root.glob("binary-*/record.json"):
        _reject_link_chain(record, label="provider rollback record")
        try:
            value = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("schema_version") == 1 and value.get("status") == "available":
            candidates.append(record.parent)
    return sorted(candidates)[-1] if candidates else None


def _require_mutation_preconditions(
    *, selected_platform: str, maintenance_window: bool, yes: bool,
) -> None:
    if not maintenance_window:
        raise ProviderError("provider mutation requires --maintenance-window after closing all Engram MCP clients")
    if not yes:
        raise ProviderError("provider mutation requires explicit --yes confirmation")


def _mutate_provider_locked(
    operation: str,
    *,
    asset: dict[str, str],
    paths: dict[str, Path],
    binary: Path,
    rollback: Path | None,
    result: dict[str, Any],
    downloader: Downloader,
) -> dict[str, Any]:
    """Perform a provider mutation while the caller holds maintenance."""
    database_backup = _backup_database(paths)
    result["database_backup"] = database_backup or "not_required"
    current_state = _read_provider_state(paths) if binary.exists() else None
    current_version = current_state["version"] if current_state else None
    if operation == "rollback":
        assert rollback is not None
        record_path = rollback / "record.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        retained = rollback / "engram"
        if _sha256(retained) != record.get("binary_sha256"):
            raise ProviderError("retained rollback binary checksum mismatch")
        recorded_version = record.get("version")
        if not isinstance(recorded_version, str) or not __import__("re").fullmatch(r"\d+\.\d+\.\d+", recorded_version):
            raise ProviderError("retained rollback record has no normalized provider version")
        current_state = _read_provider_state(paths)
        if current_state and recorded_version == current_state.get("version") and _sha256(retained) != current_state.get("binary_sha256"):
            raise ProviderError("retained rollback metadata is inconsistent with managed provider state")
        # The retained binary was content-addressed when its managed state was
        # recorded. Avoid executing `version`: upstream may perform a network
        # check, while the retained record plus checksum already establishes
        # the approved rollback identity.
        restored_version = recorded_version
        rollback_prior_binary = binary.read_bytes()
        rollback_prior_state = paths["state"].read_bytes()
        prior_record = record_path.read_bytes()
        new_rollback = _retain_binary(binary, paths["rollback_root"], version=current_version)
        result["binary_backup"] = new_rollback.name
        replaced = False
        try:
            _atomic_replace(retained, binary)
            replaced = True
            _atomic_json(paths["state"], {
                "schema_version": 1,
                "version": restored_version,
                "binary_sha256": _sha256(binary),
                "provider_path": str(binary),
                "provenance": {"kind": "managed_install"},
            })
            record["status"] = "consumed"
            _atomic_json(record_path, record)
        except BaseException:
            if replaced:
                _restore_regular_bytes(binary, rollback_prior_binary)
                _restore_regular_bytes(paths["state"], rollback_prior_state, mode=0o600)
                _restore_regular_bytes(record_path, prior_record, mode=0o600)
            shutil.rmtree(new_rollback, ignore_errors=True)
            raise
        result.update(status="rolled_back", changed=True, installed_version=restored_version)
        return result

    with tempfile.TemporaryDirectory(prefix="naos-engram-provider-") as temporary_name:
        temporary = Path(temporary_name)
        archive = temporary / asset["asset"]
        downloader(asset["url"], archive)
        result["network_accessed"] = downloader is _download_https
        if _sha256(archive) != asset["sha256"]:
            raise ProviderError("provider asset checksum does not match the release-support manifest")
        staged = _extract_binary(archive, temporary)
        prior_binary = binary.read_bytes() if binary.exists() else None
        prior_state = paths["state"].read_bytes() if paths["state"].exists() else None
        retained_backup: Path | None = None
        if binary.exists():
            retained_backup = _retain_binary(binary, paths["rollback_root"], version=current_version)
            result["binary_backup"] = retained_backup.name
        replaced = False
        try:
            _atomic_replace(staged, binary)
            replaced = True
            if _sha256(binary) != _sha256(staged):
                raise ProviderError("installed provider checksum changed during publication")
            installed_version = asset["version"]
            _atomic_json(paths["state"], {
                "schema_version": 1,
                "version": installed_version,
                "binary_sha256": _sha256(binary),
                "provider_path": str(binary),
                "provenance": {"kind": "managed_install"},
            })
        except BaseException:
            if replaced:
                _restore_regular_bytes(binary, prior_binary)
                _restore_regular_bytes(paths["state"], prior_state, mode=0o600)
            if retained_backup is not None:
                shutil.rmtree(retained_backup, ignore_errors=True)
            raise
    result.update(status="installed" if operation == "install" else "upgraded", changed=True, installed_version=asset["version"])
    return result


def mutate_provider(
    operation: str,
    support: dict[str, Any],
    *,
    home: Path,
    bin_dir: Path | None = None,
    config_dir: Path | None = None,
    selected_platform: str | None = None,
    maintenance_window: bool,
    yes: bool,
    dry_run: bool,
    downloader: Downloader = _download_https,
    active_probe: ActiveProbe = active_engram_use,
) -> dict[str, Any]:
    _reject_custom_data_dir()
    if operation not in {"install", "upgrade", "rollback"}:
        raise ProviderError("unsupported provider operation")
    selected_platform = selected_platform or platform_key()
    asset = approved_asset(support, selected_platform)
    paths = provider_paths(home, bin_dir, config_dir)
    for path in paths.values():
        _reject_link_chain(path, label="provider lifecycle path")
    _require_mutation_preconditions(
        selected_platform=selected_platform,
        maintenance_window=maintenance_window,
        yes=yes,
    )
    binary = paths["binary"]
    if operation in {"upgrade", "rollback"}:
        _require_managed_ownership(paths, binary)
    if operation == "install" and binary.exists():
        raise ProviderError("provider binary already exists; use provider upgrade")
    if operation in {"upgrade", "rollback"} and not binary.is_file():
        raise ProviderError(f"provider {operation} requires an existing regular provider binary")
    rollback = _latest_rollback(paths["rollback_root"]) if operation == "rollback" else None
    if operation == "rollback" and rollback is None:
        raise ProviderError("no retained provider binary is available for rollback")
    result = {
        "schema_version": 1,
        "operation": operation,
        "platform": selected_platform,
        "approved_version": asset["version"],
        "dry_run": dry_run,
        "network_accessed": False,
        "provider_executed": False,
        "database_backup": "planned" if paths["database"].exists() else "not_required",
        "binary_backup": "planned" if binary.exists() else "not_required",
        "changed": False,
    }
    if dry_run:
        # Dry-run is strictly read-only and network-free. It still refuses when
        # activity is observable, but it never creates a maintenance lock.
        if paths["maintenance_lock"].exists() or _active_client_leases(paths):
            raise ProviderError("provider maintenance or an MCP client lease appears active")
        if active_probe(paths["database"]):
            raise ProviderError("an Engram process or provider-store user appears active; close it and retry")
        result["status"] = "dry_run"
        result["download"] = "planned_exact_manifest_asset" if operation != "rollback" else "not_required"
        return result

    with _exclusive_maintenance(paths):
        leases = _active_client_leases(paths)
        if leases:
            raise ProviderError(
                "an Engram MCP client lease exists; close clients or manually review a stale lease"
            )
        if active_probe(paths["database"]):
            raise ProviderError("an Engram process or provider-store user appears active; close it and retry")
        if operation in {"upgrade", "rollback"}:
            _require_managed_ownership(paths, binary)
        # Re-evaluate mutable filesystem conditions after the exclusive lock.
        if operation == "install" and binary.exists():
            raise ProviderError("provider binary already exists; use provider upgrade")
        if operation in {"upgrade", "rollback"} and not binary.is_file():
            raise ProviderError(f"provider {operation} requires an existing regular provider binary")
        rollback = _latest_rollback(paths["rollback_root"]) if operation == "rollback" else None
        if operation == "rollback" and rollback is None:
            raise ProviderError("no retained provider binary is available for rollback")
        return _mutate_provider_locked(
            operation,
            asset=asset,
            paths=paths,
            binary=binary,
            rollback=rollback,
            result=result,
            downloader=downloader,
        )
