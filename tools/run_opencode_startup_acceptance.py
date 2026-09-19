#!/usr/bin/env python3
"""Produce a narrow real-host OpenCode startup receipt in a disposable profile.

This runner executes the exact pinned stable OpenCode binary, but uses only a
synthetic project registry and synthetic Engram stdio peer.  It proves host
startup, MCP initialize, and tools/list.  It does not invoke a model provider,
call a memory tool, or read the operator's home or Engram database.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import engram_memory as memory  # noqa: E402
from tools import provider as provider_lifecycle  # noqa: E402
from tools.run_mcp_host_acceptance import init_workspace, synthetic_registry  # noqa: E402


EXPECTED_VERSION = "1.18.18"
EXPECTED_BINARY_SHA256 = (
    "4f5979c2dadb06fbff1335335afaaea274e58f92e79aa43cf2ed98618d555422"
)
EXPECTED_RELEASE_ASSET_SHA256 = (
    "7d668bf26496fec8686d4e51ebb1ac2bd2e393f0c1620aa696c4c242a9e5806a"
)
EXPECTED_RELEASE_ASSET_URL = (
    "https://github.com/anomalyco/opencode/releases/download/"
    "v1.18.18/opencode-darwin-arm64.zip"
)
EXPECTED_HOMEBREW_TAP = "anomalyco/tap"
EXPECTED_METHODS = ("initialize", "notifications/initialized", "tools/list")
CREDENTIAL_CANARIES = {
    "OPENAI_API_KEY": "naos-openai-credential-canary",
    "ANTHROPIC_API_KEY": "naos-anthropic-credential-canary",
    "NAOS_CREDENTIAL_CANARY": "naos-arbitrary-credential-canary",
}
DARWIN_TEXT_ENCODING = re.compile(
    r"^0x[0-9A-Fa-f]+:(?:0x[0-9A-Fa-f]+|[0-9]+):(?:0x[0-9A-Fa-f]+|[0-9]+)$"
)


class StartupAcceptanceError(ValueError):
    """The host, disposable setup, or observed startup evidence is invalid."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_platform() -> tuple[str, str]:
    system = platform.system().casefold()
    architecture = platform.machine().casefold()
    if system == "darwin" and architecture in {"arm64", "aarch64"}:
        return "darwin_arm64", "arm64"
    raise StartupAcceptanceError(
        "this exact startup receipt is pinned to native macOS ARM64; other platforms need separate evidence"
    )


def source_identity() -> dict[str, Any]:
    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/usr/bin/git", "-C", str(ROOT), *arguments],
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "LC_ALL": "C",
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )

    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    if any(result.returncode != 0 for result in (head, tree, status)):
        raise StartupAcceptanceError(
            "toolkit Git source identity could not be established"
        )
    return {
        "git_head": head.stdout.strip(),
        "git_tree": tree.stdout.strip(),
        "clean": not bool(status.stdout),
    }


def resolve_pinned_host(candidate: Path | None) -> Path:
    selected = candidate
    if selected is None:
        discovered = shutil.which("opencode")
        selected = Path(discovered) if discovered else None
    if selected is None:
        raise StartupAcceptanceError("stable opencode executable is unavailable")
    try:
        resolved = selected.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StartupAcceptanceError(
            "stable opencode executable could not be resolved"
        ) from exc
    if (
        resolved.name != "opencode"
        or not resolved.is_file()
        or not os.access(resolved, os.X_OK)
    ):
        raise StartupAcceptanceError(
            "host executable must resolve to an executable named opencode"
        )
    observed_hash = sha256_file(resolved)
    if observed_hash != EXPECTED_BINARY_SHA256:
        raise StartupAcceptanceError(
            "stable opencode binary SHA-256 does not match the pinned Mac baseline"
        )
    return resolved


