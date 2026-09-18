# Public Release Procedure

This repository is the fresh-history public-release staging repository. Do not
make it public until every check below passes and a human approves the exact
commit and GitHub visibility change. Do not import a branch, tag, archive, or
Git object from an older private memory repository.

1. Create a separate temporary staging directory outside this repository.
2. Copy only reviewed source, documentation, synthetic fixtures, and license/
   attribution files. Do not copy `.engram`, databases, backups, real manifests,
   machine configs, logs, personal paths, or existing Git metadata.
3. Initialize a truly new Git repository with one unsigned, parentless commit
   using the allowlisted synthetic `Fixture <fixture@example.invalid>` author
   and committer identity. Retain only the intended branch ref, no tags, and
   no unreachable/loose prior objects; do not publish this development branch
   or retain its author history.
4. Run `python3 tools/sanitize_public_tree.py STAGING --require-fresh-git --forbidden-term PRIVATE_ORGANIZATION_TERM` for every known private identifier, plus a separate reviewed secret/PII scan appropriate to the publication system. The fresh-history mode requires a completely clean worktree and scans exact `HEAD` blob content and modes; symlinks, gitlinks/submodules, `.gitmodules`, unexpected modes, and unreachable objects are blockers.
5. Before uploading, check that the exact `naos-engram-memories` distribution name and release version are available in the chosen package registry. If either is unavailable, abort this release; do not choose a substitute name implicitly.
6. Publish each bootstrap as a versioned release asset and publish its SHA-256
   from `config/bootstrap-assets.v1.json` through a checksum-first acquisition
   snippet. A download command without an independently visible exact version
   and checksum is a release blocker. If the selected profile or an auditor
   requires a detached signature, verify it through the chosen external
   signing system before making that additional claim; signature absence is
   not a baseline blocker. This source branch does not publish or upload any
   asset.
7. Review every staged file manually, confirm third-party license and NOTICE
   requirements for any copied/derived material, then obtain publication
   approval. Engram upstream `main` was observed under MIT with copyright 2026
   Alan Buscaglia on 2026-08-13, but publication must bind license and
   provenance to the exact supported tag and downloaded asset rather than
   relying on that moving-branch observation. The toolkit does not vendor
   Engram source in the current package.
8. Create and publish the new public repository only after the review passes.

Installed-runtime evidence records three separate identities: the immutable Git
commit, its exact Git tree object, and a deterministic package-input SHA-256.
The package-input hash includes Git-tracked and non-ignored untracked source
inputs. It excludes generated `docs/evidence/**`, Git metadata, build/dist
products, bytecode caches, egg-info directories, and other Git-ignored local
files; the report lists those exclusions. A generated report may be written
under `docs/evidence/` or outside the source tree, and the validator recomputes
the identity after writing it. Final evidence is valid only when rerun against
the clean, immutable final commit/tree and the reported self-check succeeds. A
report from a dirty development checkout is diagnostic evidence, not
final-release attestation.

This procedure checks obvious leakage patterns; it is not proof that the tree
contains no sensitive information. Manual review remains mandatory.
