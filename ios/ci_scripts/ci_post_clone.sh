#!/bin/bash
# Xcode Cloud build gate.
#
# Approach: tag the last successfully-built commit as `last-ios-build`. On
# every clone, diff `last-ios-build..HEAD` for changes under `ios/` or
# `shared/`. If iOS changed → allow build (exit 0). If not → skip the build
# (exit 1) so backend-only pushes don't burn ASC build slots.
#
# The tag is updated by ci_post_xcodebuild.sh after a successful build —
# requires a GITHUB_PUSH_TOKEN environment secret in the Xcode Cloud
# workflow with `contents: write` permission.
#
# Bootstrap: if the tag doesn't exist (first run, or fetch failed), the
# script allows the build to proceed. Better to over-build once than miss
# a real iOS change.

set -e

echo "=== iOS-build gate (post_clone) ==="

# Fetch tags + recent history so we can resolve `last-ios-build` and
# do a meaningful diff. Xcode Cloud's clone is shallow by default.
echo "Fetching tags + recent history..."
git fetch --tags --depth=100 2>/dev/null || true

# Try to resolve the marker tag.
LAST_BUILD=$(git rev-parse --verify --quiet last-ios-build 2>/dev/null || true)

if [ -z "$LAST_BUILD" ]; then
    echo "No last-ios-build tag found — bootstrap mode, allowing this build."
    echo "(After this build succeeds, ci_post_xcodebuild.sh will set the tag.)"
    exit 0
fi

echo "Last successful iOS build was at: $LAST_BUILD"
echo "Current HEAD: $(git rev-parse HEAD)"

# Diff from last-build commit to current HEAD.
CHANGED=$(git diff --name-only "$LAST_BUILD" HEAD 2>/dev/null || echo "DIFF_FAIL")

if [ "$CHANGED" = "DIFF_FAIL" ]; then
    echo "Diff failed — likely shallow clone didn't include the tag's commit."
    echo "Allowing build (safer than skipping a possibly-real iOS change)."
    exit 0
fi

if [ -z "$CHANGED" ]; then
    echo "No changes since last build — skipping."
    exit 1
fi

# Filter for iOS-relevant paths.
IOS_CHANGED=$(echo "$CHANGED" | grep -E "^ios/|^shared/" || true)

if [ -z "$IOS_CHANGED" ]; then
    echo "No iOS files changed since last build — skipping."
    echo "(All changed files were backend-only.)"
    exit 1
fi

echo "iOS files changed since last build:"
echo "$IOS_CHANGED"
echo ""
echo "Allowing build."
exit 0
