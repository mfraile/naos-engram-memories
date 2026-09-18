#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: validate_cross_os_interop.sh --linux-engram-asset FILE [options]

Options:
  --mac-engram FILE       macOS Engram binary (default: command -v engram)
  --linux-image IMAGE     Ubuntu AMD64 test image
  --report FILE           write a sanitized JSON report
  --keep-fixture          retain the disposable fixture for diagnosis
  --expect-concurrency MODE  blocked (current negative gate) or resolved

The validator uses synthetic data and local bare Git remotes only. It does not
enable, configure, or ship cross-machine synchronization.
EOF
}

mac_engram="${MAC_ENGRAM_BIN:-$(command -v engram 2>/dev/null || true)}"
linux_asset="${LINUX_ENGRAM_ASSET:-}"
linux_image="${LINUX_TEST_IMAGE:-naos-engram-test:ubuntu24.04-amd64-v1}"
report=""
expected_concurrency="blocked"
keep_fixture=false
while (($#)); do
  case "$1" in
    --mac-engram) mac_engram=$2; shift 2 ;;
    --linux-engram-asset) linux_asset=$2; shift 2 ;;
    --linux-image) linux_image=$2; shift 2 ;;
    --report) report=$2; shift 2 ;;
    --expect-concurrency) expected_concurrency=$2; shift 2 ;;
    --keep-fixture) keep_fixture=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$expected_concurrency" in
  blocked|resolved) ;;
  *) printf '%s\n' '--expect-concurrency must be blocked or resolved' >&2; exit 2 ;;
esac

test -n "$mac_engram" && test -x "$mac_engram" || {
  printf 'a macOS Engram binary is required\n' >&2
  exit 2
}
test -n "$linux_asset" && test -f "$linux_asset" || {
  printf '%s\n' '--linux-engram-asset must identify the approved Linux AMD64 archive' >&2
  exit 2
}
test "$(uname -s)" = Darwin && test "$(uname -m)" = arm64 || {
  printf 'this cross-OS lane requires a macOS ARM64 host\n' >&2
  exit 2
}
docker image inspect "$linux_image" >/dev/null
test "$(docker image inspect "$linux_image" --format '{{.Os}}/{{.Architecture}}')" = linux/amd64 || {
  printf 'the Linux test image must be linux/amd64\n' >&2
  exit 2
}

root=$(mktemp -d "${TMPDIR:-/tmp}/naos-engram-cross-os.XXXXXX")
if ! $keep_fixture; then
  trap 'rm -rf "$root"' EXIT
fi

product=example-product
platform=example-platform
mac_marker=mac-orchid-cross-os
linux_marker=ubuntu-cobalt-cross-os
concurrent_marker=mac-concurrent-cross-os
platform_marker=mac-platform-group-b
product_remote=$root/product.git
platform_remote=$root/platform.git
mac_product=$root/mac-product
mac_platform=$root/mac-platform
linux_product=$root/linux-product
linux_platform=$root/linux-platform
mac_data=$root/mac-data
linux_product_data=$root/linux-product-data
linux_platform_data=$root/linux-platform-data
linux_bin_dir=$root/linux-bin

seed_remote() {
  local remote=$1
  local branch=$2
  local seed=$root/seed-$branch
  git init --bare -q "$remote"
  git init -q "$seed"
  git -C "$seed" checkout -qb "$branch"
  git -C "$seed" -c user.name='Synthetic Fixture' -c user.email='fixture@example.invalid' \
    commit --allow-empty -qm seed
  git -C "$seed" remote add origin "$remote"
  git -C "$seed" push -q -u origin "$branch"
}

configure_clone() {
  local remote=$1
  local branch=$2
  local checkout=$3
  git clone -q --branch "$branch" "$remote" "$checkout"
  git -C "$checkout" config user.name 'Synthetic Fixture'
  git -C "$checkout" config user.email fixture@example.invalid
}

seed_remote "$product_remote" product-memory-data
seed_remote "$platform_remote" platform-memory-data
configure_clone "$product_remote" product-memory-data "$mac_product"
configure_clone "$platform_remote" platform-memory-data "$mac_platform"

ENGRAM_DATA_DIR=$mac_data "$mac_engram" save 'Synthetic macOS product handoff' "$mac_marker" \
  --type discovery --project "$product" >/dev/null
ENGRAM_DATA_DIR=$mac_data "$mac_engram" save 'Synthetic macOS group-B handoff' "$platform_marker" \
  --type discovery --project "$platform" >/dev/null
