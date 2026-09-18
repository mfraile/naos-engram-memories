from __future__ import annotations

import copy
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_REGISTRY = ROOT / "tests" / "fixtures" / "projects.synthetic.json"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


memory = load_module("engram_memory", ROOT / "tools" / "engram_memory.py")
sanitizer = load_module("sanitize_public_tree", ROOT / "tools" / "sanitize_public_tree.py")
upstream = load_module("check_upstream", ROOT / "tools" / "check_upstream.py")
bootstrap = load_module("bootstrap", ROOT / "scripts" / "bootstrap.py")
installed_runtime = load_module("validate_installed_runtime", ROOT / "tools" / "validate_installed_runtime.py")


class ProjectRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = memory.load_json(SYNTHETIC_REGISTRY)

    def test_hidden_provider_verify_cli_rejects_relative_and_tilde_config_dirs(self) -> None:
        for config_dir in ("relative-config", "~/relative-config"):
            error = io.StringIO()
            with self.subTest(config_dir=config_dir), contextlib.redirect_stderr(error):
                code = memory.main(
                    [
                        "--registry",
                        str(SYNTHETIC_REGISTRY),
                        "provider",
                        "verify-bound",
                        "--config-dir",
                        config_dir,
                        "--path",
                        "relative-provider",
                    ]
                )
            self.assertEqual(2, code)
            self.assertIn("configuration directory must be absolute", error.getvalue())

    def test_exact_and_alias_resolution_are_canonical(self) -> None:
        project, source = memory.resolve_project(
            self.registry,
            requested="sample-product",
            remote="https://github.com/example-org/example-product.git",
        )
        self.assertEqual("example-product", project)
        self.assertEqual("registered_explicit_project", source)

    def test_registry_rejects_malformed_and_duplicate_identity_fields(self) -> None:
        malformed = copy.deepcopy(self.registry)
        malformed["projects"][0]["aliases"] = {"alias": True}
        with self.assertRaisesRegex(memory.EngramMemoryError, "list of strings"):
            memory.validate_registry(malformed)
        malformed = copy.deepcopy(self.registry)
        malformed["projects"][0]["remotes"] = [7]
        with self.assertRaisesRegex(memory.EngramMemoryError, "invalid approved remote"):
            memory.validate_registry(malformed)
        duplicate_alias = copy.deepcopy(self.registry)
        duplicate_alias["projects"][0]["aliases"].append(duplicate_alias["projects"][0]["aliases"][0])
        with self.assertRaisesRegex(memory.EngramMemoryError, "duplicate or colliding"):
            memory.validate_registry(duplicate_alias)
        duplicate_remote = copy.deepcopy(self.registry)
        duplicate_remote["projects"][0]["remotes"].append(duplicate_remote["projects"][0]["remotes"][0])
        with self.assertRaisesRegex(memory.EngramMemoryError, "duplicate or colliding"):
            memory.validate_registry(duplicate_remote)

    def test_production_registry_is_empty_valid_and_fails_unresolved(self) -> None:
        production = memory.load_json(ROOT / "config" / "projects.json")
        self.assertEqual([], production["projects"])
        memory.validate_registry(production)
        with self.assertRaisesRegex(memory.EngramMemoryError, "not approved"):
            memory.resolve_project(production, requested=None, remote="https://github.com/example-org/example-product.git")

    def test_remote_only_resolution_does_not_use_checkout_name(self) -> None:
        project, source = memory.resolve_project(
            self.registry,
            requested=None,
            remote="git@github.com:example-org/example-platform.git",
        )
        self.assertEqual("example-platform", project)
        self.assertEqual("approved_git_remote", source)

    def test_unregistered_remote_fails_closed(self) -> None:
        with self.assertRaisesRegex(memory.EngramMemoryError, "not approved"):
            memory.resolve_project(self.registry, requested=None, remote="https://example.invalid/other.git")

    def test_explicit_project_and_remote_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(memory.EngramMemoryError, "different canonical projects"):
            memory.resolve_project(
                self.registry,
                requested="example-product",
                remote="https://github.com/example-org/example-platform.git",
            )

    def test_alias_collision_is_rejected(self) -> None:
        broken = copy.deepcopy(self.registry)
        broken["projects"][0]["aliases"] = ["example-platform"]
        with self.assertRaisesRegex(memory.EngramMemoryError, "collid"):
            memory.validate_registry(broken)

    def test_remote_normalization_preserves_repository_path_case(self) -> None:
        upper = "https://Example.Invalid/Org/Repo.git"
        lower = "https://example.invalid/org/repo.git"
        self.assertNotEqual(memory.normalized_remote(upper), memory.normalized_remote(lower))
        distinct = {
            "schema_version": 1,
            "projects": [
                {"id": "upper-project", "aliases": [], "remotes": [upper]},
                {"id": "lower-project", "aliases": [], "remotes": [lower]},
            ],
        }
        memory.validate_registry(distinct)
        self.assertEqual(
            memory.normalized_remote("https://EXAMPLE.INVALID/Org/Repo.git"),
            memory.normalized_remote("git@example.invalid:Org/Repo.git"),
        )

    def test_explicit_client_templates_have_registered_project(self) -> None:
        for client in (
            "vscode-generic",
            "antigravity",
            "kilo",
            "opencode-v1",
            "cursor",
            "project-config",
        ):
            rendered = memory.client_template(client, "example-product")
            self.assertNotIn("__ENGRAM_PROJECT__", rendered)
            self.assertIn("example-product", rendered)
            json.loads(rendered)
            if client == "opencode-v1":
                document = json.loads(rendered)
                self.assertTrue(document["mcp"]["engram"]["enabled"])
                self.assertNotIn("servers", document["mcp"])
        for client in ("vscode-generic", "antigravity", "kilo", "opencode-v1", "cursor"):
            with self.assertRaisesRegex(memory.EngramMemoryError, "requires an explicit"):
                memory.client_template(client, None)
        with self.assertRaisesRegex(memory.EngramMemoryError, "not an independent"):
            memory.client_template("codex-vscode", "example-product")
        with self.assertRaisesRegex(memory.EngramMemoryError, "absolute registered workspace"):
            memory.client_template("codex", "example-product")
        with self.assertRaisesRegex(memory.EngramMemoryError, "ambiguous"):
            memory.client_template("opencode", "example-product")
        with self.assertRaisesRegex(memory.EngramMemoryError, "owner-excluded"):
            memory.client_template("opencode-v2", "example-product", host_version="1.18.15")

    def test_dynamic_claude_template_and_workspace_copilot_template(self) -> None:
        vscode = memory.client_template("vscode-generic", "example-product")
        claude = memory.client_template("claude-code")
        muse = memory.client_template("muse")
        self.assertIn('"ENGRAM_PROJECT": "example-product"', vscode)
        self.assertNotIn('"cwd"', vscode)
        self.assertNotIn("ENGRAM_PROJECT", claude)
        self.assertNotIn("ENGRAM_PROJECT", muse)
        json.loads(vscode)
        json.loads(claude)
        json.loads(muse)

    def test_client_template_escapes_windows_paths_spaces_and_quotes(self) -> None:
        wrapper = 'C:\\Program Files\\NAOS\\"quoted"\\engram-mcp-wrapper.cmd'
        parsed = json.loads(memory.client_template("cursor", "example-product", wrapper_command=wrapper))
        self.assertEqual(wrapper, parsed["mcpServers"]["engram"]["command"])
        with self.assertRaisesRegex(memory.EngramMemoryError, "absolute registered workspace"):
            memory.client_template("codex", "example-product", wrapper_command=wrapper)

    def test_codex_renderer_is_project_local_without_fixed_project_environment(self) -> None:
        workspace = ROOT / "absolute" / "example-product"
        wrapper = str(ROOT / "absolute" / "bin" / "engram-mcp-wrapper")
        rendered = memory.client_template(
            "codex", workspace_path=workspace, wrapper_command=wrapper
        )
        self.assertIn("[mcp_servers.engram]", rendered)
        self.assertIn("enabled = true", rendered)
        self.assertIn("required = true", rendered)
        self.assertIn("startup_timeout_sec = 30", rendered)
        self.assertNotIn("ENGRAM_PROJECT", rendered)
        parsed = memory.parse_codex_mcp_toml(rendered)
        self.assertEqual(wrapper, parsed["mcp_servers"]["engram"]["command"])
        self.assertEqual(str(workspace), parsed["mcp_servers"]["engram"]["cwd"])
        quoted_wrapper = str(ROOT / 'absolute' / '"quoted"' / 'engram-mcp-wrapper')
        quoted_workspace = ROOT / 'absolute' / '"quoted"' / "workspace"
        quoted = memory.parse_codex_mcp_toml(
            memory.client_template(
                "codex",
                workspace_path=quoted_workspace,
                wrapper_command=quoted_wrapper,
            )
        )
        self.assertEqual(quoted_wrapper, quoted["mcp_servers"]["engram"]["command"])
        self.assertEqual(str(quoted_workspace), quoted["mcp_servers"]["engram"]["cwd"])
        with self.assertRaisesRegex(memory.EngramMemoryError, "required startup"):
            memory.parse_codex_mcp_toml(rendered.replace("required = true", "required = false"))
        surfaces = [
            ROOT / "README.md", ROOT / "docs" / "CLIENTS.md",
            ROOT / "config" / "mcp-hosts.v1.json", ROOT / "clients" / "codex.mcp.toml",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in surfaces)
        self.assertNotIn("codex mcp add engram --env", combined)
        self.assertTrue((ROOT / "clients" / "codex.mcp.toml").is_file())
        catalogue = memory.host_catalogue(memory.load_json(ROOT / "config" / "mcp-hosts.v1.json"))
        codex = next(host for host in catalogue["hosts"] if host["id"] == "codex")
        self.assertEqual("project render-only", codex["scope"])
        self.assertIn("absolute cwd", codex["project_mode"])

    def test_codex_render_validates_absolute_registered_workspace_without_writing(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "-C", str(workspace), "init"], check=True)
            subprocess.run(
                [
                    "git", "-C", str(workspace), "remote", "add", "origin",
                    "https://github.com/example-org/example-product.git",
                ],
                check=True,
            )
            rendered = memory.render_client_adapter(
                registry,
                client="codex",
                project="example-product",
                workspace=workspace,
                wrapper_command=str(ROOT / "absolute" / "bin" / "engram-mcp-wrapper"),
            )
            parsed = memory.parse_codex_mcp_toml(rendered)
            self.assertEqual(
                str(workspace.resolve()),
                parsed["mcp_servers"]["engram"]["cwd"],
            )
            self.assertFalse((workspace / ".codex").exists())
            with self.assertRaisesRegex(memory.EngramMemoryError, "absolute managed wrapper"):
                memory.render_client_adapter(
                    registry,
                    client="codex",
                    project="example-product",
                    workspace=workspace,
                )
            with self.assertRaisesRegex(memory.EngramMemoryError, "absolute workspace"):
                memory.render_client_adapter(
                    registry,
                    client="codex",
                    project="example-product",
                    workspace=Path("relative-workspace"),
                    wrapper_command=str(ROOT / "absolute" / "bin" / "engram-mcp-wrapper"),
                )
            workspace_without_remote = Path(temporary) / "workspace-without-remote"
            workspace_without_remote.mkdir()
            subprocess.run(
                ["git", "-C", str(workspace_without_remote), "init"], check=True
            )
            with self.assertRaisesRegex(memory.EngramMemoryError, "approved Git remote"):
                memory.render_client_adapter(
                    registry,
                    client="codex",
                    project="example-product",
                    workspace=workspace_without_remote,
                    wrapper_command=str(ROOT / "absolute" / "bin" / "engram-mcp-wrapper"),
                )
            with self.assertRaisesRegex(memory.EngramMemoryError, "different canonical projects"):
                memory.render_client_adapter(
                    registry,
                    client="codex",
                    project="example-platform",
                    workspace=workspace,
                    wrapper_command=str(ROOT / "absolute" / "bin" / "engram-mcp-wrapper"),
                )


class ReleasePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.support = memory.load_json(ROOT / "config" / "release-support.json")

    def test_only_promoted_platform_is_installable(self) -> None:
        asset = memory.supported_asset(self.support, "darwin_arm64")
        self.assertEqual("1.20.0", asset["version"])
        windows = memory.supported_asset(self.support, "windows_amd64")
        self.assertEqual("1.20.0", windows["version"])
        self.assertEqual("engram_1.20.0_windows_amd64.zip", windows["asset"])

    def test_supported_asset_needs_checksum_and_platform_promotion(self) -> None:
        promoted = copy.deepcopy(self.support)
        candidate = promoted["releases"][1]
        candidate["status"] = "supported"
        candidate["platforms"]["darwin_arm64"]["status"] = "supported"
        candidate["platforms"]["darwin_arm64"]["sha256"] = "a" * 64
        asset = memory.supported_asset(promoted, "darwin_arm64")
        self.assertEqual("1.20.0", asset["version"])
        self.assertEqual("a" * 64, asset["sha256"])

    def test_release_status_does_not_check_network(self) -> None:
        status = memory.release_status(self.support)
        self.assertEqual("explicit-owner-approval", status["rollout_policy"])
        self.assertEqual("supported", status["releases"][1]["status"])

    def test_upstream_check_reports_without_mutating_policy(self) -> None:
        known = upstream.assess(self.support, "v1.20.0")
        pending = upstream.assess(self.support, "v2.0.0")
        unknown = upstream.assess(self.support, "v9.9.9")
        self.assertEqual("supported", known["manifest_status"])
        self.assertEqual("no_policy_change", known["action"])
        self.assertTrue(pending["known_to_manifest"])
        self.assertEqual("unreviewed", pending["manifest_status"])
        self.assertEqual("review_candidate", pending["action"])
        self.assertEqual("unreviewed", unknown["manifest_status"])
        self.assertEqual("review_candidate", unknown["action"])

    def test_linux_runtime_evidence_does_not_promote_named_hosts(self) -> None:
        release = next(item for item in self.support["releases"] if item["version"] == "1.20.0")
        linux = release["platforms"]["linux_amd64"]
        self.assertEqual("supported", linux["status"])
        self.assertTrue(linux["sha256"])
        self.assertIn("named host acceptance remains independently gated", linux["evidence"])

    def test_release_signing_is_optional_while_sha256_integrity_is_mandatory(self) -> None:
        assets = memory.load_json(ROOT / "config" / "bootstrap-assets.v1.json")
        self.assertEqual("sha256", assets["integrity_algorithm"])
        self.assertIn("optional", assets["release_signing"])
        self.assertIn("SHA-256 integrity remains mandatory", assets["release_signing"])
        surfaces = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "README.md", ROOT / "docs" / "PUBLIC_RELEASE.md")
        )
        self.assertNotIn("external release-signing record required", surfaces)
        self.assertIn(
            "signature absence is not a baseline blocker", " ".join(surfaces.split())
        )


