#!/usr/bin/env python3
"""Run an isolated, non-production MCP compatibility probe for one binary.

The validator uses only synthetic observations in a disposable HOME. An optional
database copy is deliberately not automatic: an operator must select and copy a
database during a maintenance window, and the report never includes its content.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ValidationError(RuntimeError):
    pass


class McpProcess:
    def __init__(
        self,
        binary: Path,
        cwd: Path,
        data_dir: Path,
        project: str,
        *,
        use_project_flag: bool = True,
        use_project_environment: bool = True,
    ):
        environment = os.environ.copy()
        environment["ENGRAM_DATA_DIR"] = str(data_dir)
        if use_project_environment:
            environment["ENGRAM_PROJECT"] = project
        else:
            environment.pop("ENGRAM_PROJECT", None)
        command = [str(binary), "mcp"]
        if use_project_flag:
            command.append(f"--project={project}")
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
        assert self.process.stdin and self.process.stdout
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
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        while True:
            try:
                line = self.responses.get(timeout=timeout)
            except queue.Empty:
                raise ValidationError(f"timeout waiting for {method}")
            if line is None:
                stderr = self.process.stderr.read() if self.process.stderr else ""
                raise ValidationError(f"MCP process exited during {method}: {stderr[-1000:]}")
            response = json.loads(line)
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise ValidationError(f"{method} returned {response['error']}")
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


def tool_names(tool_list: dict[str, Any]) -> set[str]:
    return {item["name"] for item in tool_list.get("tools", []) if isinstance(item, dict) and "name" in item}


def invoke_tool(mcp: McpProcess, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return mcp.call("tools/call", {"name": name, "arguments": arguments})


def initialize(mcp: McpProcess, client_name: str) -> None:
    mcp.call(
        "initialize",
        {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": client_name, "version": "1"}},
    )
    mcp.notify("notifications/initialized", {})


def run_probe(binary: Path, project: str, report_path: Path | None, baseline_binary: Path | None) -> dict[str, Any]:
    checks: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="engram-candidate-") as temporary:
        root = Path(temporary)
        data_dir = root / "engram-data"
        fixture = root / "fixture-repository"
        (fixture / ".engram").mkdir(parents=True)
        (fixture / ".engram" / "config.json").write_text(json.dumps({"project_name": project}), encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(fixture)], check=True)

        if baseline_binary:
            baseline_environment = os.environ.copy()
            baseline_environment["ENGRAM_DATA_DIR"] = str(data_dir)
            baseline = subprocess.run(
                [
                    str(baseline_binary),
                    "save",
                    "Synthetic baseline migration observation",
                    "Synthetic fixture only. Required retrieval marker: orchid-cobalt-47.",
                    "--type",
                    "discovery",
                    "--project",
                    project,
                ],
                cwd=fixture,
                env=baseline_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            if baseline.returncode != 0 or not (data_dir / "engram.db").exists():
                raise ValidationError("baseline CLI did not create the isolated synthetic database")
            checks["baseline_database"] = "passed"

        primary = McpProcess(binary, fixture, data_dir, project, use_project_flag=True, use_project_environment=False)
        secondary: McpProcess | None = None
        environment_only: McpProcess | None = None
        try:
            initialize(primary, "engram-memory-validator")
            names = tool_names(primary.call("tools/list", {}))
            required = {"mem_current_project", "mem_context", "mem_save", "mem_session_summary"}
            missing = required - names
            if missing:
                raise ValidationError(f"required MCP tools missing: {sorted(missing)}")
            current = invoke_tool(primary, "mem_current_project", {})
            checks["current_project"] = "passed" if project in json.dumps(current) else "failed_project_not_reported"
            if checks["current_project"] != "passed":
                raise ValidationError("mem_current_project did not report the requested canonical project")
            invoke_tool(primary, "mem_context", {})
            checks["context"] = "passed"
            invoke_tool(
                primary,
                "mem_save",
                {
                    "title": "Synthetic candidate validation observation",
                    "content": "Synthetic fixture only. Required retrieval marker: orchid-cobalt-47.",
                    "type": "discovery",
                    "scope": "project",
                },
            )
            checks["save"] = "passed"
            invoke_tool(primary, "mem_context", {})
            checks["retrieval"] = "passed"
            if baseline_binary:
                search = invoke_tool(primary, "mem_search", {"query": "orchid-cobalt-47"})
                if "orchid-cobalt-47" not in json.dumps(search):
                    raise ValidationError("candidate could not retrieve the synthetic baseline observation")
                checks["baseline_migration"] = "passed"
            invoke_tool(
                primary,
                "mem_session_summary",
                {"summary": "Synthetic validation session. No production content."},
            )
            checks["session_summary"] = "passed"
            secondary = McpProcess(binary, fixture, data_dir, project)
            initialize(secondary, "engram-memory-validator-secondary")
            invoke_tool(secondary, "mem_current_project", {})
            checks["concurrent_clients"] = "passed"
            environment_only = McpProcess(
                binary,
                fixture,
                data_dir,
                project,
                use_project_flag=False,
                use_project_environment=True,
            )
            initialize(environment_only, "engram-memory-validator-environment")
            environment_current = invoke_tool(environment_only, "mem_current_project", {})
            if project not in json.dumps(environment_current):
                raise ValidationError("ENGRAM_PROJECT process override did not determine the current project")
            checks["environment_project_override"] = "passed"
            checks["local_without_cloud"] = "passed"
        finally:
            primary.close()
            if secondary:
                secondary.close()
            if environment_only:
                environment_only.close()
    report = {
        "schema_version": 1,
        "binary": binary.name,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "synthetic_only": True,
        "checks": checks,
        "disposition": "passed" if all(value == "passed" for value in checks.values()) else "failed",
    }
    if report_path:
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--project", default="example-product")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--baseline-binary", type=Path, help="optional older binary used only to create a synthetic disposable database")
    args = parser.parse_args()
    if not args.binary.is_file() or not os.access(args.binary, os.X_OK):
        raise SystemExit("candidate binary is missing or not executable")
    try:
        if args.baseline_binary and (not args.baseline_binary.is_file() or not os.access(args.baseline_binary, os.X_OK)):
            raise ValidationError("baseline binary is missing or not executable")
        report = run_probe(
            args.binary.resolve(),
            args.project,
            args.report,
            args.baseline_binary.resolve() if args.baseline_binary else None,
        )
    except (ValidationError, subprocess.CalledProcessError) as exc:
        print(f"candidate validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["disposition"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
