#!/usr/bin/env python3
"""Un-end a Claude Code session that the model ended via EndConversation.

When the model calls its EndConversation tool, Claude Code appends a
``{"type": "ended-by-model", ...}`` entry to the session transcript
(``<configDir>/projects/<enc>/<sid>.jsonl``). On every load the transcript
scanner turns that entry into the ``endedByModel`` app-state flag, which blocks
prompts, slash commands, and ``--resume``/``--continue`` with "Claude ended
this conversation." The marker line is the only persistent record, so deleting
it makes the session resumable again.

Without arguments, lists every ended session across all projects. With a
session id (a unique prefix is enough), strips the marker lines from that
session's transcript in place.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def config_dir() -> Path:
    import os

    return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))


def marker_lines(transcript: Path):
    with open(transcript, encoding="utf-8") as f:
        for line in f:
            if '"ended-by-model"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") == "ended-by-model":
                yield entry


def list_ended() -> int:
    found = False
    for transcript in sorted(config_dir().glob("projects/*/*.jsonl")):
        markers = list(marker_lines(transcript))
        if markers:
            found = True
            print(f"{transcript.stem}  ended {markers[-1].get('timestamp', '?')}  {transcript.parent.name}")
    if not found:
        print("no ended sessions found", file=sys.stderr)
    return 0


def unend(sid_prefix: str) -> int:
    matches = [
        t
        for t in config_dir().glob("projects/*/*.jsonl")
        if t.stem.startswith(sid_prefix) and any(True for _ in marker_lines(t))
    ]
    if not matches:
        print(f"no ended session matching {sid_prefix!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"ambiguous prefix {sid_prefix!r}:", file=sys.stderr)
        for t in matches:
            print(f"  {t.stem}  ({t.parent.name})", file=sys.stderr)
        return 1

    transcript = matches[0]
    kept = []
    removed = 0
    with open(transcript, encoding="utf-8") as f:
        for line in f:
            entry = None
            if '"ended-by-model"' in line:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    pass
            if entry is not None and entry.get("type") == "ended-by-model":
                removed += 1
            else:
                kept.append(line)

    tmp = transcript.with_suffix(".jsonl.unend-tmp")
    tmp.write_text("".join(kept), encoding="utf-8")
    tmp.replace(transcript)
    print(f"removed {removed} marker(s) from {transcript}")
    print(f"resume with: claude --resume {transcript.stem}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return list_ended()
    if len(args) == 1 and not args[0].startswith("-"):
        return unend(args[0])
    print(__doc__.strip(), file=sys.stderr)
    print("\nusage: claude-unend [session-id]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
