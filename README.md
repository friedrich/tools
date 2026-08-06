# tools

Personal CLI tools and Claude Code environment, packaged as a Nix flake.

> [!NOTE]
> The contents of this repository were generated using AI.

## Tools

- **`gh-logs`** — download a GitHub Actions run (logs, metadata, markdown summary) into a local dir for inspection.
- **`gh-api-ro`** — read-only `gh api`: appends `-X GET`, so `-f`/`-F` fields become query parameters instead of a POST body.
- **`bisect-log`** — print the full git bisect range with every bad/good/skip mark.
- **`wt`** — throwaway git worktree on a random branch off HEAD; drops you into a shell, cleans up on exit if nothing would be lost.
- **`claude-grep`** — search Claude Code conversations recorded for the current directory.
- **`claude-clone`** — copy a Claude Code conversation to another folder, locally or on a remote host (scp-style destination).
- **`claude-unend`** — revive a Claude Code session ended by the model so it can be resumed.

## Usage

Run ad hoc, without installing:

```sh
nix run github:friedrich/tools#gh-logs -- -b main
```

Install into a profile:

```sh
nix profile install github:friedrich/tools          # all tools
nix profile install github:friedrich/tools#gh-logs  # one tool
```

### As a flake input

```nix
inputs.tools.url = "github:friedrich/tools";
```

Then either add packages directly:

```nix
home.packages = builtins.attrValues {
  inherit (inputs.tools.packages.${pkgs.system}) gh-logs gh-api-ro wt;
};
```

or use the Home Manager module, which also covers the Claude Code environment:

```nix
{
  imports = [ inputs.tools.homeManagerModules.default ];

  tools.enable = true;                   # all CLI tools on PATH
  tools.claudeHooks.enable = true;       # sandbox hooks in ~/.claude/hooks
  tools.claudeZshFunctions.enable = true; # claude() walk-up wrapper, claude-mitm()
}
```

## Claude Code hooks

`claudeHooks.enable` installs two scripts into `~/.claude/hooks/`:

- **`sandbox-autoallow.sh`** — PreToolUse hook that auto-allows every *sandboxed* Bash command, bypassing the static command parser. Commands that opt out of the sandbox (`dangerouslyDisableSandbox: true`) still go through the normal permission flow.
- **`sandbox-allow-cwd-read.sh`** — SessionStart/ConfigChange hook that makes the project directory readable inside the sandbox by adding `"."` to `.sandbox.filesystem.allowRead` in the project's `settings.local.json`.

The scripts require `jq` on PATH. Installing the files is not enough — wire them up in the `hooks` section of `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "~/.claude/hooks/sandbox-autoallow.sh" }]
      }
    ],
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "~/.claude/hooks/sandbox-allow-cwd-read.sh", "timeout": 5 }]
      }
    ],
    "ConfigChange": [
      {
        "hooks": [{ "type": "command", "command": "~/.claude/hooks/sandbox-allow-cwd-read.sh", "timeout": 5 }]
      }
    ]
  }
}
```

**Warning:** a PreToolUse `allow` decision bypasses the entire permission system for that call, including `permissions.deny` rules. With `sandbox-autoallow.sh` active, enforce hard limits at the sandbox layer (filesystem `denyWrite`/`denyRead`, network allowlist) — those still apply.
