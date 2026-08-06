#!/usr/bin/env python3
"""wt — create a throwaway git worktree on a random branch off HEAD, drop you
into a shell inside it, and tidy up on exit.

The worktree/branch are removed only if there's nothing to lose: `git worktree
remove` (no --force) guards uncommitted/untracked changes, and `git branch -d`
guards unmerged commits. When git refuses, the worktree is kept and the
force-cleanup command is printed.
"""

import os
import random
import subprocess
import sys
from pathlib import Path

ADJECTIVES = [
    "brave", "calm", "clever", "bold", "quiet", "swift", "gentle", "merry",
    "keen", "lucky", "sunny", "witty", "nimble", "rustic", "amber", "jolly",
    "mellow", "plucky", "snug", "zesty",
]
NOUNS = [
    "otter", "falcon", "badger", "heron", "marten", "lynx", "finch", "raven",
    "willow", "cedar", "comet", "harbor", "meadow", "pebble", "cliff", "ember",
    "thistle", "bramble", "cove", "fjord",
]


_CODES = {"green": "32", "yellow": "33", "blue": "34", "red": "31", "dim": "2", "bold": "1"}


def c(text, *names):
    if not sys.stdout.isatty():
        return text
    seq = ";".join(_CODES[n] for n in names)
    return f"\033[{seq}m{text}\033[0m"


def main() -> int:
    if subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
    ).returncode != 0:
        print("wt: not inside a git repository", file=sys.stderr)
        return 1

    if os.environ.get("IN_WT"):
        print(
            f"wt: already inside a wt shell ({os.environ.get('WT_NAME', '?')}); "
            "nesting a new one.",
            file=sys.stderr,
        )

    repo_root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    ).stdout.strip()

    def git(*args, cwd=repo_root):
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)

    base_dir = Path(repo_root) / ".wt"
    base_dir.mkdir(parents=True, exist_ok=True)

    name = None
    for _ in range(50):
        candidate = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"
        taken = git("show-ref", "--quiet", "--verify", f"refs/heads/wt/{candidate}")
        if not (base_dir / candidate).exists() and taken.returncode != 0:
            name = candidate
            break
    if name is None:
        print("wt: couldn't find a free name after 50 tries", file=sys.stderr)
        return 1

    wt = base_dir / name
    branch = f"wt/{name}"

    added = git("worktree", "add", "-b", branch, str(wt), "HEAD")
    if added.returncode != 0:
        sys.stderr.write(added.stderr)
        return 1

    head = git("rev-parse", "--short", "HEAD", cwd=str(wt)).stdout.strip()
    print(
        f"{c('●', 'blue')} {c(name, 'bold')} — worktree off {head}  "
        f"{c('(exit the shell to clean up)', 'dim')}"
    )

    shell = os.environ.get("SHELL", "/bin/sh")
    env = {**os.environ, "IN_WT": "1", "WT_NAME": name}
    try:
        subprocess.run([shell, "-i"], cwd=str(wt), env=env)
    finally:
        cleanup(git, repo_root, wt, branch)
    return 0


def cleanup(git, repo_root, wt, branch):
    rel = os.path.relpath(wt, repo_root)
    if git("worktree", "remove", str(wt)).returncode == 0:
        if git("branch", "-d", branch).returncode == 0:
            print(f"{c('✓', 'green')} removed worktree {c(rel, 'dim')}")
            print(f"{c('✓', 'green')} deleted branch {c(branch, 'dim')}")
        else:
            print(f"{c('✓', 'green')} removed worktree {c(rel, 'dim')}")
            print(f"{c('●', 'yellow')} kept branch {c(branch, 'bold')} {c('(unmerged commits)', 'dim')}")
            print("  to discard:")
            print(f"    {c(f'git branch -D {branch}', 'dim')}")
    else:
        print(
            f"{c('●', 'yellow')} kept worktree {c(rel, 'bold')} on branch {c(branch, 'bold')} "
            f"{c('(uncommitted or untracked changes)', 'dim')}"
        )
        print("  to discard:")
        print(f"    {c(f'git worktree remove --force {rel} && git branch -D {branch}', 'dim')}")
    git("worktree", "prune")


if __name__ == "__main__":
    sys.exit(main())
