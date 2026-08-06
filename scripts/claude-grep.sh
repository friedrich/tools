#!/usr/bin/env bash
# Search Claude Code conversations recorded for $PWD and print matching
# session files in creation order (oldest first). The Claude projects dir
# is named by replacing every '/' and '.' in the cwd with '-'.

if [ $# -lt 1 ]; then
  echo "usage: claude-grep <pattern> [rg-args...]" >&2
  exit 2
fi

cwd=$(pwd -P)
encoded=$(printf '%s' "$cwd" | tr '/.' '--')
dir="$HOME/.claude/projects/$encoded"
if [ ! -d "$dir" ]; then
  echo "no Claude conversations found for $cwd" >&2
  exit 1
fi

# Find matches first, then read timestamps only from matching files.
#
# jq short-circuits on the first timestamped line via first(): a plain
# `jq ... | head -n1` would leave jq writing into a closed pipe (SIGPIPE, exit
# 141), which under this wrapper's `set -o pipefail`/`errexit` aborts the
# while-loop subshell mid-iteration and drops a nondeterministic set of results.
# Keeping the short-circuit inside jq avoids the broken pipe entirely; the
# `|| true` guards against a jq parse error (e.g. a partial trailing line)
# tripping errexit.
rg -l "$@" -- "$dir"/*.jsonl | while IFS= read -r f; do
  ts=$(jq -rn 'first(inputs | select(.timestamp).timestamp | sub("\\.[0-9]+Z$"; "Z") | fromdateiso8601 | localtime | strftime("%Y-%m-%d %H:%M"))' "$f" 2>/dev/null) || true
  [ -n "$ts" ] || ts="?"
  printf '%s\t%s\n' "$ts" "$f"
done | sort
