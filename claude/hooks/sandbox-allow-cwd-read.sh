#!/usr/bin/env bash
# SessionStart + ConfigChange hook: ensure the project directory (".") is
# readable inside the sandbox by adding it to .sandbox.filesystem.allowRead in
# the project's settings.local.json (gitignored, per-machine).
#
# Only touches settings.local.json when the project already has a .claude/ dir —
# it never creates one. This avoids planting a stray .claude/ (and settings) in a
# subdirectory when Claude is launched from somewhere other than the project root
# (e.g. via a shell without the walk-up wrapper).
#
# Runs on ConfigChange too, so the entry is re-asserted whenever settings change.
# The write is idempotent (only rewrites when the content actually changes), so
# our own write — which the settings watcher does NOT recognise as its own and
# would otherwise re-fire ConfigChange for — cannot loop.
set -euo pipefail

input=$(cat)

# Project root: prefer the env var Claude Code exports to hooks; fall back to
# the cwd reported on stdin.
dir="${CLAUDE_PROJECT_DIR:-$(printf '%s' "$input" | jq -r '.cwd // empty')}"
[ -n "$dir" ] || exit 0

# Only act on a real project root — one that already has a .claude/ dir.
[ -d "$dir/.claude" ] || exit 0

file="$dir/.claude/settings.local.json"

current=$(cat "$file" 2>/dev/null || true)
# Treat missing/blank/whitespace-only as an empty object.
[ -n "${current//[[:space:]]/}" ] || current='{}'

updated=$(printf '%s' "$current" \
  | jq '.sandbox.filesystem.allowRead =
         ((.sandbox.filesystem.allowRead // []) + ["."] | unique)')

# Skip the write when nothing changes, so ConfigChange can't self-trigger.
[ "$updated" = "$(printf '%s' "$current" | jq . 2>/dev/null)" ] && exit 0

printf '%s\n' "$updated" > "$file.tmp" && mv "$file.tmp" "$file"