def homebrew_provenance(executable: Path) -> dict[str, Any]:
    prefix = executable.parent.parent
    receipt_path = prefix / "INSTALL_RECEIPT.json"
    formula_path = prefix / ".brew" / "opencode.rb"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StartupAcceptanceError(
            "OpenCode Homebrew installation receipt is unavailable"
        ) from exc
    source = receipt.get("source")
    versions = source.get("versions") if isinstance(source, dict) else None
    if (
        not isinstance(source, dict)
        or source.get("tap") != EXPECTED_HOMEBREW_TAP
        or not isinstance(versions, dict)
        or versions.get("stable") != EXPECTED_VERSION
    ):
        raise StartupAcceptanceError(
            "OpenCode is not bound to the pinned official Homebrew tap/version"
        )
    if not formula_path.is_file():
        raise StartupAcceptanceError(
            "OpenCode installed formula receipt is unavailable"
        )
    try:
        formula = formula_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StartupAcceptanceError(
            "OpenCode installed formula receipt is unavailable"
        ) from exc
    arm64_binding = re.compile(
        r"if Hardware::CPU\.arm\?\s+"
        + re.escape(f'url "{EXPECTED_RELEASE_ASSET_URL}"')
        + r"\s+"
        + re.escape(f'sha256 "{EXPECTED_RELEASE_ASSET_SHA256}"')
    )
    if (
        formula.count(f'version "{EXPECTED_VERSION}"') != 1
        or arm64_binding.search(formula) is None
    ):
        raise StartupAcceptanceError(
            "OpenCode formula does not bind the pinned Darwin ARM64 release asset"
        )
    return {
        "kind": "homebrew",
        "tap": source["tap"],
        "tap_git_head": source.get("tap_git_head"),
        "install_receipt_sha256": sha256_file(receipt_path),
        "formula_sha256": sha256_file(formula_path),
        "release_asset_url": EXPECTED_RELEASE_ASSET_URL,
        "release_asset_sha256": EXPECTED_RELEASE_ASSET_SHA256,
        "release_asset_binding": "installed_formula_darwin_arm64",
    }


def synthetic_provider(path: Path, trace_path: Path) -> None:
    source = f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

TRACE = Path({str(trace_path)!r})

def record(value):
    with TRACE.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True) + "\\n")

if len(sys.argv) > 1 and sys.argv[1] == "version":
    print("engram 1.20.0")
    raise SystemExit(0)
if len(sys.argv) > 1 and sys.argv[1] == "doctor":
    print("{{}}")
    raise SystemExit(0)
if len(sys.argv) < 2 or sys.argv[1] != "mcp":
    raise SystemExit(2)
record({{"event": "spawn", "argv": sys.argv[1:], "environment": dict(os.environ)}})
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    record({{"event": "request", "method": method}})
    if method == "initialize":
        result = {{"protocolVersion": "2025-03-26", "capabilities": {{"tools": {{}}}}, "serverInfo": {{"name": "synthetic-engram", "version": "0"}}}}
    elif method == "tools/list":
        result = {{"tools": [{{"name": "mem_current_project", "description": "synthetic", "inputSchema": {{"type": "object"}}}}]}}
        record({{"event": "tools_list_response", "tools": ["mem_current_project"]}})
    elif method == "tools/call":
        result = {{"content": [{{"type": "text", "text": "unexpected-tool-call"}}]}}
    else:
        result = {{}}
    if request.get("id") is not None:
        print(json.dumps({{"jsonrpc": "2.0", "id": request["id"], "result": result}}), flush=True)
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def read_trace(path: Path) -> list[dict[str, Any]]:
    try:
        values = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise StartupAcceptanceError(
            "synthetic provider trace is unavailable or malformed"
        ) from exc
    if not all(isinstance(value, dict) for value in values):
        raise StartupAcceptanceError(
            "synthetic provider trace contains a non-object event"
        )
    return values


def host_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    for path in (
        home,
        root / "xdg-config",
        root / "xdg-cache",
        root / "xdg-data",
        root / "xdg-state",
        root / "tmp",
    ):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(root / "tmp"),
        "LANG": "C",
        "LC_ALL": "C",
        "NO_COLOR": "1",
        "CI": "1",
        "USER": "naos-fixture",
        "LOGNAME": "naos-fixture",
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        **CREDENTIAL_CANARIES,
    }


def observed_version(
    executable: Path, environment: dict[str, str], workspace: Path
) -> str:
    result = subprocess.run(
        [str(executable), "--version"],
        cwd=workspace,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
        check=False,
    )
    versions = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or versions != [EXPECTED_VERSION]:
        raise StartupAcceptanceError(
            "stable opencode did not emit the single pinned version"
        )
    return versions[0]


