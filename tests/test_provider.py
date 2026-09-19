from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools import engram_memory as memory
from tools import provider


PLATFORM = "linux_amd64"


def provider_script(version: str) -> bytes:
    return f"#!/bin/sh\nprintf 'engram {version}\\n'\n".encode()


def fixture_archive(root: Path, version: str) -> Path:
    archive = root / f"engram-{version}.tar.gz"
    content = provider_script(version)
    info = tarfile.TarInfo("engram")
    info.size = len(content)
    info.mode = 0o755
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.addfile(info, io.BytesIO(content))
    return archive


def support_for(archive: Path, version: str) -> dict:
    return {
        "schema_version": 1,
        "releases": [{
            "version": version,
            "status": "supported",
            "platforms": {PLATFORM: {
                "status": "supported",
                "asset": archive.name,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }},
        }],
    }


def local_downloader(source: Path, calls: list[str]):
    def download(url: str, target: Path) -> None:
        calls.append(url)
        target.write_bytes(source.read_bytes())
    return download


class ProviderLifecycleTests(unittest.TestCase):
    def test_verify_bound_provider_requires_absolute_configuration_directory(self) -> None:
        for config_dir in (Path("relative-config"), Path("~/relative-config")):
            with self.subTest(config_dir=config_dir), self.assertRaisesRegex(
                provider.ProviderError, "configuration directory must be absolute"
            ):
                provider.verify_bound_provider(
                    config_dir=config_dir,
                    expected_path=Path("relative-provider"),
                )

    def test_binary_version_accepts_one_canonical_stdout_and_ignores_stderr(self) -> None:
        completed = __import__("subprocess").CompletedProcess(
            ["engram", "version"], 0,
            stdout="engram 1.20.0\n",
            stderr=(
                "update failed via 192.0.2.1:1.2.3\n"
                "https://example.invalid/releases/v9.8.7\n"
                "2026-08-13T17:12:11.123Z retry 4.5.6\n"
            ),
        )
        with mock.patch.object(provider.subprocess, "run", return_value=completed):
            self.assertEqual("1.20.0", provider._binary_version(Path("/fixture/engram"), "1.20.0"))

    def test_binary_version_rejects_missing_duplicate_and_conflicting_canonical_lines(self) -> None:
        outputs = (
            "version 1.20.0\n",
            "engram 1.20.0\nengram 1.20.0\n",
            "engram 1.20.0\nengram v1.21.0\n",
        )
        for stdout in outputs:
            with self.subTest(stdout=stdout):
                completed = __import__("subprocess").CompletedProcess(
                    ["engram", "version"], 0, stdout=stdout, stderr="engram 1.20.0\n"
                )
                with mock.patch.object(provider.subprocess, "run", return_value=completed):
                    with self.assertRaisesRegex(provider.ProviderError, "exactly one canonical"):
                        provider._binary_version(Path("/fixture/engram"), "1.20.0")

    def _managed_wrapper_fixture(self, root: Path):
        from tools import engram_memory as memory

        home = root / "home"
        workspace = root / "workspace"
        workspace.mkdir(parents=True)
        memory.ensure_user_runtime(home=home)
        registry = memory.user_config_dir(home) / "projects.json"
        registry.write_text(json.dumps({
            "schema_version": 1,
            "projects": [{
                "id": "fixture-project", "aliases": [],
                "remotes": ["https://github.com/example/fixture-project.git"],
            }],
        }))
        __import__("subprocess").run(["git", "init", "-q", str(workspace)], check=True)
        __import__("subprocess").run([
            "git", "-C", str(workspace), "remote", "add", "origin",
            "https://github.com/example/fixture-project.git",
        ], check=True)
        return home, workspace, memory.user_config_dir(home), memory.user_bin_dir(home), memory.user_wrapper_path(home)

    def _record_managed_provider(self, *, home: Path, config_dir: Path, binary: Path, version: str = "1.0.0") -> None:
        state = config_dir / "provider-state.v1.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({
            "schema_version": 1,
            "version": version,
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "provider_path": str(binary),
            "provenance": {"kind": "managed_install"},
        }))

    def test_managed_runtime_contains_provider_and_direct_script_can_start(self) -> None:
        from tools import engram_memory as memory

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            result = memory.ensure_user_runtime(home=root)
            self.assertTrue(result["changed"])
            managed = memory.user_config_dir(root)
            self.assertTrue((managed / "provider.py").is_file())
            completed = __import__("subprocess").run(
                [os.sys.executable, str(managed / "engram_memory.py"), "--help"],
                text=True, stdout=__import__("subprocess").PIPE,
                stderr=__import__("subprocess").PIPE, check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("provider", completed.stdout)

    def test_managed_wrapper_binds_provider_outside_gui_path(self) -> None:
        from tools import engram_memory as memory

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            memory.ensure_user_runtime(home=root)
            wrapper_path = memory.user_wrapper_path(root)
            wrapper = (
                wrapper_path.with_name("engram-mcp-wrapper.ps1").read_text(encoding="utf-8")
                if os.name == "nt"
                else wrapper_path.read_text(encoding="utf-8")
            )
            expected = str(memory.user_bin_dir(root) / ("engram.exe" if os.name == "nt" else "engram"))
            self.assertIn(expected, wrapper)
            if os.name != "nt":
                self.assertLess(wrapper.index('if [[ -n "${ENGRAM_BIN:-}" ]]'), wrapper.index(expected))
                self.assertLess(wrapper.index(expected), wrapper.index("command -v engram"))

    @unittest.skipIf(os.name == "nt", "POSIX wrapper fixture")
    def test_managed_wrapper_uses_bound_provider_when_gui_path_excludes_it(self) -> None:
        from tools import engram_memory as memory

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home = root / "home"
            workspace = root / "workspace"
            workspace.mkdir(parents=True)
            memory.ensure_user_runtime(home=home)
            registry = memory.user_config_dir(home) / "projects.json"
            registry.write_text(json.dumps({
                "schema_version": 1,
                "projects": [{
                    "id": "fixture-project", "aliases": [],
                    "remotes": ["https://github.com/example/fixture-project.git"],
                }],
            }))
            __import__("subprocess").run(["git", "init", "-q", str(workspace)], check=True)
            __import__("subprocess").run([
                "git", "-C", str(workspace), "remote", "add", "origin",
                "https://github.com/example/fixture-project.git",
            ], check=True)
            provider_binary = memory.user_bin_dir(home) / "engram"
            provider_binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
            provider_binary.chmod(0o755)
            self._record_managed_provider(
                home=home, config_dir=memory.user_config_dir(home), binary=provider_binary,
                version="1.0.0",
            )
            wrapper = memory.user_wrapper_path(home)
            minimal_path = root / "minimal-path"
            minimal_path.mkdir()
            for command in (
                "bash", "date", "git", "kill", "mkdir", "mktemp", "readlink",
                "rmdir", "uname",
            ):
                located = __import__("shutil").which(command)
                if located:
                    (minimal_path / command).symlink_to(located)
            completed = __import__("subprocess").run(
                [str(wrapper)], cwd=workspace,
                env={"HOME": str(home), "PATH": str(minimal_path)},
                text=True, stdout=__import__("subprocess").PIPE,
                stderr=__import__("subprocess").PIPE, check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("mcp", completed.stdout.splitlines())
            self.assertIn("--project=fixture-project", completed.stdout.splitlines())

    @unittest.skipIf(os.name == "nt", "POSIX executable-bit fixture is not meaningful on Windows")
    def test_status_is_read_only_and_never_executes_provider(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            support = support_for(archive, "1.20.0")
            binary = root / "bin" / "engram"
            binary.parent.mkdir()
            binary.write_bytes(provider_script("SHOULD-NOT-RUN"))
            binary.chmod(0o755)
            result = provider.provider_status(
                support, home=root, bin_dir=binary.parent,
                selected_platform=PLATFORM,
            )
            self.assertEqual("unmanaged_or_changed", result["binary_integrity"])
            self.assertFalse(result["provider_executed"])
            self.assertFalse(result["network_accessed"])

    @unittest.skipIf(os.name == "nt", "POSIX executable-bit fixture is not meaningful on Windows")
    def test_status_refuses_state_verified_for_non_executable_binary(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            binary = bin_dir / "engram"
            binary.write_bytes(provider_script("1.20.0"))
            binary.chmod(0o644)
            state = provider.provider_paths(root, bin_dir)["state"]
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({
                "schema_version": 1,
                "version": "1.20.0",
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "provider_path": str(binary),
                "provenance": {"kind": "managed_install"},
            }))
            result = provider.provider_status(
                support_for(archive, "1.20.0"), home=root, bin_dir=bin_dir,
                selected_platform=PLATFORM,
            )
            self.assertFalse(result["binary_executable"])
            self.assertEqual("unmanaged_or_changed", result["binary_integrity"])

    def test_malformed_provider_state_fails_closed_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            state = provider.provider_paths(root)["state"]
            state.parent.mkdir(parents=True)
            malformed_values = [
                {"schema_version": 1, "version": "1.20.0", "binary_sha256": "0" * 64, "provider_path": "/fixture/engram", "provenance": "bad"},
                {"schema_version": 1, "version": "1.20.0", "binary_sha256": "bad", "provider_path": "/fixture/engram", "provenance": {"kind": "managed_install"}},
                {"schema_version": 1, "version": "1.20.0", "binary_sha256": "0" * 64, "provider_path": "relative", "provenance": {"kind": "managed_install"}},
            ]
            for value in malformed_values:
                with self.subTest(value=value):
                    state.write_text(json.dumps(value))
                    with self.assertRaises(provider.ProviderError):
                        provider.provider_status(
                            support_for(archive, "1.20.0"), home=root,
                            selected_platform=PLATFORM,
                        )

    def test_upgrade_revalidates_state_inside_maintenance_lock(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            binary = bin_dir / "engram"
            binary.write_bytes(provider_script("1.0.0"))
            binary.chmod(0o755)
            paths = provider.provider_paths(root, bin_dir)
            self._record_managed_provider(home=root, config_dir=paths["state"].parent, binary=binary)
            archive = fixture_archive(root, "2.0.0")
            original_lock = provider._exclusive_maintenance

            @__import__("contextlib").contextmanager
            def state_changed_after_lock(observed_paths):
                with original_lock(observed_paths):
                    state_value = json.loads(observed_paths["state"].read_text())
                    state_value["provenance"] = {"kind": "adopted_existing"}
                    provider._atomic_json(observed_paths["state"], state_value)
                    yield

            before = binary.read_bytes()
            with mock.patch.object(provider, "_exclusive_maintenance", state_changed_after_lock):
                with self.assertRaisesRegex(provider.ProviderError, "malformed|not toolkit-owned"):
                    provider.mutate_provider(
                        "upgrade", support_for(archive, "2.0.0"), home=root,
                        bin_dir=bin_dir, selected_platform=PLATFORM,
                        maintenance_window=True, yes=True, dry_run=False,
                        downloader=local_downloader(archive, []),
                        active_probe=lambda _database: False,
                    )
            self.assertEqual(before, binary.read_bytes())
            self.assertFalse(paths["database_backup_root"].exists())

    def test_install_state_failure_restores_absence_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            paths = provider.provider_paths(root, root / "bin")
            original = provider._atomic_json

            def fail_state(path, value):
                if path == paths["state"]:
                    raise OSError("synthetic-state-failure")
                return original(path, value)

            with mock.patch.object(provider, "_atomic_json", side_effect=fail_state):
                with self.assertRaisesRegex(OSError, "synthetic-state-failure"):
                    provider.mutate_provider(
                        "install", support_for(archive, "1.20.0"), home=root,
                        bin_dir=root / "bin", selected_platform=PLATFORM,
                        maintenance_window=True, yes=True, dry_run=False,
                        downloader=local_downloader(archive, []),
                        active_probe=lambda _database: False,
                    )
            self.assertFalse(paths["binary"].exists())
            self.assertFalse(paths["state"].exists())

    @unittest.skipIf(os.name == "nt", "POSIX provider fixture")
    def test_upgrade_state_failure_restores_binary_state_and_no_orphan_backup(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            binary = bin_dir / "engram"
            binary.write_bytes(provider_script("1.0.0"))
            binary.chmod(0o755)
            paths = provider.provider_paths(root, bin_dir)
            self._record_managed_provider(home=root, config_dir=paths["state"].parent, binary=binary)
            binary_before = binary.read_bytes()
            state_before = paths["state"].read_bytes()
            archive = fixture_archive(root, "2.0.0")
            original = provider._atomic_json

            def fail_state(path, value):
                if path == paths["state"]:
                    raise OSError("synthetic-state-failure")
                return original(path, value)

            with mock.patch.object(provider, "_atomic_json", side_effect=fail_state):
                with self.assertRaisesRegex(OSError, "synthetic-state-failure"):
                    provider.mutate_provider(
                        "upgrade", support_for(archive, "2.0.0"), home=root,
                        bin_dir=bin_dir, selected_platform=PLATFORM,
                        maintenance_window=True, yes=True, dry_run=False,
                        downloader=local_downloader(archive, []),
                        active_probe=lambda _database: False,
                    )
            self.assertEqual(binary_before, binary.read_bytes())
            self.assertEqual(state_before, paths["state"].read_bytes())
            self.assertIsNone(provider._latest_rollback(paths["rollback_root"]))

    @unittest.skipIf(os.name == "nt", "POSIX provider fixture")
    def test_rollback_record_failure_restores_all_prior_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            binary = bin_dir / "engram"
            binary.write_bytes(provider_script("2.0.0"))
            binary.chmod(0o755)
            paths = provider.provider_paths(root, bin_dir)
            self._record_managed_provider(
                home=root, config_dir=paths["state"].parent, binary=binary,
                version="2.0.0",
            )
            old = root / "old-engram"
            old.write_bytes(provider_script("1.0.0"))
            old.chmod(0o755)
            rollback = provider._retain_binary(old, paths["rollback_root"], version="1.0.0")
            record_path = rollback / "record.json"
            binary_before = binary.read_bytes()
            state_before = paths["state"].read_bytes()
            record_before = record_path.read_bytes()
            archive = fixture_archive(root, "2.0.0")
            original = provider._atomic_json

            def fail_consumed_record(path, value):
                if path == record_path and value.get("status") == "consumed":
                    raise OSError("synthetic-record-failure")
                return original(path, value)

            with mock.patch.object(provider, "_atomic_json", side_effect=fail_consumed_record):
                with self.assertRaisesRegex(OSError, "synthetic-record-failure"):
                    provider.mutate_provider(
                        "rollback", support_for(archive, "2.0.0"), home=root,
                        bin_dir=bin_dir, selected_platform=PLATFORM,
                        maintenance_window=True, yes=True, dry_run=False,
                        active_probe=lambda _database: False,
                    )
            self.assertEqual(binary_before, binary.read_bytes())
            self.assertEqual(state_before, paths["state"].read_bytes())
            self.assertEqual(record_before, record_path.read_bytes())
            available = list(paths["rollback_root"].glob("binary-*/record.json"))
            self.assertEqual([record_path], available)

    def test_dry_run_requires_confirmation_and_has_no_download_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            support = support_for(archive, "1.20.0")
            calls: list[str] = []
            result = provider.mutate_provider(
                "install", support, home=root, bin_dir=root / "bin",
                selected_platform=PLATFORM, maintenance_window=True, yes=True,
                dry_run=True, downloader=local_downloader(archive, calls),
                active_probe=lambda _database: False,
            )
            self.assertEqual("dry_run", result["status"])
            self.assertEqual([], calls)
            self.assertFalse((root / "bin").exists())
            for maintenance, yes in ((False, True), (True, False)):
                with self.assertRaises(provider.ProviderError):
                    provider.mutate_provider(
                        "install", support, home=root, bin_dir=root / "bin",
                        selected_platform=PLATFORM, maintenance_window=maintenance,
                        yes=yes, dry_run=True, active_probe=lambda _database: False,
                    )

    def test_active_process_or_store_refuses_before_download_or_backup(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            calls: list[str] = []
            database = root / ".engram" / "engram.db"
            database.parent.mkdir()
            database.write_bytes(b"synthetic-db")
            with self.assertRaisesRegex(provider.ProviderError, "appears active"):
                provider.mutate_provider(
                    "install", support_for(archive, "1.20.0"), home=root,
                    bin_dir=root / "bin", selected_platform=PLATFORM,
                    maintenance_window=True, yes=True, dry_run=False,
                    downloader=local_downloader(archive, calls),
                    active_probe=lambda observed: observed == database,
                )
            self.assertEqual([], calls)
            self.assertFalse((root / ".engram" / "backups").exists())

    @unittest.skipIf(os.name == "nt", "POSIX wrapper fixture")
    def test_maintenance_lock_serializes_mutation_and_refuses_wrapper_start(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home, workspace, config_dir, bin_dir, wrapper = self._managed_wrapper_fixture(root)
            provider_binary = bin_dir / "engram"
            provider_calls = root / "provider-calls.log"
            provider_binary.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = version ]; then printf 'engram 1.0.0\\n'; exit 0; fi\n"
                f"printf '%s\\n' \"$*\" >> {json.dumps(str(provider_calls))}\n",
                encoding="utf-8",
            )
            provider_binary.chmod(0o755)
            self._record_managed_provider(home=home, config_dir=config_dir, binary=provider_binary)
            archive = fixture_archive(root, "2.0.0")
            support = support_for(archive, "2.0.0")
            entered = threading.Event()
            release = threading.Event()
            outcome: dict[str, object] = {}

            def delayed_download(_url: str, target: Path) -> None:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("fixture downloader timed out")
                target.write_bytes(archive.read_bytes())

            def mutate() -> None:
                try:
                    outcome["result"] = provider.mutate_provider(
                        "upgrade", support, home=home, bin_dir=bin_dir,
                        config_dir=config_dir, selected_platform=PLATFORM,
                        maintenance_window=True, yes=True, dry_run=False,
                        downloader=delayed_download,
                        active_probe=lambda _database: False,
                    )
                except BaseException as exc:  # captured for the main test thread
                    outcome["error"] = exc

            worker = threading.Thread(target=mutate, daemon=True)
            worker.start()
            self.assertTrue(entered.wait(10), "provider mutation did not reach downloader")
            paths = provider.provider_paths(home, bin_dir, config_dir)
            self.assertTrue(paths["maintenance_lock"].is_dir())

            completed = __import__("subprocess").run(
                [str(wrapper)], cwd=workspace, env={**os.environ, "HOME": str(home)},
                text=True, stdout=__import__("subprocess").PIPE,
                stderr=__import__("subprocess").PIPE, check=False,
            )
            self.assertEqual(75, completed.returncode, completed.stderr)
            self.assertIn("maintenance", completed.stderr)
            self.assertFalse(provider_calls.exists(), "wrapper spawned provider during maintenance")

            with self.assertRaisesRegex(provider.ProviderError, "maintenance is already active"):
                provider.mutate_provider(
                    "upgrade", support, home=home, bin_dir=bin_dir,
                    config_dir=config_dir, selected_platform=PLATFORM,
                    maintenance_window=True, yes=True, dry_run=False,
                    downloader=local_downloader(archive, []),
                    active_probe=lambda _database: False,
                )
            release.set()
            worker.join(10)
            self.assertFalse(worker.is_alive())
            self.assertNotIn("error", outcome)
            self.assertEqual("upgraded", outcome["result"]["status"])
            self.assertFalse(paths["maintenance_lock"].exists())

    @unittest.skipIf(os.name == "nt", "POSIX wrapper fixture")
    def test_wrapper_lease_blocks_maintenance_until_provider_exit(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home, workspace, config_dir, bin_dir, wrapper = self._managed_wrapper_fixture(root)
            paths = provider.provider_paths(home, bin_dir, config_dir)
            ready = root / "provider-ready"
            release = root / "provider-release"
            provider_binary = bin_dir / "engram"
            provider_binary.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = version ]; then printf 'engram 1.0.0\\n'; exit 0; fi\n"
                f"set -- {json.dumps(str(paths['client_leases']))}/client.*\n"
                "[ -d \"$1\" ] || exit 90\n"
                f": > {json.dumps(str(ready))}\n"
                f"while [ ! -f {json.dumps(str(release))} ]; do sleep 0.05; done\n",
                encoding="utf-8",
            )
            provider_binary.chmod(0o755)
            self._record_managed_provider(home=home, config_dir=config_dir, binary=provider_binary)
            process = __import__("subprocess").Popen(
                [str(wrapper)], cwd=workspace, env={**os.environ, "HOME": str(home)},
                text=True, stdout=__import__("subprocess").PIPE,
                stderr=__import__("subprocess").PIPE,
            )
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "wrapper provider did not observe its client lease")
                self.assertTrue(provider._active_client_leases(paths))
                archive = fixture_archive(root, "2.0.0")
                with self.assertRaisesRegex(provider.ProviderError, "MCP client lease"):
                    provider.mutate_provider(
                        "upgrade", support_for(archive, "2.0.0"), home=home,
                        bin_dir=bin_dir, config_dir=config_dir,
                        selected_platform=PLATFORM, maintenance_window=True,
                        yes=True, dry_run=False,
                        downloader=local_downloader(archive, []),
                        active_probe=lambda _database: False,
                    )
                self.assertFalse(paths["maintenance_lock"].exists())
                self.assertTrue(provider._active_client_leases(paths))
            finally:
                release.touch()
                stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(0, process.returncode, stderr)
            self.assertEqual([], provider._active_client_leases(paths))

    def test_maintenance_lock_is_released_after_mutation_failure(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            paths = provider.provider_paths(root, root / "bin")

            def fail_download(_url: str, _target: Path) -> None:
                raise OSError("synthetic download failure")

            with self.assertRaisesRegex(OSError, "synthetic download failure"):
                provider.mutate_provider(
                    "install", support_for(archive, "1.20.0"), home=root,
                    bin_dir=root / "bin", selected_platform=PLATFORM,
                    maintenance_window=True, yes=True, dry_run=False,
                    downloader=fail_download,
                    active_probe=lambda _database: False,
                )
            self.assertFalse(paths["maintenance_lock"].exists())

    def test_unknown_client_lease_blocks_without_automatic_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            paths = provider.provider_paths(root, root / "bin")
            stale = paths["client_leases"] / "client.unknown"
            stale.mkdir(parents=True)
            with self.assertRaisesRegex(provider.ProviderError, "MCP client lease"):
                provider.mutate_provider(
                    "install", support_for(archive, "1.20.0"), home=root,
                    bin_dir=root / "bin", selected_platform=PLATFORM,
                    maintenance_window=True, yes=True, dry_run=False,
                    downloader=local_downloader(archive, []),
                    active_probe=lambda _database: False,
                )
            self.assertTrue(stale.is_dir())
            self.assertFalse(paths["maintenance_lock"].exists())

    def test_ambiguous_maintenance_lock_blocks_without_automatic_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            paths = provider.provider_paths(root, root / "bin")
            paths["maintenance_lock"].mkdir(parents=True)
            (paths["maintenance_lock"] / "unknown-owner").write_text("preserve")
            with self.assertRaisesRegex(provider.ProviderError, "manual lock review"):
                provider.mutate_provider(
                    "install", support_for(archive, "1.20.0"), home=root,
                    bin_dir=root / "bin", selected_platform=PLATFORM,
                    maintenance_window=True, yes=True, dry_run=False,
                    downloader=local_downloader(archive, []),
                    active_probe=lambda _database: False,
                )
            self.assertEqual(
                "preserve",
                (paths["maintenance_lock"] / "unknown-owner").read_text(),
            )

    def test_symlinked_client_lease_root_fails_closed_without_external_write(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            paths = provider.provider_paths(root, root / "bin")
            external = root / "external"
            external.mkdir()
            paths["client_leases"].parent.mkdir(parents=True)
            try:
                paths["client_leases"].symlink_to(external, target_is_directory=True)
            except OSError as exc:
                if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink creation requires Developer Mode or elevation")
                raise
            with self.assertRaisesRegex(provider.ProviderError, "symbolic link"):
                provider.mutate_provider(
                    "install", support_for(archive, "1.20.0"), home=root,
                    bin_dir=root / "bin", selected_platform=PLATFORM,
                    maintenance_window=True, yes=True, dry_run=False,
                    downloader=local_downloader(archive, []),
                    active_probe=lambda _database: False,
                )
            self.assertEqual([], list(external.iterdir()))

    @unittest.skipIf(os.name == "nt", "POSIX process/store probe fixture")
    def test_activity_probe_detects_any_engram_command_and_open_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            database = root / "engram.db"
            database.write_bytes(b"db")
            sidecar = Path(f"{database}-wal")
            sidecar.write_bytes(b"wal")
            with mock.patch.object(provider.shutil, "which", side_effect=lambda name: f"/fixture/{name}"), mock.patch.object(
                provider.subprocess, "run",
                side_effect=[__import__("subprocess").CompletedProcess([], 0)],
            ) as runner:
                self.assertTrue(provider.active_engram_use(database))
                self.assertEqual("pgrep", Path(runner.call_args_list[0].args[0][0]).name)
                self.assertIn("engram", runner.call_args_list[0].args[0][2])
            with mock.patch.object(
                provider.shutil, "which",
                side_effect=lambda name: f"/fixture/{name}",
            ), mock.patch.object(
                provider.subprocess, "run",
                side_effect=[
                    __import__("subprocess").CompletedProcess([], 1),
                    __import__("subprocess").CompletedProcess([], 1),
                    __import__("subprocess").CompletedProcess([], 0),
                ],
            ) as runner:
                self.assertTrue(provider.active_engram_use(database))
                observed = [call.args[0][-1] for call in runner.call_args_list]
                self.assertIn(str(sidecar), observed)

    @unittest.skipIf(os.name == "nt", "POSIX process/store probe fixture")
    def test_activity_probe_handles_missing_pgrep_and_lsof(self) -> None:
        with tempfile.TemporaryDirectory() as name, mock.patch.object(provider.shutil, "which", return_value=None), mock.patch.object(provider.subprocess, "run") as runner:
            with self.assertRaisesRegex(provider.ProviderError, "pgrep is unavailable"):
                provider.active_engram_use(Path(name) / "engram.db")
            runner.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX process/store probe fixture")
    def test_activity_probe_return_codes_fail_closed(self) -> None:
        completed = __import__("subprocess").CompletedProcess
        with tempfile.TemporaryDirectory() as name:
            database = Path(name) / "engram.db"
            for code, expectation in ((0, True), (1, False)):
                with mock.patch.object(
                    provider.shutil, "which",
                    side_effect=lambda tool: f"/fixture/{tool}" if tool == "pgrep" else None,
                ), mock.patch.object(provider.subprocess, "run", return_value=completed([], code)):
                    if code == 1:
                        self.assertFalse(provider.active_engram_use(database))
                    else:
                        self.assertEqual(expectation, provider.active_engram_use(database))
            with mock.patch.object(provider.shutil, "which", return_value="/fixture/pgrep"), mock.patch.object(
                provider.subprocess, "run", return_value=completed([], 3)
            ):
                with self.assertRaisesRegex(provider.ProviderError, "pgrep failed"):
                    provider.active_engram_use(database)

            database.write_bytes(b"db")
            for code in (0, 1, 3):
                with mock.patch.object(
                    provider.shutil, "which", side_effect=lambda tool: f"/fixture/{tool}",
                ), mock.patch.object(
                    provider.subprocess, "run",
                    side_effect=[completed([], 1), completed([], code)],
                ):
                    if code == 0:
                        self.assertTrue(provider.active_engram_use(database))
                    elif code == 1:
                        self.assertFalse(provider.active_engram_use(database))
                    else:
                        with self.assertRaisesRegex(provider.ProviderError, "lsof failed"):
                            provider.active_engram_use(database)
            with mock.patch.object(
                provider.shutil, "which",
                side_effect=lambda tool: "/fixture/pgrep" if tool == "pgrep" else None,
            ), mock.patch.object(provider.subprocess, "run", return_value=completed([], 1)):
                with self.assertRaisesRegex(provider.ProviderError, "lsof is unavailable"):
                    provider.active_engram_use(database)

    @unittest.skipIf(os.name == "nt", "POSIX provider fixture")
    def test_install_upgrade_and_rollback_preserve_database_sidecars_and_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bin_dir = root / "bin"
            database = root / ".engram" / "engram.db"
            database.parent.mkdir()
            database.write_bytes(b"db")
            Path(f"{database}-wal").write_bytes(b"wal")
            Path(f"{database}-shm").write_bytes(b"shm")
            first = fixture_archive(root, "1.0.0")
            calls: list[str] = []
            installed = provider.mutate_provider(
                "install", support_for(first, "1.0.0"), home=root,
                bin_dir=bin_dir, selected_platform=PLATFORM,
                maintenance_window=True, yes=True, dry_run=False,
                downloader=local_downloader(first, calls),
                active_probe=lambda _database: False,
            )
            self.assertEqual("installed", installed["status"])
            backups = list((root / ".engram" / "backups").glob("provider-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(
                {"engram.db", "engram.db-wal", "engram.db-shm", "backup-record.json"},
                {p.name for p in backups[0].iterdir()},
            )
            record = json.loads((backups[0] / "backup-record.json").read_text())
            self.assertTrue(record["complete"])

            second = fixture_archive(root, "2.0.0")
            upgraded = provider.mutate_provider(
                "upgrade", support_for(second, "2.0.0"), home=root,
                bin_dir=bin_dir, selected_platform=PLATFORM,
                maintenance_window=True, yes=True, dry_run=False,
                downloader=local_downloader(second, calls),
                active_probe=lambda _database: False,
            )
            self.assertEqual("2.0.0", upgraded["installed_version"])
            retained = list((bin_dir / ".naos-engram-memory-rollbacks").glob("binary-*"))
            self.assertEqual(1, len(retained))
            self.assertIn(b"1.0.0", (retained[0] / "engram").read_bytes())

            rolled_back = provider.mutate_provider(
                "rollback", support_for(second, "2.0.0"), home=root,
                bin_dir=bin_dir, selected_platform=PLATFORM,
                maintenance_window=True, yes=True, dry_run=False,
                active_probe=lambda _database: False,
            )
            self.assertEqual("1.0.0", rolled_back["installed_version"])
            self.assertIn(b"1.0.0", (bin_dir / "engram").read_bytes())
            consumed = json.loads((retained[0] / "record.json").read_text())
            self.assertEqual("consumed", consumed["status"])

    @unittest.skipIf(os.name == "nt", "POSIX provider fixture")
    def test_checksum_failure_does_not_replace_existing_binary(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            binary = bin_dir / "engram"
            binary.write_bytes(provider_script("1.0.0"))
            binary.chmod(0o755)
            paths = provider.provider_paths(root, bin_dir)
            self._record_managed_provider(
                home=root, config_dir=paths["state"].parent, binary=binary, version="1.0.0"
            )
            archive = fixture_archive(root, "2.0.0")
            support = support_for(archive, "2.0.0")
            support["releases"][0]["platforms"][PLATFORM]["sha256"] = "0" * 64
            before = binary.read_bytes()
            with self.assertRaisesRegex(provider.ProviderError, "checksum"):
                provider.mutate_provider(
                    "upgrade", support, home=root, bin_dir=bin_dir,
                    selected_platform=PLATFORM, maintenance_window=True,
                    yes=True, dry_run=False,
                    downloader=local_downloader(archive, []),
                    active_probe=lambda _database: False,
                )
            self.assertEqual(before, binary.read_bytes())
            self.assertFalse((bin_dir / ".naos-engram-memory-rollbacks").exists())

    def test_unmanaged_upgrade_refuses_without_managed_state(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            binary = bin_dir / "engram"
            binary.write_bytes(provider_script("1.15.2"))
            binary.chmod(0o755)
            archive = fixture_archive(root, "1.20.0")
            support = support_for(archive, "1.20.0")
            before = binary.read_bytes()
            with self.assertRaisesRegex(provider.ProviderError, "malformed|invalid provenance|not toolkit-owned"):
                provider.mutate_provider(
                    "upgrade", support, home=root, bin_dir=bin_dir,
                    selected_platform=PLATFORM, maintenance_window=True, yes=True,
                    dry_run=False, active_probe=lambda _database: False,
                    downloader=local_downloader(archive, []),
                )
            self.assertEqual(before, binary.read_bytes())

            for malformed in ({}, {"kind": "managed_install"}):
                paths = provider.provider_paths(root, bin_dir)
                paths["state"].parent.mkdir(parents=True, exist_ok=True)
                paths["state"].write_text(json.dumps({
                    "schema_version": 1,
                    "version": "1.15.2",
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    "provenance": malformed,
                }))
                with self.assertRaisesRegex(provider.ProviderError, "malformed|invalid provenance|not toolkit-owned"):
                    provider.mutate_provider(
                        "upgrade", support, home=root, bin_dir=bin_dir,
                        selected_platform=PLATFORM, maintenance_window=True,
                        yes=True, dry_run=False,
                        active_probe=lambda _database: False,
                        downloader=local_downloader(archive, []),
                    )

    @unittest.skipIf(os.name == "nt", "POSIX provider fixture")
    def test_managed_upgrade_rollback_cycle_preserves_normalized_version(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            binary = bin_dir / "engram"
            binary.write_bytes(provider_script("1.15.2"))
            binary.chmod(0o755)
            paths = provider.provider_paths(root, bin_dir)
            self._record_managed_provider(
                home=root, config_dir=paths["state"].parent, binary=binary,
                version="1.15.2",
            )
            archive = fixture_archive(root, "1.20.0")
            support = support_for(archive, "1.20.0")
            kwargs = dict(
                support=support, home=root, bin_dir=bin_dir,
                selected_platform=PLATFORM, maintenance_window=True, yes=True,
                dry_run=False, active_probe=lambda _database: False,
            )
            self.assertEqual("1.20.0", provider.mutate_provider(
                "upgrade", downloader=local_downloader(archive, []), **kwargs
            )["installed_version"])
            self.assertEqual("1.15.2", provider.mutate_provider("rollback", **kwargs)["installed_version"])

    @unittest.skipIf(os.name == "nt", "POSIX provider fixture")
    def test_invalid_rollback_metadata_leaves_live_binary_state_and_record_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            binary = bin_dir / "engram"
            binary.write_bytes(provider_script("2.0.0"))
            binary.chmod(0o755)
            paths = provider.provider_paths(root, bin_dir)
            paths["state"].parent.mkdir(parents=True)
            paths["state"].write_text(json.dumps({
                "schema_version": 1, "version": "2.0.0",
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "provider_path": str(binary),
                "provenance": {"kind": "managed_install"},
            }))
            rollback = provider._retain_binary(binary, paths["rollback_root"], version="1.0.0")
            record_path = rollback / "record.json"
            record_value = json.loads(record_path.read_text())
            record_value["version"] = "not-semver"
            record_path.write_text(json.dumps(record_value))
            live_before = binary.read_bytes()
            state_before = paths["state"].read_bytes()
            record_before = record_path.read_bytes()
            archive = fixture_archive(root, "2.0.0")
            with self.assertRaisesRegex(provider.ProviderError, "normalized provider version"):
                provider.mutate_provider(
                    "rollback", support_for(archive, "2.0.0"), home=root,
                    bin_dir=bin_dir, selected_platform=PLATFORM,
                    maintenance_window=True, yes=True, dry_run=False,
                    active_probe=lambda _database: False,
                )
            self.assertEqual(live_before, binary.read_bytes())
            self.assertEqual(state_before, paths["state"].read_bytes())
            self.assertEqual(record_before, (rollback / "record.json").read_bytes())
            self.assertEqual("available", json.loads(record_before)["status"])

    def test_symlink_path_and_unsafe_archive_fail_closed(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            external = root / "external"
            external.mkdir()
            linked = root / "linked"
            try:
                linked.symlink_to(external, target_is_directory=True)
            except OSError as exc:
                if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink creation requires Developer Mode or elevation")
                raise
            archive = fixture_archive(root, "1.20.0")
            with self.assertRaisesRegex(provider.ProviderError, "symbolic link"):
                provider.mutate_provider(
                    "install", support_for(archive, "1.20.0"), home=root,
                    bin_dir=linked, selected_platform=PLATFORM,
                    maintenance_window=True, yes=True, dry_run=True,
                    active_probe=lambda _database: False,
                )

    def test_partial_database_and_rollback_backups_are_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            paths = provider.provider_paths(root, root / "bin")
            paths["database"].parent.mkdir()
            paths["database"].write_bytes(b"db")
            with mock.patch.object(provider, "_copy_regular", side_effect=OSError("fixture-copy-failure")):
                with self.assertRaises(OSError):
                    provider._backup_database(paths)
            backup_root = paths["database_backup_root"]
            self.assertFalse(backup_root.exists() and any(backup_root.iterdir()))

            binary = root / "bin" / "engram"
            binary.parent.mkdir(exist_ok=True)
            binary.write_bytes(provider_script("1.0.0"))
            binary.chmod(0o755)
            with mock.patch.object(provider, "_atomic_json", side_effect=OSError("fixture-record-failure")):
                with self.assertRaises(OSError):
                    provider._retain_binary(binary, paths["rollback_root"], version="1.0.0")
            self.assertIsNone(provider._latest_rollback(paths["rollback_root"]))

    def test_windows_mutation_gated_by_approved_asset_like_other_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with self.assertRaisesRegex(provider.ProviderError, "no single supported"):
                provider.mutate_provider(
                    "install", {"schema_version": 1, "releases": []},
                    home=root, selected_platform="windows_amd64",
                    maintenance_window=True, yes=True, dry_run=True,
                    active_probe=lambda _database: False,
                )

    def test_custom_data_dir_refuses_mutation_and_adoption_before_effects(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            support = support_for(archive, "1.20.0")
            candidate = root / "engram"
            candidate.write_bytes(provider_script("1.20.0"))
            candidate.chmod(0o755)
            calls: list[str] = []
            with mock.patch.dict(os.environ, {"ENGRAM_DATA_DIR": str(root / "custom")}, clear=False):
                with self.assertRaisesRegex(provider.ProviderError, "ENGRAM_DATA_DIR"):
                    provider.mutate_provider(
                        "install", support, home=root, bin_dir=root / "bin",
                        selected_platform=PLATFORM, maintenance_window=True,
                        yes=True, dry_run=False,
                        downloader=local_downloader(archive, calls),
                        active_probe=lambda _database: False,
                    )
                with self.assertRaisesRegex(provider.ProviderError, "ENGRAM_DATA_DIR"):
                    provider.adopt_existing_provider(
                        support, candidate=candidate, home=root,
                        selected_platform=PLATFORM, yes=True, dry_run=False,
                    )
            self.assertEqual([], calls)
            self.assertFalse((root / "bin").exists())
            self.assertFalse(provider.provider_paths(root)["state"].exists())

    @unittest.skipIf(os.name == "nt", "POSIX wrapper fixture")
    def test_managed_wrapper_refuses_custom_data_dir_without_provider_spawn(self) -> None:
        from tools import engram_memory as memory

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home, workspace, config_dir, bin_dir, wrapper = self._managed_wrapper_fixture(root)
            calls = root / "provider-calls"
            binary = bin_dir / "engram"
            binary.write_text(f"#!/bin/sh\nprintf called > {json.dumps(str(calls))}\n")
            binary.chmod(0o755)
            self._record_managed_provider(home=home, config_dir=config_dir, binary=binary)
            completed = __import__("subprocess").run(
                [str(wrapper)], cwd=workspace,
                env={**os.environ, "HOME": str(home), "ENGRAM_DATA_DIR": str(root / "custom")},
                text=True, stdout=__import__("subprocess").PIPE,
                stderr=__import__("subprocess").PIPE, check=False,
            )
            self.assertEqual(64, completed.returncode)
            self.assertIn("ENGRAM_DATA_DIR", completed.stderr)
            self.assertFalse(calls.exists())

    def test_managed_data_dir_declaration_is_accepted_when_it_names_the_managed_store(self) -> None:
        # NAOS governance declares data_dir: ~/.engram and documents ENGRAM_DATA_DIR
        # as a runtime override, so restating the managed store must not be refused.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            candidate = root / "engram"
            candidate.write_bytes(provider_script("1.20.0"))
            candidate.chmod(0o755)
            support = memory.load_json(memory.bundled_config_path("release-support.json"))
            managed = provider.managed_data_dir(root)
            self.assertEqual(root / ".engram", managed)
            for declared in (str(managed), f"{managed}/", f"{managed}//"):
                with self.subTest(declared=declared):
                    with mock.patch.dict(os.environ, {"ENGRAM_DATA_DIR": declared}, clear=False):
                        result = provider.adopt_existing_provider(
                            support, candidate=candidate, home=root,
                            selected_platform=PLATFORM, yes=True, dry_run=True,
                        )
                    self.assertEqual("dry_run", result["status"])

    def test_managed_data_dir_declaration_refuses_a_different_store(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            candidate = root / "engram"
            candidate.write_bytes(provider_script("1.20.0"))
            candidate.chmod(0o755)
            support = memory.load_json(memory.bundled_config_path("release-support.json"))
            for declared in (str(root / "custom"), str(root / ".engram-alt"), "/tmp/engram-elsewhere"):
                with self.subTest(declared=declared):
                    with mock.patch.dict(os.environ, {"ENGRAM_DATA_DIR": declared}, clear=False):
                        with self.assertRaisesRegex(provider.ProviderError, "different store"):
                            provider.adopt_existing_provider(
                                support, candidate=candidate, home=root,
                                selected_platform=PLATFORM, yes=True, dry_run=True,
                            )

    def test_managed_data_dir_comparison_is_lexical_not_symlink_resolving(self) -> None:
        # Resolving symlinks to force a match would accept a path the managed
        # symlink policy refuses, so an aliased spelling must still be refused.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            real = root / ".engram"
            real.mkdir()
            alias = root / "engram-alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            candidate = root / "engram"
            candidate.write_bytes(provider_script("1.20.0"))
            candidate.chmod(0o755)
            support = memory.load_json(memory.bundled_config_path("release-support.json"))
            with mock.patch.dict(os.environ, {"ENGRAM_DATA_DIR": str(alias)}, clear=False):
                with self.assertRaisesRegex(provider.ProviderError, "different store"):
                    provider.adopt_existing_provider(
                        support, candidate=candidate, home=root,
                        selected_platform=PLATFORM, yes=True, dry_run=True,
                    )

    @unittest.skipIf(os.name == "nt", "POSIX wrapper fixture")
    def test_managed_wrapper_accepts_a_declaration_of_the_managed_store(self) -> None:
        from tools import engram_memory as memory_module

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home, workspace, config_dir, bin_dir, wrapper = self._managed_wrapper_fixture(root)
            calls = root / "provider-calls"
            binary = bin_dir / "engram"
            binary.write_text(f"#!/bin/sh\nprintf called > {json.dumps(str(calls))}\n")
            binary.chmod(0o755)
            self._record_managed_provider(home=home, config_dir=config_dir, binary=binary)
            completed = __import__("subprocess").run(
                [str(wrapper)], cwd=workspace,
                env={**os.environ, "HOME": str(home), "ENGRAM_DATA_DIR": str(home / ".engram")},
                text=True, stdout=__import__("subprocess").PIPE,
                stderr=__import__("subprocess").PIPE, check=False,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(calls.exists(), "provider must start when the declaration agrees")

    def test_provider_state_uses_platform_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            configured = root / "xdg"
            prior = os.environ.get("XDG_CONFIG_HOME")
            os.environ["XDG_CONFIG_HOME"] = str(configured)
            try:
                expected = (
                    root / "AppData" / "Roaming" / "naos-engram-memory" / "provider-state.v1.json"
                    if os.name == "nt"
                    else configured / "naos-engram-memory" / "provider-state.v1.json"
                )
                self.assertEqual(expected, provider.provider_paths(root)["state"])
            finally:
                if prior is None:
                    os.environ.pop("XDG_CONFIG_HOME", None)
                else:
                    os.environ["XDG_CONFIG_HOME"] = prior

    def test_adopt_existing_resolves_symlink_and_wrapper_uses_verified_realpath(self) -> None:
        from tools import engram_memory as memory

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home = root / "home"
            config_dir = memory.user_config_dir(home)
            real = root / "homebrew" / "Cellar" / "engram" / "1.20.0" / "bin" / "engram"
            real.parent.mkdir(parents=True)
            real.write_bytes(provider_script("1.20.0"))
            real.chmod(0o755)
            link = root / "homebrew" / "bin" / "engram"
            link.parent.mkdir(parents=True)
            try:
                link.symlink_to(real)
            except OSError as exc:
                if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink creation requires Developer Mode or elevation")
                raise
            archive = fixture_archive(root, "1.20.0")
            preview = provider.adopt_existing_provider(
                support_for(archive, "1.20.0"), candidate=link, home=home,
                config_dir=config_dir, selected_platform=PLATFORM,
                yes=False, dry_run=True,
            )
            self.assertEqual("dry_run", preview["status"])
            self.assertFalse((config_dir / "provider-state.v1.json").exists())
            adopted = provider.adopt_existing_provider(
                support_for(archive, "1.20.0"), candidate=link, home=home,
                config_dir=config_dir, selected_platform=PLATFORM,
                yes=True, dry_run=False,
            )
            self.assertEqual(str(real.resolve()), adopted["provider_path"])
            self.assertTrue(adopted["symlink_resolved"])
            memory.ensure_user_runtime(home=home)
            wrapper = memory.user_wrapper_path(home).read_text(encoding="utf-8")
            self.assertIn(str(real.resolve()), wrapper)
            self.assertNotIn(str(link), wrapper)
            verified = provider.verify_bound_provider(
                config_dir=config_dir, expected_path=real
            )
            self.assertEqual("verified", verified["status"])

    @unittest.skipIf(os.name == "nt", "POSIX provider fixture")
    def test_adopted_provider_tamper_and_toolkit_upgrade_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home = root / "home"
            config_dir = root / "config"
            external = root / "brew" / "engram"
            external.parent.mkdir()
            external.write_bytes(provider_script("1.20.0"))
            external.chmod(0o755)
            archive = fixture_archive(root, "1.20.0")
            support = support_for(archive, "1.20.0")
            provider.adopt_existing_provider(
                support, candidate=external, home=home, config_dir=config_dir,
                selected_platform=PLATFORM, yes=True, dry_run=False,
            )
            with self.assertRaisesRegex(provider.ProviderError, "not toolkit-owned"):
                provider.mutate_provider(
                    "upgrade", support, home=home, bin_dir=external.parent,
                    config_dir=config_dir, selected_platform=PLATFORM,
                    maintenance_window=True, yes=True, dry_run=False,
                    downloader=local_downloader(archive, []),
                    active_probe=lambda _database: False,
                )
            before = external.read_bytes()
            self.assertEqual(before, external.read_bytes())
            external.write_bytes(provider_script("9.9.9"))
            external.chmod(0o755)
            with self.assertRaisesRegex(provider.ProviderError, "changed after"):
                provider.verify_bound_provider(config_dir=config_dir, expected_path=external.resolve())

    def test_adopt_existing_refuses_arbitrary_executable_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = fixture_archive(root, "1.20.0")
            support = support_for(archive, "1.20.0")
            arbitrary = root / "not-engram"
            arbitrary.write_bytes(provider_script("1.20.0"))
            arbitrary.chmod(0o755)
            with self.assertRaisesRegex(provider.ProviderError, "named engram"):
                provider.adopt_existing_provider(
                    support, candidate=arbitrary, home=root,
                    selected_platform=PLATFORM, yes=True, dry_run=False,
                )

    def test_runtime_dry_run_renders_exact_provisional_adopted_path(self) -> None:
        from tools import engram_memory as memory

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home = root / "home"
            external = root / "Cellar" / "engram" / "1.20.0" / "bin" / "engram"
            external.parent.mkdir(parents=True)
            external.write_bytes(provider_script("1.20.0"))
            external.chmod(0o755)
            preview = memory.ensure_user_runtime(
                home=home, dry_run=True, provider_path_override=external
            )
            self.assertTrue(
                any("engram-mcp-wrapper" in item for item in preview["would_change"])
            )
            wrapper_assets = memory.rendered_wrapper_assets(
                memory.user_config_dir(home), memory.user_bin_dir(home),
                provider_path_override=external,
            )
            wrapper_name = "engram-mcp-wrapper.ps1" if os.name == "nt" else "engram-mcp-wrapper"
            rendered = wrapper_assets[wrapper_name][1]
            self.assertIn(str(external.resolve()), rendered)
            self.assertFalse(memory.user_config_dir(home).exists())

    def test_failed_runtime_publication_restores_prior_provider_state(self) -> None:
        from tools import engram_memory as memory

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home = root / "home"
            config_dir = memory.user_config_dir(home)
            config_dir.mkdir(parents=True)
            prior = b'{"prior":"exact-bytes"}\n'
            state = config_dir / "provider-state.v1.json"
            state.write_bytes(prior)
            provider.restore_provider_state(config_dir=config_dir, prior=prior)
            self.assertEqual(prior, state.read_bytes())
            provider.restore_provider_state(config_dir=config_dir, prior=None)
            self.assertFalse(state.exists())

    @unittest.skipIf(os.name == "nt", "POSIX provider fixture")
    def test_direct_adopt_runtime_failure_restores_state_and_runtime(self) -> None:
        from tools import engram_memory as memory

        def tree_snapshot(root: Path) -> dict[str, tuple[str, int, bytes | None]]:
            snapshot: dict[str, tuple[str, int, bytes | None]] = {}
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                if path.is_dir():
                    snapshot[relative] = ("directory", path.stat().st_mode & 0o7777, None)
                else:
                    snapshot[relative] = ("file", path.stat().st_mode & 0o7777, path.read_bytes())
            return snapshot

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home = root / "home"
            config_dir = memory.user_config_dir(home)
            previous = root / "previous" / "engram"
            selected = root / "selected" / "engram"
            for candidate in (previous, selected):
                candidate.parent.mkdir()
                candidate.write_bytes(provider_script("1.20.0"))
                candidate.chmod(0o755)
            support = memory.load_json(memory.bundled_config_path("release-support.json"))
            provider.adopt_existing_provider(
                support, candidate=previous, home=home, config_dir=config_dir,
                yes=True, dry_run=False,
            )
            memory.ensure_user_runtime(home=home, provider_path_override=previous)
            state_path = config_dir / "provider-state.v1.json"
            wrapper_path = memory.user_wrapper_path(home)
            state_before = state_path.read_bytes()
            wrapper_before = wrapper_path.read_bytes()
            runtime_state_before = memory.runtime_configuration_state(home)
            tree_before = tree_snapshot(home)
            real_atomic_write = memory.atomic_write_text

            def fail_late_manifest(target: Path, text: str, *, encoding: str):
                if target.name == "managed-runtime.v1.json":
                    raise OSError("synthetic-late-manifest-publication-failure")
                return real_atomic_write(target, text, encoding=encoding)

            with mock.patch.object(memory, "atomic_write_text", side_effect=fail_late_manifest):
                with self.assertRaisesRegex(OSError, "synthetic-late-manifest"):
                    memory.main([
                        "--registry", str(Path(__file__).resolve().parent / "fixtures" / "projects.synthetic.json"),
                        "provider", "adopt", "--path", str(selected), "--home", str(home), "--yes",
                    ])
            self.assertEqual(state_before, state_path.read_bytes())
            self.assertEqual(wrapper_before, wrapper_path.read_bytes())
            self.assertEqual(runtime_state_before, memory.runtime_configuration_state(home))
            self.assertEqual(tree_before, tree_snapshot(home))


def windows_fixture_archive(root: Path, version: str) -> Path:
    """A synthetic engram_<version>_windows_amd64.zip containing only engram.exe."""
    archive = root / f"engram-{version}-windows.zip"
    content = f"synthetic-fixture-not-runnable-{version}".encode()
    with __import__("zipfile").ZipFile(archive, "w") as bundle:
        bundle.writestr("engram.exe", content)
    return archive


def windows_support_for(archive: Path, version: str) -> dict:
    return {
        "schema_version": 1,
        "releases": [{
            "version": version,
            "status": "supported",
            "platforms": {"windows_amd64": {
                "status": "supported",
                "asset": archive.name,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }},
        }],
    }


class PlatformGateTests(unittest.TestCase):
    """Platform detection and the explicit unvalidated-platform adoption escape."""

    def test_windows_platform_key_reports_real_architecture(self) -> None:
        # platform_key returned windows_amd64 unconditionally, so an ARM64 host
        # silently selected the x86_64 asset instead of failing closed.
        cases = (
            ({"PROCESSOR_ARCHITECTURE": "AMD64"}, "AMD64", "windows_amd64"),
            ({"PROCESSOR_ARCHITECTURE": "ARM64"}, "ARM64", "windows_arm64"),
            # A 32-bit process on an ARM64 host reports x86 in
            # PROCESSOR_ARCHITECTURE, so the WOW64 variable wins.
            ({"PROCESSOR_ARCHITECTURE": "x86", "PROCESSOR_ARCHITEW6432": "ARM64"}, "x86", "windows_arm64"),
            ({}, "ARM64", "windows_arm64"),
        )
        for environment, machine, expected in cases:
            with self.subTest(expected=expected):
                with (
                    mock.patch.object(provider.os, "name", "nt"),
                    mock.patch.dict(provider.os.environ, environment, clear=True),
                    mock.patch.object(provider.host_platform, "machine", lambda value=machine: value),
                ):
                    self.assertEqual(expected, provider.platform_key())

    def test_windows_arm64_has_no_approved_asset_and_fails_closed(self) -> None:
        support = memory.load_json(memory.bundled_config_path("release-support.json"))
        self.assertIn("windows_amd64", provider.validated_platforms(support))
        self.assertNotIn("windows_arm64", provider.validated_platforms(support))
        with self.assertRaisesRegex(provider.ProviderError, "windows_arm64"):
            provider.approved_asset(support, "windows_arm64")

    def test_supported_release_version_is_the_single_supported_release(self) -> None:
        support = memory.load_json(memory.bundled_config_path("release-support.json"))
        self.assertEqual("1.20.0", provider.supported_release_version(support))
        with self.assertRaisesRegex(provider.ProviderError, "no single supported"):
            provider.supported_release_version({"schema_version": 1, "releases": []})

    def test_adopt_refuses_unvalidated_platform_and_names_the_escape(self) -> None:
        support = memory.load_json(memory.bundled_config_path("release-support.json"))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            candidate = root / "bin" / "engram"
            candidate.parent.mkdir()
            candidate.write_bytes(provider_script("1.20.0"))
            candidate.chmod(0o755)
            for unvalidated in ("darwin_amd64", "linux_arm64"):
                with self.subTest(platform=unvalidated):
                    with self.assertRaises(provider.ProviderError) as caught:
                        provider.adopt_existing_provider(
                            support, candidate=candidate, home=root / "home",
                            selected_platform=unvalidated, yes=True, dry_run=True,
                        )
                    message = str(caught.exception)
                    self.assertIn("--allow-unvalidated-platform", message)
                    self.assertIn("darwin_arm64", message)

    def test_adopt_escape_binds_provider_but_keeps_version_and_hash_pins(self) -> None:
        support = memory.load_json(memory.bundled_config_path("release-support.json"))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            home = root / "home"
            config_dir = memory.user_config_dir(home)
            candidate = root / "bin" / "engram"
            candidate.parent.mkdir()
            candidate.write_bytes(provider_script("1.20.0"))
            candidate.chmod(0o755)
            result = provider.adopt_existing_provider(
                support, candidate=candidate, home=home, config_dir=config_dir,
                selected_platform="linux_arm64", yes=True, dry_run=False,
                allow_unvalidated_platform=True,
            )
            self.assertEqual("adopted", result["status"])
            self.assertEqual("linux_arm64", result["platform"])
            self.assertEqual("unvalidated_owner_approved", result["platform_validation"])
            self.assertEqual("1.20.0", result["installed_version"])
            state = memory.load_json(config_dir / "provider-state.v1.json")
            self.assertEqual(
                hashlib.sha256(candidate.read_bytes()).hexdigest(), state["binary_sha256"]
            )
            self.assertEqual("unvalidated_owner_approved", state["provenance"]["platform_validation"])
            self.assertEqual("linux_arm64", state["provenance"]["platform"])

            # The escape waives only the platform asset allowlist: a binary that
            # reports a different version is still refused.
            wrong = root / "wrong" / "engram"
            wrong.parent.mkdir()
            wrong.write_bytes(provider_script("1.15.1"))
            wrong.chmod(0o755)
            with self.assertRaisesRegex(provider.ProviderError, "manifest-approved version"):
                provider.adopt_existing_provider(
                    support, candidate=wrong, home=home, config_dir=config_dir,
                    selected_platform="linux_arm64", yes=True, dry_run=True,
                    allow_unvalidated_platform=True,
                )

    def test_escape_does_not_unlock_download_based_mutation(self) -> None:
        support = memory.load_json(memory.bundled_config_path("release-support.json"))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for operation in ("install", "upgrade", "rollback"):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(provider.ProviderError, "linux_arm64"):
                        provider.mutate_provider(
                            operation, support, home=root / "home",
                            bin_dir=root / "bin", selected_platform="linux_arm64",
                            maintenance_window=True, yes=True, dry_run=True,
                        )


@unittest.skipUnless(os.name == "nt", "Windows provider lifecycle tests")
class WindowsProviderLifecycleTests(unittest.TestCase):
    """Additive coverage for the Windows-specific provider.py code paths.

    These tests are deterministic and run on native Windows CI; they never
    modify or depend on the Darwin/Linux code paths
    exercised by ProviderLifecycleTests above.
    """

    def test_windows_process_running_detects_presence_and_absence(self) -> None:
        absent = __import__("subprocess").CompletedProcess([], 0, stdout="INFO: No tasks are running which match the specified criteria.\n")
        present = __import__("subprocess").CompletedProcess([], 0, stdout='"engram.exe","1234","Console","1","12,345 K"\n')
        with mock.patch.object(provider.shutil, "which", return_value="C:\\Windows\\System32\\tasklist.exe"), mock.patch.object(
            provider.subprocess, "run", return_value=absent
        ):
            self.assertFalse(provider._windows_process_running("engram.exe"))
        with mock.patch.object(provider.shutil, "which", return_value="C:\\Windows\\System32\\tasklist.exe"), mock.patch.object(
            provider.subprocess, "run", return_value=present
        ):
            self.assertTrue(provider._windows_process_running("engram.exe"))

    def test_windows_process_running_requires_tasklist(self) -> None:
        with mock.patch.object(provider.shutil, "which", return_value=None):
            with self.assertRaisesRegex(provider.ProviderError, "tasklist is unavailable"):
                provider._windows_process_running("engram.exe")

    def test_windows_process_running_fails_closed_on_tasklist_error(self) -> None:
        failed = __import__("subprocess").CompletedProcess([], 1, stdout="")
        with mock.patch.object(provider.shutil, "which", return_value="C:\\Windows\\System32\\tasklist.exe"), mock.patch.object(
            provider.subprocess, "run", return_value=failed
        ):
            with self.assertRaisesRegex(provider.ProviderError, "tasklist failed"):
                provider._windows_process_running("engram.exe")

    def test_windows_file_locked_detects_missing_and_open_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            missing = root / "engram.db"
            self.assertFalse(provider._windows_file_locked(missing))
            present = root / "engram.db-wal"
            present.write_bytes(b"wal")
            self.assertFalse(provider._windows_file_locked(present))
            with mock.patch.object(provider.os, "open", side_effect=OSError("fixture-locked")):
                self.assertTrue(provider._windows_file_locked(present))

    def test_active_engram_use_windows_branch_process_running(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            database = Path(name) / "engram.db"
            with mock.patch.object(provider.os, "name", "nt"), mock.patch.object(
                provider, "_windows_process_running", return_value=True
            ):
                self.assertTrue(provider.active_engram_use(database))

    def test_active_engram_use_windows_branch_idle(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            database = Path(name) / "engram.db"
            with mock.patch.object(provider.os, "name", "nt"), mock.patch.object(
                provider, "_windows_process_running", return_value=False
            ), mock.patch.object(provider, "_windows_file_locked", return_value=False):
                self.assertFalse(provider.active_engram_use(database))

    def test_extract_binary_zip_installs_single_engram_exe(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = windows_fixture_archive(root, "1.20.0")
            destination = root / "staged"
            destination.mkdir()
            staged = provider._extract_binary(archive, destination)
            self.assertEqual("engram.exe", staged.name)
            self.assertTrue(staged.is_file())

    def test_extract_binary_zip_rejects_multiple_members_and_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            zipfile = __import__("zipfile")
            multiple = root / "multiple.zip"
            with zipfile.ZipFile(multiple, "w") as bundle:
                bundle.writestr("engram.exe", b"one")
                bundle.writestr("nested/engram.exe", b"two")
            destination = root / "dest1"
            destination.mkdir()
            with self.assertRaisesRegex(provider.ProviderError, "exactly one regular engram.exe"):
                provider._extract_binary(multiple, destination)

            unsafe = root / "unsafe.zip"
            with zipfile.ZipFile(unsafe, "w") as bundle:
                bundle.writestr("../escape/engram.exe", b"evil")
            destination2 = root / "dest2"
            destination2.mkdir()
            with self.assertRaisesRegex(provider.ProviderError, "unsafe member"):
                provider._extract_binary(unsafe, destination2)

    def test_provider_status_windows_reports_binary_detection_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bin_dir = root / "bin"
            support = {"schema_version": 1, "releases": []}
            status_before = provider.provider_status(
                support, home=root, bin_dir=bin_dir, selected_platform="windows_amd64",
            )
            self.assertEqual("experimental_mutation_disabled", status_before["status"])
            self.assertFalse(status_before["binary_detected"])
            self.assertTrue(status_before["read_only"])
            self.assertFalse(status_before["network_accessed"])

            bin_dir.mkdir()
            (bin_dir / "engram.exe").write_bytes(b"fixture")
            status_after = provider.provider_status(
                support, home=root, bin_dir=bin_dir, selected_platform="windows_amd64",
            )
            self.assertTrue(status_after["binary_detected"])

    def test_provider_status_windows_uses_normal_supported_contract(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = windows_fixture_archive(root, "1.20.0")
            support = windows_support_for(archive, "1.20.0")
            status = provider.provider_status(
                support, home=root, bin_dir=root / "bin", selected_platform="windows_amd64",
            )
            self.assertEqual("1.20.0", status["approved_version"])
            self.assertEqual("absent", status["binary_integrity"])
            self.assertFalse(status["toolkit_mutation_available"])

    def test_adopt_existing_provider_allows_windows_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = windows_fixture_archive(root, "1.20.0")
            support = windows_support_for(archive, "1.20.0")
            candidate = root / "engram.exe"
            candidate.write_bytes(b"fixture-not-a-real-pe")
            with mock.patch.object(provider, "_binary_version", return_value="1.20.0"):
                adopted = provider.adopt_existing_provider(
                    support, candidate=candidate, home=root,
                    selected_platform="windows_amd64", yes=True, dry_run=False,
                )
            self.assertEqual("adopted", adopted["status"])
            self.assertEqual(str(candidate.resolve()), adopted["provider_path"])

    def test_mutate_provider_install_allows_windows_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            archive = windows_fixture_archive(root, "1.20.0")
            support = windows_support_for(archive, "1.20.0")
            bin_dir = root / "bin"
            calls: list[str] = []
            with mock.patch.object(provider, "_binary_version", return_value="1.20.0"):
                installed = provider.mutate_provider(
                    "install", support, home=root, bin_dir=bin_dir,
                    selected_platform="windows_amd64", maintenance_window=True,
                    yes=True, dry_run=False,
                    downloader=local_downloader(archive, calls),
                    active_probe=lambda _database: False,
                )
            self.assertEqual("installed", installed["status"])
            self.assertTrue((bin_dir / "engram.exe").is_file())


if __name__ == "__main__":
    unittest.main()
