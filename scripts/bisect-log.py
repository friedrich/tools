#!/usr/bin/env python3
"""Print the bisect range with every bad/good/skip mark.

Reads .git/BISECT_LOG to recover the complete history of bisect verdicts,
not just the current refs/bisect/{bad,good-*,skip-*} pointers (refs/bisect/bad
only tracks the latest bad commit).

By default only the range still to be bisected is shown: ancestors of the
current (deepest) bads that are not reachable from any good, plus the good
commits bounding that range. With --full the
display is bounded by the topmost bads (those with no other bad as descendant)
as the ceiling, and the deepest goods (those with no other good as ancestor)
as the floor, so every commit ever marked is visible.
"""

import argparse
import os
import re
import subprocess
import sys


USE_COLOR = sys.stdout.isatty()
COLOR = {
    "bad":  "\033[91m" if USE_COLOR else "",
    "good": "\033[92m" if USE_COLOR else "",
    "skip": "\033[90m" if USE_COLOR else "",
}
RESET = "\033[0m" if USE_COLOR else ""
BOLD = "\033[1m" if USE_COLOR else ""
ICON = {
    "bad":  "✗",
    "good": "✓",
    "skip": "?",
}
# Prettify git's ASCII graph; safe to apply over ANSI codes since escape
# sequences contain none of these characters.
GRAPH_CHARS = str.maketrans({
    "*": "┿",
    "|": "│",
    "/": "╱",
    "\\": "╲",
    "-": "─",
    "_": "─",
})


def git(*args, check=True):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=check
    ).stdout


def main():
    parser = argparse.ArgumentParser(
        description="Show the remaining bisect range with bad/good/skip marks."
    )
    parser.add_argument(
        "-f", "--full",
        action="store_true",
        help="show the full historical range including all good/bad marks, "
             "not just the commits still to be tested",
    )
    opts = parser.parse_args()

    git_dir = git("rev-parse", "--git-dir").strip()
    log_path = os.path.join(git_dir, "BISECT_LOG")
    if not os.path.exists(log_path):
        sys.exit("no active bisect (BISECT_LOG missing)")

    # Collect every oid ever declared bad/good/skip.
    decl_re = re.compile(r"^# (bad|good|skip): \[([0-9a-f]+)\]")
    marks = {}
    with open(log_path) as f:
        for line in f:
            m = decl_re.match(line)
            if not m:
                continue
            tag, oid = m.group(1), m.group(2)
            marks[oid] = tag

    head = _bisect_head()

    bads = [oid for oid, tag in marks.items() if tag == "bad"]
    goods = [oid for oid, tag in marks.items() if tag == "good"]
    if not bads:
        sys.exit("no bad commit recorded in BISECT_LOG")

    if opts.full:
        # Ceiling: topmost bads (no other bad as descendant).
        # Floor: deepest goods (no other good as ancestor) — exclude only
        # their parents so the goods themselves stay visible.
        ceiling = _extremes(bads, role="topmost")
        floor = [f"{oid}^@" for oid in _extremes(goods, role="deepest")]
    else:
        # Remaining suspects: ancestors of the current (deepest) bads not
        # reachable from any good, as in `git bisect visualize` — plus the
        # boundary goods themselves (^@ excludes only their parents).
        ceiling = _extremes(bads, role="deepest")
        floor = [f"{oid}^@" for oid in goods]

    args = [
        "log",
        "--graph",
        f"--color={'always' if USE_COLOR else 'never'}",
        "--decorate=short",
        "--decorate-refs-exclude=refs/bisect/*",
        "--format=%x00%H%x09%h%x09%D%x09%s",
        *ceiling,
    ]
    if floor:
        args.append("--not")
        args.extend(floor)

    deco_color = "\033[33m" if USE_COLOR else ""
    head_color = "\033[1;36m" if USE_COLOR else ""
    for line in git(*args).splitlines():
        # The NUL sentinel separates git's graph prefix from the commit
        # fields; lines without it are pure graph connectors (|, /, \).
        graph, sep, record = line.partition("\x00")
        graph = graph.translate(GRAPH_CHARS)
        if not sep:
            print(f"  {graph}")
            continue
        parts = record.split("\t", 3)
        full = parts[0]
        short = parts[1]
        deco = parts[2] if len(parts) > 2 else ""
        subject = parts[3] if len(parts) > 3 else ""
        tag = marks.get(full)
        sym = f"{COLOR[tag]}{ICON[tag]}{RESET}" if tag else " "
        is_head = full == head
        ptr = f"{head_color}→{RESET}" if is_head else " "
        deco_part = f" {deco_color}({deco}){RESET}" if deco else ""
        line = f"{ptr} {graph}{short} {sym}{deco_part} {subject}"
        if is_head and USE_COLOR:
            # Re-assert bold after every inner reset so colored spans don't
            # clear it; git emits both ESC[m and ESC[0m as resets.
            line = BOLD + line.replace("\033[m", "\033[m" + BOLD) \
                              .replace(RESET, RESET + BOLD) + RESET
        print(line)


def _bisect_head():
    """Full oid of the commit currently under test.

    In `git bisect --no-checkout` mode the commit is pointed to by the
    BISECT_HEAD ref rather than checked out, so prefer it over HEAD.
    """
    out = git("rev-parse", "--verify", "--quiet", "BISECT_HEAD", check=False).strip()
    return out or git("rev-parse", "HEAD").strip()


def _is_ancestor(a, b):
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", a, b]
    ).returncode == 0


def _extremes(commits, *, role):
    """Filter commits to the topological extremes within the set.

    role="deepest": keep commits with no other set member as ancestor.
    role="topmost": keep commits with no other set member as descendant.
    """
    result = []
    for c in commits:
        if role == "deepest":
            blocked = any(o != c and _is_ancestor(o, c) for o in commits)
        else:  # topmost
            blocked = any(o != c and _is_ancestor(c, o) for o in commits)
        if not blocked:
            result.append(c)
    return result


if __name__ == "__main__":
    main()