def evaluate_startup_trace(
    trace: list[dict[str, Any]],
    *,
    host_returncode: int,
    host_output: str,
    expected_home: Path,
) -> tuple[dict[str, Any], bool]:
    spawn_events = [event for event in trace if event.get("event") == "spawn"]
    methods = [
        event.get("method") for event in trace if event.get("event") == "request"
    ]
    listed_tools = {
        tool
        for event in trace
        if event.get("event") == "tools_list_response"
        for tool in event.get("tools", [])
        if isinstance(tool, str)
    }
    child_environment_value = (
        spawn_events[0].get("environment", {}) if len(spawn_events) == 1 else {}
    )
    child_environment = (
        child_environment_value if isinstance(child_environment_value, dict) else {}
    )
    expected_environment = {
        "HOME": str(expected_home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": "/tmp",
        "LANG": "C",
        "LC_ALL": "C",
        "NO_COLOR": "1",
        "ENGRAM_PROJECT": "example-product",
    }
    permitted_platform_environment: set[str] = set()
    darwin_text_encoding = child_environment.get("__CF_USER_TEXT_ENCODING")
    if isinstance(darwin_text_encoding, str) and DARWIN_TEXT_ENCODING.fullmatch(
        darwin_text_encoding
    ):
        permitted_platform_environment.add("__CF_USER_TEXT_ENCODING")
    unexpected_environment = sorted(
        key
        for key in child_environment
        if key not in expected_environment and key not in permitted_platform_environment
    )
    expected_environment_present = all(
        child_environment.get(key) == value
        for key, value in expected_environment.items()
    )
    provider_environment_isolated = (
        expected_environment_present and not unexpected_environment
    )
    canary_values = set(CREDENTIAL_CANARIES.values())
    credentials_forwarded = any(
        child_environment.get(name) == value
        for name, value in CREDENTIAL_CANARIES.items()
    ) or any(
        isinstance(value, str) and value in canary_values
        for value in child_environment.values()
    )
    return (
        {
            "host_exit_zero": host_returncode == 0,
            "server_status_connected": "engram" in host_output.casefold()
            and "connected" in host_output.casefold(),
            "wrapper_argv_exact": len(spawn_events) == 1
            and spawn_events[0].get("argv")
            == ["mcp", "--tools=mem_current_project,mem_context,mem_search,mem_get_observation,mem_save,mem_session_summary", "--project=example-product"],
            "provider_environment_isolated": provider_environment_isolated,
            "unexpected_environment_variables": unexpected_environment,
            "expected_methods_observed": methods == list(EXPECTED_METHODS),
            "methods_observed": methods,
            "mem_current_project_listed": "mem_current_project" in listed_tools,
            "tools_call_observed": "tools/call" in methods,
        },
        credentials_forwarded,
    )


def validate_receipt_semantics(receipt: dict[str, Any]) -> None:
    """Keep success/failure dispositions aligned with the observed checks."""
    checks = receipt["checks"]
    isolation = receipt["isolation"]
    successful_evidence = all(
        (
            checks["host_exit_zero"],
            checks["server_status_connected"],
            checks["wrapper_argv_exact"],
            checks["provider_environment_isolated"],
            not checks["unexpected_environment_variables"],
            checks["expected_methods_observed"],
            checks["methods_observed"] == list(EXPECTED_METHODS),
            checks["mem_current_project_listed"],
            not checks["tools_call_observed"],
            not isolation["credentials_forwarded"],
        )
    )
    disposition = receipt["disposition"]
    if disposition == "failed":
        if successful_evidence:
            raise StartupAcceptanceError(
                "failed startup receipt contains wholly successful evidence"
            )
        return
    if not successful_evidence:
        raise StartupAcceptanceError(
            "successful startup disposition is not supported by its checks"
        )
    expected_clean = disposition == "startup_validated_tool_call_unverified"
    if receipt["source"]["clean"] is not expected_clean:
        raise StartupAcceptanceError(
            "startup disposition does not match the source cleanliness state"
        )


def build_receipt(executable: Path, *, generated_at: str) -> dict[str, Any]:
    platform_id, architecture = current_platform()
    source = source_identity()
    resolved = resolve_pinned_host(executable)
    provenance = homebrew_provenance(resolved)
    with tempfile.TemporaryDirectory(prefix="naos-opencode-startup-") as temporary:
        root = Path(temporary)
        environment = host_environment(root)
        home = Path(environment["HOME"])
        workspace = root / "workspace"
        init_workspace(workspace, "https://github.com/example-org/example-product.git")
        (workspace / ".git" / "info" / "exclude").write_text(
            "/opencode.json\n/opencode.json.engram-backup-*\n",
            encoding="utf-8",
        )
        version = observed_version(resolved, environment, workspace)

        config_dir = root / "managed-config"
        bin_dir = root / "bin"
        config_dir.mkdir()
        bin_dir.mkdir()
        registry_path = config_dir / "projects.json"
        registry_path.write_text(
            json.dumps(synthetic_registry(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        shutil.copy2(
            ROOT / "tools" / "engram_memory.py", config_dir / "engram_memory.py"
        )
        shutil.copy2(ROOT / "tools" / "provider.py", config_dir / "provider.py")
        shutil.copy2(
            ROOT / "config" / "release-support.json",
            config_dir / "release-support.json",
        )
        trace_path = root / "synthetic-provider-trace.jsonl"
        provider = root / "engram"
        synthetic_provider(provider, trace_path)
        provider_lifecycle.adopt_existing_provider(
            memory.load_json(ROOT / "config" / "release-support.json"),
            candidate=provider,
            home=home,
            config_dir=config_dir,
            yes=True,
            dry_run=False,
        )
        wrapper_assets = memory.rendered_wrapper_assets(
            config_dir,
            bin_dir,
            provider_path_override=provider,
        )
        wrapper, wrapper_content = wrapper_assets["engram-mcp-wrapper"]
        wrapper.write_text(wrapper_content, encoding="utf-8")
        wrapper.chmod(0o755)
        memory.install_client(
            synthetic_registry(),
            client="opencode-v1",
            project="example-product",
            workspace=workspace,
            home=home,
            wrapper_path=wrapper,
            host_executable=resolved,
            _allow_unmanaged_wrapper=True,
        )
        result = subprocess.run(
            [str(resolved), "mcp", "list", "--pure"],
            cwd=workspace,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        trace = read_trace(trace_path)
        checks, credentials_forwarded = evaluate_startup_trace(
            trace,
            host_returncode=result.returncode,
            host_output=result.stdout,
            expected_home=home,
        )
        checks_passed = all(
            (
                checks["host_exit_zero"],
                checks["server_status_connected"],
                checks["wrapper_argv_exact"],
                checks["provider_environment_isolated"],
                checks["expected_methods_observed"],
                not credentials_forwarded,
                not checks["tools_call_observed"],
                checks["mem_current_project_listed"],
            )
        )
        if not checks_passed:
            disposition = "failed"
        elif source["clean"]:
            disposition = "startup_validated_tool_call_unverified"
        else:
            disposition = "diagnostic_startup_validated_dirty_source"
        receipt = {
            "schema_version": 1,
            "generated_at": generated_at,
            "host_id": "opencode-v1",
            "evidence_class": "real_host",
            "evidence_scope": "startup_initialize_tools_list",
            "runtime_support_claim": False,
            "platform": platform_id,
            "source": source,
            "host": {
                "resolved_executable": str(resolved),
                "version": version,
                "sha256": sha256_file(resolved),
                "architecture": architecture,
                "package_provenance": provenance,
            },
            "isolation": {
                "disposable_home": True,
                "live_home_accessed": False,
                "credentials_forwarded": credentials_forwarded,
                "external_plugins_disabled": True,
                "autoupdate_disabled": True,
                "model_provider_used": False,
            },
            "fixtures": {
                "project_registry": "synthetic",
                "engram_provider": "synthetic_stdio",
                "production_memory_accessed": False,
            },
            "checks": checks,
            "disposition": disposition,
            "limitations": [
                "No model provider was invoked.",
                "mem_current_project was listed but not called.",
                "Network activity was not syscall-instrumented.",
                "This receipt applies only to the exact pinned host binary, platform, source identity, and synthetic provider fixture.",
            ],
        }
        validate_receipt_semantics(receipt)
        return receipt


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host-executable", type=Path, default=Path("/opt/homebrew/bin/opencode")
    )
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = build_receipt(
            args.host_executable,
            generated_at=args.generated_at or utc_now(),
        )
    except (
        StartupAcceptanceError,
        memory.EngramMemoryError,
        provider_lifecycle.ProviderError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        memory.atomic_write_text(args.output, encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if receipt["disposition"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
