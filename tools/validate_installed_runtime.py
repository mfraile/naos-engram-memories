#!/usr/bin/env python3
"""Validate a built toolkit wheel and real Engram wrapper in disposable state.

The probe never reads a live Engram database or edits a real client. It builds
the current source without network access, installs it into a temporary virtual
environment, creates synthetic user configuration and a synthetic Git remote,
then exercises only MCP initialization, tool discovery, and
``mem_current_project``. An unregistered repository must fail closed before the
Engram MCP process starts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RuntimeValidationError(RuntimeError):
    """A required runtime acceptance condition failed."""


def validation_disposition(checks: dict[str, str], *, source_worktree_dirty: bool) -> str:
    if not all(value == "passed" for value in checks.values()):
        return "failed"
    return "candidate_passed_not_release_attestation" if source_worktree_dirty else "passed"


class StdioMcp:
    def __init__(self, command: list[str], *, cwd: Path, environment: dict[str, str]):
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if not self.process.stdin or not self.process.stdout:
            raise RuntimeValidationError("MCP stdio pipes were not created")
        self.request_id = 0
        self.responses: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def call(self, method: str, params: dict[str, Any], timeout: float = 15) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        assert self.process.stdin and self.process.stdout
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n"
        )
        self.process.stdin.flush()
        while True:
            try:
                line = self.responses.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeValidationError(f"timeout waiting for {method}")
            if line is None:
                stderr = self.process.stderr.read() if self.process.stderr else ""
                raise RuntimeValidationError(f"MCP process exited during {method}: {stderr[-1000:]}")
            response = json.loads(line)
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeValidationError(f"{method} returned {response['error']}")
            return response["result"]

    def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self.process.stdin
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdin:
            self.process.stdin.close()
        if self.process.stdout:
            self.process.stdout.close()
        if self.process.stderr:
            self.process.stderr.close()
        self.reader.join(timeout=5)


def run_checked(command: list[str], *, cwd: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeValidationError(
            f"command failed ({result.returncode}): {command[0]} {command[1] if len(command) > 1 else ''}; "
            f"stderr={result.stderr[-1000:]}"
        )
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def windows_archive_identity(
    source: Path, archive: Path, binary: Path, *, allow_experimental: bool = False
) -> dict[str, str]:
    support = json.loads((source / "config" / "release-support.json").read_text(encoding="utf-8"))
    assets = [
        release.get("platforms", {}).get("windows_amd64")
        for release in support.get("releases", [])
        if release.get("status") == "supported"
    ]
    assets = [
        asset
        for asset in assets
        if isinstance(asset, dict)
        and (
            asset.get("status") == "supported"
            or (allow_experimental and asset.get("status") == "experimental")
        )
    ]
    if len(assets) != 1:
        raise RuntimeValidationError("Windows archive validation requires one supported release asset")
    asset = assets[0]
    expected_name = asset.get("asset")
    expected_sha256 = asset.get("sha256")
    if archive.name != expected_name or not isinstance(expected_sha256, str):
        raise RuntimeValidationError("Windows archive does not match the supported release asset")
    archive_sha256 = sha256(archive)
    if archive_sha256 != expected_sha256:
        raise RuntimeValidationError("Windows archive checksum does not match release support")
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = [entry for entry in bundle.infolist() if not entry.is_dir()]
            normalized = [Path(entry.filename.replace("\\", "/")) for entry in entries]
            if any(path.is_absolute() or ".." in path.parts for path in normalized):
                raise RuntimeValidationError("Windows archive contains an unsafe path")
            matches = [entry for entry, path in zip(entries, normalized) if path.name == "engram.exe"]
            if len(matches) != 1 or len({path.as_posix() for path in normalized}) != len(normalized):
                raise RuntimeValidationError("Windows archive must contain one unambiguous engram.exe")
            extracted_sha256 = hashlib.sha256(bundle.read(matches[0])).hexdigest()
    except zipfile.BadZipFile as exc:
        raise RuntimeValidationError("Windows release asset is not a valid ZIP archive") from exc
    binary_sha256 = sha256(binary)
    if extracted_sha256 != binary_sha256:
        raise RuntimeValidationError("validated Windows binary does not match the supported archive")
    return {
        "engram_archive_sha256": archive_sha256,
        "engram_binary_sha256": binary_sha256,
    }


SOURCE_IGNORES = {".git", "__pycache__", "build", "dist"}
PACKAGE_INPUT_EXCLUDED_PREFIXES = {("docs", "evidence")}


def package_input_sha256(source: Path) -> str:
    """Hash package/source inputs while excluding explicitly generated evidence."""
    digest = hashlib.sha256()
    listed = subprocess.run(
        ["git", "-C", str(source), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    paths = (
        [source / raw.decode("utf-8") for raw in listed.stdout.split(b"\0") if raw]
        if listed.returncode == 0
        else list(source.rglob("*"))
    )
    for path in sorted(paths):
        relative = path.relative_to(source)
        if (
            any(part in SOURCE_IGNORES for part in relative.parts)
            or any(relative.parts[: len(prefix)] == prefix for prefix in PACKAGE_INPUT_EXCLUDED_PREFIXES)
            or path.suffix in {".pyc", ".pyo"}
            or any(part.endswith(".egg-info") for part in relative.parts)
        ):
            continue
        if path.is_symlink():
            raise RuntimeValidationError(f"source tree contains a symbolic link: {relative}")
        if not path.is_file():
            continue
        encoded = relative.as_posix().encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def source_identity(source: Path, *, expected_commit: str | None = None, expected_tree: str | None = None) -> dict[str, Any]:
    calculated_tree = package_input_sha256(source)
    if expected_tree is not None and expected_tree != calculated_tree:
        raise RuntimeValidationError("explicit source-tree hash does not match the validated source")
    commit = expected_commit
    git_tree: str | None = None
    dirty: bool | None = None
    git_commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    if git_commit.returncode == 0:
        observed_commit = git_commit.stdout.strip()
        if expected_commit is not None and expected_commit != observed_commit:
            raise RuntimeValidationError("explicit source commit does not match the Git checkout")
        commit = observed_commit
        tree_result = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD^{tree}"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        if tree_result.returncode != 0:
            raise RuntimeValidationError("Git source tree identity could not be resolved")
        git_tree = tree_result.stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        dirty = status.returncode != 0 or bool(status.stdout)
    if commit is None:
        raise RuntimeValidationError("source is not a Git checkout; pass --source-commit with the reviewed provenance")
    return {
        "source_commit": commit,
        "source_git_tree": git_tree,
        "package_input_sha256": calculated_tree,
        "package_input_exclusions": ["docs/evidence/**", ".git/**", "build/**", "dist/**", "**/__pycache__/**", "**/*.egg-info/**", "Git-ignored files"],
        "source_worktree_dirty": dirty,
    }


def run_probe(
    source: Path,
    engram_binary: Path,
    project: str,
    remote: str,
    *,
    provenance: dict[str, Any],
    allow_experimental_platform: bool = False,
) -> dict[str, Any]:
    checks: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="naos-engram-installed-runtime-") as temporary:
        root = Path(temporary)
        source_copy = root / "source"
        wheelhouse = root / "wheelhouse"
        venv = root / "venv"
        home = root / "home"
        config_home = root / "config-home"
        workspace = root / "registered-workspace"
        unregistered = root / "unregistered-workspace"
        shutil.copytree(
            source,
            source_copy,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.egg-info", "build", "dist"),
        )
        if allow_experimental_platform:
            support_path = source_copy / "config" / "release-support.json"
            support = json.loads(support_path.read_text(encoding="utf-8"))
            platform_name = "windows_amd64" if os.name == "nt" else None
            if platform_name is None:
                raise RuntimeValidationError("experimental-platform override is currently Windows-only")
            matching = [
                release.get("platforms", {}).get(platform_name)
                for release in support.get("releases", [])
            ]
            assets = [asset for asset in matching if isinstance(asset, dict)]
            if len(assets) != 1 or assets[0].get("status") != "experimental":
                raise RuntimeValidationError("experimental-platform override requires one experimental platform asset")
            assets[0]["status"] = "supported"
            support_path.write_text(json.dumps(support, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        wheelhouse.mkdir()
        environment = os.environ.copy()
        environment["PIP_NO_INDEX"] = "1"
        environment["PIP_CACHE_DIR"] = str(root / "pip-cache")
        run_checked(
            [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheelhouse)],
            cwd=source_copy,
            environment=environment,
        )
        wheels = list(wheelhouse.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeValidationError("wheel build did not produce exactly one artifact")
        wheel = wheels[0]
        checks["offline_wheel_build"] = "passed"

        run_checked([sys.executable, "-m", "venv", str(venv)], cwd=source_copy, environment=environment)
        venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        executable = venv / ("Scripts/naos-engram-memory.exe" if os.name == "nt" else "bin/naos-engram-memory")
        bootstrap_executable = venv / (
            "Scripts/naos-engram-memory-bootstrap.exe"
            if os.name == "nt"
            else "bin/naos-engram-memory-bootstrap"
        )
        run_checked(
            [str(venv_python), "-m", "pip", "install", "--no-deps", str(wheel)],
            cwd=source_copy,
            environment=environment,
        )
        if not bootstrap_executable.is_file():
            raise RuntimeValidationError("installed wheel omitted the bootstrap entrypoint")
        for asset in (
            "bootstrap.py",
            "bootstrap.sh",
            "bootstrap.ps1",
            "engram_mcp_wrapper.sh",
            "engram_mcp_wrapper.ps1",
        ):
            verified = run_checked(
                [str(bootstrap_executable), "--verify-asset", asset],
                cwd=root,
                environment=environment,
            )
            if not json.loads(verified.stdout).get("verified"):
                raise RuntimeValidationError(f"installed asset failed integrity verification: {asset}")
        checks["installed_asset_integrity"] = "passed"
        checks["clone_free_wheel_install"] = "passed"

        runtime_environment = {
            **environment,
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home),
        }
        if os.name == "nt":
            config_home = home / "AppData" / "Roaming"
            runtime_environment["APPDATA"] = str(config_home)
            runtime_environment["XDG_CONFIG_HOME"] = str(home / ".config")
        adopted = json.loads(run_checked(
            [str(executable), "provider", "adopt", "--path", str(engram_binary), "--yes"],
            cwd=source_copy,
            environment=runtime_environment,
        ).stdout)
        if os.path.realpath(adopted.get("provider_path", "")) != os.path.realpath(engram_binary):
            raise RuntimeValidationError("provider adoption did not record the selected real executable")
        provider_state_path = config_home / "naos-engram-memory" / "provider-state.v1.json"
        state = json.loads(provider_state_path.read_text(encoding="utf-8"))
        if (
            state.get("provider_path") != os.path.realpath(engram_binary)
            or state.get("binary_sha256") != sha256(engram_binary)
            or state.get("provenance", {}).get("kind") != "adopted_existing"
        ):
            raise RuntimeValidationError("provider adoption state is not bound to path, hash, and provenance")
        checks["provider_adoption_binding"] = "passed"
        run_checked(
            [str(executable), "runtime", "install", "--yes", "--non-interactive"],
            cwd=source_copy,
            environment=runtime_environment,
        )
        wrapper = (
            config_home / "naos-engram-memory" / "bin" / "engram-mcp-wrapper.cmd"
            if os.name == "nt"
            else home / ".local" / "bin" / "engram-mcp-wrapper"
        )
        registry = config_home / "naos-engram-memory" / "projects.json"
        required_runtime = (
            registry,
            config_home / "naos-engram-memory" / "release-support.json",
            config_home / "naos-engram-memory" / "mcp-hosts.v1.json",
            config_home / "naos-engram-memory" / "engram_memory.py",
            config_home / "naos-engram-memory" / "provider.py",
            config_home / "naos-engram-memory" / "clients" / "cursor.mcp.json",
            config_home / "naos-engram-memory" / "instructions" / "engram-project-lifecycle.md",
            wrapper,
        )
        if not all(path.is_file() for path in required_runtime):
            raise RuntimeValidationError("runtime install did not create every durable user runtime asset")
        if str(venv) in str(registry) or str(config_home) not in str(registry):
            raise RuntimeValidationError("user registry was placed inside the package environment")
        wrapper_text = (
            wrapper.with_name("engram-mcp-wrapper.ps1").read_text(encoding="utf-8")
            if os.name == "nt"
            else wrapper.read_text(encoding="utf-8")
        )
        if str(config_home / "naos-engram-memory") not in wrapper_text:
            raise RuntimeValidationError("installed wrapper does not bind the durable user config directory")
        if str(venv_python.resolve()) not in wrapper_text:
            raise RuntimeValidationError("installed wrapper does not bind the selected absolute interpreter")
        if os.path.realpath(engram_binary) not in wrapper_text:
            raise RuntimeValidationError("installed wrapper does not bind the verified adopted provider")
        if "/.config/engram-memory" in wrapper_text:
            raise RuntimeValidationError("installed wrapper retains the legacy config namespace")
        checks["durable_user_runtime"] = "passed"

        managed_tool = config_home / "naos-engram-memory" / "engram_memory.py"
        managed_provider = config_home / "naos-engram-memory" / "provider.py"
        collision = root / "collision" / "tools"
        collision.mkdir(parents=True)
        (collision / "__init__.py").write_text("", encoding="utf-8")
        (collision / "provider.py").write_text("raise RuntimeError('untrusted provider imported')\n", encoding="utf-8")
        binding_environment = {**runtime_environment, "PYTHONPATH": str(collision.parent)}
        binding = run_checked(
            [
                str(venv_python), "-c",
                "import importlib.util,sys; p=sys.argv[1]; s=importlib.util.spec_from_file_location('managed_runtime',p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print(m.provider_lifecycle.__file__)",
                str(managed_tool),
            ],
            cwd=root, environment=binding_environment,
        ).stdout.strip()
        if os.path.realpath(binding) != os.path.realpath(managed_provider):
            raise RuntimeValidationError("managed runtime did not bind its sibling provider.py")
        checks["managed_provider_binding"] = "passed"

        seed = json.loads(registry.read_text(encoding="utf-8"))
        if seed.get("projects") != [] or "example-product" in registry.read_text(encoding="utf-8"):
            raise RuntimeValidationError("installed durable registry is not an empty production seed")
        run_checked(
            [
                str(executable), "project", "register", "--id", project,
                "--remote", remote, "--yes", "--non-interactive",
            ],
            cwd=source_copy,
            environment=runtime_environment,
        )

        workspace.mkdir()
        unregistered.mkdir()
        run_checked(["git", "init", "-q", str(workspace)], cwd=root, environment=runtime_environment)
        run_checked(["git", "-C", str(workspace), "remote", "add", "origin", remote], cwd=root, environment=runtime_environment)
        run_checked(["git", "init", "-q", str(unregistered)], cwd=root, environment=runtime_environment)

        mcp = StdioMcp([str(wrapper)], cwd=workspace, environment=runtime_environment)
        try:
            mcp.call(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "naos-engram-installed-runtime-validator", "version": "1"},
                },
            )
            mcp.notify("notifications/initialized", {})
            checks["wrapper_mcp_initialize"] = "passed"
            tools = mcp.call("tools/list", {}).get("tools", [])
            names = {item.get("name") for item in tools if isinstance(item, dict)}
            if "mem_current_project" not in names:
                raise RuntimeValidationError("installed wrapper did not expose mem_current_project")
            checks["wrapper_tools_list"] = "passed"
            current = mcp.call("tools/call", {"name": "mem_current_project", "arguments": {}})
            if project not in json.dumps(current):
                raise RuntimeValidationError("installed wrapper returned the wrong canonical project")
            checks["wrapper_current_project"] = "passed"
        finally:
            mcp.close()

        failed = subprocess.run(
            [str(wrapper)],
            cwd=unregistered,
            env=runtime_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        if failed.returncode != 64:
            raise RuntimeValidationError(
                f"unregistered repository did not fail closed with exit 64 (got {failed.returncode})"
            )
        checks["unregistered_project_fails_closed"] = "passed"

        version = f"engram {adopted['installed_version']}"
        return {
            "schema_version": 1,
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "synthetic_only": True,
            "platform": {"system": platform.system(), "architecture": platform.machine()},
            "python": platform.python_version(),
            "toolkit_version": "1.0.0",
            "engram_version": version,
            "engram_binary_sha256": sha256(engram_binary),
            "network_accessed": None,
            "network_access_status": "not_observed_during_external_provider_adoption",
            "wheel_sha256": sha256(wheel),
            **provenance,
            "checks": checks,
            "disposition": validation_disposition(
                checks,
                source_worktree_dirty=bool(provenance["source_worktree_dirty"]),
            ),
            "limitations": [
                "This validates the provider and installed stdio wrapper, not a specific GUI or coding-agent host.",
                "No live memory database, real client configuration, cloud transport, or production project was used.",
                "External provider adoption executes the selected binary's version command; its network behavior was not observed by the validator.",
                *(["The source checkout was dirty, so this is diagnostic candidate evidence and not final release attestation."] if provenance["source_worktree_dirty"] else []),
                *(["Windows experimental platform status was temporarily overridden only inside the disposable validation copy; release promotion remains a separate decision."] if allow_experimental_platform else []),
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--engram-binary", type=Path, required=True)
    parser.add_argument("--engram-archive", type=Path, help="official Windows ZIP used to bind archive and executable identity")
    parser.add_argument("--project", default="example-product")
    parser.add_argument("--remote", default="https://github.com/example-org/example-product.git")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--source-commit", help="expected Git commit, or explicit provenance for a non-Git source")
    parser.add_argument("--source-tree", help="expected SHA-256 of package inputs (generated evidence excluded)")
    parser.add_argument(
        "--allow-experimental-platform",
        action="store_true",
        help="temporarily promote the Windows platform only inside the disposable candidate copy",
    )
    args = parser.parse_args()
    if not args.engram_binary.is_file() or not os.access(args.engram_binary, os.X_OK):
        raise SystemExit("Engram binary is missing or not executable")
    try:
        source = args.source.resolve()
        provenance = source_identity(source, expected_commit=args.source_commit, expected_tree=args.source_tree)
        if args.engram_archive:
            provenance.update(
                windows_archive_identity(
                    source,
                    args.engram_archive.resolve(),
                    args.engram_binary.resolve(),
                    allow_experimental=args.allow_experimental_platform,
                )
            )
        report = run_probe(
            source,
            args.engram_binary.resolve(),
            args.project,
            args.remote,
            provenance=provenance,
            allow_experimental_platform=args.allow_experimental_platform,
        )
    except (RuntimeValidationError, subprocess.TimeoutExpired) as exc:
        print(f"installed runtime validation failed: {exc}", file=sys.stderr)
        return 1
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        after_publication = source_identity(source, expected_commit=report["source_commit"])
        stable_fields = ("source_commit", "source_git_tree", "package_input_sha256")
        if any(after_publication[field] != report[field] for field in stable_fields):
            print("installed runtime validation failed: evidence publication changed its claimed provenance", file=sys.stderr)
            return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["disposition"] in {"passed", "candidate_passed_not_release_attestation"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
