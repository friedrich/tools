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
  tools.claudeZshFunctions.enable = true; # claude() walk-up wrapper, claude-mitm()
}
```
