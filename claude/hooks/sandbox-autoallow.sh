#!/usr/bin/env bash
# Auto-allow sandboxed Bash commands, bypassing the static command parser.
# A PreToolUse "allow" decision short-circuits the parser/prompt; the sandbox
# wrapping still happens at execution (gated on sandbox.enabled), so the command
# runs contained and sandbox filesystem/network deny-settings remain in force.
#
# Commands that opt OUT of the sandbox (dangerouslyDisableSandbox:true) are left
# to the normal permission flow — we never auto-allow an unsandboxed command.
#
# WARNING: a PreToolUse "allow" bypasses the ENTIRE permission system for the
# call, including permissions.deny / Bash(...) deny rules. A sandboxed command
# that matches a Bash deny rule will still run. Deny rules are NOT a hard stop
# while this hook is active — enforce hard limits at the sandbox layer instead
# (filesystem denyWrite/denyRead, network allowlist), since those still apply.
#
# ===========================================================================
# Moved verbatim from the user CLAUDE.md ("Inline Bash: what blocks auto-allow").
# Now mostly moot for sandboxed commands (this hook auto-allows them), but still
# applies to UNSANDBOXED (dangerouslyDisableSandbox) commands on the normal path.
#
# ## Inline Bash: what blocks auto-allow
#
# Even with `autoAllowBashIfSandboxed` on, the static parser falls through to a
# user prompt for anything it can't prove safe. **When any of the patterns below
# appear, write the commands to a script file (e.g. `$TMPDIR/run.sh`) and execute
# the script instead.** Bias toward scripting: if there's any doubt, script it.
#
# **Patterns that block auto-allow (script it instead):**
#
# - Bare `$VAR` references — must be double-quoted (`"$f"`, never `$f`). Common
#   cases people forget: loop vars, `$?`, `$$`, `$1`.
# - `find` with `-exec`
# - `find` with a variable in the search root (e.g. `find "$TMPDIR/..."`)
# - Backslash-escaped whitespace (`foo\ bar` → use `"foo bar"`).
# - `jq -f` (reading a jq program from a file)
# - A bare `=` anywhere in the command
#
# **The parser is under-documented.** The list above is observed behaviour, not
# exhaustive — other tokens or command shapes may also trip it.
#
# **Trigger word: "shoo".** If the user says **shoo**, the previous Bash call
# violated one of the rules above. If you can confidently identify and apply the
# fix in place, re-run Bash with the corrected command. Otherwise, write the
# commands to a script and execute the script.
#
# When Claude Code is in don't-ask mode, the user has no way to remind you by
# saying "shoo". If your command is denied, consider whether the parser flagged it.
# ===========================================================================
input=$(cat)
if [ "$(jq -r '.tool_input.dangerouslyDisableSandbox // false' <<<"$input")" = "true" ]; then
  exit 0
fi
jq -nc '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"allow",permissionDecisionReason:"sandboxed; parser bypassed"}}'
