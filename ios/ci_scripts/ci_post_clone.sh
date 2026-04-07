#!/bin/bash
# Xcode Cloud: skip build if no iOS files changed in the latest commit.
# This prevents backend-only pushes from burning App Store Connect build quota.

set -e

echo "Checking if iOS files changed..."

# Get files changed in the latest commit
CHANGED=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || echo "")

# If git diff fails (first commit, shallow clone, etc.), allow the build
if [ -z "$CHANGED" ]; then
    echo "Could not determine changed files — allowing build."
    exit 0
fi

# Check if any iOS-related files changed
IOS_CHANGED=$(echo "$CHANGED" | grep -E "^ios/|^shared/" || true)

if [ -z "$IOS_CHANGED" ]; then
    echo "No iOS files changed — skipping build."
    echo "Changed files:"
    echo "$CHANGED"
    exit 1  # Non-zero exit cancels the Xcode Cloud build
fi

echo "iOS files changed — proceeding with build:"
echo "$IOS_CHANGED"
