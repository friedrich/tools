#!/usr/bin/env python3
"""Undo the last compaction of a Claude Code session.

Compaction is append-only: it appends a ``{"type": "system", "subtype":
"compact_boundary"}`` marker plus a summary message to the transcript
(``<configDir>/projects/<enc>/<sid>.jsonl``) and leaves the earlier records in
place; on load, everything before the last boundary is dropped in favour of the
summary. Truncating the transcript right before that marker therefore brings
the full history back.

This tool does the truncation non-destructively: it writes the truncated
transcript as a new session (fresh id, ``sessionId`` fields rewritten, sidecar
dir and file-history snapshots copied — the same recipe as claude-clone) in the
same project dir and leaves the original untouched. Anything recorded after the
last compaction — including turns since — is not part of the copy.

Without arguments, lists every compacted session across all projects. With a
session id (a unique prefix is enough), creates the truncated copy and prints
the resume command.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path


def config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def _parse(line: str) -> dict | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def boundaries(transcript: Path) -> list[tuple[int, dict]]:
    """(line index, entry) for every compact_boundary marker in the transcript."""
    out = []
    with open(transcript, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if '"compact_boundary"' not in line:
                continue
            entry = _parse(line)
            if entry and entry.get("type") == "system" \
                    and entry.get("subtype") == "compact_boundary":
                out.append((i, entry))
    return out


def list_compacted() -> int:
    found = False
    for transcript in sorted(config_dir().glob("projects/*/*.jsonl")):
        marks = boundaries(transcript)
        if not marks:
            continue
        found = True
        last = marks[-1][1]
        trigger = last.get("compactMetadata", {}).get("trigger", "?")
        n = f"  ({len(marks)} compactions)" if len(marks) > 1 else ""
        print(f"{transcript.stem}  compacted {last.get('timestamp', '?')} "
              f"({trigger}){n}  {transcript.parent.name}")
    if not found:
        print("no compacted sessions found", file=sys.stderr)
    return 0


def rewrite_sid(line: str, old_sid: str, new_sid: str) -> str:
    """Rewrite the sessionId field, copying the line verbatim when untouched."""
    obj = _parse(line)
    if obj is None or obj.get("sessionId") != old_sid:
        return line
    obj["sessionId"] = new_sid
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"


def uncompact(sid_prefix: str) -> int:
    matches = [
        t
        for t in config_dir().glob("projects/*/*.jsonl")
        if t.stem.startswith(sid_prefix) and boundaries(t)
    ]
    if not matches:
        print(f"no compacted session matching {sid_prefix!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"ambiguous prefix {sid_prefix!r}:", file=sys.stderr)
        for t in matches:
            print(f"  {t.stem}  ({t.parent.name})", file=sys.stderr)
        return 1

    transcript = matches[0]
    old_sid = transcript.stem
    new_sid = str(uuid.uuid4())

    with open(transcript, encoding="utf-8") as f:
        lines = f.readlines()
    cut = boundaries(transcript)[-1][0]

    out_jsonl = transcript.parent / f"{new_sid}.jsonl"
    with open(out_jsonl, "w", encoding="utf-8") as f:
        f.writelines(rewrite_sid(line, old_sid, new_sid) for line in lines[:cut])

    sidecar = transcript.with_suffix("")
    if sidecar.is_dir():
        out_sidecar = transcript.parent / new_sid
        shutil.copytree(sidecar, out_sidecar)
        subdir = out_sidecar / "subagents"
        if subdir.is_dir():
            for sub in subdir.glob("*.jsonl"):
                text = "".join(rewrite_sid(line, old_sid, new_sid)
                               for line in sub.read_text(encoding="utf-8").splitlines(keepends=True))
                sub.write_text(text, encoding="utf-8")

    file_history = config_dir() / "file-history" / old_sid
    if file_history.is_dir():
        shutil.copytree(file_history, config_dir() / "file-history" / new_sid)

    print(f"truncated {old_sid} at line {cut + 1} "
          f"({len(lines) - cut} record(s) after the last compaction dropped)")
    print(f"new session: {out_jsonl}")
    print("resume with:")
    print(f"claude --resume {new_sid}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        return list_compacted()
    if len(args) == 1 and not args[0].startswith("-"):
        return uncompact(args[0])
    print(__doc__.strip(), file=sys.stderr)
    print("\nusage: claude-uncompact [session-id]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
