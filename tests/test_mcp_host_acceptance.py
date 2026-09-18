from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def required_keyword_errors(instance, schema, path: str = "$") -> list[str]:
    """Evaluate only the JSON Schema ``required`` keyword for test fixtures."""
    errors: list[str] = []
    if isinstance(instance, dict) and isinstance(schema, dict):
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}.{name}")
        for name, child_schema in schema.get("properties", {}).items():
            if name in instance:
                errors.extend(
                    required_keyword_errors(
                        instance[name], child_schema, f"{path}.{name}"
                    )
                )
    elif isinstance(instance, list) and isinstance(schema, dict):
        child_schema = schema.get("items")
        if isinstance(child_schema, dict):
            for index, item in enumerate(instance):
                errors.extend(
                    required_keyword_errors(item, child_schema, f"{path}[{index}]")
                )
    return errors


acceptance = load_module(
    "run_mcp_host_acceptance", ROOT / "tools" / "run_mcp_host_acceptance.py"
)


class MCPHostAcceptanceMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogue = acceptance.load_json(ROOT / "config" / "mcp-hosts.v1.json")
        self.matrix = acceptance.load_json(
            ROOT / "config" / "mcp-host-acceptance.v1.json"
        )
        self.observations = acceptance.load_json(
            ROOT / "config" / "mcp-host-observations.v1.json"
        )

    def test_matrix_classifies_every_catalogue_entry_exactly_once(self) -> None:
        acceptance.validate_matrix(self.catalogue, self.matrix, self.observations)
        catalogue_ids = [item["id"] for item in self.catalogue["hosts"]]
        matrix_ids = [item["id"] for item in self.matrix["hosts"]]
        self.assertEqual(set(catalogue_ids), set(matrix_ids))
        self.assertEqual(len(catalogue_ids), len(matrix_ids))
        self.assertEqual(len(matrix_ids), len(set(matrix_ids)))

    def test_every_unpromoted_or_ineligible_host_has_exact_gate(self) -> None:
        for host in self.matrix["hosts"]:
            self.assertTrue(host["blocker"], host["id"])
            self.assertTrue(
                host["promotion_command"].startswith(
                    "python3 tools/run_mcp_host_acceptance.py --host "
                )
            )
            self.assertIn("--output reports/mcp-hosts/", host["promotion_command"])
            self.assertTrue(host["promotion_gate"], host["id"])

    def test_schema_or_protocol_evidence_cannot_claim_runtime_support(self) -> None:
        for evidence_class in ("static_schema", "mcp_protocol", "real_host_inventory"):
            with self.assertRaisesRegex(acceptance.AcceptanceError, "cannot claim"):
                acceptance.check(
                    "fixture",
                    "passed",
                    evidence_class,
                    "fixture",
                    runtime_support_claim=True,
                )

    def test_matrix_refuses_missing_host(self) -> None:
        broken = copy.deepcopy(self.matrix)
        broken["hosts"] = broken["hosts"][:-1]
        with self.assertRaisesRegex(acceptance.AcceptanceError, "coverage mismatch"):
            acceptance.validate_matrix(self.catalogue, broken)

    def test_stable_opencode_and_terminal_v2_disposition_are_disjoint(self) -> None:
        entries = {item["id"]: item for item in self.matrix["hosts"]}
        self.assertEqual("opencode", entries["opencode-v1"]["cli_binary"])
        self.assertNotIn("required_major", entries["opencode-v1"])
        self.assertNotIn("opencode-v2", entries)
        terminal = self.catalogue["terminal_dispositions"]
        self.assertEqual("opencode-v2", terminal[0]["id"])
        self.assertEqual("owner_excluded", terminal[0]["disposition"])
        broken = copy.deepcopy(self.matrix)
        next(item for item in broken["hosts"] if item["id"] == "opencode-v1")[
            "cli_binary"
        ] = "opencode2"
        with self.assertRaisesRegex(acceptance.AcceptanceError, "stable OpenCode V1"):
            acceptance.validate_matrix(self.catalogue, broken)

    def test_report_schema_is_closed_and_forbids_static_runtime_claim(self) -> None:
        schema = acceptance.load_json(
            ROOT / "config" / "schemas" / "mcp-host-acceptance-report.v1.schema.json"
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("observation_register", schema["required"])
        host_schema = schema["properties"]["host_results"]["items"]
        self.assertFalse(host_schema["additionalProperties"])
        self.assertIn("runtime_support_claim", host_schema["required"])
        self.assertEqual(
            False,
            host_schema["properties"]["local_probe"]["properties"][
                "runtime_support_claim"
            ]["const"],
        )
        self.assertIn("observation_refs", host_schema["required"])

    def test_partial_real_host_evidence_is_retained_without_promotion(self) -> None:
        for host_id in ("codex", "claude-code"):
            matrix_host = next(
                item for item in self.matrix["hosts"] if item["id"] == host_id
            )
            catalogue_host = next(
                item for item in self.catalogue["hosts"] if item["id"] == host_id
            )
            self.assertIn("observed_real_host_case", matrix_host)
            self.assertEqual("manual", catalogue_host["support_tier"])
            self.assertIn(
                "partial real-host evidence, not promotion",
                catalogue_host["evidence"]["fixture_or_report"],
            )
            self.assertTrue(catalogue_host["evidence"]["platforms_tested"])
        codex_catalogue = next(
            item for item in self.catalogue["hosts"] if item["id"] == "codex"
        )
        self.assertIn(
            "VS Code 1.133.0", codex_catalogue["evidence"]["host_version_tested"]
        )
        claude_catalogue = next(
            item for item in self.catalogue["hosts"] if item["id"] == "claude-code"
        )
        self.assertIsNone(claude_catalogue["evidence"]["host_version_tested"])

    def test_codex_dynamic_workspace_observation_remains_partial(self) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        codex = observed["codex-cli-0-145-0-dynamic-workspace-20260818"]
        self.assertEqual("codex", codex["host_id"])
        self.assertEqual("standalone_cli", codex["execution_mode"])
        self.assertEqual("process_cwd", codex["project_resolution"]["workspace_signal"])
        self.assertEqual(
            "approved_git_remote",
            codex["project_resolution"]["wrapper_resolution_source"],
        )
        self.assertEqual(
            "absent", codex["project_resolution"]["incoming_project_signal"]
        )
        self.assertEqual("passed", codex["checks"]["mem_current_project"])
        self.assertEqual("passed", codex["checks"]["clean_profile_isolation"])
        self.assertEqual("not_run", codex["checks"]["credential_isolation"])
        self.assertTrue(
            all(
                probe["status"] == "passed" and probe["observed_count"] == 0
                for probe in codex["clean_quit"].values()
            )
        )
        self.assertFalse(codex["runtime_support_claim"])
        self.assertTrue(
            any(
                "transient process" in limitation for limitation in codex["limitations"]
            )
        )

    def test_codex_cli_reliability_observation_is_exact_and_not_promoted(self) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        observation_id = "codex-cli-0-145-0-reliability-20260818"
        codex = observed[observation_id]
        self.assertEqual("codex", codex["host_id"])
        self.assertEqual("standalone_cli", codex["execution_mode"])
        for check_name in (
            "registered_canonical_project",
            "unregistered_project",
            "provider_unavailable",
            "several_projects_isolated",
            "restart_persistence",
        ):
            self.assertEqual("passed", codex["checks"][check_name])
        self.assertEqual("not_run", codex["checks"]["remote_mismatch"])
        self.assertEqual("not_run", codex["checks"]["credential_isolation"])
        self.assertTrue(
            all(
                probe["status"] == "passed" and probe["observed_count"] == 0
                for probe in codex["clean_quit"].values()
            )
        )
        self.assertFalse(codex["runtime_support_claim"])

        receipt_path = (
            ROOT
            / "docs/evidence/codex-0.145.0-darwin-arm64-reliability-20260818.json"
        )
        receipt = acceptance.load_json(receipt_path)
        host_cases = {item["id"]: item for item in receipt["host_cases"]}
        for case_id in (
            "registered_project_initial",
            "unregistered_project",
            "missing_remote",
            "provider_unavailable",
            "same_basename_second_project",
            "registered_project_restart_and_recovery",
        ):
            self.assertEqual("passed", host_cases[case_id]["status"])
        diagnostics = {item["id"]: item for item in receipt["discarded_diagnostics"]}
        self.assertFalse(diagnostics["concurrent_three_server_fixture"]["acceptance_credit"])
        self.assertFalse(receipt["runtime_support_claim"])
        self.assertEqual(
            hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
            codex["source_refs"][0]["sha256"],
        )
        matrix = next(item for item in self.matrix["hosts"] if item["id"] == "codex")
        self.assertIn(observation_id, matrix["observation_refs"])

    def test_codex_desktop_negative_workspace_signal_is_retained(self) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        observation_id = (
            "codex-desktop-26-810-52044-workspace-signal-negative-20260818"
        )
        desktop = observed[observation_id]
        self.assertEqual("codex", desktop["host_id"])
        self.assertEqual("desktop_app_embedded_engine", desktop["execution_mode"])
        self.assertEqual(
            {"outer_application", "embedded_engine"},
            {layer["role"] for layer in desktop["host_layers"]},
        )
        self.assertEqual(
            "naos-engram-memories",
            desktop["project_resolution"]["expected_project"],
        )
        self.assertIsNone(desktop["project_resolution"]["returned_project"])
        self.assertEqual(
            "process_cwd", desktop["project_resolution"]["workspace_signal"]
        )
        self.assertEqual(
            "absent", desktop["project_resolution"]["incoming_project_signal"]
        )
        self.assertEqual(
            "not_retained",
            desktop["project_resolution"]["wrapper_resolution_source"],
        )
        self.assertEqual(
            "not_retained", desktop["checks"]["host_connected_mcp_server"]
        )
        self.assertEqual("not_performed", desktop["checks"]["mem_current_project"])
        self.assertEqual("passed", desktop["checks"]["workspace_signal_provenance"])
        self.assertEqual(
            "not_retained", desktop["checks"]["wrapper_resolution_provenance"]
        )
        self.assertEqual("passed", desktop["checks"]["clean_profile_isolation"])
        self.assertTrue(
            all(
                probe["status"] == "passed" and probe["observed_count"] == 0
                for probe in desktop["clean_quit"].values()
            )
        )
        self.assertFalse(desktop["runtime_support_claim"])
        self.assertTrue(
            any("filesystem root" in limitation for limitation in desktop["limitations"])
        )
        matrix = next(item for item in self.matrix["hosts"] if item["id"] == "codex")
        self.assertIn(observation_id, matrix["observation_refs"])
        receipt = acceptance.load_json(
            ROOT
            / "docs/evidence/codex-desktop-26.810.52044-embedded-0.148.0-alpha.9-darwin-arm64-workspace-signal-negative-20260818.json"
        )
        self.assertFalse(receipt["mcp_child_observation"]["expected_workspace_match"])
        self.assertEqual(
            "filesystem_root", receipt["mcp_child_observation"]["cwd_state"]
        )
        self.assertEqual(
            0,
            receipt["mcp_child_observation"]["mem_current_project_call_count"],
        )

    def test_codex_project_config_positive_modes_remain_partial(self) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        desktop_id = (
            "codex-desktop-26-810-52044-project-config-current-project-20260818"
        )
        desktop = observed[desktop_id]
        self.assertEqual("desktop_app_embedded_engine", desktop["execution_mode"])
        self.assertEqual("passed", desktop["checks"]["mem_current_project"])
        self.assertEqual("process_cwd", desktop["project_resolution"]["workspace_signal"])
        self.assertEqual(
            "approved_git_remote",
            desktop["project_resolution"]["wrapper_resolution_source"],
        )
        self.assertTrue(
            all(probe["status"] == "not_retained" for probe in desktop["clean_quit"].values())
        )

        vscode_id = (
            "codex-vscode-1-133-0-extension-26-814-41407-current-project-20260818"
        )
        vscode = observed[vscode_id]
        self.assertEqual("codex", vscode["host_id"])
        self.assertEqual("ide_extension_embedded_engine", vscode["execution_mode"])
        self.assertEqual(
            {"outer_application", "ide_extension", "embedded_engine"},
            {layer["role"] for layer in vscode["host_layers"]},
        )
        self.assertEqual("passed", vscode["checks"]["mem_current_project"])
        self.assertEqual("not_run", vscode["checks"]["clean_profile_isolation"])
        self.assertTrue(
            all(
                probe["status"] == "passed" and probe["observed_count"] == 0
                for probe in vscode["clean_quit"].values()
            )
        )
        self.assertFalse(desktop["runtime_support_claim"])
        self.assertFalse(vscode["runtime_support_claim"])
        matrix = next(item for item in self.matrix["hosts"] if item["id"] == "codex")
        self.assertIn(desktop_id, matrix["observation_refs"])
        self.assertIn(vscode_id, matrix["observation_refs"])

    def test_claude_observations_separate_execution_mode_from_stable_adapter(
        self,
    ) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        self.assertEqual(
            {
                "codex-cli-20260813",
                "codex-cli-0-145-0-dynamic-workspace-20260818",
                "codex-cli-0-145-0-reliability-20260818",
                "codex-desktop-26-810-52044-workspace-signal-negative-20260818",
                "codex-desktop-26-810-52044-project-config-current-project-20260818",
                "codex-vscode-1-133-0-extension-26-814-41407-current-project-20260818",
                "claude-code-standalone-cli-20260813",
                "opencode-v1-startup-20260817",
                "opencode-v1-current-project-20260817",
                "muse-current-project-20260817",
                "cursor-desktop-app-3-16-17-current-project-20260817",
                "antigravity-cli-1-1-13-current-project-20260817",
                "antigravity-desktop-app-2-3-1-current-project-20260817",
                "kilo-code-vscode-extension-7-4-22-current-project-20260817",
                "claude-code-desktop-app-embedded-20260817",
                "claude-code-desktop-app-embedded-unregistered-20260817",
            },
            set(observed),
        )
        desktop = observed["claude-code-desktop-app-embedded-20260817"]
        self.assertEqual("claude-code", desktop["host_id"])
        self.assertEqual("desktop_app_embedded_engine", desktop["execution_mode"])
        self.assertEqual(
            {"outer_application", "embedded_engine"},
            {layer["role"] for layer in desktop["host_layers"]},
        )
        self.assertEqual(
            "process_override", desktop["project_resolution"]["provider_source"]
        )
        self.assertEqual("empty", desktop["project_resolution"]["project_path_state"])
        self.assertEqual(
            "not_retained", desktop["project_resolution"]["workspace_signal"]
        )
        self.assertEqual("not_run", desktop["checks"]["restart_persistence"])
        self.assertFalse(desktop["runtime_support_claim"])

    def test_claude_unregistered_desktop_diagnostic_retains_its_claim_ceiling(
        self,
    ) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        diagnostic = observed["claude-code-desktop-app-embedded-unregistered-20260817"]
        self.assertEqual("failed", diagnostic["checks"]["host_connected_mcp_server"])
        self.assertEqual("not_performed", diagnostic["checks"]["mem_current_project"])
        self.assertEqual("not_run", diagnostic["checks"]["unregistered_project"])
        self.assertIsNone(diagnostic["project_resolution"]["expected_project"])
        self.assertIsNone(diagnostic["project_resolution"]["returned_project"])
        self.assertTrue(
            all(
                probe["status"] == "passed" and probe["observed_count"] == 0
                for probe in diagnostic["clean_quit"].values()
            )
        )
        self.assertFalse(diagnostic["runtime_support_claim"])
        self.assertTrue(
            any(
                "does not satisfy the unregistered-project acceptance case"
                in limitation
                for limitation in diagnostic["limitations"]
            )
        )

        contradicted = copy.deepcopy(self.observations)
        diagnostic = next(
            item
            for item in contradicted["observations"]
            if item["id"] == "claude-code-desktop-app-embedded-unregistered-20260817"
        )
        diagnostic["project_resolution"]["returned_project"] = "invented-project"
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "returned project is inconsistent"
        ):
            acceptance.validate_observation_register(self.catalogue, contradicted)

    def test_observation_schema_requires_both_project_fields_for_passed_lookup(
        self,
    ) -> None:
        schema = acceptance.load_json(
            ROOT / "config" / "schemas" / "mcp-host-observations.v1.schema.json"
        )
        passed_lookup_rule = next(
            rule
            for rule in schema["$defs"]["observation"]["allOf"]
            if rule.get("if", {})
            .get("properties", {})
            .get("checks", {})
            .get("properties", {})
            .get("mem_current_project", {})
            .get("const")
            == "passed"
        )
        project_properties = passed_lookup_rule["then"]["properties"][
            "project_resolution"
        ]["properties"]
        required_nonempty = {"type": "string", "minLength": 1}
        self.assertEqual(required_nonempty, project_properties["expected_project"])
        self.assertEqual(required_nonempty, project_properties["returned_project"])

        contradicted = copy.deepcopy(self.observations)
        positive = next(
            item
            for item in contradicted["observations"]
            if item["checks"]["mem_current_project"] == "passed"
        )
        positive["project_resolution"]["expected_project"] = None
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "canonical project mismatches"
        ):
            acceptance.validate_observation_register(self.catalogue, contradicted)

    def test_clean_quit_counts_match_their_disposition(self) -> None:
        nonzero_pass = copy.deepcopy(self.observations)
        desktop = next(
            item
            for item in nonzero_pass["observations"]
            if item["id"] == "claude-code-desktop-app-embedded-20260817"
        )
        desktop["clean_quit"]["runtime_processes"]["observed_count"] = 1
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "passed with nonzero count"
        ):
            acceptance.validate_observation_register(self.catalogue, nonzero_pass)

        zero_failure = copy.deepcopy(self.observations)
        desktop = next(
            item
            for item in zero_failure["observations"]
            if item["id"] == "claude-code-desktop-app-embedded-20260817"
        )
        desktop["clean_quit"]["runtime_processes"] = {
            "status": "failed",
            "method": "Synthetic negative fixture.",
            "observed_count": 0,
        }
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "failed without positive count"
        ):
            acceptance.validate_observation_register(self.catalogue, zero_failure)

        positive_failure = copy.deepcopy(zero_failure)
        desktop = next(
            item
            for item in positive_failure["observations"]
            if item["id"] == "claude-code-desktop-app-embedded-20260817"
        )
        desktop["clean_quit"]["runtime_processes"]["observed_count"] = 1
        acceptance.validate_observation_register(self.catalogue, positive_failure)

    def test_observation_register_rejects_false_promotion_and_mismatched_reference(
        self,
    ) -> None:
        promoted = copy.deepcopy(self.observations)
        promoted["observations"][0]["runtime_support_claim"] = True
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "cannot claim runtime support"
        ):
            acceptance.validate_observation_register(self.catalogue, promoted)

        mismatched = copy.deepcopy(self.matrix)
        claude = next(
            item for item in mismatched["hosts"] if item["id"] == "claude-code"
        )
        claude["observation_refs"] = ["missing-observation"]
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "unknown or mismatched"
        ):
            acceptance.validate_matrix(self.catalogue, mismatched, self.observations)

        unreferenced = copy.deepcopy(self.matrix)
        claude = next(
            item for item in unreferenced["hosts"] if item["id"] == "claude-code"
        )
        claude["observation_refs"] = claude["observation_refs"][:1]
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "unreferenced evidence"
        ):
            acceptance.validate_matrix(self.catalogue, unreferenced, self.observations)

    def test_desktop_observation_requires_distinct_outer_and_embedded_layers(
        self,
    ) -> None:
        broken = copy.deepcopy(self.observations)
        desktop = next(
            item
            for item in broken["observations"]
            if item["execution_mode"] == "desktop_app_embedded_engine"
        )
        desktop["host_layers"] = desktop["host_layers"][:1]
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "do not match execution mode"
        ):
            acceptance.validate_observation_register(self.catalogue, broken)

        missing_version = copy.deepcopy(self.observations)
        desktop = next(
            item
            for item in missing_version["observations"]
            if item["execution_mode"] == "desktop_app_embedded_engine"
        )
        desktop["host_layers"][0]["version"] = None
        with self.assertRaisesRegex(acceptance.AcceptanceError, "layer version"):
            acceptance.validate_observation_register(self.catalogue, missing_version)

        extra_layer = copy.deepcopy(self.observations)
        desktop = next(
            item
            for item in extra_layer["observations"]
            if item["execution_mode"] == "desktop_app_embedded_engine"
        )
        desktop["host_layers"].append(
            {
                "role": "standalone_engine",
                "name": "unclaimed layer",
                "version": "1",
                "sha256": None,
            }
        )
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "do not match execution mode"
        ):
            acceptance.validate_observation_register(self.catalogue, extra_layer)

    def test_ide_extension_observation_requires_all_three_layers(self) -> None:
        broken = copy.deepcopy(self.observations)
        ide = next(
            item
            for item in broken["observations"]
            if item["execution_mode"] == "ide_extension_embedded_engine"
        )
        ide["host_layers"] = ide["host_layers"][:2]
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "do not match execution mode"
        ):
            acceptance.validate_observation_register(self.catalogue, broken)

    def test_startup_only_observation_does_not_claim_a_tool_result(self) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        opencode = observed["opencode-v1-startup-20260817"]
        self.assertEqual("not_performed", opencode["checks"]["mem_current_project"])
        self.assertIsNone(opencode["project_resolution"]["returned_project"])
        self.assertIsNone(opencode["provider"]["version"])

        contradicted = copy.deepcopy(self.observations)
        startup = next(
            item
            for item in contradicted["observations"]
            if item["id"] == opencode["id"]
        )
        startup["project_resolution"]["returned_project"] = "example-product"
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "returned project is inconsistent"
        ):
            acceptance.validate_observation_register(self.catalogue, contradicted)

    def test_opencode_current_project_observation_remains_partial(self) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        opencode = observed["opencode-v1-current-project-20260817"]
        self.assertEqual("opencode-v1", opencode["host_id"])
        self.assertEqual("1.18.18", opencode["host_layers"][0]["version"])
        self.assertEqual("passed", opencode["checks"]["mem_current_project"])
        self.assertEqual("passed", opencode["checks"]["registered_canonical_project"])
        self.assertEqual(
            "naos-engram-memories",
            opencode["project_resolution"]["returned_project"],
        )
        self.assertEqual(
            "process_override", opencode["project_resolution"]["provider_source"]
        )
        self.assertEqual(
            "present", opencode["project_resolution"]["incoming_project_signal"]
        )
        self.assertEqual("not_performed", opencode["checks"]["memory_read"])
        self.assertEqual("not_performed", opencode["checks"]["memory_save"])
        self.assertTrue(
            all(
                probe["status"] == "passed" and probe["observed_count"] == 0
                for probe in opencode["clean_quit"].values()
            )
        )
        self.assertFalse(opencode["runtime_support_claim"])

    def test_muse_current_project_observation_retains_lifecycle_failure(self) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        muse = observed["muse-current-project-20260817"]
        self.assertEqual("muse", muse["host_id"])
        self.assertEqual("0.1.0-R708.1", muse["host_layers"][0]["version"])
        self.assertEqual("passed", muse["checks"]["mem_current_project"])
        self.assertEqual("passed", muse["checks"]["registered_canonical_project"])
        self.assertEqual(
            "naos-engram-memories", muse["project_resolution"]["returned_project"]
        )
        self.assertEqual("failed", muse["clean_quit"]["runtime_processes"]["status"])
        self.assertEqual(5, muse["clean_quit"]["runtime_processes"]["observed_count"])
        self.assertEqual("passed", muse["clean_quit"]["database_holders"]["status"])
        self.assertEqual(0, muse["clean_quit"]["database_holders"]["observed_count"])
        self.assertEqual("failed", muse["clean_quit"]["client_leases"]["status"])
        self.assertEqual(1, muse["clean_quit"]["client_leases"]["observed_count"])
        self.assertFalse(muse["runtime_support_claim"])

    def test_cursor_current_project_observation_retains_partial_claim_ceiling(
        self,
    ) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        cursor = observed["cursor-desktop-app-3-16-17-current-project-20260817"]
        self.assertEqual("cursor", cursor["host_id"])
        self.assertEqual(
            "desktop_app_embedded_engine", cursor["execution_mode"]
        )
        self.assertEqual(
            {"outer_application", "embedded_engine"},
            {layer["role"] for layer in cursor["host_layers"]},
        )
        self.assertEqual("3.16.17", cursor["host_layers"][0]["version"])
        self.assertEqual("passed", cursor["checks"]["mem_current_project"])
        self.assertEqual(
            "naos-engram-memories",
            cursor["project_resolution"]["returned_project"],
        )
        self.assertEqual(
            "process_cwd", cursor["project_resolution"]["workspace_signal"]
        )
        self.assertEqual(
            "registered_explicit_project",
            cursor["project_resolution"]["wrapper_resolution_source"],
        )
        self.assertTrue(
            all(
                probe["status"] == "passed" and probe["observed_count"] == 0
                for probe in cursor["clean_quit"].values()
            )
        )
        self.assertTrue(
            any("initially disabled" in item for item in cursor["limitations"])
        )
        self.assertFalse(cursor["runtime_support_claim"])

    def test_kilo_current_project_observation_retains_partial_claim_ceiling(
        self,
    ) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        kilo = observed[
            "kilo-code-vscode-extension-7-4-22-current-project-20260817"
        ]
        self.assertEqual("kilo", kilo["host_id"])
        self.assertEqual("desktop_app_embedded_engine", kilo["execution_mode"])
        self.assertEqual(
            {"outer_application", "embedded_engine"},
            {layer["role"] for layer in kilo["host_layers"]},
        )
        self.assertEqual("1.133.0", kilo["host_layers"][0]["version"])
        self.assertEqual("7.4.22", kilo["host_layers"][1]["version"])
        self.assertEqual("passed", kilo["checks"]["mem_current_project"])
        self.assertEqual(
            "naos-engram-memories",
            kilo["project_resolution"]["returned_project"],
        )
        self.assertEqual(
            "process_cwd", kilo["project_resolution"]["workspace_signal"]
        )
        self.assertEqual(
            "registered_explicit_project",
            kilo["project_resolution"]["wrapper_resolution_source"],
        )
        self.assertTrue(
            all(
                probe["status"] == "passed" and probe["observed_count"] == 0
                for probe in kilo["clean_quit"].values()
            )
        )
        self.assertTrue(
            any("$schema" in item for item in kilo["limitations"])
        )
        self.assertFalse(kilo["runtime_support_claim"])

    def test_antigravity_execution_modes_retain_distinct_cwd_evidence(self) -> None:
        observed = acceptance.validate_observation_register(
            self.catalogue, self.observations
        )
        cli = observed["antigravity-cli-1-1-13-current-project-20260817"]
        app = observed[
            "antigravity-desktop-app-2-3-1-current-project-20260817"
        ]
        self.assertEqual("standalone_cli", cli["execution_mode"])
        self.assertEqual("1.1.13", cli["host_layers"][0]["version"])
        self.assertEqual("desktop_app_embedded_engine", app["execution_mode"])
        self.assertEqual(
            {"outer_application", "embedded_engine"},
            {layer["role"] for layer in app["host_layers"]},
        )
        for observation in (cli, app):
            self.assertEqual("antigravity", observation["host_id"])
            self.assertEqual("passed", observation["checks"]["mem_current_project"])
            self.assertEqual(
                "naos-engram-memories",
                observation["project_resolution"]["returned_project"],
            )
            self.assertEqual(
                "registered_explicit_project",
                observation["project_resolution"]["wrapper_resolution_source"],
            )
            self.assertTrue(
                all(
                    probe["status"] == "passed" and probe["observed_count"] == 0
                    for probe in observation["clean_quit"].values()
                )
            )
            self.assertFalse(observation["runtime_support_claim"])
        self.assertTrue(
            any("filesystem root" in item for item in app["limitations"])
        )

    def test_observation_timestamps_and_source_paths_fail_closed(self) -> None:
        naive = copy.deepcopy(self.observations)
        naive["observations"][0]["recorded_at"] = "2026-08-13T14:06:42"
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "recorded_at is invalid"
        ):
            acceptance.validate_observation_register(self.catalogue, naive)

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            evidence_dir = temporary_root / "docs" / "evidence"
            evidence_dir.mkdir(parents=True)
            target = evidence_dir / "target.json"
            target.write_text("{}\n", encoding="utf-8")
            linked = evidence_dir / "linked.json"
            try:
                linked.symlink_to(target.name)
            except OSError as exc:
                self.skipTest(f"symlink fixture requires Windows Developer Mode or equivalent privilege: {exc}")
            unsafe = copy.deepcopy(self.observations)
            unsafe["observations"] = [unsafe["observations"][0]]
            unsafe["observations"][0]["source_refs"] = [
                {
                    "repository_path": "docs/evidence/linked.json",
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }
            ]
            with unittest.mock.patch.object(acceptance, "ROOT", temporary_root):
                with self.assertRaisesRegex(
                    acceptance.AcceptanceError, "source reference is unsafe"
                ):
                    acceptance.validate_observation_register(self.catalogue, unsafe)

    def test_observation_source_refs_are_checkout_pinned_to_lf(self) -> None:
        source_paths = sorted(
            {
                reference["repository_path"]
                for observation in self.observations["observations"]
                for reference in observation["source_refs"]
            }
        )
        result = subprocess.run(
            ["git", "check-attr", "-z", "eol", "--", *source_paths],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
        fields = result.stdout.split(b"\0")
        self.assertEqual(b"", fields.pop())
        self.assertEqual(0, len(fields) % 3)
        observed = {
            fields[index].decode(): fields[index + 2].decode()
            for index in range(0, len(fields), 3)
        }
        self.assertEqual({path: "lf" for path in source_paths}, observed)

    def test_observation_register_rejects_unretained_provenance_claims(self) -> None:
        broken = copy.deepcopy(self.observations)
        desktop = next(
            item
            for item in broken["observations"]
            if item["execution_mode"] == "desktop_app_embedded_engine"
        )
        desktop["checks"]["workspace_signal_provenance"] = "passed"
        with self.assertRaisesRegex(
            acceptance.AcceptanceError, "falsely claims workspace"
        ):
            acceptance.validate_observation_register(self.catalogue, broken)


class MCPHostAcceptanceScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogue = acceptance.load_json(ROOT / "config" / "mcp-hosts.v1.json")
        self.matrix = acceptance.load_json(
            ROOT / "config" / "mcp-host-acceptance.v1.json"
        )
        self.observations = acceptance.load_json(
            ROOT / "config" / "mcp-host-observations.v1.json"
        )

    def build(self):
        return acceptance.build_report(
            self.catalogue,
            self.matrix,
            platform="darwin_arm64",
            selected_host=None,
            probe_local_hosts=False,
            generated_at="2026-08-13T00:00:00+00:00",
        )

    def test_report_schema_rejects_observation_omissions(self) -> None:
        schema = acceptance.load_json(
            ROOT / "config" / "schemas" / "mcp-host-acceptance-report.v1.schema.json"
        )
        report = self.build()
        self.assertEqual([], required_keyword_errors(report, schema))

        missing_register = copy.deepcopy(report)
        del missing_register["observation_register"]
        self.assertEqual(
            ["$.observation_register"],
            required_keyword_errors(missing_register, schema),
        )

        missing_host_refs = copy.deepcopy(report)
        del missing_host_refs["host_results"][0]["observation_refs"]
        self.assertEqual(
            ["$.host_results[0].observation_refs"],
            required_keyword_errors(missing_host_refs, schema),
        )

    def test_synthetic_suite_covers_required_fail_closed_and_lifecycle_cases(
        self,
    ) -> None:
        report = self.build()
        results = {item["id"]: item for item in report["scenario_results"]}
        self.assertEqual(set(self.matrix["required_cases"]), set(results))
        self.assertTrue(all(item["status"] == "passed" for item in results.values()))
        self.assertEqual(
            "mcp_protocol", results["provider_unavailable"]["evidence_class"]
        )
        self.assertEqual("mcp_protocol", results["remote_mismatch"]["evidence_class"])
        self.assertEqual(
            "static_schema", results["instruction_conflict_refused"]["evidence_class"]
        )

    def test_full_report_is_complete_but_promotes_no_host(self) -> None:
        report = self.build()
        self.assertEqual(len(self.catalogue["hosts"]), report["coverage"]["reported_hosts"])
        self.assertTrue(report["coverage"]["complete"])
        self.assertEqual("classified_not_promoted", report["disposition"])
        self.assertFalse(report["live_home_accessed"])
        self.assertFalse(report["credentials_forwarded"])
        self.assertTrue(report["synthetic_only"])
        self.assertEqual(16, report["observation_register"]["observation_count"])
        self.assertRegex(report["observation_register"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(
            all(not item["runtime_support_claim"] for item in report["host_results"])
        )
        claude = next(
            item for item in report["host_results"] if item["host_id"] == "claude-code"
        )
        self.assertEqual(3, len(claude["observation_refs"]))
        codex = next(
            item for item in report["host_results"] if item["host_id"] == "codex"
        )
        self.assertEqual(
            [
                "codex-cli-20260813",
                "codex-cli-0-145-0-dynamic-workspace-20260818",
                "codex-cli-0-145-0-reliability-20260818",
                "codex-desktop-26-810-52044-workspace-signal-negative-20260818",
                "codex-desktop-26-810-52044-project-config-current-project-20260818",
                "codex-vscode-1-133-0-extension-26-814-41407-current-project-20260818",
            ],
            codex["observation_refs"],
        )
        opencode = next(
            item for item in report["host_results"] if item["host_id"] == "opencode-v1"
        )
        self.assertEqual(
            [
                "opencode-v1-startup-20260817",
                "opencode-v1-current-project-20260817",
            ],
            opencode["observation_refs"],
        )
        muse = next(
            item for item in report["host_results"] if item["host_id"] == "muse"
        )
        self.assertEqual(["muse-current-project-20260817"], muse["observation_refs"])
        cursor = next(
            item for item in report["host_results"] if item["host_id"] == "cursor"
        )
        self.assertEqual(
            ["cursor-desktop-app-3-16-17-current-project-20260817"],
            cursor["observation_refs"],
        )
        antigravity = next(
            item for item in report["host_results"] if item["host_id"] == "antigravity"
        )
        self.assertEqual(
            [
                "antigravity-cli-1-1-13-current-project-20260817",
                "antigravity-desktop-app-2-3-1-current-project-20260817",
            ],
            antigravity["observation_refs"],
        )
        kilo = next(
            item for item in report["host_results"] if item["host_id"] == "kilo"
        )
        self.assertEqual(
            ["kilo-code-vscode-extension-7-4-22-current-project-20260817"],
            kilo["observation_refs"],
        )
        self.assertTrue(
            all(
                not item["observation_refs"]
                for item in report["host_results"]
                if item["host_id"]
                not in {
                    "codex",
                    "claude-code",
                    "opencode-v1",
                    "muse",
                    "cursor",
                    "antigravity",
                    "kilo",
                }
            )
        )
        self.assertTrue(
            all(
                item["disposition"] in {"not_promoted", "ineligible"}
                for item in report["host_results"]
            )
        )
        provider_count = len(self.matrix["combination_policy"]["provider_profiles"])
        self.assertEqual(
            len(self.catalogue["hosts"]) * provider_count,
            len(report["combination_results"]),
        )
        self.assertTrue(
            all(
                not item["runtime_support_claim"]
                for item in report["combination_results"]
            )
        )
        self.assertTrue(
            all(
                item["status"] in {"blocked", "not_applicable"}
                for item in report["combination_results"]
            )
        )

    def test_codex_render_only_toml_adapter_is_statically_valid(self) -> None:
        result = acceptance.static_schema_check("codex")
        self.assertEqual("passed", result["status"])
        self.assertEqual("static_schema", result["evidence_class"])
        self.assertFalse(result["runtime_support_claim"])

    def test_fixed_timestamp_report_is_deterministic(self) -> None:
        first = json.dumps(self.build(), sort_keys=True)
        second = json.dumps(self.build(), sort_keys=True)
        self.assertEqual(first, second)

    def test_single_host_report_retains_shared_cases_without_claiming_complete_matrix(
        self,
    ) -> None:
        report = acceptance.build_report(
            self.catalogue,
            self.matrix,
            platform="darwin_arm64",
            selected_host="muse",
            probe_local_hosts=False,
            generated_at="2026-08-13T00:00:00+00:00",
        )
        self.assertEqual(1, report["coverage"]["reported_hosts"])
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual("classified_not_promoted", report["disposition"])
        self.assertEqual(
            "mcp_protocol", report["host_results"][0]["current_evidence_class"]
        )
        self.assertFalse(report["host_results"][0]["runtime_support_claim"])

    def test_windows_classification_retains_unrun_synthetic_cases_without_provider_gate_failure(
        self,
    ) -> None:
        report = acceptance.build_report(
            self.catalogue,
            self.matrix,
            platform="windows_amd64",
            selected_host="codex",
            probe_local_hosts=False,
            generated_at="2026-09-18T00:00:00+00:00",
        )
        self.assertEqual("classified_not_promoted", report["disposition"])
        self.assertEqual(
            {"not_run"},
            {item["status"] for item in report["scenario_results"]},
        )
        self.assertTrue(
            all(not item["runtime_support_claim"] for item in report["scenario_results"])
        )

    def test_local_cli_probe_uses_disposable_environment_and_stays_inventory_only(
        self,
    ) -> None:
        entry = next(item for item in self.matrix["hosts"] if item["id"] == "codex")
        with tempfile.TemporaryDirectory() as temporary:
            if os.name == "nt":
                binary = Path(temporary) / "codex.cmd"
                binary.write_text(
                    "@echo off\n"
                    "if \"%HOME%\"==\"\" exit /b 91\n"
                    "if not \"%OPENAI_API_KEY%\"==\"\" exit /b 92\n"
                    "if \"%1\"==\"--version\" echo codex-cli 9.8.7\n"
                    "exit /b 0\n",
                    encoding="utf-8",
                )
            else:
                binary = Path(temporary) / "codex"
                binary.write_text(
                    "#!/bin/sh\n"
                    'case "$HOME" in */home) ;; *) exit 91 ;; esac\n'
                    'test -z "${OPENAI_API_KEY:-}" || exit 92\n'
                    "if test \"${1:-}\" = --version; then echo 'codex-cli 9.8.7'; fi\n"
                    "exit 0\n",
                    encoding="utf-8",
                )
                binary.chmod(0o755)
            environment = {
                "PATH": temporary + os.pathsep + os.environ.get("PATH", ""),
                "OPENAI_API_KEY": "must-not-be-forwarded",
            }
            with unittest.mock.patch.dict(os.environ, environment, clear=False):
                result = acceptance.local_probe(entry, True)
        self.assertEqual("executed", result["status"])
        self.assertEqual("9.8.7", result["version"])
        self.assertEqual("real_host_inventory", result["evidence_class"])
        self.assertFalse(result["runtime_support_claim"])
        self.assertTrue(all(item["status"] == "passed" for item in result["checks"]))

    def test_opencode_identity_is_executable_not_reported_semver(self) -> None:
        entry = next(
            item for item in self.matrix["hosts"] if item["id"] == "opencode-v1"
        )
        with tempfile.TemporaryDirectory() as temporary:
            if os.name == "nt":
                binary = Path(temporary) / "opencode.cmd"
                binary.write_text("@echo off\necho opencode 2.4.0\n", encoding="utf-8")
            else:
                binary = Path(temporary) / "opencode"
                binary.write_text("#!/bin/sh\necho 'opencode 2.4.0'\n", encoding="utf-8")
                binary.chmod(0o755)
            with unittest.mock.patch.dict(
                os.environ,
                {"PATH": temporary + os.pathsep + os.environ.get("PATH", "")},
                clear=False,
            ):
                result = acceptance.local_probe(entry, True)
        self.assertEqual("executed", result["status"])
        self.assertEqual("2.4.0", result["version"])
        self.assertFalse(result["runtime_support_claim"])

    def test_version_parser_ignores_gui_error_timestamps(self) -> None:
        output = (
            "[0813/150601.206907:ERROR:electron] disposable-profile warning\n"
            "1.132.1\n"
            "c2d1b13fdc4a77628e5f3bb70173351c8f2fbad1\n"
        )
        self.assertEqual("1.132.1", acceptance.sanitized_version(output))

    def test_cli_emits_classified_report(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/run_mcp_host_acceptance.py",
                "--host",
                "openrouter",
                "--platform",
                "darwin_arm64",
                "--generated-at",
                "2026-08-13T00:00:00+00:00",
            ],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual("openrouter", report["host_results"][0]["host_id"])
        self.assertEqual("ineligible", report["host_results"][0]["disposition"])
        self.assertFalse(report["host_results"][0]["runtime_support_claim"])


if __name__ == "__main__":
    unittest.main()
