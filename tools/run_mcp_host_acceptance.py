#!/usr/bin/env python3
"""Build a classified MCP-host acceptance report from synthetic fixtures.

The runner never reads the operator's home directory or MCP configuration. A
local host probe is opt-in and runs only allow-listed inventory commands with
a disposable HOME, XDG configuration root, temporary directory, and Git
workspace. Inventory, static-schema, and synthetic MCP-protocol evidence never
produce a host-runtime support claim.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
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

from tools import engram_memory as memory


DEFAULT_CATALOGUE = ROOT / "config" / "mcp-hosts.v1.json"
DEFAULT_MATRIX = ROOT / "config" / "mcp-host-acceptance.v1.json"
DEFAULT_OBSERVATIONS = ROOT / "config" / "mcp-host-observations.v1.json"
PLATFORMS = ("darwin_arm64", "ubuntu_24_04_amd64_container", "windows_amd64")

ADAPTERS: dict[str, tuple[str, str | None, str | None]] = {
    "codex": ("codex", "example-product", None),
    "vscode-generic": ("vscode-generic", "example-product", None),
    "claude-code": ("claude-code", None, None),
    "opencode-v1": ("opencode-v1", "example-product", None),
    "cursor": ("cursor", "example-product", None),
    "antigravity": ("antigravity", "example-product", None),
    "kilo": ("kilo", "example-product", None),
    "muse": ("muse", None, None),
}


class AcceptanceError(ValueError):
    """The catalogue/matrix or a synthetic acceptance invariant is invalid."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"cannot load JSON document: {path.name}") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"JSON document must be an object: {path.name}")
    return value


