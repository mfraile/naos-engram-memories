#!/usr/bin/env python3
"""Fail closed when a proposed public toolkit tree contains obvious private data."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


FORBIDDEN_PATH_PARTS = {".engram"}
FORBIDDEN_FILENAMES = {"engram.db", "engram.db-wal", "engram.db-shm", "manifest.json"}
IGNORED_PATH_PARTS = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
MAX_PUBLIC_FILE_BYTES = 2_000_000
SQLITE_HEADER = b"SQLite format 3\x00"
CONTENT_PATTERNS = {
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "GitHub fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private-key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "local hostname": re.compile(r"\b[A-Za-z0-9-]+\.local\b", re.IGNORECASE),
}
FRESH_AUTHOR_NAME = "Fixture"
FRESH_AUTHOR_EMAIL = "fixture@example.invalid"


def scan_file_bytes(relative: Path, raw: bytes, forbidden_terms: tuple[str, ...]) -> list[str]:
    failures: list[str] = []
    folded_parts = tuple(part.casefold() for part in relative.parts)
    if any(part in FORBIDDEN_PATH_PARTS for part in folded_parts):
        return [f"forbidden path component: {relative}"]
    if relative.name.casefold() in FORBIDDEN_FILENAMES:
        return [f"forbidden runtime-memory filename: {relative}"]
    folded_path = relative.as_posix().casefold()
    for term in forbidden_terms:
        if term and term.casefold() in folded_path:
            failures.append(f"forbidden publication term ({term}) detected in path: {relative}")
    if len(raw) > MAX_PUBLIC_FILE_BYTES:
        return [f"unexpected file larger than 2MB: {relative}"]
    if raw.startswith(SQLITE_HEADER):
        return [f"SQLite database content detected in: {relative}"]
    if b"\x00" in raw:
        return [f"unexpected binary file: {relative}"]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [f"unexpected binary file: {relative}"]
    private_terms = {
        f"forbidden publication term ({term})": re.compile(re.escape(term), re.IGNORECASE)
        for term in forbidden_terms
        if term
    }
    for description, pattern in {**CONTENT_PATTERNS, **private_terms}.items():
        if pattern.search(text):
            failures.append(f"{description} detected in: {relative}")
    return failures


def scan_committed_tree(root: Path, forbidden_terms: tuple[str, ...]) -> list[str]:
    """Inspect exact HEAD blobs and modes, independent of the working tree."""
    failures: list[str] = []
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-rz", "-r", "HEAD"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    )
    if listing.returncode != 0:
        return ["public staging commit tree could not be inspected"]
    for record in listing.stdout.split(b"\x00"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode_bytes, kind_bytes, object_id_bytes = metadata.split(b" ", 2)
        try:
            relative = Path(raw_name.decode("utf-8"))
        except UnicodeDecodeError:
            failures.append("public staging tree contains a non-UTF-8 path")
            continue
        mode = mode_bytes.decode("ascii", errors="replace")
        kind = kind_bytes.decode("ascii", errors="replace")
        if relative.name.casefold() == ".gitmodules":
            failures.append("public staging tree must not contain .gitmodules")
        if mode == "160000" or kind == "commit":
            failures.append(f"gitlink/submodule is not allowed in public staging: {relative}")
            continue
        if mode == "120000":
            failures.append(f"symbolic link is not allowed in public staging: {relative}")
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            failures.append(f"unexpected Git tree mode {mode} for: {relative}")
            continue
        blob = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", object_id_bytes.decode("ascii")],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        if blob.returncode != 0:
            failures.append(f"committed blob could not be read: {relative}")
            continue
        failures.extend(scan_file_bytes(relative, blob.stdout, forbidden_terms))
    return failures


def scan(root: Path, require_fresh_git: bool, forbidden_terms: tuple[str, ...] = ()) -> list[str]:
    failures: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            failures.append(f"symbolic link is not allowed in public staging: {relative}")
            continue
        if any(part.casefold() in IGNORED_PATH_PARTS for part in relative.parts) or path.suffix.casefold() in IGNORED_SUFFIXES:
            continue
        if not path.is_file():
            continue
        failures.extend(scan_file_bytes(relative, path.read_bytes(), forbidden_terms))
    if require_fresh_git:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        if status.returncode != 0 or status.stdout:
            failures.append("public staging repository must have a completely clean tracked, staged, and untracked worktree")
        result = subprocess.run(
            ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0 or result.stdout.strip() != "1":
            failures.append("public staging tree must have exactly one fresh Git commit")
        metadata = subprocess.run(
            ["git", "-C", str(root), "show", "-s", "--format=%P%n%an%n%ae%n%cn%n%ce%n%G?", "HEAD"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        fields = metadata.stdout.splitlines() if metadata.returncode == 0 else []
        if len(fields) != 6 or fields[0] != "":
            failures.append("public staging commit must be parentless")
        elif fields[1:5] != [FRESH_AUTHOR_NAME, FRESH_AUTHOR_EMAIL, FRESH_AUTHOR_NAME, FRESH_AUTHOR_EMAIL]:
            failures.append("public staging commit must use the allowlisted synthetic author and committer")
        elif fields[5] != "N":
            failures.append("public staging commit must be unsigned")
        message = subprocess.run(
            ["git", "-C", str(root), "show", "-s", "--format=%B", "HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        if message.returncode != 0:
            failures.append("public staging commit message could not be inspected")
        else:
            failures.extend(scan_file_bytes(Path("<commit-message>"), message.stdout, forbidden_terms))
        refs = subprocess.run(
            ["git", "-C", str(root), "for-each-ref", "--format=%(refname)"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        ref_names = [line for line in refs.stdout.splitlines() if line]
        head_ref = subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "-q", "HEAD"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        ).stdout.strip()
        if not head_ref or ref_names != [head_ref]:
            failures.append("public staging repository must contain only its intended branch ref and no tags")
        failures.extend(scan_file_bytes(Path("<branch-ref>") / head_ref, b"", forbidden_terms))
        fsck = subprocess.run(
            ["git", "-C", str(root), "fsck", "--unreachable", "--no-reflogs"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
        )
        if fsck.returncode != 0 or fsck.stdout.strip():
            failures.append("public staging repository contains unreachable or dangling objects")
        failures.extend(scan_committed_tree(root, forbidden_terms))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--require-fresh-git", action="store_true")
    parser.add_argument("--forbidden-term", action="append", default=[], help="case-insensitive private term that must not appear in staged content")
    args = parser.parse_args()
    failures = scan(args.root.resolve(), args.require_fresh_git, tuple(args.forbidden_term))
    if failures:
        print("PUBLIC SANITIZATION FAILED", file=sys.stderr)
        print("\n".join(f"- {failure}" for failure in failures), file=sys.stderr)
        return 1
    print("PUBLIC SANITIZATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
