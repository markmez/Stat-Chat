#!/bin/bash
# Xcode Cloud post-build hook.
#
# After a successful build, advance the `last-ios-build` tag to HEAD and push
# it to origin. The next push's ci_post_clone.sh will diff against this tag
# to decide whether iOS files have changed since the last successful build.
#
# Requires GITHUB_PUSH_TOKEN to be set as an environment secret in the
# Xcode Cloud workflow (https://developer.apple.com/documentation/xcode/configuring-environment-variables-and-secrets-in-xcode-cloud).
# Token needs `contents: write` permission on the repo.
#
# Failures here are non-fatal: if the tag can't be advanced, the worst
# case is the next post_clone gate sees a stale starting point and
# over-builds — never under-builds.

set -e

echo "=== iOS-build gate (post_xcodebuild) ==="

# Only advance the tag on successful build. CI_XCODEBUILD_EXIT_CODE is set
# by Xcode Cloud (see Apple's CI docs for the env var contract).
if [ "${CI_XCODEBUILD_EXIT_CODE:-0}" != "0" ]; then
    echo "Build did not succeed (exit=$CI_XCODEBUILD_EXIT_CODE) — leaving tag untouched."
    exit 0
fi

if [ -z "${GITHUB_PUSH_TOKEN:-}" ]; then
    echo "GITHUB_PUSH_TOKEN not set — can't push tag back to origin."
    echo "Set it as an Environment Variable (Secret) in the workflow."
    exit 0  # non-fatal — gate falls back to bootstrap mode next run
fi

CI_COMMIT_SHA="${CI_COMMIT:-$(git rev-parse HEAD)}"
echo "Advancing last-ios-build → $CI_COMMIT_SHA"

# Configure a lightweight identity (required by some git operations even
# for tag pushes — Xcode Cloud's CI user has none by default).
git config user.email "xcode-cloud@secondsignalapps.com"
git config user.name  "Xcode Cloud"

# Force-update the tag locally to the just-built commit.
git tag -f last-ios-build "$CI_COMMIT_SHA"

# Push the tag using the token. Inject into the existing remote URL so we
# don't need to know the exact GitHub URL format here.
REMOTE_URL=$(git remote get-url origin)
case "$REMOTE_URL" in
    https://github.com/*)
        AUTH_URL="${REMOTE_URL/https:\/\//https://x-access-token:${GITHUB_PUSH_TOKEN}@}"
        ;;
    *)
        echo "Unexpected remote URL format: $REMOTE_URL — can't inject token."
        exit 0
        ;;
esac

git push -f "$AUTH_URL" "refs/tags/last-ios-build" >/dev/null 2>&1 && \
    echo "✓ Pushed last-ios-build → $CI_COMMIT_SHA" || \
    echo "⚠ Tag push failed — gate will use prior tag value next run."

exit 0