class InventoryAndSanitizationTests(unittest.TestCase):
    def test_catalogue_records_experimental_blockers_and_unsupported_transport(self) -> None:
        catalogue = memory.host_catalogue(memory.load_json(ROOT / "config" / "mcp-hosts.v1.json"))
        hosts = {host["id"]: host for host in catalogue["hosts"]}
        self.assertEqual("experimental", hosts["cursor"]["support_tier"])
        self.assertEqual("experimental", hosts["lm-studio"]["support_tier"])
        self.assertEqual("experimental", hosts["anythingllm"]["support_tier"])
        self.assertEqual("ineligible", hosts["open-webui"]["support_tier"])
        required = {"host_type", "supported_platform_version", "schema_evidence", "scope", "project_mode", "config_target", "detection_command", "support_tier", "risk_note", "repair_path", "verification_instruction"}
        for host in hosts.values():
            self.assertFalse(required - set(host), f"missing catalogue fields for {host['id']}")
        terminal = catalogue["terminal_dispositions"]
        self.assertEqual(
            [{"id": "opencode-v2", "disposition": "owner_excluded", "reason": terminal[0]["reason"]}],
            terminal,
        )
        self.assertNotIn("opencode-v2", hosts)
        self.assertFalse((ROOT / "clients" / "opencode-v2.mcp.json").exists())

    def test_catalogue_rejects_duplicate_ids_bogus_tiers_and_malformed_evidence(self) -> None:
        catalogue = memory.load_json(ROOT / "config" / "mcp-hosts.v1.json")
        duplicate = copy.deepcopy(catalogue)
        duplicate["hosts"].append(copy.deepcopy(duplicate["hosts"][0]))
        with self.assertRaisesRegex(memory.EngramMemoryError, "duplicate or invalid"):
            memory.host_catalogue(duplicate)
        bogus = copy.deepcopy(catalogue)
        bogus["hosts"][0]["support_tier"] = "recommended"
        with self.assertRaisesRegex(memory.EngramMemoryError, "support tier"):
            memory.host_catalogue(bogus)
        overlap = copy.deepcopy(catalogue)
        overlap["terminal_dispositions"][0]["id"] = overlap["hosts"][0]["id"]
        with self.assertRaisesRegex(memory.EngramMemoryError, "must be disjoint"):
            memory.host_catalogue(overlap)
        for path, value, message in (
            (("instruction_surfaces",), {"bad": True}, "instruction_surfaces"),
            (("evidence", "platforms_tested"), "darwin_arm64", "platforms_tested"),
            (("evidence", "checked_on"), 20260813, "checked_on"),
        ):
            malformed = copy.deepcopy(catalogue)
            target = malformed["hosts"][0]
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            with self.assertRaisesRegex(memory.EngramMemoryError, message):
                memory.host_catalogue(malformed)

    def test_onboard_is_read_only_non_sensitive_and_accepts_relative_project(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        catalogue = memory.load_json(ROOT / "config" / "mcp-hosts.v1.json")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            project = root / "project"
            home.mkdir()
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            subprocess.run(["git", "-C", str(project), "remote", "add", "origin", "https://github.com/example-org/example-product.git"], check=True)
            canary = root / "provider-was-run"
            provider = root / "engram"
            provider.write_text(f"#!/bin/sh\ntouch '{canary}'\nexit 0\n", encoding="utf-8")
            provider.chmod(0o755)
            report = memory.onboarding_summary(registry, catalogue, project, str(provider), home)
            encoded = json.dumps(report)
            self.assertTrue(report["read_only"])
            self.assertFalse(report["network_accessed"])
            self.assertTrue(report["external_commands_invoked"])
            self.assertFalse(report["external_commands_may_access_network"])
            self.assertFalse(report["provider_commands_invoked"])
            self.assertEqual(["git_local_config", "python_version_probe"], report["external_command_categories"])
            self.assertFalse(canary.exists())
            self.assertTrue(report["engram"]["available"])
            self.assertEqual("external_candidate_detected_unverified", report["engram"]["discovery_status"])
            self.assertIsNone(report["engram"]["version"])
            self.assertEqual("not_probed", report["engram"]["database"]["doctor"])
            self.assertEqual("not_probed", report["engram"]["project_inventory"]["status"])
            self.assertEqual("local", report["profile"])
            self.assertNotIn(str(home), encoded)
            self.assertNotIn(str(project), encoded)

    def test_doctor_uses_managed_state_without_executing_provider(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        catalogue = memory.load_json(ROOT / "config" / "mcp-hosts.v1.json")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            project = root / "project"
            home.mkdir()
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            subprocess.run(["git", "-C", str(project), "remote", "add", "origin", "https://github.com/example-org/example-product.git"], check=True)
            provider = root / "engram"
            provider.write_text(
                "#!/bin/sh\n"
                f"touch '{root / 'provider-was-run'}'\n"
                "exit 91\n",
                encoding="utf-8",
            )
            provider.chmod(0o755)
            report = memory.onboarding_summary(
                registry, catalogue, project, str(provider), home, operation="doctor"
            )
            self.assertFalse(report["network_accessed"])
            self.assertEqual("not_accessed", report["network_access_status"])
            self.assertTrue(report["external_commands_invoked"])
            self.assertFalse(report["external_commands_may_access_network"])
            self.assertFalse(report["provider_commands_invoked"])
            self.assertEqual(["git_local_config", "python_version_probe"], report["external_command_categories"])
            self.assertFalse((root / "provider-was-run").exists())
            self.assertTrue(report["engram"]["available"])
            self.assertEqual("external_candidate_detected_unverified", report["engram"]["discovery_status"])
            self.assertEqual("not_probed", report["engram"]["database"]["doctor"])
            self.assertEqual("not_probed", report["engram"]["project_inventory"]["status"])

    def test_instruction_install_preserves_content_and_is_marker_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            target = project / "AGENTS.md"
            target.write_text("# Existing policy\n\nKeep this.\n", encoding="utf-8")
            plan = memory.install_instruction(project, "codex", dry_run=True)
            self.assertEqual("dry_run", plan["status"])
            self.assertNotIn(memory.INSTRUCTION_BEGIN, target.read_text(encoding="utf-8"))
            installed = memory.install_instruction(project, "codex")
            self.assertEqual("installed", installed["status"])
            text = target.read_text(encoding="utf-8")
            self.assertIn("Keep this.", text)
            self.assertEqual(1, text.count(memory.INSTRUCTION_BEGIN))
            repeated = memory.install_instruction(project, "codex")
            self.assertEqual("already_configured", repeated["status"])

    def test_instruction_install_refuses_conflicting_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            target = project / "CLAUDE.md"
            original = memory.INSTRUCTION_BEGIN + "\nmissing end\n"
            target.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(memory.EngramMemoryError, "conflicting managed markers"):
                memory.install_instruction(project, "claude-code")
            self.assertEqual(original, target.read_text(encoding="utf-8"))

    def test_instruction_install_refuses_conflicting_lifecycle_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            target = project / "AGENTS.md"
            original = "# Existing policy\n\nAlways save every conversation and automatically sync.\n"
            target.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(memory.EngramMemoryError, "conflict with the managed lifecycle"):
                memory.install_instruction(project, "codex")
            self.assertEqual(original, target.read_text(encoding="utf-8"))

    def test_project_registration_is_dry_run_then_backed_up_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "projects.json"
            shutil.copy2(SYNTHETIC_REGISTRY, registry)
            original = registry.read_text(encoding="utf-8")
            plan = memory.register_project(registry, project_id="fixture-project", remote="https://example.invalid/fixture.git", dry_run=True)
            self.assertEqual("dry_run", plan["status"])
            self.assertEqual(original, registry.read_text(encoding="utf-8"))
            result = memory.register_project(registry, project_id="fixture-project", remote="https://example.invalid/fixture.git")
            self.assertEqual("registered", result["status"])
            backups = sorted(root.glob("projects.json.engram-backup-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(original, backups[0].read_text(encoding="utf-8"))

    def test_project_registry_supports_sequential_registrations_with_preserved_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "projects.json"
            shutil.copy2(SYNTHETIC_REGISTRY, registry)
            original = registry.read_text(encoding="utf-8")
            first = memory.register_project(registry, project_id="fixture-alpha", remote="https://example.invalid/alpha.git")
            after_first = registry.read_text(encoding="utf-8")
            second = memory.register_project(registry, project_id="fixture-beta", remote="https://example.invalid/beta.git")
            self.assertEqual("registered", first["status"])
            self.assertEqual("registered", second["status"])
            backups = sorted(root.glob("projects.json.engram-backup-*"))
            self.assertEqual(2, len(backups))
            self.assertEqual({original, after_first}, {path.read_text(encoding="utf-8") for path in backups})
            projects = {project["id"] for project in json.loads(registry.read_text(encoding="utf-8"))["projects"]}
            self.assertTrue({"fixture-alpha", "fixture-beta"} <= projects)

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_project_registry_refuses_symlinked_parent_without_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            registry = outside / "projects.json"
            shutil.copy2(SYNTHETIC_REGISTRY, registry)
            original = registry.read_text(encoding="utf-8")
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                memory.register_project(
                    linked / "projects.json",
                    project_id="fixture-new",
                    remote="https://example.invalid/new.git",
                    authorized_base=root,
                )
            self.assertEqual(original, registry.read_text(encoding="utf-8"))
            self.assertEqual([], list(outside.glob("projects.json.engram-backup-*")))

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_project_registry_refuses_nested_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            nested = outside / "nested"
            nested.mkdir(parents=True)
            registry = nested / "projects.json"
            shutil.copy2(SYNTHETIC_REGISTRY, registry)
            original = registry.read_text(encoding="utf-8")
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                memory.register_project(
                    linked / "nested" / "projects.json",
                    project_id="fixture-new",
                    remote="https://example.invalid/new.git",
                )
            self.assertEqual(original, registry.read_text(encoding="utf-8"))
            self.assertEqual([], list(nested.glob("projects.json.engram-backup-*")))

    def test_instruction_updates_create_unique_backups_without_overwrite(self) -> None:
        original_template = memory.INSTRUCTION_TEMPLATE
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                workspace = root / "workspace"
                workspace.mkdir()
                target = workspace / "AGENTS.md"
                target.write_text("# Existing\n\nKeep this.\n", encoding="utf-8")
                memory.install_instruction(workspace, "codex")
                first_managed = target.read_text(encoding="utf-8")
                updated_template = root / "updated-instruction.md"
                updated_template.write_text(
                    memory.render_instruction().replace(
                        "Treat memory as advisory and verify material recalled claims.",
                        "Treat memory as advisory and verify material recalled claims twice when consequential.",
                    ),
                    encoding="utf-8",
                )
                memory.INSTRUCTION_TEMPLATE = updated_template
                memory.install_instruction(workspace, "codex")
                backups = sorted(workspace.glob("AGENTS.md.engram-backup-*"))
                self.assertEqual(2, len(backups))
                self.assertEqual(
                    {"# Existing\n\nKeep this.\n", first_managed},
                    {path.read_text(encoding="utf-8") for path in backups},
                )
                self.assertIn("twice when consequential", target.read_text(encoding="utf-8"))
        finally:
            memory.INSTRUCTION_TEMPLATE = original_template

    def test_project_proposal_uses_origin_not_folder_basename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "misleading-folder"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/canonical-product.git"], check=True)
            proposal = memory.propose_project_from_remote(workspace)
            self.assertEqual("canonical-product", proposal["project"])
            self.assertNotEqual(workspace.name, proposal["project"])

    def test_remote_transport_spellings_resolve_to_one_canonical_identity(self) -> None:
        self.assertEqual(
            memory.normalized_remote("https://example.invalid/group/project.git"),
            memory.normalized_remote("ssh://git@example.invalid/group/project.git"),
        )
        self.assertEqual(
            memory.normalized_remote("https://example.invalid/group/project.git"),
            memory.normalized_remote("git@example.invalid:group/project.git"),
        )

    def test_remote_identity_preserves_nondefault_port_authority(self) -> None:
        default = memory.normalized_remote("ssh://git@example.invalid:22/group/project.git")
        self.assertEqual(default, memory.normalized_remote("git@example.invalid:group/project.git"))
        port_2222 = memory.normalized_remote("ssh://git@example.invalid:2222/group/project.git")
        port_3333 = memory.normalized_remote("ssh://git@example.invalid:3333/group/project.git")
        https_8443 = memory.normalized_remote("https://example.invalid:8443/group/project.git")
        self.assertNotEqual(default, port_2222)
        self.assertNotEqual(port_2222, port_3333)
        self.assertNotEqual(port_2222, https_8443)

        registry = {
            "schema_version": 1,
            "projects": [
                {
                    "id": "port-project",
                    "aliases": [],
                    "remotes": ["ssh://git@example.invalid:2222/group/project.git"],
                }
            ],
        }
        memory.validate_registry(registry)
        with self.assertRaisesRegex(memory.EngramMemoryError, "not approved"):
            memory.project_by_remote(registry, "ssh://git@example.invalid:3333/group/project.git")

    def test_registry_rejects_malformed_remote_port(self) -> None:
        candidate = copy.deepcopy(memory.load_json(SYNTHETIC_REGISTRY))
        candidate["projects"].append(
            {
                "id": "bad-port",
                "aliases": [],
                "remotes": ["ssh://git@example.invalid:not-a-port/group/project.git"],
            }
        )
        with self.assertRaisesRegex(memory.EngramMemoryError, "invalid port"):
            memory.validate_registry(candidate)

    def test_observed_remote_must_be_credential_free_before_resolution(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        observed = (
            "https://user:secret@github.com/example-org/example-product.git",
            "https://github.com/example-org/example-product.git?token=secret",
            "file://github.com/example-org/example-product.git",
            "ftp://github.com/example-org/example-product.git",
            "ssh://person@github.com/example-org/example-product.git",
        )
        for remote in observed:
            with self.subTest(remote=remote), self.assertRaises(memory.EngramMemoryError):
                memory.project_by_remote(registry, remote)
            with self.subTest(explicit=remote), self.assertRaises(memory.EngramMemoryError):
                memory.resolve_project(registry, requested="example-product", remote=remote)

    def test_project_proposal_refuses_remote_with_embedded_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://user:secret@example.invalid/project.git"], check=True)
            with self.assertRaisesRegex(memory.EngramMemoryError, "embedded credentials"):
                memory.propose_project_from_remote(workspace)

    def test_registry_refuses_explicit_credential_bearing_remotes(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        for remote in (
            "https://token@example.invalid/project.git",
            "https://user:password@example.invalid/project.git",
            "ssh://git:secret@example.invalid/project.git",
            "token@github.com:example/project.git",
            "oauth2:SECRET@gitlab.com/example/project.git",
            "x-access-token:SECRET@github.com/example/project.git",
            "user@github.com:example/project.git",
            "ssh://person@example.invalid/project.git",
            "file:///tmp/project.git",
            "/tmp/project.git",
            "http://example.invalid/project.git",
            "https://example.invalid/project.git?access_token=secret",
            "https://example.invalid/project.git?key=supersecret",
            "https://example.invalid/project.git?oauth2=supersecret",
        ):
            candidate = copy.deepcopy(registry)
            candidate["projects"].append({"id": "credential-fixture", "aliases": [], "remotes": [remote]})
            with self.subTest(remote=remote), self.assertRaises(memory.EngramMemoryError):
                memory.validate_registry(candidate)
        legitimate = copy.deepcopy(registry)
        legitimate["projects"].append(
            {
                "id": "ssh-fixture",
                "aliases": [],
                "remotes": [
                    "ssh://git@example.invalid/project.git",
                    "git@example-alt.invalid:group/project.git",
                ],
            }
        )
        memory.validate_registry(legitimate)

    def test_from_remote_override_cannot_persist_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path = root / "projects.json"
            shutil.copy2(SYNTHETIC_REGISTRY, registry_path)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://example.invalid/new-project.git"], check=True)
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                code = memory.main([
                    "--registry", str(registry_path),
                    "project", "register",
                    "--from-remote", str(workspace),
                    "--remote", "https://token@example.invalid/new-project.git",
                    "--yes", "--non-interactive",
                ])
            self.assertEqual(2, code)
            self.assertIn("embedded credentials", error.getvalue())
            self.assertNotIn("token@", registry_path.read_text(encoding="utf-8"))

    def test_cursor_install_and_verify_are_candidate_configuration_checks_only(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            wrapper = workspace / "bin" / "engram-mcp-wrapper"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/example-product.git"], check=True)
            result = memory.install_client(registry, client="cursor", project="example-product", workspace=workspace, wrapper_path=wrapper, _allow_unmanaged_wrapper=True)
            self.assertEqual("installed", result["status"])
            verification = memory.verify_client(registry, client="cursor", project="example-product", workspace=workspace, wrapper=wrapper, host_version=None, _allow_unmanaged_wrapper=True)
            self.assertTrue(verification["configuration_valid"])
            self.assertFalse(verification["runtime_verified"])

    def test_client_install_refuses_arbitrary_or_tampered_wrapper(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/example-product.git"], check=True)
            memory.ensure_user_runtime(home=home)
            arbitrary = root / "engram-mcp-wrapper"
            arbitrary.write_text("#!/bin/sh\n", encoding="utf-8")
            arbitrary.chmod(0o755)
            with self.assertRaisesRegex(memory.EngramMemoryError, "exact integrity-verified"):
                memory.install_client(registry, client="cursor", project="example-product", workspace=workspace, home=home, wrapper_path=arbitrary)
            wrapper = memory.user_wrapper_path(home)
            linked_wrapper = root / "linked" / "engram-mcp-wrapper"
            linked_wrapper.parent.mkdir()
            try:
                linked_wrapper.symlink_to(wrapper)
            except OSError as exc:
                if sys.platform == "win32" and getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink creation requires Developer Mode or elevation")
                raise
            with self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                memory.install_client(
                    registry, client="cursor", project="example-product",
                    workspace=workspace, home=home, wrapper_path=linked_wrapper,
                )
            wrapper.write_text("#!/bin/sh\n# tampered\n", encoding="utf-8")
            wrapper.chmod(0o755)
            with self.assertRaisesRegex(memory.EngramMemoryError, "exact integrity-verified"):
                memory.install_client(registry, client="cursor", project="example-product", workspace=workspace, home=home)

    def test_client_install_with_verified_runtime_wrapper_succeeds(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/example-product.git"], check=True)
            memory.ensure_user_runtime(home=home)
            result = memory.install_client(registry, client="cursor", project="example-product", workspace=workspace, home=home)
            self.assertEqual("installed", result["status"])

    def test_opencode_windows_cmd_shim_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shim = Path(temporary) / "opencode.cmd"
            shim.write_text("@echo off\n", encoding="utf-8")
            with unittest.mock.patch.object(memory.os, "name", "nt"):
                self.assertEqual(shim.resolve(), memory.validate_opencode_executable("opencode-v1", shim))

    def test_opencode_v1_install_requires_git_ignored_local_configuration(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            wrapper = root / "bin" / "engram-mcp-wrapper"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run([
                "git", "-C", str(workspace), "remote", "add", "origin",
                "https://github.com/example-org/example-product.git",
            ], check=True)
            opencode = root / "opencode"
            opencode.write_text("#!/bin/sh\n", encoding="utf-8")
            opencode.chmod(0o755)
            before = subprocess.run(
                ["git", "-C", str(workspace), "status", "--porcelain=v1"],
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout
            with self.assertRaisesRegex(memory.EngramMemoryError, "must be locally ignored"):
                memory.install_client(
                    registry, client="opencode-v1", project="example-product",
                    workspace=workspace, wrapper_path=wrapper,
                    host_executable=opencode, _allow_unmanaged_wrapper=True,
                )
            self.assertFalse((workspace / "opencode.json").exists())
            self.assertEqual(
                before,
                subprocess.run(
                    ["git", "-C", str(workspace), "status", "--porcelain=v1"],
                    text=True,
                    stdout=subprocess.PIPE,
                    check=True,
                ).stdout,
            )
            (workspace / ".git" / "info" / "exclude").write_text(
                "/opencode.json\n/opencode.json.engram-backup-*\n",
                encoding="utf-8",
            )
            result = memory.install_client(
                registry, client="opencode-v1", project="example-product",
                workspace=workspace, wrapper_path=wrapper,
                host_executable=opencode, _allow_unmanaged_wrapper=True,
            )
            self.assertEqual("installed", result["status"])
            document = json.loads((workspace / "opencode.json").read_text(encoding="utf-8"))
            self.assertIn("engram", document["mcp"])
            self.assertNotIn("servers", document["mcp"])
            verified = memory.verify_client(
                registry, client="opencode-v1", project="example-product",
                workspace=workspace, wrapper=wrapper, host_version=None,
                host_executable=opencode, _allow_unmanaged_wrapper=True,
            )
            self.assertTrue(verified["configuration_valid"])
            self.assertEqual(
                "",
                subprocess.run(
                    ["git", "-C", str(workspace), "status", "--porcelain=v1"],
                    text=True,
                    stdout=subprocess.PIPE,
                    check=True,
                ).stdout,
            )

    def test_opencode_v1_refuses_beta_shape_and_unignored_backup_without_mutation(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(
                ["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/example-product.git"],
                check=True,
            )
            wrapper = root / "bin" / "engram-mcp-wrapper"
            old_wrapper = root / "old" / "engram-mcp-wrapper"
            opencode = root / "opencode"
            for executable in (wrapper, old_wrapper, opencode):
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text("#!/bin/sh\n", encoding="utf-8")
                executable.chmod(0o755)
            exclude = workspace / ".git" / "info" / "exclude"
            exclude.write_text("/opencode.json\n", encoding="utf-8")
            target = workspace / "opencode.json"
            beta = '{"mcp":{"servers":{"engram":{"type":"local"}}}}\n'
            target.write_text(beta, encoding="utf-8")
            with self.assertRaisesRegex(memory.EngramMemoryError, "V2 beta"):
                memory.install_client(
                    registry,
                    client="opencode-v1",
                    project="example-product",
                    workspace=workspace,
                    wrapper_path=wrapper,
                    host_executable=opencode,
                    _allow_unmanaged_wrapper=True,
                )
            with self.assertRaisesRegex(memory.EngramMemoryError, "V2 beta"):
                memory.verify_client(
                    registry,
                    client="opencode-v1",
                    project="example-product",
                    workspace=workspace,
                    wrapper=wrapper,
                    host_version=None,
                    host_executable=opencode,
                    _allow_unmanaged_wrapper=True,
                )
            self.assertEqual(beta, target.read_text(encoding="utf-8"))
            self.assertEqual([], list(workspace.glob("opencode.json.engram-backup-*")))

            target.write_text(
                memory.client_template(
                    "opencode-v1", "example-product", wrapper_command=str(old_wrapper)
                ),
                encoding="utf-8",
            )
            original = target.read_text(encoding="utf-8")
            with self.assertRaisesRegex(memory.EngramMemoryError, "backup must be locally ignored"):
                memory.install_client(
                    registry,
                    client="opencode-v1",
                    project="example-product",
                    workspace=workspace,
                    wrapper_path=wrapper,
                    host_executable=opencode,
                    repair=True,
                    _allow_unmanaged_wrapper=True,
                )
            self.assertEqual(original, target.read_text(encoding="utf-8"))
            self.assertEqual([], list(workspace.glob("opencode.json.engram-backup-*")))

    def test_opencode_v1_refuses_tracked_configuration_and_v2_adapter(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(
                ["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/example-product.git"],
                check=True,
            )
            wrapper = root / "engram-mcp-wrapper"
            opencode = root / "opencode"
            for executable in (wrapper, opencode):
                executable.write_text("#!/bin/sh\n", encoding="utf-8")
                executable.chmod(0o755)
            target = workspace / "opencode.json"
            target.write_text(memory.client_template("opencode-v1", "example-product", wrapper_command=str(wrapper)), encoding="utf-8")
            subprocess.run(["git", "-C", str(workspace), "add", "-f", "opencode.json"], check=True)
            with self.assertRaisesRegex(memory.EngramMemoryError, "is tracked"):
                memory.verify_client(
                    registry,
                    client="opencode-v1",
                    project="example-product",
                    workspace=workspace,
                    wrapper=wrapper,
                    host_version=None,
                    host_executable=opencode,
                    _allow_unmanaged_wrapper=True,
                )
            with self.assertRaisesRegex(memory.EngramMemoryError, "owner-excluded"):
                memory.install_client(
                    registry,
                    client="opencode-v2",
                    project="example-product",
                    workspace=workspace,
                    wrapper_path=wrapper,
                    _allow_unmanaged_wrapper=True,
                )

    def test_client_repair_updates_only_a_provably_managed_entry(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run([
                "git", "-C", str(workspace), "remote", "add", "origin",
                "https://github.com/example-org/example-product.git",
            ], check=True)
            old_wrapper = root / "old" / "engram-mcp-wrapper"
            new_wrapper = root / "new" / "engram-mcp-wrapper"
            for wrapper in (old_wrapper, new_wrapper):
                wrapper.parent.mkdir()
                wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
                wrapper.chmod(0o755)
            target = workspace / ".vscode" / "mcp.json"
            target.parent.mkdir()
            target.write_text(
                memory.client_template(
                    "vscode-generic", "example-product", wrapper_command=str(old_wrapper)
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(memory.EngramMemoryError, "refusing to overwrite"):
                memory.install_client(
                    registry, client="vscode-generic", project="example-product",
                    workspace=workspace, wrapper_path=new_wrapper, _allow_unmanaged_wrapper=True,
                )
            repaired = memory.install_client(
                registry, client="vscode-generic", project="example-product",
                workspace=workspace, wrapper_path=new_wrapper, repair=True, _allow_unmanaged_wrapper=True,
            )
            self.assertEqual("repaired", repaired["status"])
            self.assertTrue(repaired["backup_created"])
            self.assertEqual(
                str(Path(os.path.abspath(new_wrapper))),
                json.loads(target.read_text(encoding="utf-8"))["servers"]["engram"]["command"],
            )
            repeated = memory.install_client(
                registry, client="vscode-generic", project="example-product",
                workspace=workspace, wrapper_path=new_wrapper, repair=True, _allow_unmanaged_wrapper=True,
            )
            self.assertEqual("already_configured", repeated["status"])
            document = json.loads(target.read_text(encoding="utf-8"))
            document["servers"]["engram"] = {"command": "/tmp/unrelated-tool", "env": {}}
            target.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(memory.EngramMemoryError, "not provably toolkit-managed"):
                memory.install_client(
                    registry, client="vscode-generic", project="example-product",
                    workspace=workspace, wrapper_path=new_wrapper, repair=True, _allow_unmanaged_wrapper=True,
                )

    def test_guided_muse_install_reaches_preview_only_policy_without_workspace_args(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                code = memory.main([
                    "--registry", str(SYNTHETIC_REGISTRY),
                    "client", "install", "--client", "muse", "--home", str(home),
                    "--dry-run", "--yes", "--non-interactive",
                ])
            self.assertEqual(2, code)
            self.assertIn("no safe managed installer; use render-client", error.getvalue())
            self.assertNotIn("required", error.getvalue())

    def test_noninteractive_write_requires_yes_or_dry_run(self) -> None:
        with self.assertRaisesRegex(memory.EngramMemoryError, "explicit confirmation"):
            memory.confirm_write("fixture write", yes=False, non_interactive=True, dry_run=False)
        self.assertFalse(memory.confirm_write("fixture write", yes=False, non_interactive=True, dry_run=True))
        self.assertTrue(memory.confirm_write("fixture write", yes=True, non_interactive=True, dry_run=False))

    def test_guided_onboarding_requires_explicit_noninteractive_choice(self) -> None:
        with self.assertRaisesRegex(memory.EngramMemoryError, "requires --choice"):
            memory.guided_onboarding_choice(None, non_interactive=True)
        result = memory.guided_onboarding_choice("defer", non_interactive=True)
        self.assertTrue(result["preview_only"])
        self.assertFalse(result["writes_performed"])

    def test_runtime_state_distinguishes_absent_partial_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            absent = memory.runtime_configuration_state(home)
            self.assertEqual("absent", absent["status"])
            self.assertFalse(absent["configured"])
            memory.user_config_dir(home).mkdir(parents=True)
            partial = memory.runtime_configuration_state(home)
            self.assertEqual("partial", partial["status"])
            self.assertFalse(partial["configured"])
            memory.ensure_user_runtime(home=home)
            complete = memory.runtime_configuration_state(home)
            self.assertEqual("complete", complete["status"])
            self.assertTrue(complete["configured"])
            self.assertEqual([], complete["missing_components"])
            self.assertEqual("verified_current", complete["integrity"])

    def test_runtime_manifest_detects_drift_and_explicit_repair_backs_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            first = memory.ensure_user_runtime(home=home)
            self.assertEqual("installed", first["status"])
            engine = memory.user_config_dir(home) / "engram_memory.py"
            original = engine.read_text(encoding="utf-8")
            engine.write_text("# locally modified managed asset\n", encoding="utf-8")
            state = memory.runtime_configuration_state(home)
            self.assertFalse(state["configured"])
            self.assertEqual("stale_or_modified", state["integrity"])
            with self.assertRaisesRegex(memory.EngramMemoryError, "locally changed"):
                memory.ensure_user_runtime(home=home)
            self.assertEqual("# locally modified managed asset\n", engine.read_text(encoding="utf-8"))
            repaired = memory.ensure_user_runtime(home=home, repair=True)
            self.assertEqual("repaired", repaired["status"])
            self.assertIn("engram_memory.py", repaired["backups_created"])
            self.assertEqual(original, engine.read_text(encoding="utf-8"))
            backups = list(engine.parent.glob("engram_memory.py.engram-backup-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual("# locally modified managed asset\n", backups[0].read_text(encoding="utf-8"))
            self.assertEqual("already_configured", memory.ensure_user_runtime(home=home)["status"])

    def test_runtime_dry_run_is_idempotent_and_reports_exact_drift_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            absent = memory.ensure_user_runtime(home=home, dry_run=True)
            self.assertTrue(absent["changed"])
            self.assertIn("projects.json", absent["would_change"])
            self.assertFalse(home.exists())
            memory.ensure_user_runtime(home=home)
            current = memory.ensure_user_runtime(home=home, dry_run=True)
            self.assertFalse(current["changed"])
            self.assertEqual([], current["would_change"])
            engine = memory.user_config_dir(home) / "engram_memory.py"
            engine.write_text("# drift\n", encoding="utf-8")
            with self.assertRaisesRegex(memory.EngramMemoryError, "locally changed"):
                memory.ensure_user_runtime(home=home, dry_run=True)
            repair = memory.ensure_user_runtime(home=home, dry_run=True, repair=True)
            self.assertTrue(repair["changed"])
            self.assertEqual(["engram_memory.py"], repair["would_change"])
            self.assertEqual(["engram_memory.py"], repair["would_back_up"])
            self.assertEqual("# drift\n", engine.read_text(encoding="utf-8"))

    def test_runtime_dry_run_refuses_invalid_registry_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            config = memory.user_config_dir(home)
            config.mkdir(parents=True)
            registry = config / "projects.json"
            registry.write_text('{"schema_version":1,"projects":"invalid"}\n', encoding="utf-8")
            before = registry.read_bytes()
            with self.assertRaisesRegex(memory.EngramMemoryError, "project registry"):
                memory.ensure_user_runtime(home=home, dry_run=True, repair=True)
            self.assertEqual(before, registry.read_bytes())
            self.assertEqual(["projects.json"], [path.name for path in config.iterdir()])

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_runtime_dry_run_refuses_symlink_before_planning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            config = memory.user_config_dir(home)
            config.mkdir(parents=True)
            external = root / "external-registry"
            external.write_text("external\n", encoding="utf-8")
            (config / "projects.json").symlink_to(external)
            with self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                memory.ensure_user_runtime(home=home, dry_run=True, repair=True)
            self.assertEqual("external\n", external.read_text(encoding="utf-8"))

    def test_pre_manifest_runtime_requires_reviewed_repair_and_preserves_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            config = memory.user_config_dir(home)
            config.mkdir(parents=True)
            registry = config / "projects.json"
            registry.write_text((SYNTHETIC_REGISTRY).read_text(encoding="utf-8"), encoding="utf-8")
            engine = config / "engram_memory.py"
            engine.write_text("# prior managed runtime\n", encoding="utf-8")
            before_registry = registry.read_bytes()
            with self.assertRaisesRegex(memory.EngramMemoryError, "pre-manifest"):
                memory.ensure_user_runtime(home=home)
            with self.assertRaisesRegex(memory.EngramMemoryError, "pre-manifest"):
                memory.ensure_user_runtime(home=home, dry_run=True)
            repaired = memory.ensure_user_runtime(home=home, repair=True)
            self.assertEqual("repaired", repaired["status"])
            self.assertEqual(before_registry, registry.read_bytes())
            self.assertTrue((config / "managed-runtime.v1.json").is_file())
            self.assertEqual(1, len(list(config.glob("engram_memory.py.engram-backup-*"))))

    def test_runtime_preflight_refusal_creates_no_missing_registry_or_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            config = memory.user_config_dir(home)
            config.mkdir(parents=True)
            engine = config / "engram_memory.py"
            engine.write_text("# prior managed runtime\n", encoding="utf-8")
            before = {path.name: path.read_bytes() for path in config.iterdir()}
            with self.assertRaisesRegex(memory.EngramMemoryError, "pre-manifest"):
                memory.ensure_user_runtime(home=home)
            after = {path.name: path.read_bytes() for path in config.iterdir()}
            self.assertEqual(before, after)
            self.assertFalse((config / "projects.json").exists())
            self.assertFalse((config / "managed-runtime.v1.json").exists())

    def test_invalid_runtime_manifest_refusal_is_write_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            config = memory.user_config_dir(home)
            config.mkdir(parents=True)
            manifest = config / "managed-runtime.v1.json"
            manifest.write_text('{"schema_version": 99}\n', encoding="utf-8")
            before = manifest.read_bytes()
            with self.assertRaisesRegex(memory.EngramMemoryError, "manifest is invalid"):
                memory.ensure_user_runtime(home=home)
            self.assertEqual(before, manifest.read_bytes())
            self.assertEqual(["managed-runtime.v1.json"], [path.name for path in config.iterdir()])

    def test_guided_local_write_reports_mutation_and_post_write_state(self) -> None:
        registry = SYNTHETIC_REGISTRY
        catalogue = ROOT / "config" / "mcp-hosts.v1.json"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/example-product.git"], check=True)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = memory.main([
                    "--registry", str(registry), "--catalogue", str(catalogue),
                    "onboard", "--project", str(workspace), "--home", str(home),
                    "--engram-bin", "missing-engram", "--guided", "--choice", "local",
                    "--yes", "--non-interactive",
                ])
            self.assertEqual(0, code)
            report = json.loads(output.getvalue())
            self.assertFalse(report["read_only"])
            self.assertFalse(report["guided"]["preview_only"])
            self.assertTrue(report["guided"]["writes_performed"])
            self.assertTrue(report["runtime_state"]["configured"])
            self.assertEqual("complete", report["runtime_state"]["status"])

    def test_guided_local_dry_run_remains_read_only_preview(self) -> None:
        registry = SYNTHETIC_REGISTRY
        catalogue = ROOT / "config" / "mcp-hosts.v1.json"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/example-product.git"], check=True)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = memory.main([
                    "--registry", str(registry), "--catalogue", str(catalogue),
                    "onboard", "--project", str(workspace), "--home", str(home),
                    "--engram-bin", "missing-engram", "--guided", "--choice", "local",
                    "--dry-run", "--non-interactive",
                ])
            self.assertEqual(0, code)
            report = json.loads(output.getvalue())
            self.assertTrue(report["read_only"])
            self.assertTrue(report["guided"]["preview_only"])
            self.assertFalse(report["guided"]["writes_performed"])
            self.assertFalse(report["runtime_state"]["configured"])
            self.assertFalse(home.exists())

    def test_discover_python_returns_absolute_supported_interpreter(self) -> None:
        discovered = memory.discover_python([sys.executable])
        self.assertTrue(Path(discovered).is_absolute())
        self.assertTrue(Path(discovered).is_file())

    def test_redaction_removes_local_user_path_and_secret(self) -> None:
        value = memory.redact("/Users/someone/work token=abc123")
        self.assertNotIn("someone", value)
        self.assertIn("[REDACTED]", value)

    def test_inventory_is_aggregate_and_content_free(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text("model = 'example'\n", encoding="utf-8")
            report = memory.inventory(registry, ROOT, "missing-engram-binary", home=home)
            encoded = json.dumps(report)
            self.assertEqual(5, report["schema_version"])
            self.assertTrue(report["read_only"])
            self.assertFalse(report["provider_probe"]["external_commands_invoked"])
            self.assertFalse(report["provider_probe"]["network_accessed"])
            self.assertNotIn(str(home), encoded)
            self.assertNotIn("observation_content", encoded)
            self.assertEqual(
                "configuration_present_without_reference",
                report["client_mcp_reference_status"]["codex"],
            )

    def test_inventory_never_executes_upstream_provider(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            canary = root / "provider-was-run"
            fake = root / "engram"
            fake.write_text(f"#!/bin/sh\ntouch '{canary}'\nexit 91\n", encoding="utf-8")
            fake.chmod(0o755)
            report = memory.inventory(registry, ROOT, str(fake), home=home)
            self.assertFalse(canary.exists())
            self.assertTrue(report["engram"]["available"])
            self.assertEqual("external_candidate_detected_unverified", report["engram"]["discovery_status"])
            self.assertFalse(report["provider_probe"]["external_commands_invoked"])
            self.assertFalse(report["provider_probe"]["external_commands_may_access_network"])
            self.assertFalse(report["provider_probe"]["network_accessed"])
            self.assertEqual("managed_state_and_checksum_only", report["provider_probe"]["mode"])
            self.assertEqual("not_probed", report["engram"]["database"]["doctor"])
            self.assertEqual("not_probed", report["engram"]["project_inventory"]["status"])

    def test_mcp_reference_status_is_not_a_client_health_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "mcp.json"
            self.assertEqual("not_detected", memory.mcp_reference_status((config,)))
            config.write_text('{"mcpServers": {"other": {}}}', encoding="utf-8")
            self.assertEqual("configuration_present_without_reference", memory.mcp_reference_status((config,)))
            config.write_text('{"command": "engram-mcp-wrapper"}', encoding="utf-8")
            self.assertEqual("reference_detected_unvalidated", memory.mcp_reference_status((config,)))

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_mcp_reference_inventory_does_not_follow_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            (outside / "nested" / "deeper").mkdir(parents=True)
            (outside / "nested" / "deeper" / "mcp.json").write_text('{"command":"engram-mcp-wrapper"}', encoding="utf-8")
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            self.assertEqual(
                "configuration_unreadable",
                memory.mcp_reference_status((linked / "nested" / "deeper" / "mcp.json",)),
            )

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_read_config_refuses_deep_symlink_without_disclosing_external_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            secret_id = "external-registry-id-must-not-be-disclosed"
            (outside / "projects.json").write_text(
                json.dumps({"schema_version": 1, "projects": [{"id": secret_id, "aliases": [], "remotes": []}]}),
                encoding="utf-8",
            )
            linked = root / "safe" / "linked"
            linked.parent.mkdir()
            linked.symlink_to(outside, target_is_directory=True)
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                code = memory.main(["--registry", str(linked / "nested" / ".." / "projects.json"), "inventory"])
            self.assertEqual(2, code)
            self.assertIn("symbolic link", error.getvalue())
            self.assertNotIn(secret_id, error.getvalue())

    def test_workspace_installer_is_dry_run_idempotent_and_collision_safe(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            wrapper = workspace / "bin" / "engram-mcp-wrapper"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/example-org/example-product.git",
                ],
                check=True,
            )
            plan = memory.install_client(
                registry,
                client="antigravity",
                project="example-product",
                workspace=workspace,
                wrapper_path=wrapper,
                _allow_unmanaged_wrapper=True,
                dry_run=True,
            )
            target = workspace / ".agents" / "mcp_config.json"
            self.assertEqual("dry_run", plan["status"])
            self.assertFalse(target.exists())
            installed = memory.install_client(
                registry,
                client="antigravity",
                project="example-product",
                workspace=workspace,
                wrapper_path=wrapper,
                _allow_unmanaged_wrapper=True,
            )
            self.assertEqual("installed", installed["status"])
            self.assertEqual("example-product", json.loads(target.read_text())["mcpServers"]["engram"]["env"]["ENGRAM_PROJECT"])
            self.assertEqual(str(Path(os.path.abspath(wrapper))), json.loads(target.read_text())["mcpServers"]["engram"]["command"])
            repeated = memory.install_client(
                registry,
                client="antigravity",
                project="example-product",
                workspace=workspace,
                wrapper_path=wrapper,
                _allow_unmanaged_wrapper=True,
            )
            self.assertEqual("already_configured", repeated["status"])
            payload = json.loads(target.read_text())
            payload["mcpServers"]["engram"]["command"] = "different-wrapper"
            target.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(memory.EngramMemoryError, "differs"):
                memory.install_client(
                    registry,
                    client="antigravity",
                    project="example-product",
                    workspace=workspace,
                    wrapper_path=wrapper,
                    _allow_unmanaged_wrapper=True,
                )

    def test_workspace_installer_refuses_comment_bearing_jsonc(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            wrapper = workspace / "bin" / "engram-mcp-wrapper"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/example-org/example-product.git",
                ],
                check=True,
            )
            target = workspace / ".kilo" / "kilo.jsonc"
            target.parent.mkdir()
            original = "// user comment\n{}\n"
            target.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(memory.EngramMemoryError, "strict JSON"):
                memory.install_client(
                    registry,
                    client="kilo",
                    project="example-product",
                    workspace=workspace,
                    wrapper_path=wrapper,
                    _allow_unmanaged_wrapper=True,
                )
            self.assertEqual(original, target.read_text(encoding="utf-8"))

    def test_workspace_installer_rejects_remote_mismatch_without_creating_a_file(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            wrapper = workspace / "bin" / "engram-mcp-wrapper"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/example-org/example-platform.git",
                ],
                check=True,
            )
            target = workspace / ".vscode" / "mcp.json"
            with self.assertRaisesRegex(memory.EngramMemoryError, "different canonical projects"):
                memory.install_client(
                    registry,
                    client="vscode-generic",
                    project="example-product",
                    workspace=workspace,
                    wrapper_path=wrapper,
                    _allow_unmanaged_wrapper=True,
                )
            self.assertFalse(target.exists())

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_workspace_installer_refuses_symlinked_parent_without_external_write(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/example-product.git"], check=True)
            wrapper = root / "wrapper"
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            (workspace / ".cursor").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                memory.install_client(
                    registry,
                    client="cursor",
                    project="example-product",
                    workspace=workspace,
                    wrapper_path=wrapper,
                    _allow_unmanaged_wrapper=True,
                )
            self.assertFalse((outside / "mcp.json").exists())

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_client_verification_refuses_symlinked_parent(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            outside = root / "outside"
            workspace.mkdir()
            outside.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/example-product.git"], check=True)
            wrapper = root / "wrapper"
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            rendered = memory.client_template(
                "cursor", "example-product", wrapper_command=str(wrapper.resolve())
            )
            (outside / "mcp.json").write_text(rendered, encoding="utf-8")
            (workspace / ".cursor").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                memory.verify_client(
                    registry,
                    client="cursor",
                    project="example-product",
                    workspace=workspace,
                    wrapper=wrapper,
                    host_version=None,
                    _allow_unmanaged_wrapper=True,
                )

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_instruction_installer_refuses_symlink_target_without_copying_external_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            secret = root / "secret"
            secret.write_text("external-sensitive-fixture\n", encoding="utf-8")
            (workspace / "AGENTS.md").symlink_to(secret)
            with self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                memory.install_instruction(workspace, "codex")
            self.assertEqual("external-sensitive-fixture\n", secret.read_text(encoding="utf-8"))
            self.assertEqual([], list(workspace.glob("AGENTS.md.engram-backup-*")))

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_exclusive_temporary_write_does_not_follow_predictable_symlink(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/example-product.git"], check=True)
            wrapper = root / "wrapper"
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            config_dir = workspace / ".cursor"
            config_dir.mkdir()
            secret = root / "secret"
            secret.write_text("unchanged\n", encoding="utf-8")
            predictable = config_dir / ".mcp.json.engram-memory.tmp"
            predictable.symlink_to(secret)
            result = memory.install_client(
                registry,
                client="cursor",
                project="example-product",
                workspace=workspace,
                wrapper_path=wrapper,
                _allow_unmanaged_wrapper=True,
            )
            self.assertEqual("installed", result["status"])
            self.assertEqual("unchanged\n", secret.read_text(encoding="utf-8"))
            self.assertTrue(predictable.is_symlink())

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_runtime_install_refuses_broken_symlink_target_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            config_dir = home / ".config" / memory.TOOLKIT_NAMESPACE
            config_dir.mkdir(parents=True)
            outside = root / "outside-projects.json"
            (config_dir / "projects.json").symlink_to(outside)
            with self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                memory.ensure_user_runtime(home=home)
            self.assertFalse(outside.exists())
            self.assertEqual(["projects.json"], [path.name for path in config_dir.iterdir()])

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_runtime_install_refuses_symlinked_config_path_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            outside = root / "outside-config"
            outside.mkdir()
            (home / ".config").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                memory.ensure_user_runtime(home=home)
            self.assertEqual([], list(outside.iterdir()))

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_runtime_refuses_symlinked_home_root_before_external_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            home = root / "home-link"
            home.symlink_to(outside, target_is_directory=True)
            for dry_run in (False, True):
                with self.subTest(dry_run=dry_run), self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                    memory.ensure_user_runtime(home=home, dry_run=dry_run)
            self.assertEqual([], list(outside.iterdir()))

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_client_install_and_verify_refuse_symlinked_workspace_root(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            workspace = root / "workspace-link"
            workspace.symlink_to(outside, target_is_directory=True)
            wrapper = root / "wrapper"
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            for dry_run in (False, True):
                with self.subTest(operation="install", dry_run=dry_run), self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                    memory.install_client(registry, client="cursor", project="example-product", workspace=workspace, wrapper_path=wrapper, dry_run=dry_run, _allow_unmanaged_wrapper=True)
            with self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                memory.verify_client(registry, client="cursor", project="example-product", workspace=workspace, wrapper=wrapper, host_version=None, _allow_unmanaged_wrapper=True)
            self.assertEqual([], list(outside.iterdir()))

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_runtime_install_refuses_nested_symlink_in_explicit_external_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            outside = root / "outside"
            outside.mkdir()
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            for field in ("config_dir", "bin_dir"):
                with self.subTest(field=field):
                    kwargs = {
                        "home": home,
                        "config_dir": home / "config",
                        "bin_dir": home / "bin",
                    }
                    kwargs[field] = linked / "nested" / field
                    with self.assertRaisesRegex(memory.EngramMemoryError, "symbolic link"):
                        memory.ensure_user_runtime(**kwargs)
            self.assertEqual([], list(outside.iterdir()))

    def test_muse_user_installer_is_preview_only(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            wrapper = home / "bin" / "engram-mcp-wrapper"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            with self.assertRaisesRegex(memory.EngramMemoryError, "no safe managed installer"):
                memory.install_client(registry, client="muse", project="example-product", home=home)

    def test_muse_template_remains_render_only(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            wrapper = home / "bin" / "engram-mcp-wrapper"
            wrapper.parent.mkdir()
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            wrapper.chmod(0o755)
            rendered = memory.client_template("muse", wrapper_command=str(wrapper))
            self.assertEqual(str(wrapper), json.loads(rendered)["mcp_servers"]["engram"]["command"])

    def test_vscode_user_config_paths_are_platform_specific(self) -> None:
        home = Path("/synthetic-home")
        self.assertEqual(
            (home / "Library" / "Application Support" / "Code" / "User" / "mcp.json",),
            memory.vscode_user_config_paths(home, platform="darwin"),
        )
        self.assertEqual(
            (home / ".config" / "Code" / "User" / "mcp.json",),
            memory.vscode_user_config_paths(home, platform="linux"),
        )
        self.assertIn(
            home / "AppData" / "Roaming" / "Code" / "User" / "mcp.json",
            memory.vscode_user_config_paths(home, platform="win32"),
        )

    def test_public_sanitizer_rejects_memory_artifacts_and_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".engram").mkdir()
            (root / ".engram" / "manifest.json").write_text("{}", encoding="utf-8")
            (root / "token.txt").write_text("ghp_" + ("a" * 32), encoding="utf-8")
            failures = sanitizer.scan(root, require_fresh_git=False)
            self.assertTrue(any("forbidden path" in failure for failure in failures))
            self.assertTrue(any("GitHub token" in failure for failure in failures))

    def test_public_sanitizer_accepts_synthetic_text_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("Synthetic toolkit fixture.\n", encoding="utf-8")
            self.assertEqual([], sanitizer.scan(root, require_fresh_git=False))

    def test_public_sanitizer_ignores_python_bytecode_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "fixture.cpython-312.pyc").write_bytes(b"\x00\xff")
            self.assertEqual([], sanitizer.scan(root, require_fresh_git=False))

    def test_public_sanitizer_rejects_operator_supplied_private_term(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("private-name\n", encoding="utf-8")
            failures = sanitizer.scan(root, require_fresh_git=False, forbidden_terms=("private-name",))
            self.assertTrue(any("forbidden publication term" in failure for failure in failures))

    def test_public_sanitizer_rejects_casefolded_memory_and_private_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            memory_dir = root / ".ENGRAM"
            memory_dir.mkdir()
            (memory_dir / "chunks.json").write_text("{}\n", encoding="utf-8")
            private_dir = root / "Private-Term"
            private_dir.mkdir()
            (private_dir / "notes.md").write_text("innocuous\n", encoding="utf-8")
            failures = sanitizer.scan(root, False, ("private-term",))
            self.assertTrue(any("forbidden path component" in item for item in failures))
            self.assertTrue(any("detected in path" in item for item in failures))

    def test_public_sanitizer_rejects_renamed_sqlite_and_oversize_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "innocent.dat").write_bytes(b"SQLite format 3\x00" + b"fixture")
            (root / "large.txt").write_bytes(b"x" * (sanitizer.MAX_PUBLIC_FILE_BYTES + 1))
            failures = sanitizer.scan(root, require_fresh_git=False)
            self.assertTrue(any("SQLite database content" in failure for failure in failures))
            self.assertTrue(any("larger than 2MB" in failure for failure in failures))

    @unittest.skipIf(os.name == "nt", "symbolic-link behavior is validated on Unix hosts")
    def test_public_sanitizer_rejects_absolute_target_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            external = Path(temporary).parent / "external-sanitizer-fixture"
            (root / "linked.txt").symlink_to(external.absolute())
            failures = sanitizer.scan(root, require_fresh_git=False)
            self.assertTrue(any("symbolic link is not allowed" in failure for failure in failures))

    def test_public_sanitizer_accepts_one_commit_fresh_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("Synthetic toolkit fixture.\n", encoding="utf-8")
            for command in (
                ["git", "init", "-q", str(root)],
                ["git", "-C", str(root), "add", "README.md"],
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "-qm",
                    "Initial synthetic toolkit",
                ],
            ):
                subprocess.run(command, check=True)
            self.assertEqual([], sanitizer.scan(root, require_fresh_git=True))

    def test_public_sanitizer_fresh_history_rejects_identity_ref_and_dangling_object(self) -> None:
        def fresh_repo(root: Path, *, name: str = "Fixture", email: str = "fixture@example.invalid") -> None:
            (root / "README.md").write_text("Synthetic toolkit fixture.\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-qm", "fixture"], check=True)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "identity"
            root.mkdir()
            fresh_repo(root, name="Personal Name", email="personal@example.invalid")
            self.assertTrue(any("synthetic author" in item for item in sanitizer.scan(root, True)))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "ref"
            root.mkdir()
            fresh_repo(root)
            subprocess.run(["git", "-C", str(root), "tag", "unexpected"], check=True)
            self.assertTrue(any("intended branch ref" in item for item in sanitizer.scan(root, True)))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "dangling"
            root.mkdir()
            fresh_repo(root)
            subprocess.run(["git", "-C", str(root), "hash-object", "-w", "README.md"], check=True, stdout=subprocess.PIPE)
            # The current file is already reachable; write a distinct dangling blob.
            subprocess.run(["git", "-C", str(root), "hash-object", "-w", "--stdin"], input=b"dangling\n", check=True, stdout=subprocess.PIPE)
            self.assertTrue(any("unreachable" in item for item in sanitizer.scan(root, True)))

    def test_public_sanitizer_fresh_history_scans_exact_clean_commit(self) -> None:
        def fresh_repo(root: Path) -> None:
            root.mkdir()
            (root / "README.md").write_text("Synthetic toolkit fixture.\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            subprocess.run([
                "git", "-C", str(root), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture",
            ], check=True)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "deleted-secret"
            fresh_repo(root)
            secret = root / "committed.txt"
            synthetic_token = "gh" + "p_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
            secret.write_text(synthetic_token + "\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "committed.txt"], check=True)
            subprocess.run([
                "git", "-C", str(root), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "--amend", "-qm", "fixture",
            ], check=True)
            secret.unlink()
            failures = sanitizer.scan(root, True)
            self.assertTrue(any("completely clean" in item for item in failures))
            self.assertTrue(any("GitHub token" in item for item in failures))

        for state in ("modified", "staged", "untracked"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / state
                fresh_repo(root)
                if state == "modified":
                    (root / "README.md").write_text("changed\n", encoding="utf-8")
                else:
                    extra = root / "extra.txt"
                    extra.write_text("extra\n", encoding="utf-8")
                    if state == "staged":
                        subprocess.run(["git", "-C", str(root), "add", "extra.txt"], check=True)
                self.assertTrue(any("completely clean" in item for item in sanitizer.scan(root, True)))

    def test_public_sanitizer_fresh_history_rejects_gitlink_and_gitmodules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "gitlink"
            root.mkdir()
            (root / "README.md").write_text("Synthetic toolkit fixture.\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            subprocess.run([
                "git", "-C", str(root), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture",
            ], check=True)
            prior = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                text=True, stdout=subprocess.PIPE,
            ).stdout.strip()
            subprocess.run(
                ["git", "-C", str(root), "update-index", "--add", "--cacheinfo", f"160000,{prior},vendor"],
                check=True,
            )
            subprocess.run([
                "git", "-C", str(root), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "--amend", "-qm", "fixture",
            ], check=True)
            failures = sanitizer.scan(root, True)
            self.assertTrue(any("gitlink/submodule" in item for item in failures))

    def test_public_sanitizer_fresh_history_scans_commit_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("Synthetic toolkit fixture.\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            token = "gh" + "p_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
            subprocess.run([
                "git", "-C", str(root), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "-qm", token,
            ], check=True)
            failures = sanitizer.scan(root, True)
            self.assertTrue(any("GitHub token" in item and "commit-message" in item for item in failures))

    def test_public_sanitizer_fresh_history_scans_branch_ref(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "README.md").write_text("Synthetic toolkit fixture.\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", "-b", "private-term", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            subprocess.run([
                "git", "-C", str(root), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture",
            ], check=True)
            failures = sanitizer.scan(root, True, ("private-term",))
            self.assertTrue(any("branch-ref" in item and "forbidden publication term" in item for item in failures))


class ScriptContractTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "Unix setup shims are checked on Unix runners")
    def test_source_setup_verifies_and_reuses_one_supported_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            calls = root / "calls"
            old_canary = root / "old-executed-tool"
            old = root / "python3"
            old.write_text(
                f"#!/bin/sh\nif [ \"${{1:-}}\" = -c ]; then exit 1; fi\ntouch '{old_canary}'\nexit 91\n",
                encoding="utf-8",
            )
            old.chmod(0o755)
            good = root / "python"
            good.write_text(
                f"#!/bin/sh\nif [ \"${{1:-}}\" = -c ]; then exit 0; fi\nprintf x >> '{calls}'\nexec {str(Path(sys.executable).resolve())!r} \"$@\"\n",
                encoding="utf-8",
            )
            good.chmod(0o755)
            environment = {**os.environ, "HOME": str(home), "PATH": str(root) + os.pathsep + os.environ.get("PATH", "")}
            environment.pop("NAOS_ENGRAM_MEMORY_PYTHON", None)
            result = subprocess.run(
                ["bash", "scripts/setup.sh", "--inventory"], cwd=ROOT, env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(old_canary.exists())
            self.assertEqual("x", calls.read_text(encoding="utf-8"))
            calls.unlink()
            configured = subprocess.run(
                ["bash", "scripts/setup.sh", "--python", str(good), "--inventory"], cwd=ROOT,
                env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(0, configured.returncode, configured.stderr)
            self.assertEqual("x", calls.read_text(encoding="utf-8"))

    @unittest.skipIf(sys.platform == "win32", "Unix setup shims are checked on Unix runners")
    def test_source_setup_missing_python_has_exact_noninteractive_remediation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("python3", "python", "py"):
                candidate = root / name
                candidate.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                candidate.chmod(0o755)
            dirname = root / "dirname"
            dirname.write_text("#!/bin/sh\nexec /usr/bin/dirname \"$@\"\n", encoding="utf-8")
            dirname.chmod(0o755)
            environment = {**os.environ, "HOME": str(root / "home"), "PATH": str(root)}
            environment.pop("NAOS_ENGRAM_MEMORY_PYTHON", None)
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "scripts" / "setup.sh"), "--inventory"], env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn(
                "Exact remediation: install Python 3.10+, then rerun with --python /absolute/path.",
                result.stderr,
            )

    def test_powershell_setup_uses_verified_shared_interpreter_selection(self) -> None:
        source = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn("[string]$Python", source)
        self.assertIn("function Resolve-Python", source)
        self.assertIn("sys.version_info >= (3, 10)", source)
        self.assertIn("$ResolvedPython = Resolve-Python", source)
        self.assertIn("& $ResolvedPython.Command @($ResolvedPython.LauncherArgs) $Tool @ToolArgs", source)
        self.assertNotIn("& $python.Source", source)

    def test_installed_runtime_report_identity_binds_commit_and_exact_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "fixture.txt").write_text("first\n", encoding="utf-8")
            (source / ".gitignore").write_text(".pytest_cache/\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "-C", str(source), "add", "fixture.txt", ".gitignore"], check=True)
            subprocess.run(
                ["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
                check=True,
            )
            identity = installed_runtime.source_identity(source)
            self.assertRegex(identity["source_commit"], r"^[0-9a-f]{40}$")
            self.assertRegex(identity["source_git_tree"], r"^[0-9a-f]{40}$")
            self.assertRegex(identity["package_input_sha256"], r"^[0-9a-f]{64}$")
            self.assertFalse(identity["source_worktree_dirty"])
            original_tree = identity["package_input_sha256"]
            evidence = source / "docs" / "evidence" / "generated.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("{}\n", encoding="utf-8")
            after_evidence = installed_runtime.source_identity(source)
            self.assertEqual(original_tree, after_evidence["package_input_sha256"])
            cache = source / ".pytest_cache" / "generated.txt"
            cache.parent.mkdir()
            cache.write_text("ignored\n", encoding="utf-8")
            after_cache = installed_runtime.source_identity(source)
            self.assertEqual(original_tree, after_cache["package_input_sha256"])
            (source / "fixture.txt").write_text("second\n", encoding="utf-8")
            changed = installed_runtime.source_identity(source)
            self.assertEqual(identity["source_commit"], changed["source_commit"])
            self.assertEqual(identity["source_git_tree"], changed["source_git_tree"])
            self.assertNotEqual(original_tree, changed["package_input_sha256"])
            self.assertTrue(changed["source_worktree_dirty"])
            with self.assertRaisesRegex(installed_runtime.RuntimeValidationError, "source-tree hash"):
                installed_runtime.source_identity(source, expected_tree=original_tree)

    def test_installed_runtime_disposition_distinguishes_dirty_candidate(self) -> None:
        checks = {"wheel": "passed", "wrapper": "passed"}
        self.assertEqual(
            "passed",
            installed_runtime.validation_disposition(checks, source_worktree_dirty=False),
        )
        self.assertEqual(
            "candidate_passed_not_release_attestation",
            installed_runtime.validation_disposition(checks, source_worktree_dirty=True),
        )
        self.assertEqual(
            "failed",
            installed_runtime.validation_disposition(
                {**checks, "wrapper": "failed"}, source_worktree_dirty=False
            ),
        )

    def test_windows_archive_identity_binds_supported_zip_to_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            config = source / "config"
            config.mkdir(parents=True)
            binary = Path(temporary) / "engram.exe"
            binary.write_bytes(b"official-fixture-binary")
            archive = Path(temporary) / "engram_1.20.0_windows_amd64.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("engram.exe", binary.read_bytes())
            (config / "release-support.json").write_text(
                json.dumps({
                    "releases": [{
                        "version": "1.20.0",
                        "status": "supported",
                        "platforms": {"windows_amd64": {
                            "status": "supported",
                            "asset": archive.name,
                            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        }},
                    }],
                }),
                encoding="utf-8",
            )
            identity = installed_runtime.windows_archive_identity(source, archive, binary)
            self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), identity["engram_archive_sha256"])
            self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(), identity["engram_binary_sha256"])
            binary.write_bytes(b"different")
            with self.assertRaisesRegex(installed_runtime.RuntimeValidationError, "does not match"):
                installed_runtime.windows_archive_identity(source, archive, binary)

    @unittest.skipIf(sys.platform == "win32", "Unix bootstrap shims are checked on Unix runners")
    def test_bootstrap_shell_probes_explicit_and_skips_old_discovered_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts" / "bootstrap.sh", scripts / "bootstrap.sh")
            output = root / "argv.json"
            (scripts / "bootstrap.py").write_text(
                "import json, os, sys\nopen(os.environ['BOOTSTRAP_ARGV'], 'w').write(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            good = root / "python"
            good.write_text(
                f"#!/bin/sh\nif [ \"${{1:-}}\" = -c ]; then exit 0; fi\nexec {str(Path(sys.executable).resolve())!r} \"$@\"\n",
                encoding="utf-8",
            )
            good.chmod(0o755)
            old = root / "python3"
            old.write_text("#!/bin/sh\nif [ \"${1:-}\" = -c ]; then exit 1; fi\nprintf bad > \"$OLD_PYTHON_CANARY\"\nexit 91\n", encoding="utf-8")
            old.chmod(0o755)
            canary = root / "old-ran-bootstrap"
            environment = {
                **os.environ,
                "PATH": str(root) + os.pathsep + os.environ.get("PATH", ""),
                "BOOTSTRAP_ARGV": str(output),
                "OLD_PYTHON_CANARY": str(canary),
            }
            discovered = subprocess.run([str(scripts / "bootstrap.sh"), "--list"], env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            self.assertEqual(0, discovered.returncode, discovered.stderr)
            self.assertEqual(["--list"], json.loads(output.read_text(encoding="utf-8")))
            self.assertFalse(canary.exists())
            output.unlink()
            explicit = subprocess.run([str(scripts / "bootstrap.sh"), "--python", str(good), "--list"], env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            self.assertEqual(0, explicit.returncode, explicit.stderr)
            self.assertEqual(["--python", str(good), "--list"], json.loads(output.read_text(encoding="utf-8")))

    def test_powershell_bootstrap_probes_version_before_execution(self) -> None:
        source = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")
        self.assertIn("sys.version_info >= (3, 10)", source)
        self.assertIn("foreach ($entry in $candidates)", source)
        self.assertLess(source.index("Get-Command python3"), source.index("Get-Command python -ErrorAction"))
        self.assertLess(source.index("Get-Command python -ErrorAction"), source.index("Get-Command py -ErrorAction"))

    def test_bootstrap_refuses_unpinned_or_substitute_package(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            bootstrap.main(["--package", "naos-engram-memories", "--list"])
        self.assertEqual(2, raised.exception.code)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            bootstrap.main(["--package", "other-package==0.1.0", "--list"])
        self.assertEqual(2, raised.exception.code)

    def test_bootstrap_requires_explicit_python_when_multiple_are_noninteractive(self) -> None:
        candidates = [
            {"path": "/fixture/python-a", "launcher_args": [], "version": [3, 11, 0], "supported": True},
            {"path": "/fixture/python-b", "launcher_args": [], "version": [3, 12, 0], "supported": True},
        ]
        with self.assertRaisesRegex(ValueError, "multiple supported Python"):
            bootstrap.select_python_candidate(candidates, explicit=None, non_interactive=True)

    def test_bootstrap_honours_explicit_python_selection(self) -> None:
        selected = {"path": str(Path(sys.executable).resolve()), "launcher_args": [], "version": [3, 12, 0], "supported": True}
        with unittest.mock.patch.object(bootstrap.shutil, "which", return_value=selected["path"]):
            self.assertEqual(
                selected,
                bootstrap.select_python_candidate([selected], explicit="fixture-python", non_interactive=True),
            )

    def test_bootstrap_rejects_zero_negative_and_out_of_range_selection(self) -> None:
        candidates = [
            {"path": "/fixture/python-a", "launcher_args": [], "version": [3, 11, 0], "supported": True},
            {"path": "/fixture/python-b", "launcher_args": [], "version": [3, 12, 0], "supported": True},
        ]
        for answer in ("0", "-1", "3"):
            with (
                self.subTest(answer=answer),
                unittest.mock.patch.object(bootstrap.sys.stdin, "isatty", return_value=True),
                unittest.mock.patch("builtins.input", return_value=answer),
                self.assertRaisesRegex(ValueError, "valid Python candidate"),
            ):
                bootstrap.select_python_candidate(candidates, explicit=None, non_interactive=False)

    def test_bootstrap_enumerates_supported_explicit_interpreter_without_installing(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/bootstrap.py", "--python", sys.executable, "--list"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        selected = next(item for item in payload["candidates"] if item["path"] == str(Path(sys.executable).resolve()))
        self.assertTrue(selected["supported"])
        self.assertFalse(payload["network_accessed"])

    def test_bootstrap_noninteractive_install_requires_consent(self) -> None:
        selected = {"path": str(Path(sys.executable).resolve()), "launcher_args": [], "version": [3, 12, 0], "supported": True}
        error = io.StringIO()
        with (
            unittest.mock.patch.object(bootstrap, "candidates", return_value=[selected]),
            unittest.mock.patch.object(bootstrap.shutil, "which", return_value="/fixture/pipx"),
            contextlib.redirect_stderr(error),
        ):
            code = bootstrap.main(["--python", selected["path"], "--non-interactive"])
        self.assertEqual(2, code)
        self.assertIn("requires consent", error.getvalue())

    def test_bootstrap_explains_missing_pipx_before_toolkit_consent(self) -> None:
        selected = {"path": str(Path(sys.executable).resolve()), "launcher_args": [], "version": [3, 12, 0], "supported": True}
        error = io.StringIO()
        with (
            unittest.mock.patch.object(bootstrap, "candidates", return_value=[selected]),
            unittest.mock.patch.object(bootstrap, "pipx_install_offer", return_value={"available": True, "manager": "fixture", "command": ["fixture-pm", "install", "pipx"]}),
            unittest.mock.patch.object(bootstrap.shutil, "which", return_value=None),
            contextlib.redirect_stderr(error),
        ):
            code = bootstrap.main(["--python", selected["path"], "--non-interactive"])
        self.assertEqual(2, code)
        self.assertIn("pipx is required", error.getvalue())
        self.assertIn("fixture-pm", error.getvalue())
        self.assertNotIn("Package installation requires consent", error.getvalue())

    def test_bootstrap_noninteractive_python_offer_never_executes_package_manager(self) -> None:
        error = io.StringIO()
        with (
            unittest.mock.patch.object(bootstrap, "candidates", return_value=[]),
            unittest.mock.patch.object(bootstrap, "python_install_offer", return_value={"available": True, "manager": "fixture", "command": ["fixture-pm", "install", "python"]}),
            unittest.mock.patch.object(bootstrap.subprocess, "run") as runner,
            contextlib.redirect_stderr(error),
        ):
            code = bootstrap.main(["--install-python", "--yes", "--non-interactive"])
        self.assertEqual(2, code)
        runner.assert_not_called()
        self.assertIn('"network_accessed": false', error.getvalue())
        self.assertIn("fixture-pm", error.getvalue())

    def test_bootstrap_consent_execution_does_not_claim_network_observation(self) -> None:
        output = io.StringIO()
        completed = subprocess.CompletedProcess(["fixture-pm"], 0)
        with (
            unittest.mock.patch.object(bootstrap, "candidates", return_value=[]),
            unittest.mock.patch.object(bootstrap, "python_install_offer", return_value={"available": True, "manager": "fixture", "command": ["fixture-pm", "install", "python"]}),
            unittest.mock.patch.object(bootstrap.subprocess, "run", return_value=completed) as runner,
            contextlib.redirect_stdout(output),
        ):
            code = bootstrap.main(["--install-python", "--yes"])
        self.assertEqual(0, code)
        runner.assert_called_once()
        payload = json.loads(output.getvalue())
        self.assertIsNone(payload["network_accessed"])
        self.assertEqual("not_observed_by_bootstrap", payload["network_access_status"])

    def test_bootstrap_noninteractive_pipx_offer_never_executes_package_manager(self) -> None:
        selected = {"path": str(Path(sys.executable).resolve()), "launcher_args": [], "version": [3, 12, 0], "supported": True}
        error = io.StringIO()
        with (
            unittest.mock.patch.object(bootstrap, "candidates", return_value=[selected]),
            unittest.mock.patch.object(bootstrap, "python_install_offer", return_value={"available": False, "manager": None, "command": None}),
            unittest.mock.patch.object(bootstrap, "pipx_install_offer", return_value={"available": True, "manager": "fixture", "command": ["fixture-pm", "install", "pipx"]}),
            unittest.mock.patch.object(bootstrap.shutil, "which", return_value=None),
            unittest.mock.patch.object(bootstrap.subprocess, "run") as runner,
            contextlib.redirect_stderr(error),
        ):
            code = bootstrap.main(["--install-pipx", "--yes", "--non-interactive"])
        self.assertEqual(2, code)
        runner.assert_not_called()
        self.assertIn('"network_accessed": false', error.getvalue())
        self.assertIn("fixture-pm", error.getvalue())

    def test_bootstrap_explicit_pipx_install_reports_truthfully_then_stops(self) -> None:
        selected = {"path": str(Path(sys.executable).resolve()), "launcher_args": [], "version": [3, 12, 0], "supported": True}
        output = io.StringIO()
        error = io.StringIO()
        completed = subprocess.CompletedProcess(["fixture-pm", "install", "pipx"], 0)
        with (
            unittest.mock.patch.object(bootstrap, "candidates", return_value=[selected]),
            unittest.mock.patch.object(bootstrap, "python_install_offer", return_value={"available": False, "manager": None, "command": None}),
            unittest.mock.patch.object(bootstrap, "pipx_install_offer", return_value={"available": True, "manager": "fixture", "command": ["fixture-pm", "install", "pipx"]}),
            unittest.mock.patch.object(bootstrap.shutil, "which", side_effect=[None, "/fixture/pipx"]),
            unittest.mock.patch.object(bootstrap.subprocess, "run", return_value=completed) as runner,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            code = bootstrap.main(["--install-pipx", "--yes"])
        self.assertEqual(0, code)
        runner.assert_called_once_with(["fixture-pm", "install", "pipx"], check=False)
        payload = json.loads(output.getvalue())
        self.assertIsNone(payload["network_accessed"])
        self.assertEqual("not_observed_by_bootstrap", payload["network_access_status"])
        self.assertTrue(payload["pipx_detected_after_install"])
        self.assertIn("Rerun bootstrap", error.getvalue())

    def test_bootstrap_checksums_its_delivered_assets(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/bootstrap.py", "--verify-asset", "bootstrap.py"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(json.loads(result.stdout)["verified"])

    def test_powershell_bootstrap_forwards_all_install_choices(self) -> None:
        source = (ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$InstallPipx", source)
        self.assertIn("[switch]$PipFallback", source)
        self.assertIn("[string]$Package", source)
        self.assertIn("$arguments += '--install-pipx'", source)
        self.assertIn("$arguments += '--pip-fallback'", source)
        self.assertIn("$arguments += @('--package', $Package)", source)
        self.assertIn("Get-Command python3", source)
        self.assertIn("LauncherArgs = @('-3')", source)
        self.assertIn("& $candidate.Source @launcherArgs @arguments", source)

    def test_powershell_source_configure_delegates_to_hardened_runtime(self) -> None:
        source = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn("@('runtime', 'install', '--home', $HOME, '--yes', '--non-interactive')", source)
        self.assertNotIn("Copy-Item $Registry", source)
        self.assertNotIn("Remove-Item -Path $ConfigDir", source)

    def test_powershell_binary_and_database_maintenance_is_disabled(self) -> None:
        source = (ROOT / "scripts" / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn("Windows binary/database maintenance is experimental and disabled", source)
        for unsafe in ("Invoke-WebRequest", "Expand-Archive", "Backup-Database", "Copy-Item $candidate", "Restore-PreviousBinary"):
            self.assertNotIn(unsafe, source)

    def test_source_setup_has_no_independent_backup_or_replacement_path(self) -> None:
        source_text = (ROOT / "scripts" / "setup.sh").read_text(encoding="utf-8")
        self.assertNotIn("exclusive_backup_file", source_text)
        self.assertNotIn("cp --", source_text)
        self.assertIn("provider \"$provider_action\"", source_text)

    def test_powershell_wrapper_does_not_run_provider_diagnostics(self) -> None:
        source = (ROOT / "scripts" / "engram_mcp_wrapper.ps1").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"&\s+\$EngramBin\s+version")
        self.assertNotRegex(source, r"&\s+\$EngramBin\s+doctor")
        self.assertIn("mcp '--tools=agent'", source)
        for variable in (
            "ENGRAM_CLOUD_AUTOSYNC", "ENGRAM_CLOUD_SERVER", "ENGRAM_CLOUD_TOKEN",
            "ENGRAM_REMOTE_URL", "ENGRAM_TOKEN",
            "ENGRAM_DATABASE_URL", "ENGRAM_JWT_SECRET",
        ):
            self.assertIn(variable, source)

    @unittest.skipIf(sys.platform == "win32", "Unix literal wrapper rendering is checked on Unix runners")
    def test_rendered_wrapper_paths_are_literal_not_shell_code(self) -> None:
        with tempfile.TemporaryDirectory(prefix="naos wrapper 'space' ") as temporary:
            root = Path(temporary)
            for payload in ("$(touch CANARY)", "`touch CANARY2`", "quote ' and spaces"):
                with self.subTest(payload=payload):
                    config = root / payload
                    bin_dir = root / "bin"
                    config.mkdir(parents=True, exist_ok=True)
                    bin_dir.mkdir(exist_ok=True)
                    assets = memory.rendered_wrapper_assets(config, bin_dir)
                    wrapper_target, wrapper_text = assets["engram-mcp-wrapper"]
                    wrapper_target.write_text(wrapper_text, encoding="utf-8")
                    wrapper_target.chmod(0o755)
                    result = subprocess.run([str(wrapper_target)], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                    self.assertNotEqual(0, result.returncode)
                    self.assertFalse((root / "CANARY").exists())
                    self.assertFalse((root / "CANARY2").exists())
                    self.assertIn("NAOS_ENGRAM_MEMORY_CONFIG_DIR=", wrapper_text)

    def test_powershell_and_cmd_wrapper_rendering_use_language_literals(self) -> None:
        config = Path("C:/Program Files/NAOS/O'Brien")
        with (
            unittest.mock.patch.object(memory.os, "name", "nt"),
            unittest.mock.patch.object(memory, "RESOURCE_SCRIPT_DIR", ROOT / "scripts"),
            unittest.mock.patch.object(memory.provider_lifecycle, "configured_provider_path", side_effect=lambda _config, default: default),
        ):
            assets = memory.rendered_wrapper_assets(config, Path("C:/fixture/bin"))
        ps = assets["engram-mcp-wrapper.ps1"][1]
        cmd = assets["engram-mcp-wrapper.cmd"][1]
        self.assertIn("O''Brien", ps)
        self.assertNotIn("__NAOS_ENGRAM_MEMORY_CONFIG_DIR__", ps)
        self.assertEqual('@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0engram-mcp-wrapper.ps1" %*\r\n', cmd)

    def test_wrapper_rendering_refuses_control_characters(self) -> None:
        with self.assertRaisesRegex(memory.EngramMemoryError, "control characters"):
            memory.rendered_wrapper_assets(Path("/fixture/bad\npath"), Path("/fixture/bin"))

    def test_built_wheel_finds_catalogue_and_templates_in_clean_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            wheelhouse = root / "wheelhouse"
            environment = os.environ.copy()
            environment["PIP_NO_INDEX"] = "1"
            shutil.copytree(
                ROOT,
                source,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.egg-info", "build", "dist"),
            )
            wheelhouse.mkdir()
            built = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheelhouse)],
                cwd=source,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            wheels = list(wheelhouse.glob("*.whl"))
            self.assertEqual(1, len(wheels))
            with zipfile.ZipFile(wheels[0]) as archive:
                names = set(archive.namelist())
            self.assertFalse(
                any(name.endswith("clients/opencode-v2.mcp.json") for name in names)
            )
            for asset in (
                "bootstrap.py",
                "bootstrap.sh",
                "bootstrap.ps1",
                "engram_mcp_wrapper.sh",
                "engram_mcp_wrapper.ps1",
            ):
                self.assertTrue(
                    any(name.endswith(f"share/naos-engram-memory/scripts/{asset}") for name in names),
                    f"wheel omitted {asset}",
                )
            for asset in (
                "config/mcp-host-observations.v1.json",
                "config/schemas/mcp-host-observations.v1.schema.json",
                "clients/codex.mcp.toml",
            ):
                self.assertTrue(
                    any(name.endswith(f"share/naos-engram-memory/{asset}") for name in names),
                    f"wheel omitted {asset}",
                )
            self.assertTrue(any(name.endswith("scripts/bootstrap.py") and ".data/" not in name for name in names))
            venv = root / "venv"
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
            executable = venv / ("Scripts/naos-engram-memory.exe" if sys.platform == "win32" else "bin/naos-engram-memory")
            bootstrap_executable = venv / ("Scripts/naos-engram-memory-bootstrap.exe" if sys.platform == "win32" else "bin/naos-engram-memory-bootstrap")
            venv_python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            installed = subprocess.run(
                [str(venv_python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, installed.returncode, installed.stderr)
            self.assertTrue(bootstrap_executable.is_file())
            verified_asset = subprocess.run(
                [str(bootstrap_executable), "--verify-asset", "engram_mcp_wrapper.sh"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, verified_asset.returncode, verified_asset.stderr)
            self.assertTrue(json.loads(verified_asset.stdout)["verified"])
            listed = subprocess.run(
                [str(executable), "client", "list"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, listed.returncode, listed.stderr)
            self.assertEqual(1, json.loads(listed.stdout)["schema_version"])
            rendered = subprocess.run(
                [str(executable), "instruction", "render"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, rendered.returncode, rendered.stderr)
            self.assertIn(memory.INSTRUCTION_BEGIN, rendered.stdout)
            metadata = next((venv / "lib").rglob("naos_engram_memories-1.0.0.dist-info/METADATA"))
            self.assertIn("Name: naos-engram-memories", metadata.read_text(encoding="utf-8"))
            home = root / "home"
            config_home = home / ".config"
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin", "https://github.com/example-org/example-product.git"], check=True)
            installed_environment = {**environment, "HOME": str(home), "XDG_CONFIG_HOME": str(config_home)}
            if sys.platform == "win32":
                installed_environment["APPDATA"] = str(home / "AppData" / "Roaming")
            runtime = subprocess.run(
                [str(executable), "runtime", "install", "--yes", "--non-interactive"],
                env=installed_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, runtime.returncode, runtime.stderr)
            user_config = (
                home / "AppData" / "Roaming" / "naos-engram-memory"
                if sys.platform == "win32"
                else config_home / "naos-engram-memory"
            )
            self.assertTrue((user_config / "projects.json").is_file())
            self.assertFalse(
                (user_config / "mcp-host-observations.v1.json").exists(),
                "version-only evidence updates must not drift the managed runtime",
            )
            self.assertEqual([], json.loads((user_config / "projects.json").read_text(encoding="utf-8"))["projects"])
            installed_wrapper = (
                home / "AppData" / "Roaming" / "naos-engram-memory" / "bin" / "engram-mcp-wrapper.cmd"
                if sys.platform == "win32"
                else home / ".local" / "bin" / "engram-mcp-wrapper"
            )
            self.assertTrue(installed_wrapper.is_file())
            wrapper_text = (
                installed_wrapper.with_name("engram-mcp-wrapper.ps1").read_text(encoding="utf-8")
                if sys.platform == "win32"
                else installed_wrapper.read_text(encoding="utf-8")
            )
            self.assertIn(str(user_config), wrapper_text)
            self.assertNotIn("/.config/engram-memory", wrapper_text)
            self.assertNotIn(str(venv), str(user_config))
            registered = subprocess.run(
                [
                    str(executable), "project", "register", "--id", "example-product",
                    "--remote", "https://github.com/example-org/example-product.git",
                    "--yes", "--non-interactive",
                ],
                env=installed_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, registered.returncode, registered.stderr)
            configured = subprocess.run(
                [
                    str(executable), "client", "install", "--client", "cursor",
                    "--project", "example-product", "--workspace", str(workspace),
                    "--yes", "--non-interactive",
                ],
                env=installed_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, configured.returncode, configured.stderr)
            self.assertTrue((workspace / ".cursor" / "mcp.json").is_file())

    @unittest.skipIf(sys.platform == "win32", "Unix shell syntax is checked on Unix runners")
    def test_shell_scripts_parse(self) -> None:
        result = subprocess.run(
            ["bash", "-n", "scripts/bootstrap.sh", "scripts/setup.sh", "scripts/engram_mcp_wrapper.sh", "scripts/engram-memory"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_no_legacy_runtime_sync_or_latest_resolution(self) -> None:
        surfaces = [
            ROOT / "scripts" / "setup.sh",
            ROOT / "scripts" / "setup.ps1",
            ROOT / "instructions" / "engram-memory-strict.instructions.md",
            ROOT / "instructions" / "engram-memory-relaxed.instructions.md",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in surfaces)
        self.assertNotIn("releases/latest", combined)
        self.assertNotIn("sync --all", combined)
        self.assertNotIn("engram-push", combined)
        self.assertIn("maintenance-window", combined.lower())

    def test_wrapper_passes_validated_project_by_env_and_flag(self) -> None:
        wrapper = (ROOT / "scripts" / "engram_mcp_wrapper.sh").read_text(encoding="utf-8")
        windows_wrapper = (ROOT / "scripts" / "engram_mcp_wrapper.ps1").read_text(encoding="utf-8")
        self.assertIn("WORKSPACE_SIGNAL=claude_project_dir", wrapper)
        self.assertIn("WORKSPACE_SIGNAL=process_cwd", wrapper)
        self.assertIn("resolution_source=${RESOLUTION_SOURCE}", wrapper)
        self.assertIn("incoming_project_signal=${INCOMING_PROJECT_SIGNAL}", wrapper)
        self.assertIn("$workspaceSignal = if ($env:CLAUDE_PROJECT_DIR)", windows_wrapper)
        self.assertIn("resolution_source=$resolutionSource", windows_wrapper)
        self.assertIn("incoming_project_signal=$incomingProjectSignal", windows_wrapper)
        self.assertIn('/usr/bin/env -i', wrapper)
        self.assertIn('ENGRAM_PROJECT="$CANONICAL_PROJECT"', wrapper)
        self.assertIn('mcp --tools=agent --project="$CANONICAL_PROJECT"', wrapper)
        self.assertIn('--config-dir "$NAOS_ENGRAM_MEMORY_CONFIG_DIR"', wrapper)
        self.assertIn('--config-dir $ConfigDir', windows_wrapper)
        self.assertIn("GetEnvironmentVariables('Process')", windows_wrapper)
        self.assertIn("canonical-project-resolution-failed", wrapper)
        self.assertIn("naos-engram-memory", wrapper)
        self.assertIn("naos-engram-memory", windows_wrapper)
        self.assertIn("legacy toolkit config detected", wrapper)
        self.assertIn("legacy toolkit config detected", windows_wrapper)
        self.assertNotIn("Add-Content", windows_wrapper)
        self.assertNotIn(">> \"$NAOS_ENGRAM_MEMORY_LOG\"", wrapper)
        self.assertIn("Toolkit path overrides are not accepted", windows_wrapper)

    def test_runtime_refuses_unmigrated_legacy_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            legacy = memory.legacy_user_config_dir(home)
            legacy.mkdir(parents=True)
            marker = legacy / "projects.json"
            marker.write_text("legacy fixture\n", encoding="utf-8")
            with self.assertRaisesRegex(memory.EngramMemoryError, "was not read or merged"):
                memory.ensure_user_runtime(home=home)
            self.assertEqual("legacy fixture\n", marker.read_text(encoding="utf-8"))
            self.assertFalse((home / ".config" / "naos-engram-memory").exists())

    def test_runtime_refuses_legacy_environment_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with unittest.mock.patch.dict(os.environ, {"ENGRAM_MEMORY_CONFIG_DIR": str(Path(temporary) / "legacy")}, clear=False):
                with self.assertRaisesRegex(memory.EngramMemoryError, "legacy toolkit environment"):
                    memory.ensure_user_runtime(home=Path(temporary) / "home")

    @unittest.skipIf(sys.platform == "win32", "Unix wrapper legacy refusal is checked on Unix runners")
    def test_wrapper_refuses_legacy_directory_without_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            legacy = root / "config" / "engram-memory"
            legacy.mkdir(parents=True)
            marker = legacy / "projects.json"
            marker.write_text("must remain unread fixture\n", encoding="utf-8")
            environment = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(root / "config")}
            wrapper = root / "wrapper"
            wrapper.write_text(
                (ROOT / "scripts" / "engram_mcp_wrapper.sh").read_text(encoding="utf-8")
                .replace("__PYTHON_EXECUTABLE__", str(Path(sys.executable).resolve())),
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            result = subprocess.run(
                [str(wrapper)],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(64, result.returncode)
            self.assertIn("no files were read or merged", result.stderr)
            self.assertEqual("must remain unread fixture\n", marker.read_text(encoding="utf-8"))
            self.assertFalse((root / "config" / "naos-engram-memory").exists())

    @unittest.skipIf(sys.platform == "win32", "Unix wrapper integration is checked on Unix runners")
    def test_wrapper_uses_claude_project_dir_for_dynamic_resolution(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        project = registry["projects"][0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / "managed-config"
            repository = root / "repository"
            fake_engram = root / "engram"
            provider_call_log = root / "provider-calls.log"
            shutil.copytree(ROOT / "config", managed)
            shutil.copy2(ROOT / "tools" / "engram_memory.py", managed / "engram_memory.py")
            shutil.copy2(ROOT / "tools" / "provider.py", managed / "provider.py")
            shutil.copy2(SYNTHETIC_REGISTRY, managed / "projects.json")
            shutil.copy2(ROOT / "config" / "release-support.json", managed / "release-support.json")
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "remote", "add", "origin", project["remotes"][0]], check=True
            )
            fake_engram.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                "from pathlib import Path\n"
                "if len(sys.argv) > 1 and sys.argv[1] == 'version':\n"
                "    print('engram 1.20.0')\n"
                "elif len(sys.argv) > 1 and sys.argv[1] == 'doctor':\n"
                "    print('{}')\n"
                "elif len(sys.argv) > 1 and sys.argv[1] == 'mcp':\n"
                f"    Path({str(provider_call_log)!r}).write_text(' '.join(sys.argv[1:]) + '\\n')\n"
                "else:\n"
                "    raise SystemExit(2)\n",
                encoding="utf-8",
            )
            fake_engram.chmod(0o755)
            memory.provider_lifecycle.adopt_existing_provider(
                memory.load_json(ROOT / "config" / "release-support.json"),
                candidate=fake_engram, home=root / "home", config_dir=managed,
                yes=True, dry_run=False,
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(root / "home"),
                    "XDG_CONFIG_HOME": str(root / "host-controlled-xdg"),
                    "ENGRAM_BIN": str(fake_engram),
                    "CLAUDE_PROJECT_DIR": str(repository),
                }
            )
            wrapper = root / "engram-mcp-wrapper"
            wrapper.write_text(
                (ROOT / "scripts" / "engram_mcp_wrapper.sh").read_text(encoding="utf-8")
                .replace("__NAOS_ENGRAM_MEMORY_CONFIG_DIR__", str(managed))
                .replace("__PYTHON_EXECUTABLE__", str(Path(sys.executable).resolve()))
                .replace("__NAOS_ENGRAM_PROVIDER_BIN__", str(fake_engram)),
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            result = subprocess.run(
                [str(wrapper)],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
            self.assertIn(f"project={project['id']}", result.stderr)
            self.assertIn("workspace_signal=claude_project_dir", result.stderr)
            self.assertIn("resolution_source=approved_git_remote", result.stderr)
            self.assertIn("incoming_project_signal=absent", result.stderr)
            calls = provider_call_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual([f"mcp --tools=agent --project={project['id']}"], calls)

            process_cwd_environment = dict(environment)
            process_cwd_environment.pop("CLAUDE_PROJECT_DIR")
            process_cwd = subprocess.run(
                [str(wrapper)],
                cwd=repository,
                env=process_cwd_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(
                0,
                process_cwd.returncode,
                f"stdout:\n{process_cwd.stdout}\nstderr:\n{process_cwd.stderr}",
            )
            self.assertIn("workspace_signal=process_cwd", process_cwd.stderr)
            self.assertIn("resolution_source=approved_git_remote", process_cwd.stderr)
            self.assertIn("incoming_project_signal=absent", process_cwd.stderr)

    @unittest.skipIf(sys.platform == "win32", "Unix child environment isolation is checked on Unix runners")
    def test_wrapper_scrubs_arbitrary_client_credentials_before_provider_spawn(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        project = registry["projects"][0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            managed = root / "managed-config"
            repository = root / "repository"
            fake_engram = root / "engram"
            provider_environment_log = root / "provider-environment.json"
            shutil.copytree(ROOT / "config", managed)
            shutil.copy2(ROOT / "tools" / "engram_memory.py", managed / "engram_memory.py")
            shutil.copy2(ROOT / "tools" / "provider.py", managed / "provider.py")
            shutil.copy2(SYNTHETIC_REGISTRY, managed / "projects.json")
            shutil.copy2(ROOT / "config" / "release-support.json", managed / "release-support.json")
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "remote", "add", "origin", project["remotes"][0]],
                check=True,
            )
            fake_engram.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "if len(sys.argv) > 1 and sys.argv[1] == 'version':\n"
                "    print('engram 1.20.0')\n"
                "elif len(sys.argv) > 1 and sys.argv[1] == 'doctor':\n"
                "    print('{}')\n"
                "elif len(sys.argv) > 1 and sys.argv[1] == 'mcp':\n"
                f"    Path({str(provider_environment_log)!r}).write_text(json.dumps(dict(os.environ), sort_keys=True))\n"
                "else:\n"
                "    raise SystemExit(2)\n",
                encoding="utf-8",
            )
            fake_engram.chmod(0o755)
            memory.provider_lifecycle.adopt_existing_provider(
                memory.load_json(ROOT / "config" / "release-support.json"),
                candidate=fake_engram,
                home=home,
                config_dir=managed,
                yes=True,
                dry_run=False,
            )
            wrapper = root / "engram-mcp-wrapper"
            wrapper.write_text(
                (ROOT / "scripts" / "engram_mcp_wrapper.sh")
                .read_text(encoding="utf-8")
                .replace("__NAOS_ENGRAM_MEMORY_CONFIG_DIR__", str(managed))
                .replace("__PYTHON_EXECUTABLE__", str(Path(sys.executable).resolve()))
                .replace("__NAOS_ENGRAM_PROVIDER_BIN__", str(fake_engram)),
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            environment = {
                **os.environ,
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(root / "host-controlled-xdg"),
                "ENGRAM_BIN": str(fake_engram),
                "CLAUDE_PROJECT_DIR": str(repository),
                "OPENAI_API_KEY": "must-not-cross-wrapper",
                "ANTHROPIC_API_KEY": "must-not-cross-wrapper",
                "UNRELATED_CREDENTIAL_CANARY": "must-not-cross-wrapper",
            }
            result = subprocess.run(
                [str(wrapper)],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
            observed = json.loads(provider_environment_log.read_text(encoding="utf-8"))
            darwin_runtime_value = observed.pop("__CF_USER_TEXT_ENCODING", None)
            if sys.platform == "darwin" and darwin_runtime_value is not None:
                self.assertRegex(
                    darwin_runtime_value,
                    r"^0x[0-9A-Fa-f]+:(?:0x[0-9A-Fa-f]+|[0-9]+):(?:0x[0-9A-Fa-f]+|[0-9]+)$",
                )
            else:
                self.assertIsNone(darwin_runtime_value)
            self.assertEqual(
                {
                    "HOME": str(home),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "TMPDIR": "/tmp",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "NO_COLOR": "1",
                    "ENGRAM_PROJECT": project["id"],
                },
                observed,
            )

    @unittest.skipIf(sys.platform == "win32", "Unix bound provider state is checked on Unix runners")
    def test_wrapper_refuses_ambient_provider_state_when_bound_state_is_missing(self) -> None:
        registry = memory.load_json(SYNTHETIC_REGISTRY)
        project = registry["projects"][0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            bound = root / "bound-config"
            ambient_root = root / "ambient-xdg"
            ambient = ambient_root / memory.TOOLKIT_NAMESPACE
            repository = root / "repository"
            fake_engram = root / "engram"
            provider_canary = root / "provider-spawned"
            bound.mkdir()
            shutil.copy2(ROOT / "tools" / "engram_memory.py", bound / "engram_memory.py")
            shutil.copy2(ROOT / "tools" / "provider.py", bound / "provider.py")
            shutil.copy2(SYNTHETIC_REGISTRY, bound / "projects.json")
            shutil.copy2(ROOT / "config" / "release-support.json", bound / "release-support.json")
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            subprocess.run(
                ["git", "-C", str(repository), "remote", "add", "origin", project["remotes"][0]],
                check=True,
            )
            fake_engram.write_text(
                f"#!{sys.executable}\n"
                "import sys\n"
                "from pathlib import Path\n"
                "if len(sys.argv) > 1 and sys.argv[1] == 'version':\n"
                "    print('engram 1.20.0')\n"
                "elif len(sys.argv) > 1 and sys.argv[1] == 'doctor':\n"
                "    print('{}')\n"
                "elif len(sys.argv) > 1 and sys.argv[1] == 'mcp':\n"
                f"    Path({str(provider_canary)!r}).write_text('spawned')\n"
                "else:\n"
                "    raise SystemExit(2)\n",
                encoding="utf-8",
            )
            fake_engram.chmod(0o755)
            ambient.mkdir(parents=True)
            memory.provider_lifecycle.adopt_existing_provider(
                memory.load_json(ROOT / "config" / "release-support.json"),
                candidate=fake_engram,
                home=home,
                config_dir=ambient,
                yes=True,
                dry_run=False,
            )
            wrapper = root / "engram-mcp-wrapper"
            wrapper.write_text(
                (ROOT / "scripts" / "engram_mcp_wrapper.sh")
                .read_text(encoding="utf-8")
                .replace("__NAOS_ENGRAM_MEMORY_CONFIG_DIR__", str(bound))
                .replace("__PYTHON_EXECUTABLE__", str(Path(sys.executable).resolve()))
                .replace("__NAOS_ENGRAM_PROVIDER_BIN__", str(fake_engram)),
                encoding="utf-8",
            )
            wrapper.chmod(0o755)
            result = subprocess.run(
                [str(wrapper)],
                cwd=repository,
                env={
                    **os.environ,
                    "HOME": str(home),
                    "XDG_CONFIG_HOME": str(ambient_root),
                    "ENGRAM_BIN": str(fake_engram),
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(64, result.returncode)
            self.assertIn("provider integrity verification failed", result.stderr)
            self.assertFalse(provider_canary.exists())

    @unittest.skipIf(sys.platform == "win32", "Unix cloud environment refusal is checked on Unix runners")
    def test_wrapper_refuses_cloud_environment_before_provider_spawn(self) -> None:
        for variable in (
            "ENGRAM_CLOUD_AUTOSYNC", "ENGRAM_CLOUD_SERVER", "ENGRAM_CLOUD_TOKEN",
            "ENGRAM_REMOTE_URL", "ENGRAM_TOKEN",
            "ENGRAM_DATABASE_URL", "ENGRAM_JWT_SECRET",
        ):
            with self.subTest(variable=variable), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                provider_bin = root / "engram"
                canary = root / "provider-spawned"
                provider_bin.write_text('#!/bin/sh\ntouch "$PROVIDER_CANARY"\n', encoding="utf-8")
                provider_bin.chmod(0o755)
                wrapper = root / "wrapper"
                wrapper.write_text(
                    (ROOT / "scripts" / "engram_mcp_wrapper.sh").read_text(encoding="utf-8")
                    .replace("__NAOS_ENGRAM_MEMORY_CONFIG_DIR__", str(root / "config"))
                    .replace("__PYTHON_EXECUTABLE__", str(Path(sys.executable).resolve()))
                    .replace("__NAOS_ENGRAM_PROVIDER_BIN__", str(provider_bin)),
                    encoding="utf-8",
                )
                wrapper.chmod(0o755)
                environment = {
                    **os.environ, variable: "synthetic-value",
                    "HOME": str(root / "home"), "PROVIDER_CANARY": str(canary),
                }
                result = subprocess.run(
                    [str(wrapper)], cwd=root, env=environment, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                )
                self.assertEqual(64, result.returncode)
                self.assertIn("cloud/autosync", result.stderr.lower())
                self.assertFalse(canary.exists())

    @unittest.skipIf(sys.platform == "win32", "Unix installed-wrapper boundaries are checked on Unix runners")
    def test_installed_wrapper_refuses_symlinked_bound_paths_and_path_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            canary = outside / "executed"
            external_tool = outside / "external.py"
            external_tool.write_text(f"from pathlib import Path\nPath({str(canary)!r}).write_text('ran')\n", encoding="utf-8")
            secret_id = "external-registry-id-must-not-leak"
            external_registry = outside / "projects.json"
            external_registry.write_text(json.dumps({"schema_version": 1, "projects": [{"id": secret_id, "aliases": [], "remotes": []}]}), encoding="utf-8")

            for mode in ("parent", "tool", "registry"):
                with self.subTest(mode=mode):
                    bound = root / mode / "config"
                    bound.parent.mkdir(parents=True)
                    if mode == "parent":
                        bound.parent.rmdir()
                        bound.parent.symlink_to(outside, target_is_directory=True)
                    else:
                        bound.mkdir()
                        if mode == "tool":
                            (bound / "engram_memory.py").symlink_to(external_tool)
                            (bound / "projects.json").write_text('{"schema_version":1,"projects":[]}\n', encoding="utf-8")
                        else:
                            shutil.copy2(ROOT / "tools" / "engram_memory.py", bound / "engram_memory.py")
                            (bound / "projects.json").symlink_to(root / "missing-external-registry")
                    wrapper = root / f"wrapper-{mode}"
                    wrapper.write_text(
                        (ROOT / "scripts" / "engram_mcp_wrapper.sh").read_text(encoding="utf-8")
                        .replace("__NAOS_ENGRAM_MEMORY_CONFIG_DIR__", str(bound))
                        .replace("__PYTHON_EXECUTABLE__", str(Path(sys.executable).resolve())),
                        encoding="utf-8",
                    )
                    wrapper.chmod(0o755)
                    result = subprocess.run([str(wrapper)], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                    self.assertEqual(64, result.returncode)
                    self.assertIn("symbolic link", result.stderr)
                    self.assertNotIn(secret_id, result.stderr)
                    self.assertFalse(canary.exists())

            redirect = root / "redirect"
            redirect.mkdir()
            override_log = redirect / "log"
            environment = {**os.environ, "NAOS_ENGRAM_MEMORY_LOG": str(override_log)}
            result = subprocess.run([str(wrapper)], cwd=root, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            self.assertEqual(64, result.returncode)
            self.assertIn("path overrides", result.stderr)
            self.assertFalse(override_log.exists())

    @unittest.skipIf(sys.platform == "win32", "Unix setup integration is checked on Unix runners")
    def test_source_configure_delegates_and_refuses_symlinked_config_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel"
            sentinel.write_text("preserve\n", encoding="utf-8")
            config_dir = root / "config-link"
            config_dir.symlink_to(outside, target_is_directory=True)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "NAOS_ENGRAM_MEMORY_CONFIG_DIR": str(config_dir),
                    "NAOS_ENGRAM_MEMORY_BIN_DIR": str(bin_dir),
                }
            )
            result = subprocess.run(
                ["bash", "scripts/setup.sh", "--configure"],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(2, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
            self.assertIn("symbolic link", result.stderr)
            self.assertEqual("preserve\n", sentinel.read_text(encoding="utf-8"))
            self.assertEqual(["sentinel"], [path.name for path in outside.iterdir()])

    @unittest.skipIf(sys.platform == "win32", "Unix setup integration is checked on Unix runners")
    def test_source_configure_uses_hardened_runtime_installer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment.pop("NAOS_ENGRAM_MEMORY_CONFIG_DIR", None)
            environment.pop("NAOS_ENGRAM_MEMORY_BIN_DIR", None)
            environment.pop("XDG_CONFIG_HOME", None)
            result = subprocess.run(
                ["bash", "scripts/setup.sh", "--configure"],
                cwd=ROOT,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, result.returncode, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
            self.assertTrue((home / ".config" / memory.TOOLKIT_NAMESPACE / "projects.json").is_file())
            self.assertTrue(memory.user_wrapper_path(home).is_file())
            managed_tool = memory.user_config_dir(home) / "engram_memory.py"
            managed_tool.write_text(managed_tool.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")
            refused = subprocess.run(
                ["bash", "scripts/setup.sh", "--configure"], cwd=ROOT, env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertNotEqual(0, refused.returncode)
            self.assertIn("runtime repair", refused.stderr)

    @unittest.skipIf(sys.platform == "win32", "Unix source registry selection is checked on Unix runners")
    def test_source_inventory_and_render_use_registered_durable_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run([
                "git", "-C", str(workspace), "remote", "add", "origin",
                "https://github.com/example-org/example-product.git",
            ], check=True)
            environment = {**os.environ, "HOME": str(home)}
            environment.pop("XDG_CONFIG_HOME", None)
            configured = subprocess.run(
                ["bash", "scripts/setup.sh", "--configure"], cwd=ROOT, env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(0, configured.returncode, configured.stderr)
            shutil.copy2(SYNTHETIC_REGISTRY, memory.user_config_dir(home) / "projects.json")
            inventory = subprocess.run(
                ["bash", str(ROOT / "scripts" / "setup.sh"), "--inventory"], cwd=workspace,
                env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(0, inventory.returncode, inventory.stderr)
            self.assertEqual("example-product", json.loads(inventory.stdout)["current_repository"]["project"])
            rendered = subprocess.run(
                [
                    "bash", "scripts/setup.sh", "--render-client", "--client", "cursor",
                    "--project", "example-product",
                ], cwd=ROOT, env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(0, rendered.returncode, rendered.stderr)
            self.assertEqual("example-product", json.loads(rendered.stdout)["mcpServers"]["engram"]["env"]["ENGRAM_PROJECT"])
            codex_rendered = subprocess.run(
                [
                    "bash", "scripts/setup.sh", "--render-client", "--client", "codex",
                    "--project", "example-product", "--workspace", str(workspace),
                    "--wrapper", str(memory.user_wrapper_path(home)),
                ], cwd=ROOT, env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(0, codex_rendered.returncode, codex_rendered.stderr)
            self.assertIn(f'cwd = "{workspace.resolve()}"', codex_rendered.stdout)
            self.assertIn("required = true", codex_rendered.stdout)
            self.assertNotIn("ENGRAM_PROJECT", codex_rendered.stdout)
            self.assertFalse((workspace / ".codex").exists())

    @unittest.skipIf(sys.platform == "win32", "Unix source upgrade dispatch is checked on Unix runners")
    def test_source_upgrade_does_not_collapse_to_install_when_provider_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            environment = {**os.environ, "HOME": str(home), "NAOS_ENGRAM_MEMORY_BIN_DIR": str(home / "bin")}
            result = subprocess.run(
                ["bash", "scripts/setup.sh", "--upgrade", "--maintenance-window", "--yes", "--dry-run"],
                cwd=ROOT, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("not toolkit-owned", result.stderr)

    @unittest.skipIf(sys.platform == "win32", "Unix setup integration is checked on Unix runners")
    def test_source_setup_install_client_requires_consent_and_uses_managed_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            workspace = root / "workspace"
            home.mkdir()
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
            subprocess.run([
                "git", "-C", str(workspace), "remote", "add", "origin",
                "https://github.com/example-org/example-product.git",
            ], check=True)
            environment = os.environ.copy()
            environment["HOME"] = str(home)
            environment.pop("XDG_CONFIG_HOME", None)
            configured = subprocess.run(
                ["bash", "scripts/setup.sh", "--configure"], cwd=ROOT, env=environment,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(0, configured.returncode, configured.stderr)
            user_registry = memory.user_config_dir(home) / "projects.json"
            shutil.copy2(SYNTHETIC_REGISTRY, user_registry)
            base = [
                "bash", "scripts/setup.sh", "--install-client", "--client", "cursor",
                "--project", "example-product", "--workspace", str(workspace),
            ]
            preview = subprocess.run(
                [*base, "--dry-run"], cwd=ROOT, env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(0, preview.returncode, preview.stderr)
            self.assertFalse((workspace / ".cursor" / "mcp.json").exists())
            refused = subprocess.run(
                base, cwd=ROOT, env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(2, refused.returncode)
            self.assertIn("requires --yes", refused.stderr)
            installed = subprocess.run(
                [*base, "--yes"], cwd=ROOT, env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(0, installed.returncode, installed.stderr)
            self.assertTrue((workspace / ".cursor" / "mcp.json").is_file())

    @unittest.skipUnless(sys.platform == "darwin", "manifest upgrade path is tested on its supported Darwin host")
    def test_source_upgrade_refuses_symlinked_binary_or_database_paths_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            pgrep = fake_bin / "pgrep"
            pgrep.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            pgrep.chmod(0o755)
            outside = root / "outside"
            outside.mkdir()
            environment = os.environ.copy()
            environment.update({
                "HOME": str(home),
                "PATH": str(fake_bin) + os.pathsep + environment.get("PATH", ""),
            })
            linked_bin = root / "linked-bin"
            linked_bin.symlink_to(outside, target_is_directory=True)
            environment["NAOS_ENGRAM_MEMORY_BIN_DIR"] = str(linked_bin)
            result = subprocess.run(
                ["bash", "scripts/setup.sh", "--install-supported", "--maintenance-window", "--dry-run", "--yes"],
                cwd=ROOT, env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("symbolic link", result.stderr)
            self.assertEqual([], list(outside.iterdir()))

            safe_bin = root / "safe-bin"
            safe_bin.mkdir()
            database_target = outside / "database"
            database_target.write_text("synthetic\n", encoding="utf-8")
            database_link = home / ".engram" / "engram.db"
            database_link.parent.mkdir()
            database_link.symlink_to(database_target)
            environment["NAOS_ENGRAM_MEMORY_BIN_DIR"] = str(safe_bin)
            result = subprocess.run(
                ["bash", "scripts/setup.sh", "--install-supported", "--maintenance-window", "--dry-run", "--yes"],
                cwd=ROOT, env=environment, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("symbolic link", result.stderr)
            self.assertEqual("synthetic\n", database_target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
