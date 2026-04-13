#!/bin/bash
# Xcode Cloud: skip build if no iOS files changed in the latest commit.
# This prevents backend-only pushes from burning App Store Connect build quota.

set -e

echo "Checking if iOS files changed..."

# Try multiple approaches for shallow clones
CHANGED=""

# Approach 1: git diff HEAD~1
CHANGED=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || echo "")

# Approach 2: if that failed, try fetching more history
if [ -z "$CHANGED" ]; then
    echo "HEAD~1 failed, fetching more history..."
    git fetch --deepen=2 2>/dev/null || true
    CHANGED=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || echo "")
fi

# Approach 3: use git log to get changed files
if [ -z "$CHANGED" ]; then
    echo "Trying git log..."
    CHANGED=$(git log --name-only --pretty=format: -1 HEAD 2>/dev/null || echo "")
fi

# If we still can't determine, allow the build
if [ -z "$CHANGED" ]; then
    echo "Could not determine changed files — allowing build."
    exit 0
fi

echo "Changed files:"
echo "$CHANGED"

# Check if any iOS-related files changed
IOS_CHANGED=$(echo "$CHANGED" | grep -E "^ios/|^shared/" || true)

if [ -z "$IOS_CHANGED" ]; then
    echo "No iOS files changed — skipping build."
    exit 1  # Non-zero exit cancels the Xcode Cloud build
fi

echo "iOS files changed — proceeding with build:"
echo "$IOS_CHANGED"
