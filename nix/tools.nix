{ pkgs }:
{
  # Force read-only `gh api`: appends `-X GET` (gh uses last-wins for --method).
  gh-api-ro = pkgs.writeShellApplication {
    name = "gh-api-ro";
    runtimeInputs = [ pkgs.gh ];
    text = ''exec gh api "$@" -X GET'';
  };

  # Download a GitHub Actions run's logs, metadata, and a markdown summary.
  gh-logs = pkgs.writeShellApplication {
    name = "gh-logs";
    runtimeInputs = [ pkgs.gh ];
    text = ''exec ${pkgs.python3}/bin/python3 ${../scripts/gh-logs.py} "$@"'';
  };

  # Print the full bisect range with every bad/good/skip mark.
  bisect-log = pkgs.writeShellApplication {
    name = "bisect-log";
    runtimeInputs = [ pkgs.git ];
    text = ''exec ${pkgs.python3}/bin/python3 ${../scripts/bisect-log.py} "$@"'';
  };

  # Throwaway git worktree on a random branch off HEAD, with auto-cleanup on exit.
  wt = pkgs.writeShellApplication {
    name = "wt";
    runtimeInputs = [ pkgs.git ];
    text = ''exec ${pkgs.python3}/bin/python3 ${../scripts/wt.py} "$@"'';
  };

  # Search Claude Code conversation logs for the current working directory.
  claude-grep = pkgs.writeShellApplication {
    name = "claude-grep";
    runtimeInputs = [
      pkgs.ripgrep
      pkgs.jq
    ];
    text = builtins.readFile ../scripts/claude-grep.sh;
  };

  # Clone a Claude Code conversation into another folder under a fresh session id.
  claude-clone = pkgs.writeShellApplication {
    name = "claude-clone";
    text = ''exec ${pkgs.python3}/bin/python3 ${../scripts/claude-clone.py} "$@"'';
  };

  # Un-end a model-ended Claude Code session so it can be resumed.
  claude-unend = pkgs.writeShellApplication {
    name = "claude-unend";
    text = ''exec ${pkgs.python3}/bin/python3 ${../scripts/claude-unend.py} "$@"'';
  };

  # Undo the last compaction of a Claude Code session, as a new session.
  claude-uncompact = pkgs.writeShellApplication {
    name = "claude-uncompact";
    text = ''exec ${pkgs.python3}/bin/python3 ${../scripts/claude-uncompact.py} "$@"'';
  };
}
