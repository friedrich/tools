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

    claudeZshFunctions.enable = lib.mkEnableOption "the claude()/claude-mitm() zsh functions";

    cpermBroker.enable = lib.mkEnableOption ''
      the cperm broker, a launchd agent that applies cperm edits from sandboxed
      Claude Code commands once they are allowed in a dialog (macOS)'';
  };

  config = lib.mkMerge [
    (lib.mkIf cfg.enable {
      home.packages = builtins.attrValues toolPackages;
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

    (lib.mkIf cfg.cpermBroker.enable {
      launchd.agents.cperm-broker = {
        enable = true;
        config = {
          ProgramArguments = [
            (lib.getExe toolPackages.cperm)
            "serve"
          ];
          RunAtLoad = true;
          KeepAlive = true;
          ProcessType = "Interactive";
          StandardErrorPath = "${config.home.homeDirectory}/Library/Logs/cperm-broker.log";
        };
      };
    })
  ];
}
