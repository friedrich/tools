{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.tools;
  toolPackages = import ./tools.nix { inherit pkgs; };
in
{
  options.tools = {
    enable = lib.mkEnableOption "the CLI tools (gh-logs, gh-api-ro, bisect-log, wt, claude-*)";

    # The hook scripts need jq on PATH, and the hooks section of
    # ~/.claude/settings.json must reference them (see README).
    claudeHooks.enable = lib.mkEnableOption "the Claude Code sandbox hooks in ~/.claude/hooks";

    claudeZshFunctions.enable = lib.mkEnableOption "the claude()/claude-mitm() zsh functions";
  };

  config = lib.mkMerge [
    (lib.mkIf cfg.enable {
      home.packages = builtins.attrValues toolPackages;
    })

    (lib.mkIf cfg.claudeHooks.enable {
      home.file.".claude/hooks/sandbox-autoallow.sh" = {
        source = ../claude/hooks/sandbox-autoallow.sh;
        executable = true;
      };
      home.file.".claude/hooks/sandbox-allow-cwd-read.sh" = {
        source = ../claude/hooks/sandbox-allow-cwd-read.sh;
        executable = true;
      };
    })

    (lib.mkIf cfg.claudeZshFunctions.enable {
      programs.zsh.initContent = ''
        # run claude from the nearest ancestor that has .claude/settings.json,
        # .claude/settings.local.json, or CLAUDE.md; stops before $HOME so the
        # global ~/.claude doesn't count as a match
        claude() {
          local dir=$PWD
          while [[ $dir != / && $dir != $HOME ]]; do
            if [[ -f $dir/.claude/settings.json || -f $dir/.claude/settings.local.json || -f $dir/CLAUDE.md ]]; then
              (cd "$dir" && command claude "$@")
              return
            fi
            dir=''${dir:h}
          done
          command claude "$@"
        }

        claude-mitm() {
          HTTPS_PROXY=http://127.0.0.1:8080 HTTP_PROXY=http://127.0.0.1:8080 claude "$@"
        }
      '';
    })
  ];
}