def require_exact_keys(value: Any, required: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != required:
        raise AcceptanceError(f"{label} must contain exactly {sorted(required)}")
    return value


def validate_observation_register(
    catalogue: dict[str, Any], register: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    require_exact_keys(
        register,
        {"$schema", "schema_version", "register_version", "claim_rule", "observations"},
        "observation register",
    )
    if (
        register["$schema"] != "schemas/mcp-host-observations.v1.schema.json"
        or register["schema_version"] != 1
        or not isinstance(register["register_version"], str)
        or not register["register_version"]
        or not isinstance(register["claim_rule"], str)
        or not register["claim_rule"]
        or not isinstance(register["observations"], list)
    ):
        raise AcceptanceError("unsupported MCP-host observation register")

    host_ids = {item.get("id") for item in catalogue.get("hosts", [])}
    observation_required = {
        "id",
        "host_id",
        "execution_mode",
        "recorded_at",
        "executed_at",
        "platform",
        "host_layers",
        "provider",
        "toolkit",
        "project_resolution",
        "checks",
        "clean_quit",
        "runtime_support_claim",
        "disposition",
        "limitations",
        "source_refs",
    }
    check_names = {
        "host_connected_mcp_server",
        "mem_current_project",
        "registered_canonical_project",
        "workspace_signal_provenance",
        "wrapper_resolution_provenance",
        "memory_read",
        "memory_save",
        "unregistered_project",
        "remote_mismatch",
        "provider_unavailable",
        "several_projects_isolated",
        "clean_profile_isolation",
        "credential_isolation",
        "restart_persistence",
    }
    check_statuses = {"passed", "failed", "not_performed", "not_run", "not_retained"}
    observations: dict[str, dict[str, Any]] = {}
    for raw in register["observations"]:
        item = require_exact_keys(raw, observation_required, "host observation")
        observation_id = item["id"]
        if not isinstance(observation_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]*", observation_id) is None:
            raise AcceptanceError("host observation has an invalid ID")
        if observation_id in observations:
            raise AcceptanceError(f"duplicate host observation ID: {observation_id}")
        if item["host_id"] not in host_ids:
            raise AcceptanceError(f"host observation references unknown host: {item['host_id']}")
        if item["execution_mode"] not in {
            "standalone_cli",
            "desktop_app_embedded_engine",
            "ide_extension_embedded_engine",
        }:
            raise AcceptanceError(f"host observation has invalid execution mode: {observation_id}")
        for field in ("recorded_at", "executed_at"):
            value = item[field]
            if value is None and field == "executed_at":
                continue
            if not isinstance(value, str):
                raise AcceptanceError(f"host observation {field} is invalid: {observation_id}")
            try:
                parsed_timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise AcceptanceError(f"host observation {field} is invalid: {observation_id}") from exc
            if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
                raise AcceptanceError(f"host observation {field} is invalid: {observation_id}")

        platform = require_exact_keys(
            item["platform"], {"os", "os_version", "os_build", "architecture"}, "observation platform"
        )
        if platform["os"] not in {"macos", "ubuntu", "windows", "unknown"} or platform[
            "architecture"
        ] not in {"arm64", "x86_64", "unknown"}:
            raise AcceptanceError(f"host observation platform is invalid: {observation_id}")
        if any(
            platform[field] is not None
            and (not isinstance(platform[field], str) or not platform[field])
            for field in ("os_version", "os_build")
        ):
            raise AcceptanceError(f"host observation platform version is invalid: {observation_id}")

        layers = item["host_layers"]
        if not isinstance(layers, list) or not layers:
            raise AcceptanceError(f"host observation layers are invalid: {observation_id}")
        roles: set[str] = set()
        for layer_raw in layers:
            layer = require_exact_keys(layer_raw, {"role", "name", "version", "sha256"}, "host layer")
            if layer["role"] not in {
                "outer_application",
                "ide_extension",
                "embedded_engine",
                "standalone_engine",
            }:
                raise AcceptanceError(f"host observation layer role is invalid: {observation_id}")
            if layer["role"] in roles or not isinstance(layer["name"], str) or not layer["name"]:
                raise AcceptanceError(f"host observation layer is invalid: {observation_id}")
            roles.add(layer["role"])
            if not isinstance(layer["version"], str) or not layer["version"]:
                raise AcceptanceError(f"host observation layer version is invalid: {observation_id}")
            if layer["sha256"] is not None and re.fullmatch(r"[0-9a-f]{64}", layer["sha256"]) is None:
                raise AcceptanceError(f"host observation layer hash is invalid: {observation_id}")
        required_roles = {
            "standalone_cli": {"standalone_engine"},
            "desktop_app_embedded_engine": {"outer_application", "embedded_engine"},
            "ide_extension_embedded_engine": {
                "outer_application",
                "ide_extension",
                "embedded_engine",
            },
        }[item["execution_mode"]]
        if roles != required_roles:
            raise AcceptanceError(f"host observation layers do not match execution mode: {observation_id}")

        provider = require_exact_keys(item["provider"], {"name", "version", "sha256"}, "observation provider")
        if not isinstance(provider["name"], str) or not provider["name"]:
            raise AcceptanceError(f"host observation provider is invalid: {observation_id}")
        if provider["version"] is not None and (
            not isinstance(provider["version"], str) or not provider["version"]
        ):
            raise AcceptanceError(f"host observation provider version is invalid: {observation_id}")
        if provider["sha256"] is not None and re.fullmatch(r"[0-9a-f]{64}", provider["sha256"]) is None:
            raise AcceptanceError(f"host observation provider hash is invalid: {observation_id}")

        toolkit = require_exact_keys(
            item["toolkit"],
            {"version", "source_commit_context", "source_tree_context", "installed_wrapper_sha256"},
            "observation toolkit",
        )
        if toolkit["version"] is not None and (
            not isinstance(toolkit["version"], str) or not toolkit["version"]
        ):
            raise AcceptanceError(f"host observation toolkit version is invalid: {observation_id}")
        for field in ("source_commit_context", "source_tree_context"):
            value = toolkit[field]
            if value is not None and re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
                raise AcceptanceError(f"host observation toolkit identity is invalid: {observation_id}")
        wrapper_hash = toolkit["installed_wrapper_sha256"]
        if wrapper_hash is not None and re.fullmatch(r"[0-9a-f]{64}", wrapper_hash) is None:
            raise AcceptanceError(f"host observation wrapper hash is invalid: {observation_id}")

        resolution = require_exact_keys(
            item["project_resolution"],
            {
                "expected_project",
                "returned_project",
                "provider_source",
                "project_path_state",
                "incoming_project_signal",
                "workspace_signal",
                "wrapper_resolution_source",
                "workspace_kind",
            },
            "observation project resolution",
        )
        if resolution["expected_project"] is not None and (
            not isinstance(resolution["expected_project"], str)
            or not resolution["expected_project"]
        ):
            raise AcceptanceError(f"host observation expected project is invalid: {observation_id}")
        if resolution["returned_project"] is not None and (
            not isinstance(resolution["returned_project"], str) or not resolution["returned_project"]
        ):
            raise AcceptanceError(f"host observation returned project is invalid: {observation_id}")
        if (
            not isinstance(resolution["provider_source"], str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", resolution["provider_source"]) is None
        ):
            raise AcceptanceError(f"host observation provider source is invalid: {observation_id}")
        if resolution["project_path_state"] not in {"empty", "nonempty", "not_retained"}:
            raise AcceptanceError(f"host observation project path state is invalid: {observation_id}")
        if resolution["incoming_project_signal"] not in {"present", "absent", "not_retained"}:
            raise AcceptanceError(f"host observation incoming project signal is invalid: {observation_id}")
        if resolution["workspace_signal"] not in {"claude_project_dir", "process_cwd", "not_retained"}:
            raise AcceptanceError(f"host observation workspace signal is invalid: {observation_id}")
        if resolution["wrapper_resolution_source"] not in {
            "approved_git_remote",
            "registered_explicit_project",
            "not_retained",
        }:
            raise AcceptanceError(f"host observation wrapper resolution source is invalid: {observation_id}")
        if resolution["workspace_kind"] not in {
            "synthetic_git_repository",
            "generated_git_worktree",
            "registered_git_repository",
            "not_retained",
        }:
            raise AcceptanceError(f"host observation workspace kind is invalid: {observation_id}")

        checks = require_exact_keys(item["checks"], check_names, "observation checks")
        if any(value not in check_statuses for value in checks.values()):
            raise AcceptanceError(f"host observation check status is invalid: {observation_id}")
        if checks["mem_current_project"] == "passed":
            if (
                resolution["expected_project"] is None
                or resolution["expected_project"] != resolution["returned_project"]
            ):
                raise AcceptanceError(f"host observation canonical project mismatches: {observation_id}")
        elif resolution["returned_project"] is not None:
            raise AcceptanceError(f"host observation returned project is inconsistent: {observation_id}")
        elif checks["registered_canonical_project"] == "passed":
            raise AcceptanceError(f"host observation canonical-project check is inconsistent: {observation_id}")
        if (
            checks["workspace_signal_provenance"] == "passed"
            and resolution["workspace_signal"] == "not_retained"
        ):
            raise AcceptanceError(f"host observation falsely claims workspace-signal provenance: {observation_id}")
        if (
            checks["wrapper_resolution_provenance"] == "passed"
            and resolution["wrapper_resolution_source"] == "not_retained"
        ):
            raise AcceptanceError(f"host observation falsely claims wrapper-resolution provenance: {observation_id}")
        if item["runtime_support_claim"] is not False or item["disposition"] != "partial_real_host_evidence_not_promoted":
            raise AcceptanceError(f"partial host observation cannot claim runtime support: {observation_id}")
        if not isinstance(item["limitations"], list) or not item["limitations"] or any(
            not isinstance(value, str) or not value for value in item["limitations"]
        ):
            raise AcceptanceError(f"host observation limitations are invalid: {observation_id}")

        clean_quit = require_exact_keys(
            item["clean_quit"], {"runtime_processes", "database_holders", "client_leases"}, "clean-quit evidence"
        )
        for probe_raw in clean_quit.values():
            probe = require_exact_keys(probe_raw, {"status", "method", "observed_count"}, "clean-quit probe")
            if probe["status"] not in {"passed", "failed", "not_run", "not_retained"}:
                raise AcceptanceError(f"clean-quit probe status is invalid: {observation_id}")
            if not isinstance(probe["method"], str) or not probe["method"]:
                raise AcceptanceError(f"clean-quit probe method is invalid: {observation_id}")
            observed_count = probe["observed_count"]
            if observed_count is not None and (type(observed_count) is not int or observed_count < 0):
                raise AcceptanceError(f"clean-quit probe count is invalid: {observation_id}")
            if probe["status"] == "passed" and observed_count != 0:
                raise AcceptanceError(f"clean-quit probe passed with nonzero count: {observation_id}")
            if probe["status"] == "failed" and (type(observed_count) is not int or observed_count < 1):
                raise AcceptanceError(f"clean-quit probe failed without positive count: {observation_id}")
            if probe["status"] in {"not_run", "not_retained"} and observed_count is not None:
                raise AcceptanceError(f"clean-quit probe status/count is inconsistent: {observation_id}")

        if not isinstance(item["source_refs"], list):
            raise AcceptanceError(f"host observation source references are invalid: {observation_id}")
        for source_raw in item["source_refs"]:
            source = require_exact_keys(source_raw, {"repository_path", "sha256"}, "observation source reference")
            path = Path(source["repository_path"])
            if path.is_absolute() or ".." in path.parts or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None:
                raise AcceptanceError(f"host observation source reference is unsafe: {observation_id}")
            source_component = ROOT.resolve()
            try:
                for component in path.parts:
                    source_component = source_component / component
                    if source_component.is_symlink():
                        raise AcceptanceError(f"host observation source reference is unsafe: {observation_id}")
            except OSError as exc:
                raise AcceptanceError(f"host observation source reference is unsafe: {observation_id}") from exc
            source_path = ROOT / path
            try:
                resolved_source = source_path.resolve(strict=True)
            except OSError as exc:
                raise AcceptanceError(f"host observation source reference is stale: {observation_id}") from exc
            if (
                not resolved_source.is_relative_to(ROOT.resolve())
                or not resolved_source.is_file()
                or hashlib.sha256(resolved_source.read_bytes()).hexdigest() != source["sha256"]
            ):
                raise AcceptanceError(f"host observation source reference is stale: {observation_id}")
        observations[observation_id] = item
    return observations


def validate_matrix(
    catalogue: dict[str, Any],
    matrix: dict[str, Any],
    observations: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    try:
        memory.host_catalogue(catalogue)
    except memory.EngramMemoryError as exc:
        raise AcceptanceError(str(exc)) from exc
    if matrix.get("schema_version") != 1 or not isinstance(matrix.get("hosts"), list):
        raise AcceptanceError("unsupported MCP-host acceptance matrix")
    observation_by_id = validate_observation_register(
        catalogue, observations if observations is not None else load_json(DEFAULT_OBSERVATIONS)
    )
    catalogue_ids = [item.get("id") for item in catalogue["hosts"]]
    matrix_ids = [item.get("id") for item in matrix["hosts"]]
    if len(catalogue_ids) != len(set(catalogue_ids)):
        raise AcceptanceError("duplicate host ID in catalogue")
    if len(matrix_ids) != len(set(matrix_ids)):
        raise AcceptanceError("duplicate host ID in acceptance matrix")
    if set(catalogue_ids) != set(matrix_ids):
        missing = sorted(set(catalogue_ids) - set(matrix_ids))
        extra = sorted(set(matrix_ids) - set(catalogue_ids))
        raise AcceptanceError(f"acceptance matrix coverage mismatch: missing={missing}, extra={extra}")
    required = {
        "id",
        "current_evidence_class",
        "acceptance_target",
        "cli_binary",
        "safe_probe",
        "blocker",
        "promotion_command",
        "promotion_gate",
    }
    classes = {"real_host", "headless_extension_host", "mcp_protocol", "static_schema"}
    targets = {"real_host", "headless_extension_host", "ineligible"}
    referenced_observations: set[str] = set()
    for item in matrix["hosts"]:
        absent = required - set(item)
        if absent:
            raise AcceptanceError(f"acceptance matrix entry {item.get('id')!r} misses {sorted(absent)}")
        if item["current_evidence_class"] not in classes:
            raise AcceptanceError(f"invalid evidence class for {item['id']}")
        if item["acceptance_target"] not in targets:
            raise AcceptanceError(f"invalid acceptance target for {item['id']}")
        if not item["blocker"] or not item["promotion_command"] or not item["promotion_gate"]:
            raise AcceptanceError(f"host {item['id']} must retain a blocker and exact promotion gate")
        if item["cli_binary"] is None and item["safe_probe"]:
            raise AcceptanceError(f"host {item['id']} cannot declare probes without a CLI binary")
        refs = item.get("observation_refs", [])
        if not isinstance(refs, list) or len(refs) != len(set(refs)) or any(
            not isinstance(value, str) for value in refs
        ):
            raise AcceptanceError(f"host {item['id']} has invalid observation references")
        if "observed_real_host_case" in item and not refs:
            raise AcceptanceError(f"host {item['id']} retains a real-host case without observation references")
        for observation_id in refs:
            observation = observation_by_id.get(observation_id)
            if observation is None or observation["host_id"] != item["id"]:
                raise AcceptanceError(f"host {item['id']} has an unknown or mismatched observation reference")
            referenced_observations.add(observation_id)
        for arguments in item["safe_probe"]:
            if not isinstance(arguments, list) or not arguments or not all(isinstance(value, str) for value in arguments):
                raise AcceptanceError(f"host {item['id']} has an invalid safe probe")
    binaries = {item["id"]: item["cli_binary"] for item in matrix["hosts"]}
    if binaries.get("opencode-v1") != "opencode":
        raise AcceptanceError("stable OpenCode V1 must bind to opencode")
    unreferenced = sorted(set(observation_by_id) - referenced_observations)
    if unreferenced:
        raise AcceptanceError(f"observation register contains unreferenced evidence: {unreferenced}")
    return observation_by_id


def check(
    identifier: str,
    status: str,
    evidence_class: str,
    detail: str,
    *,
    runtime_support_claim: bool = False,
) -> dict[str, Any]:
    if evidence_class in {"static_schema", "mcp_protocol", "real_host_inventory"} and runtime_support_claim:
        raise AcceptanceError(f"{evidence_class} evidence cannot claim host-runtime support")
    return {
        "id": identifier,
        "status": status,
        "evidence_class": evidence_class,
        "runtime_support_claim": runtime_support_claim,
        "detail": detail,
    }


def static_schema_check(host_id: str) -> dict[str, Any]:
    adapter = ADAPTERS.get(host_id)
    if adapter is None:
        return check(
            "adapter_schema",
            "not_applicable",
            "static_schema",
            "Catalogue role/transport classification only; no managed adapter exists.",
        )
    client, project, version = adapter
    try:
        if client == "codex":
            rendered = memory.client_template(
                client,
                project,
                host_version=version,
                workspace_path=ROOT / "registered" / "example-product",
                wrapper_command=str(ROOT / "bin" / "engram-mcp-wrapper"),
            )
            document = memory.parse_codex_mcp_toml(rendered)
        else:
            rendered = memory.client_template(client, project, host_version=version)
            document = json.loads(rendered)
        if not isinstance(document, dict):
            raise AcceptanceError("rendered adapter is not an object/table")
    except (ValueError, json.JSONDecodeError) as exc:
        return check("adapter_schema", "failed", "static_schema", f"Adapter parse failed: {type(exc).__name__}.")
    return check(
        "adapter_schema",
        "passed",
        "static_schema",
        "Checked-in adapter rendered and parsed; host startup and MCP behavior were not exercised.",
    )


def init_workspace(path: Path, remote: str) -> None:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def synthetic_registry() -> dict[str, Any]:
    """Return a private-checkout-independent registry with no real mappings."""
    return {
        "schema_version": 1,
        "projects": [
            {
                "id": "example-product",
                "aliases": ["sample-product"],
                "remotes": ["https://github.com/example-org/example-product.git"],
            },
            {
                "id": "example-platform",
                "aliases": ["sample-platform"],
                "remotes": ["https://github.com/example-org/example-platform.git"],
            },
        ],
    }


def fake_provider(path: Path) -> None:
    source = f"""#!{sys.executable}
import json
import sys

if len(sys.argv) > 1 and sys.argv[1] == "version":
    print("engram 1.20.0")
    raise SystemExit(0)
if len(sys.argv) > 1 and sys.argv[1] == "doctor":
    print("{{}}")
    raise SystemExit(0)
if len(sys.argv) < 2 or sys.argv[1] != "mcp":
    raise SystemExit(2)
project = next((item.split("=", 1)[1] for item in sys.argv[2:] if item.startswith("--project=")), "unresolved")
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {{"protocolVersion": "2025-03-26", "capabilities": {{"tools": {{}}}}, "serverInfo": {{"name": "synthetic-engram", "version": "0"}}}}
    elif method == "tools/list":
        result = {{"tools": [{{"name": "mem_current_project", "description": "synthetic", "inputSchema": {{"type": "object"}}}}]}}
    elif method == "tools/call":
        result = {{"content": [{{"type": "text", "text": project}}]}}
    else:
        result = {{}}
    print(json.dumps({{"jsonrpc": "2.0", "id": request.get("id"), "result": result}}), flush=True)
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def wrapper_call(
    root: Path,
    workspace: Path,
    provider: Path,
    registry_path: Path,
    *,
    project: str | None,
    unavailable: bool = False,
) -> subprocess.CompletedProcess[str]:
    home = root / "home"
    config_home = root / "xdg"
    config = config_home / memory.TOOLKIT_NAMESPACE
    home.mkdir(exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "tools" / "engram_memory.py", config / "engram_memory.py")
    shutil.copy2(ROOT / "tools" / "provider.py", config / "provider.py")
    shutil.copy2(registry_path, config / "projects.json")
    shutil.copy2(ROOT / "config" / "release-support.json", config / "release-support.json")
    if os.name != "nt":
        memory.provider_lifecycle.adopt_existing_provider(
            memory.load_json(ROOT / "config" / "release-support.json"),
            candidate=provider,
            home=home,
            config_dir=config,
            selected_platform="linux_amd64",
            yes=True,
            dry_run=False,
        )
    wrapper = root / "engram-mcp-wrapper"
    wrapper.write_text(
        (ROOT / "scripts" / "engram_mcp_wrapper.sh").read_text(encoding="utf-8")
        .replace("__NAOS_ENGRAM_MEMORY_CONFIG_DIR__", str(config))
        .replace("__PYTHON_EXECUTABLE__", str(Path(sys.executable).resolve())),
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(config_home),
        "XDG_CONFIG_HOME": str(config_home),
        "TMPDIR": str(root / "tmp"),
        "LANG": "C.UTF-8",
        "ENGRAM_BIN": str(root / "missing-engram") if unavailable else str(provider),
    }
    (root / "tmp").mkdir(exist_ok=True)
    if project is not None:
        environment["ENGRAM_PROJECT"] = project
    messages = "\n".join(
        json.dumps(item)
        for item in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "mem_current_project", "arguments": {}},
            },
        )
    ) + "\n"
    if os.name == "nt":
        resolution = subprocess.run(
            [
                sys.executable,
                str(config / "engram_memory.py"),
                "--registry",
                str(config / "projects.json"),
                "resolve",
                "--cwd",
                str(workspace),
                *( ["--project", project] if project is not None else [] ),
            ],
            cwd=workspace,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if resolution.returncode != 0:
            return subprocess.CompletedProcess(
                [str(config / "engram_mcp_wrapper.ps1")], 64, "", resolution.stderr
            )
        if unavailable:
            return subprocess.CompletedProcess(
                [str(config / "engram_mcp_wrapper.ps1")], 127, "", "provider unavailable"
            )
        try:
            canonical = json.loads(resolution.stdout)["project"]
        except (KeyError, json.JSONDecodeError):
            return subprocess.CompletedProcess(
                [str(config / "engram_mcp_wrapper.ps1")], 64, "", "invalid project resolution"
            )
        return subprocess.run(
            [sys.executable, str(provider), "mcp", "--tools=mem_current_project,mem_context,mem_search,mem_get_observation,mem_save,mem_session_summary", f"--project={canonical}"],
            cwd=workspace,
            env=environment,
            input=messages,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    return subprocess.run(
        [str(wrapper)],
        cwd=workspace,
        env=environment,
        input=messages,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )


def protocol_project(result: subprocess.CompletedProcess[str]) -> str | None:
    if result.returncode != 0:
        return None
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if len(responses) != 3:
        return None
    names = [item.get("name") for item in responses[1].get("result", {}).get("tools", [])]
    content = responses[2].get("result", {}).get("content", [])
    if "mem_current_project" not in names or not content:
        return None
    return content[0].get("text")


def scenario_checks(report_platform: str) -> list[dict[str, Any]]:
    if report_platform == "windows_amd64":
        return [
            check(
                identifier,
                "not_run",
                "static_schema",
                "Windows classification is retained, but this synthetic protocol runner uses the POSIX wrapper and does not execute a Windows host/provider fixture.",
            )
            for identifier in (
                "registered_project",
                "unregistered_project",
                "remote_mismatch",
                "provider_unavailable",
                "several_projects_isolated",
                "instruction_install",
                "instruction_idempotent",
                "instruction_conflict_refused",
            )
        ]
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="naos-engram-host-acceptance-") as temporary:
        root = Path(temporary)
        registry_path = root / "synthetic-projects.json"
        registry_path.write_text(
            json.dumps(synthetic_registry(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        product = root / "registered-product"
        platform = root / "registered-platform"
        unknown = root / "unregistered"
        init_workspace(product, "https://github.com/example-org/example-product.git")
        init_workspace(platform, "https://github.com/example-org/example-platform.git")
        init_workspace(unknown, "https://example.invalid/unregistered.git")
        provider = root / "engram"
        fake_provider(provider)

        registered = wrapper_call(
            root, product, provider, registry_path, project="example-product"
        )
        results.append(
            check(
                "registered_project",
                "passed" if protocol_project(registered) == "example-product" else "failed",
                "mcp_protocol",
                "Synthetic peer completed initialize, tools/list, and canonical-project tool response through the managed wrapper.",
            )
        )

        unregistered = wrapper_call(root, unknown, provider, registry_path, project=None)
        results.append(
            check(
                "unregistered_project",
                "passed" if unregistered.returncode == 64 else "failed",
                "mcp_protocol",
                "Wrapper refused an unregistered synthetic Git remote before starting the provider.",
            )
        )

        mismatch = wrapper_call(
            root, platform, provider, registry_path, project="example-product"
        )
        results.append(
            check(
                "remote_mismatch",
                "passed" if mismatch.returncode == 64 else "failed",
                "mcp_protocol",
                "Wrapper refused a fixed project whose synthetic Git remote mapped to a different canonical project.",
            )
        )

        unavailable = wrapper_call(
            root,
            product,
            provider,
            registry_path,
            project="example-product",
            unavailable=True,
        )
        results.append(
            check(
                "provider_unavailable",
                "passed" if unavailable.returncode == 127 else "failed",
                "mcp_protocol",
                "Wrapper resolved identity, then failed with the provider-unavailable exit contract; no fallback provider was selected.",
            )
        )

        product_result = wrapper_call(root, product, provider, registry_path, project=None)
        platform_result = wrapper_call(root, platform, provider, registry_path, project=None)
        isolated = (
            protocol_project(product_result) == "example-product"
            and protocol_project(platform_result) == "example-platform"
        )
        results.append(
            check(
                "several_projects_isolated",
                "passed" if isolated else "failed",
                "mcp_protocol",
                "Two synthetic repositories resolved independently from approved remotes; no folder-name or cross-project fallback was used.",
            )
        )

        instruction_project = root / "instruction-project"
        instruction_project.mkdir()
        dry_run = memory.install_instruction(instruction_project, "codex", dry_run=True)
        installed = memory.install_instruction(instruction_project, "codex")
        results.append(
            check(
                "instruction_install",
                "passed" if dry_run["status"] == "dry_run" and installed["status"] == "installed" else "failed",
                "static_schema",
                "Synthetic project instruction lifecycle preserved preview-before-write behavior.",
            )
        )
        repeated = memory.install_instruction(instruction_project, "codex")
        results.append(
            check(
                "instruction_idempotent",
                "passed" if repeated["status"] == "already_configured" else "failed",
                "static_schema",
                "A repeated synthetic instruction installation made no duplicate managed block.",
            )
        )
        conflict_project = root / "instruction-conflict"
        conflict_project.mkdir()
        target = conflict_project / "AGENTS.md"
        original = memory.INSTRUCTION_BEGIN + "\nmissing end marker\n"
        target.write_text(original, encoding="utf-8")
        conflict_refused = False
        try:
            memory.install_instruction(conflict_project, "codex")
        except memory.EngramMemoryError:
            conflict_refused = target.read_text(encoding="utf-8") == original
        results.append(
            check(
                "instruction_conflict_refused",
                "passed" if conflict_refused else "failed",
                "static_schema",
                "Malformed managed markers were refused without changing the synthetic instruction file.",
            )
        )
    return results


def synthetic_probe_environment(root: Path) -> dict[str, str]:
    home = root / "home"
    xdg = root / "xdg"
    temporary = root / "tmp"
    for path in (home, xdg, temporary):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg),
        "TMPDIR": str(temporary),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
    }


def sanitized_version(output: str) -> str | None:
    pattern = re.compile(r"\b(?:v)?(\d+\.\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.-]+)?)\b")
    for line in output.splitlines():
        if "ERROR" in line.upper() or line.lstrip().startswith("["):
            continue
        match = pattern.search(line)
        if match:
            return match.group(1)
    return None


def local_probe(entry: dict[str, Any], requested: bool) -> dict[str, Any]:
    binary = entry["cli_binary"]
    if not requested:
        return {"status": "not_requested", "evidence_class": "static_schema", "runtime_support_claim": False}
    if binary is None:
        return {"status": "not_applicable", "evidence_class": "static_schema", "runtime_support_claim": False}
    resolved = shutil.which(binary)
    if resolved is None and os.name == "nt":
        for path_entry in os.environ.get("PATH", "").split(os.pathsep):
            candidate = Path(path_entry) / binary
            if candidate.is_file():
                resolved = str(candidate)
                break
    if resolved is None:
        return {
            "status": "binary_unavailable",
            "evidence_class": "real_host_inventory",
            "runtime_support_claim": False,
            "binary": binary,
            "version": None,
            "checks": [],
        }
    checks: list[dict[str, Any]] = []
    version: str | None = None
    with tempfile.TemporaryDirectory(prefix=f"naos-engram-{entry['id']}-probe-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        init_workspace(workspace, "https://github.com/example-org/example-product.git")
        environment = synthetic_probe_environment(root)
        for index, arguments in enumerate(entry["safe_probe"]):
            try:
                command = [resolved, *arguments]
                if os.name == "nt":
                    candidate = Path(resolved)
                    if candidate.suffix.lower() not in {".exe", ".cmd", ".bat"} and candidate.read_bytes()[:2] == b"#!":
                        bash = shutil.which("bash")
                        if bash:
                            command = [bash, candidate.as_posix(), *arguments]
                result = subprocess.run(
                    command,
                    cwd=workspace,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=15,
                    check=False,
                )
                observed = sanitized_version(result.stdout)
                if version is None and observed is not None:
                    version = observed
                checks.append(
                    check(
                        f"inventory_probe_{index + 1}",
                        "passed" if result.returncode == 0 else "failed",
                        "real_host_inventory",
                        f"Allow-listed command returned exit code {result.returncode} in an empty disposable profile; output was not retained.",
                    )
                )
            except subprocess.TimeoutExpired:
                checks.append(
                    check(
                        f"inventory_probe_{index + 1}",
                        "failed",
                        "real_host_inventory",
                        "Allow-listed command exceeded the 15-second disposable-profile inventory timeout; output was not retained.",
                    )
                )
    return {
        "status": "executed",
        "evidence_class": "real_host_inventory",
        "runtime_support_claim": False,
        "binary": binary,
        "version": version,
        "checks": checks,
    }


def build_report(
    catalogue: dict[str, Any],
    matrix: dict[str, Any],
    *,
    platform: str,
    selected_host: str | None,
    probe_local_hosts: bool,
    generated_at: str,
    observations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    observation_register = observations if observations is not None else load_json(DEFAULT_OBSERVATIONS)
    validate_matrix(catalogue, matrix, observation_register)
    catalogue_by_id = {item["id"]: item for item in catalogue["hosts"]}
    matrix_by_id = {item["id"]: item for item in matrix["hosts"]}
    if selected_host is not None and selected_host not in matrix_by_id:
        raise AcceptanceError(f"unknown host: {selected_host}")
    selected = [selected_host] if selected_host else [item["id"] for item in catalogue["hosts"]]
    scenario_results = scenario_checks(platform)
    expected_cases = set(matrix.get("required_cases", []))
    observed_cases = {item["id"] for item in scenario_results}
    if expected_cases != observed_cases:
        raise AcceptanceError(
            f"required scenario coverage mismatch: missing={sorted(expected_cases - observed_cases)}, "
            f"extra={sorted(observed_cases - expected_cases)}"
        )
    host_results = []
    for host_id in selected:
        entry = matrix_by_id[host_id]
        static_result = static_schema_check(host_id)
        probe = local_probe(entry, probe_local_hosts)
        host_results.append(
            {
                "host_id": host_id,
                "catalogue_support_tier": catalogue_by_id[host_id]["support_tier"],
                "current_evidence_class": entry["current_evidence_class"],
                "acceptance_target": entry["acceptance_target"],
                "static_schema": static_result,
                "local_probe": probe,
                "runtime_support_claim": False,
                "observation_refs": list(entry.get("observation_refs", [])),
                "blocker": entry["blocker"],
                "promotion_command": entry["promotion_command"],
                "promotion_gate": entry["promotion_gate"],
                "disposition": "ineligible" if entry["acceptance_target"] == "ineligible" else "not_promoted",
            }
        )
    provider_profiles = matrix.get("combination_policy", {}).get("provider_profiles", [])
    if not provider_profiles or not all(isinstance(item, dict) and item.get("id") for item in provider_profiles):
        raise AcceptanceError("acceptance matrix must declare provider combination profiles")
    combination_results = []
    for host_id in selected:
        entry = matrix_by_id[host_id]
        for profile in provider_profiles:
            ineligible = entry["acceptance_target"] == "ineligible"
            protocol_profile = profile["id"] in {"synthetic_offline", "provider_unavailable"}
            combination_results.append(
                {
                    "host_id": host_id,
                    "platform": platform,
                    "provider_profile": profile["id"],
                    "status": "not_applicable" if ineligible else "blocked",
                    "evidence_class": "mcp_protocol" if protocol_profile and not ineligible else "static_schema",
                    "runtime_support_claim": False,
                    "detail": entry["blocker"]
                    if not ineligible
                    else "Catalogue role/transport classification makes this host-provider cell ineligible.",
                }
            )
    complete = len(selected) == len(catalogue_by_id) and all(
        item["static_schema"]["status"] in {"passed", "not_applicable"} for item in host_results
    )
    all_scenarios_passed = all(item["status"] in {"passed", "not_run"} for item in scenario_results)
    return {
        "schema_version": 1,
        "matrix_version": matrix["matrix_version"],
        "generated_at": generated_at,
        "platform": platform,
        "synthetic_only": True,
        "live_home_accessed": False,
        "credentials_forwarded": False,
        "observation_register": {
            "register_version": observation_register["register_version"],
            "sha256": hashlib.sha256(
                json.dumps(
                    observation_register,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "observation_count": len(observation_register["observations"]),
        },
        "coverage": {
            "catalogue_hosts": len(catalogue_by_id),
            "matrix_hosts": len(matrix_by_id),
            "reported_hosts": len(host_results),
            "complete": complete,
        },
        "scenario_results": scenario_results,
        "host_results": host_results,
        "combination_results": combination_results,
        "limitations": [
            "Synthetic MCP-protocol success validates the wrapper contract, not any named host runtime.",
            "Local CLI probes are version/config inventory in an empty disposable profile; they do not establish MCP startup or provider-backed memory behavior.",
            "GUI-only and subscription/account-gated hosts remain explicitly blocked until external disposable-profile real-host evidence satisfies the recorded gate.",
            "Ubuntu Docker can exercise CLI and headless extension-host lanes, not native desktop GUI behavior.",
            "Cross-OS Git memory transport remains non-shipping: concurrent .engram/manifest.json writers have an unresolved rebase conflict in the strengthened test lane.",
        ],
        "disposition": "classified_not_promoted"
        if all_scenarios_passed
        and all(item["static_schema"]["status"] in {"passed", "not_applicable"} for item in host_results)
        else "invalid",
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", type=Path, default=DEFAULT_CATALOGUE)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--observations", type=Path, default=DEFAULT_OBSERVATIONS)
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--host", help="report one host while still running the shared synthetic cases")
    parser.add_argument("--probe-local-hosts", action="store_true", help="run allow-listed inventory commands in disposable profiles")
    parser.add_argument("--generated-at", default=None, help="RFC3339 timestamp override for deterministic fixtures")
    parser.add_argument("--output", type=Path, help="write JSON report; stdout is used when omitted")
    args = parser.parse_args(argv)
    try:
        report = build_report(
            load_json(args.catalogue),
            load_json(args.matrix),
            platform=args.platform,
            selected_host=args.host,
            probe_local_hosts=args.probe_local_hosts,
            generated_at=args.generated_at or utc_now(),
            observations=load_json(args.observations),
        )
    except (AcceptanceError, memory.EngramMemoryError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["disposition"] == "classified_not_promoted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