(cd "$mac_product" && ENGRAM_DATA_DIR=$mac_data "$mac_engram" sync --project "$product" >/dev/null)
git -C "$mac_product" add -f .engram
git -C "$mac_product" commit -qm 'synthetic macOS product delta'
git -C "$mac_product" push -q origin product-memory-data
(cd "$mac_platform" && ENGRAM_DATA_DIR=$mac_data "$mac_engram" sync --project "$platform" >/dev/null)
git -C "$mac_platform" add -f .engram
git -C "$mac_platform" commit -qm 'synthetic macOS platform delta'
git -C "$mac_platform" push -q origin platform-memory-data

configure_clone "$product_remote" product-memory-data "$linux_product"
configure_clone "$platform_remote" platform-memory-data "$linux_platform"
mkdir -p "$linux_bin_dir"
tar -xzf "$linux_asset" -C "$linux_bin_dir"
linux_engram=$(find "$linux_bin_dir" -type f -name engram -perm -u+x | head -1)
test -n "$linux_engram"
linux_rel=${linux_engram#$root}

docker run --rm --network none --platform linux/amd64 \
  -e LINUX_ENGRAM="$linux_rel" \
  -e PRODUCT="$product" -e PLATFORM="$platform" \
  -e MAC_MARKER="$mac_marker" -e LINUX_MARKER="$linux_marker" \
  -e PLATFORM_MARKER="$platform_marker" \
  -v "$root:/work" "$linux_image" bash -lc '
    set -euo pipefail
    chmod +x "/work$LINUX_ENGRAM"
    for repo in linux-product linux-platform; do
      git config --global --add safe.directory "/work/$repo"
      git -C "/work/$repo" config user.name "Synthetic Fixture"
      git -C "/work/$repo" config user.email fixture@example.invalid
    done
    git -C /work/linux-product remote set-url origin /work/product.git
    git -C /work/linux-platform remote set-url origin /work/platform.git

    export ENGRAM_DATA_DIR=/work/linux-product-data
    cd /work/linux-product
    "/work$LINUX_ENGRAM" sync --import >/dev/null
    product_result=$("/work$LINUX_ENGRAM" search "$MAC_MARKER" --project "$PRODUCT")
    printf "%s\n" "$product_result" | grep -E "^Found [1-9][0-9]* memories?:" >/dev/null
    printf "%s\n" "$product_result" | grep -F "$MAC_MARKER" >/dev/null
    denied_result=$("/work$LINUX_ENGRAM" search "$PLATFORM_MARKER" --project "$PLATFORM")
    printf "%s\n" "$denied_result" | grep -F "No memories found for:" >/dev/null
    "/work$LINUX_ENGRAM" save "Synthetic Ubuntu product handoff" "$LINUX_MARKER" \
      --type discovery --project "$PRODUCT" >/dev/null
    "/work$LINUX_ENGRAM" sync --project "$PRODUCT" >/dev/null
    git add -f .engram
    git commit -qm "synthetic Ubuntu pending delta"

    export ENGRAM_DATA_DIR=/work/linux-platform-data
    cd /work/linux-platform
    "/work$LINUX_ENGRAM" sync --import >/dev/null
    platform_result=$("/work$LINUX_ENGRAM" search "$PLATFORM_MARKER" --project "$PLATFORM")
    printf "%s\n" "$platform_result" | grep -E "^Found [1-9][0-9]* memories?:" >/dev/null
    printf "%s\n" "$platform_result" | grep -F "$PLATFORM_MARKER" >/dev/null
    denied_product=$("/work$LINUX_ENGRAM" search "$MAC_MARKER" --project "$PRODUCT")
    printf "%s\n" "$denied_product" | grep -F "No memories found for:" >/dev/null
  '

# Create a real concurrent macOS delta after Ubuntu committed from the previous head.
ENGRAM_DATA_DIR=$mac_data "$mac_engram" save 'Synthetic concurrent macOS delta' "$concurrent_marker" \
  --type discovery --project "$product" >/dev/null
(cd "$mac_product" && ENGRAM_DATA_DIR=$mac_data "$mac_engram" sync --project "$product" >/dev/null)
git -C "$mac_product" add -f .engram
git -C "$mac_product" commit -qm 'synthetic concurrent macOS delta'
git -C "$mac_product" push -q origin product-memory-data

docker run --rm --network none --platform linux/amd64 \
  -e LINUX_ENGRAM="$linux_rel" -e PRODUCT="$product" \
  -v "$root:/work" "$linux_image" bash -lc '
    set -euo pipefail
    chmod +x "/work$LINUX_ENGRAM"
    git config --global --add safe.directory /work/linux-product
    git -C /work/linux-product remote set-url origin /work/product.git
    cd /work/linux-product
    if git push -q origin product-memory-data; then
      printf "expected the first concurrent push to be rejected\n" >&2
      exit 73
    fi
    printf "rejected\n" > /work/concurrent-push-result
    set +e
    git pull -q --rebase origin product-memory-data > /work/reconcile-output 2>&1
    reconcile_code=$?
    set -e
    printf "%s\n" "$reconcile_code" > /work/reconcile-code
    if test "$reconcile_code" -eq 0; then
      ENGRAM_DATA_DIR=/work/linux-product-data "/work$LINUX_ENGRAM" sync --import >/dev/null
      git push -q origin product-memory-data
    else
      git rebase --abort >/dev/null 2>&1 || true
    fi
  '
test "$(cat "$root/concurrent-push-result")" = rejected
reconcile_code=$(cat "$root/reconcile-code")
if test "$expected_concurrency" = blocked; then
  test "$reconcile_code" -ne 0 || {
    printf 'expected the current manifest reconciliation defect, but rebase succeeded\n' >&2
    exit 74
  }
  grep -F '.engram/manifest.json' "$root/reconcile-output" >/dev/null
  concurrency_disposition=redesign_required
else
  test "$reconcile_code" -eq 0 || {
    printf 'concurrent reconciliation remains blocked; see the retained negative evidence\n' >&2
    exit 75
  }
  concurrency_disposition=passed
fi

if test "$concurrency_disposition" = passed; then
  git -C "$mac_product" pull -q --rebase origin product-memory-data
  (cd "$mac_product" && ENGRAM_DATA_DIR=$mac_data "$mac_engram" sync --import >/dev/null)
  linux_result=$(ENGRAM_DATA_DIR=$mac_data "$mac_engram" search "$linux_marker" --project "$product")
  printf '%s\n' "$linux_result" | grep -E '^Found 1 memor(y|ies):' >/dev/null
  printf '%s\n' "$linux_result" | grep -F "$linux_marker" >/dev/null

  # Repeated import and no-new-data export must be idempotent/no-op.
  (cd "$mac_product" && ENGRAM_DATA_DIR=$mac_data "$mac_engram" sync --import >/dev/null)
  (cd "$mac_product" && ENGRAM_DATA_DIR=$mac_data "$mac_engram" sync --project "$product" >/dev/null)
  test -z "$(git -C "$mac_product" status --porcelain --untracked-files=all)"
fi

mac_version=$("$mac_engram" version | tr -d '\r')
linux_version=$(docker run --rm --network none --platform linux/amd64 -e LINUX_ENGRAM="$linux_rel" \
  -v "$root:/work" "$linux_image" bash -lc 'chmod +x "/work$LINUX_ENGRAM"; "/work$LINUX_ENGRAM" version' | tr -d '\r')
image_id=$(docker image inspect "$linux_image" --format '{{.Id}}')
if test -n "$report"; then
  REPORT=$report MAC_VERSION=$mac_version LINUX_VERSION=$linux_version IMAGE_ID=$image_id \
    CONCURRENCY_DISPOSITION=$concurrency_disposition \
    python3 - <<'PY'
import json, os
from datetime import datetime, timezone
from pathlib import Path
report = {
    "schema_version": 1,
    "executed_at": datetime.now(timezone.utc).isoformat(),
    "synthetic_only": True,
    "shipping_feature_enabled": False,
    "platforms": ["darwin_arm64", "ubuntu_24.04_amd64_container"],
    "engram_versions": {"darwin_arm64": os.environ["MAC_VERSION"], "linux_amd64": os.environ["LINUX_VERSION"]},
    "linux_image_id": os.environ["IMAGE_ID"],
    "checks": {
        "mac_to_ubuntu_handoff": "passed",
        "ubuntu_pending_handoff_created": "passed",
        "separate_membership_stores": "passed",
        "project_isolation": "passed",
        "concurrent_push_rejected": "passed",
        "bounded_rebase_and_retry": "passed" if os.environ["CONCURRENCY_DISPOSITION"] == "passed" else "failed_manifest_conflict",
        "canonical_project_buckets": "passed",
        "repeat_import_idempotent": "passed" if os.environ["CONCURRENCY_DISPOSITION"] == "passed" else "not_reached",
        "no_change_export_noop": "passed" if os.environ["CONCURRENCY_DISPOSITION"] == "passed" else "not_reached",
    },
    "limitations": [
        "This is a non-shipping regression for the deferred personal/trusted-replica design.",
        "A private Git replica is durable and revocation cannot retract prior clones or imported data.",
        "The Ubuntu lane validates CLI/provider behavior, not native Linux GUI extensions.",
    ],
    "disposition": "passed" if os.environ["CONCURRENCY_DISPOSITION"] == "passed" else "redesign_required_before_transport_promotion",
}
Path(os.environ["REPORT"]).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
fi
if test "$concurrency_disposition" = passed; then
  printf 'cross-OS interoperability: passed (synthetic, non-shipping)\n'
else
  printf 'cross-OS interoperability: local/project/group checks passed; concurrent transport redesign required\n'
fi
