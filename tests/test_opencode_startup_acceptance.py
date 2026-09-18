from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import run_opencode_startup_acceptance as startup


ROOT = Path(__file__).resolve().parents[1]


class OpenCodeStartupAcceptanceTests(unittest.TestCase):
    def valid_trace(self, home: Path) -> list[dict]:
        return [
            {
                "event": "spawn",
                "argv": ["mcp", "--tools=agent", "--project=example-product"],
                "environment": {
                    "HOME": str(home),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "TMPDIR": "/tmp",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "NO_COLOR": "1",
                    "ENGRAM_PROJECT": "example-product",
                },
            },
            {"event": "request", "method": "initialize"},
            {"event": "request", "method": "notifications/initialized"},
            {"event": "request", "method": "tools/list"},
            {"event": "tools_list_response", "tools": ["mem_current_project"]},
        ]

    def test_trace_requires_exact_startup_methods_argv_tool_and_environment(
        self,
    ) -> None:
        home = Path("/fixture/home")
        checks, credentials_forwarded = startup.evaluate_startup_trace(
            self.valid_trace(home),
            host_returncode=0,
            host_output="engram connected",
            expected_home=home,
        )
        self.assertFalse(credentials_forwarded)
        for key in (
            "host_exit_zero",
            "server_status_connected",
            "wrapper_argv_exact",
            "provider_environment_isolated",
            "expected_methods_observed",
            "mem_current_project_listed",
        ):
            self.assertTrue(checks[key], key)
        self.assertEqual([], checks["unexpected_environment_variables"])
        self.assertFalse(checks["tools_call_observed"])

        contaminated = self.valid_trace(home)
        contaminated[0]["environment"]["OPENAI_API_KEY"] = startup.CREDENTIAL_CANARIES[
            "OPENAI_API_KEY"
        ]
        contaminated_checks, contaminated_credentials = startup.evaluate_startup_trace(
            contaminated,
            host_returncode=0,
            host_output="engram connected",
            expected_home=home,
        )
        self.assertTrue(contaminated_credentials)
        self.assertFalse(contaminated_checks["provider_environment_isolated"])
        self.assertEqual(
            ["OPENAI_API_KEY"],
            contaminated_checks["unexpected_environment_variables"],
        )

        for darwin_text_encoding in ("0x1F5:0:2", "0x1F5:0x0:0x0"):
            with self.subTest(darwin_text_encoding=darwin_text_encoding):
                darwin_runtime = self.valid_trace(home)
                darwin_runtime[0]["environment"][
                    "__CF_USER_TEXT_ENCODING"
                ] = darwin_text_encoding
                darwin_checks, darwin_credentials = startup.evaluate_startup_trace(
                    darwin_runtime,
                    host_returncode=0,
                    host_output="engram connected",
                    expected_home=home,
                )
                self.assertFalse(darwin_credentials)
                self.assertTrue(darwin_checks["provider_environment_isolated"])
                self.assertEqual(
                    [], darwin_checks["unexpected_environment_variables"]
                )

        for malformed_darwin_text_encoding in (
            "0x1F5:0",
            "0x1F5:0:2:3",
            "0x1F5:zero:2",
            "1F5:0:2",
        ):
            with self.subTest(
                malformed_darwin_text_encoding=malformed_darwin_text_encoding
            ):
                malformed_darwin_runtime = self.valid_trace(home)
                malformed_darwin_runtime[0]["environment"][
                    "__CF_USER_TEXT_ENCODING"
                ] = malformed_darwin_text_encoding
                malformed_checks, malformed_credentials = (
                    startup.evaluate_startup_trace(
                        malformed_darwin_runtime,
                        host_returncode=0,
                        host_output="engram connected",
                        expected_home=home,
                    )
                )
                self.assertFalse(malformed_credentials)
                self.assertFalse(malformed_checks["provider_environment_isolated"])
                self.assertEqual(
                    ["__CF_USER_TEXT_ENCODING"],
                    malformed_checks["unexpected_environment_variables"],
                )

        unapproved = self.valid_trace(home)
        unapproved[0]["environment"]["UNAPPROVED_NON_SECRET"] = "fixture"
        unapproved_checks, unapproved_credentials = startup.evaluate_startup_trace(
            unapproved,
            host_returncode=0,
            host_output="engram connected",
            expected_home=home,
        )
        self.assertFalse(unapproved_credentials)
        self.assertFalse(unapproved_checks["provider_environment_isolated"])
        self.assertEqual(
            ["UNAPPROVED_NON_SECRET"],
            unapproved_checks["unexpected_environment_variables"],
        )

        missing_method = self.valid_trace(home)
        del missing_method[2]
        missing_checks, _ = startup.evaluate_startup_trace(
            missing_method,
            host_returncode=0,
            host_output="engram connected",
            expected_home=home,
        )
        self.assertFalse(missing_checks["expected_methods_observed"])

    def test_tools_call_is_observed_but_never_promoted_by_startup_scope(self) -> None:
        home = Path("/fixture/home")
        trace = self.valid_trace(home)
        trace.insert(-1, {"event": "request", "method": "tools/call"})
        checks, _ = startup.evaluate_startup_trace(
            trace,
            host_returncode=0,
            host_output="engram connected",
            expected_home=home,
        )
        self.assertTrue(checks["tools_call_observed"])
        self.assertFalse(checks["expected_methods_observed"])

    def test_pinned_host_rejects_wrong_name_hash_and_non_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrong_name = root / "opencode2"
            wrong_name.write_text("fixture", encoding="utf-8")
            wrong_name.chmod(0o755)
            with self.assertRaisesRegex(
                startup.StartupAcceptanceError, "named opencode"
            ):
                startup.resolve_pinned_host(wrong_name)
            host = root / "opencode"
            host.write_text("fixture", encoding="utf-8")
            host.chmod(0o755)
            with self.assertRaisesRegex(startup.StartupAcceptanceError, "SHA-256"):
                startup.resolve_pinned_host(host)
            with mock.patch.object(
                startup, "EXPECTED_BINARY_SHA256", startup.sha256_file(host)
            ):
                self.assertEqual(host.resolve(), startup.resolve_pinned_host(host))

    def test_homebrew_provenance_is_bound_to_official_tap_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            prefix = Path(temporary) / startup.EXPECTED_VERSION
            executable = prefix / "bin" / "opencode"
            formula = prefix / ".brew" / "opencode.rb"
            executable.parent.mkdir(parents=True)
            formula.parent.mkdir(parents=True)
            executable.write_text("fixture", encoding="utf-8")
            formula.write_text(
                "class Opencode < Formula\n"
                f'  version "{startup.EXPECTED_VERSION}"\n'
                "  on_macos do\n"
                "    if Hardware::CPU.arm?\n"
                f'      url "{startup.EXPECTED_RELEASE_ASSET_URL}"\n'
                f'      sha256 "{startup.EXPECTED_RELEASE_ASSET_SHA256}"\n'
                "    end\n"
                "  end\n"
                "end\n",
                encoding="utf-8",
            )
            receipt = {
                "source": {
                    "tap": startup.EXPECTED_HOMEBREW_TAP,
                    "tap_git_head": "a" * 40,
                    "versions": {"stable": startup.EXPECTED_VERSION},
                }
            }
            (prefix / "INSTALL_RECEIPT.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            observed = startup.homebrew_provenance(executable)
            self.assertEqual("homebrew", observed["kind"])
            self.assertEqual(startup.EXPECTED_HOMEBREW_TAP, observed["tap"])
            self.assertEqual(
                startup.EXPECTED_RELEASE_ASSET_URL, observed["release_asset_url"]
            )
            self.assertEqual(
                "installed_formula_darwin_arm64",
                observed["release_asset_binding"],
            )
            valid_formula = formula.read_text(encoding="utf-8")
            formula.write_text(
                valid_formula.replace(startup.EXPECTED_RELEASE_ASSET_SHA256, "0" * 64),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                startup.StartupAcceptanceError, "pinned Darwin ARM64"
            ):
                startup.homebrew_provenance(executable)
            formula.write_text(valid_formula, encoding="utf-8")
            receipt["source"]["tap"] = "other/tap"
            (prefix / "INSTALL_RECEIPT.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                startup.StartupAcceptanceError, "official Homebrew"
            ):
                startup.homebrew_provenance(executable)

    def test_receipt_schema_is_closed_and_preserves_startup_claim_ceiling(self) -> None:
        schema = json.loads(
            (
                ROOT / "config" / "schemas" / "mcp-host-startup-receipt.v1.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(False, schema["properties"]["runtime_support_claim"]["const"])
        self.assertEqual(
            "boolean",
            schema["properties"]["isolation"]["properties"]["credentials_forwarded"][
                "type"
            ],
        )
        self.assertEqual(4, len(schema["allOf"]))
        self.assertIn(
            "startup_validated_tool_call_unverified",
            schema["properties"]["disposition"]["enum"],
        )

    def test_receipt_semantics_reject_false_success_and_preserve_failure(self) -> None:
        home = Path("/fixture/home")
        checks, credentials_forwarded = startup.evaluate_startup_trace(
            self.valid_trace(home),
            host_returncode=0,
            host_output="engram connected",
            expected_home=home,
        )
        receipt = {
            "source": {"clean": True},
            "isolation": {"credentials_forwarded": credentials_forwarded},
            "checks": checks,
            "disposition": "startup_validated_tool_call_unverified",
        }
        startup.validate_receipt_semantics(receipt)

        receipt["checks"]["host_exit_zero"] = False
        with self.assertRaisesRegex(
            startup.StartupAcceptanceError, "not supported by its checks"
        ):
            startup.validate_receipt_semantics(receipt)

        receipt["disposition"] = "failed"
        startup.validate_receipt_semantics(receipt)

        receipt["checks"]["host_exit_zero"] = True
        with self.assertRaisesRegex(
            startup.StartupAcceptanceError, "wholly successful evidence"
        ):
            startup.validate_receipt_semantics(receipt)

        receipt["disposition"] = "startup_validated_tool_call_unverified"
        receipt["checks"]["methods_observed"] = []
        with self.assertRaisesRegex(
            startup.StartupAcceptanceError, "not supported by its checks"
        ):
            startup.validate_receipt_semantics(receipt)


if __name__ == "__main__":
    unittest.main()
