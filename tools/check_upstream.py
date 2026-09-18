#!/usr/bin/env python3
"""Report the newest upstream Engram release without changing local policy."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "config" / "release-support.json"
DEFAULT_API = "https://api.github.com/repos/Gentleman-Programming/engram/releases/latest"


def assess(manifest: dict[str, Any], latest_tag: str) -> dict[str, Any]:
    latest_version = latest_tag.removeprefix("v")
    known = next((item for item in manifest.get("releases", []) if item.get("version") == latest_version), None)
    return {
        "latest_version": latest_version,
        "known_to_manifest": known is not None,
        "manifest_status": known.get("status") if known else "unreviewed",
        "action": "review_candidate"
        if known is None or known.get("status") == "unreviewed"
        else "no_policy_change",
    }


def latest_tag(api_url: str, timeout: int) -> str:
    request = urllib.request.Request(api_url, headers={"Accept": "application/vnd.github+json", "User-Agent": "engram-memory-toolkit-release-check"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310: URL is explicit CLI input for testability.
        payload = json.load(response)
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("upstream release response did not contain a semver tag_name")
    return tag


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--api-url", default=DEFAULT_API)
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    print(json.dumps(assess(manifest, latest_tag(args.api_url, args.timeout)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
