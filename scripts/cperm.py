#!/usr/bin/env python3
"""Edit Claude Code permission and sandbox settings.

    cperm sandbox ro PATH...      sandbox.filesystem.allowRead
    cperm sandbox rw PATH...      sandbox.filesystem.allowRead + allowWrite
    cperm sandbox host HOST...    sandbox.network.allowedDomains
    cperm sandbox socket PATH...  sandbox.network.allowUnixSockets
    cperm sandbox mach NAME...    sandbox.network.allowMachLookup
    cperm sandbox local-binding   sandbox.network.allowLocalBinding
    cperm allow RULE...           permissions.allow, e.g. 'Bash(nix:*)'
    cperm rm COMMAND...           removes what COMMAND would add
    cperm show                    lists the entries of every scope

PATH uses sandbox path syntax:
  /               absolute path from filesystem root
  ~/              relative to home directory
  ./ or no prefix relative to the project root for project settings, or to
                  ~/.claude for user settings
A trailing / or /** is ignored, so ~/build, ~/build/ and ~/build/** are the same.

Paths in Read(...) and Edit(...) rules use a different syntax:
  //path          absolute path from filesystem root, e.g. Read(//Users/alice/secrets/**)
  ~/path          path from home directory, e.g. Read(~/Documents/*.pdf)
  /path           path relative to the settings source, e.g. Edit(/src/**/*.ts)
  path or ./path  path relative to current directory, e.g. Read(*.env)

Each edit prints a diff of the file it changed.
"""

from __future__ import annotations

import argparse
import difflib
import functools
import json
import json.scanner
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCOPES = ("user", "project", "local")
HOST = re.compile(
    r"^(?:(?:\*\.)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*|\[[0-9A-Fa-f:.]+\])(?::\d+)?$"
)
RULE = re.compile(r"^[A-Za-z][\w-]*(?:\(.+\))?$")

ALLOW_READ = ("sandbox", "filesystem", "allowRead")
ALLOW_WRITE = ("sandbox", "filesystem", "allowWrite")
ALLOWED_DOMAINS = ("sandbox", "network", "allowedDomains")
UNIX_SOCKETS = ("sandbox", "network", "allowUnixSockets")
MACH_LOOKUP = ("sandbox", "network", "allowMachLookup")
LOCAL_BINDING = ("sandbox", "network", "allowLocalBinding")
PERMISSIONS_ALLOW = ("permissions", "allow")

SHOWN = [
    ("permissions", "allow"),
    ("permissions", "ask"),
    ("permissions", "deny"),
    ("permissions", "additionalDirectories"),
    *[("sandbox", "filesystem", k) for k in ("allowRead", "denyRead", "allowWrite", "denyWrite")],
    *[("sandbox", "network", k) for k in (
        "allowedDomains", "deniedDomains", "allowUnixSockets", "allowMachLookup", "allowLocalBinding")],
]


class UsageError(Exception):
    pass


def dotted(path: tuple[str, ...]) -> str:
    return ".".join(path)


# --- locations ---------------------------------------------------------------


def config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


@functools.cache
def project_root() -> Path:
    """Returns the working directory of the Claude Code process, else the current one.

    The Claude Code process stays in the project directory when the shell of
    its Bash tool changes directory.
    """
    pid = os.environ.get("CLAUDE_PID")
    if not pid:
        return Path.cwd()
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise UsageError(f"cannot find the project directory of Claude Code (pid {pid}): {e}")
    for line in out.splitlines():
        if line.startswith("n"):
            return Path(line[1:])
    raise UsageError(f"cannot find the project directory of Claude Code (pid {pid})")


def settings_file(scope: str) -> Path:
    if scope == "user":
        return config_dir() / "settings.json"
    name = "settings.json" if scope == "project" else "settings.local.json"
    return project_root() / ".claude" / name


# --- settings document -------------------------------------------------------


class Settings:
    """A settings file that is re-serialized with each array's original layout.

    Claude Code writes arrays one element per line; prettier-formatted files put
    short arrays on one line. Arrays keep whichever layout they had. New arrays
    go on one line (when it fits in 80 columns) only if the file already has a
    non-empty one-line array.
    """

    def __init__(self, text: str):
        self.inline: set[int] = set()
        self.original: list[list] = []  # keeps ids in `inline` from being reused
        self.data: dict = self._load(text) if text.strip() else {}
        self.prefer_inline = bool(self.inline)
        self.newline = "" if text and not text.endswith("\n") else "\n"

    def _load(self, text: str) -> dict:
        decoder = json.JSONDecoder()
        parse_array = decoder.parse_array

        def tracking(s_and_end, scan_once, *rest):
            s, start = s_and_end
            value, end = parse_array(s_and_end, scan_once, *rest)
            self.original.append(value)
            if value and "\n" not in s[start:end]:
                self.inline.add(id(value))
            return value, end

        decoder.parse_array = tracking
        decoder.scan_once = json.scanner.py_make_scanner(decoder)
        data = decoder.decode(text)
        if not isinstance(data, dict):
            raise UsageError("settings file does not hold a JSON object")
        return data

    def dumps(self) -> str:
        return self._dump(self.data, 0, 0) + self.newline

    def _dump(self, value, indent: int, prefix: int) -> str:
        pad = "  " * indent
        if isinstance(value, dict):
            if not value:
                return "{}"
            items = []
            for k, v in value.items():
                key = json.dumps(k, ensure_ascii=False)
                items.append(f"{pad}  {key}: " + self._dump(v, indent + 1, len(pad) + 4 + len(key)))
            return "{\n" + ",\n".join(items) + "\n" + pad + "}"
        if isinstance(value, list):
            if not value:
                return "[]"
            is_new = all(value is not a for a in self.original)
            one_line = id(value) in self.inline or (is_new and self.prefer_inline)
            if one_line and not any(isinstance(v, (dict, list)) for v in value):
                text = "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in value) + "]"
                if prefix + len(text) + 1 <= 80:
                    return text
            items = [pad + "  " + self._dump(v, indent + 1, len(pad) + 2) for v in value]
            return "[\n" + ",\n".join(items) + "\n" + pad + "]"
        return json.dumps(value, ensure_ascii=False)

    def get(self, path: tuple[str, ...]):
        node = self.data
        for k in path:
            if not isinstance(node, dict):
                return None
            node = node.get(k)
        return node

    def list(self, path: tuple[str, ...], create: bool) -> list | None:
        node = self.data
        for k in path[:-1]:
            child = node.get(k)
            if child is None and create:
                child = node[k] = {}
            if child is None:
                return None
            if not isinstance(child, dict):
                raise UsageError(f"{dotted(path)}: {k} is not an object")
            node = child
        value = node.get(path[-1])
        if value is None and create:
            value = node[path[-1]] = []
        if value is not None and not isinstance(value, list):
            raise UsageError(f"{dotted(path)} is not a list")
        return value

    def prune(self, path: tuple[str, ...]) -> None:
        """Deletes the containers along `path` that have become empty."""
        for depth in range(len(path), 0, -1):
            parent = self.get(path[:depth - 1])
            if not isinstance(parent, dict) or parent.get(path[depth - 1]) not in ([], {}):
                return
            del parent[path[depth - 1]]


# --- edits -------------------------------------------------------------------


class Edit:
    def __init__(self, settings: Settings, remove: bool):
        self.settings = settings
        self.remove = remove
        self.notes: list[str] = []

    def entry(self, path: tuple[str, ...], entry: str) -> None:
        entries = self.settings.list(path, create=not self.remove)
        present = entry in (entries or [])
        if self.remove:
            if not present:
                self.notes.append(f"not in {dotted(path)}: {entry}")
                return
            entries[:] = [e for e in entries if e != entry]
            self.settings.prune(path)
        elif present:
            self.notes.append(f"already in {dotted(path)}: {entry}")
        else:
            entries.append(entry)

    def local_binding(self) -> None:
        network = self.settings.get(LOCAL_BINDING[:-1])
        if self.remove:
            if not isinstance(network, dict) or LOCAL_BINDING[-1] not in network:
                self.notes.append(f"{dotted(LOCAL_BINDING)} is not set")
                return
            del network[LOCAL_BINDING[-1]]
            self.settings.prune(LOCAL_BINDING[:-1])
        elif isinstance(network, dict) and network.get(LOCAL_BINDING[-1]) is True:
            self.notes.append(f"{dotted(LOCAL_BINDING)} is already true")
        else:
            node = self.settings.data
            for k in LOCAL_BINDING[:-1]:
                node = node.setdefault(k, {})
                if not isinstance(node, dict):
                    raise UsageError(f"{dotted(LOCAL_BINDING)}: {k} is not an object")
            node[LOCAL_BINDING[-1]] = True


def validate(args) -> None:
    if args.cmd == "sandbox" and args.what == "host":
        for v in args.values:
            if "://" in v or "/" in v:
                raise UsageError(f"{v!r}: pass a host name, not a URL")
            if not HOST.match(v):
                raise UsageError(f"{v!r} is not a host, *.wildcard, or [IPv6] with optional :port")
    if args.cmd == "allow":
        for v in args.values:
            if not RULE.match(v):
                raise UsageError(f"{v!r} is not a permission rule such as Bash(nix:*) or WebSearch")


def apply(edit: Edit, args) -> None:
    if args.cmd == "allow":
        for rule in args.values:
            edit.entry(PERMISSIONS_ALLOW, rule)
        return
    what = args.what
    if what == "local-binding":
        edit.local_binding()
        return
    for v in args.values:
        if what in ("ro", "rw"):
            edit.entry(ALLOW_READ, v)
            if what == "rw":
                edit.entry(ALLOW_WRITE, v)
            elif edit.remove and v in (edit.settings.list(ALLOW_WRITE, create=False) or []):
                edit.notes.append(f"warning: {v} is still in {dotted(ALLOW_WRITE)}")
        elif what == "host":
            edit.entry(ALLOWED_DOMAINS, v)
        elif what == "socket":
            edit.entry(UNIX_SOCKETS, v)
        elif what == "mach":
            edit.entry(MACH_LOOKUP, v)


def write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if target.exists():
            shutil.copymode(target, tmp)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


SANDBOX_HINT = "Settings files are protected inside the Claude Code sandbox; run cperm outside it."


def read(path: Path) -> str | None:
    """Returns the file's text, or None if it does not exist."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        raise UsageError(f"cannot read {path}: {e.strerror}. {SANDBOX_HINT}")


def run_edit(args) -> int:
    validate(args)
    path = settings_file(args.scope)
    try:
        target = path.resolve()  # the user settings file is often a symlink
    except OSError:
        target = path
    old = read(target) or ""
    try:
        settings = Settings(old)
    except json.JSONDecodeError as e:
        raise UsageError(f"{path}: invalid JSON: {e}")

    edit = Edit(settings, args.remove)
    apply(edit, args)
    for note in edit.notes:
        print(note)

    new = settings.dumps()
    if not old.strip() and settings.data == {}:
        new = old
    shown = str(path) if path == target else f"{path} -> {target}"
    if new == old:
        print(f"no change: {shown}")
        return 0
    print(f"{'would edit' if args.dry_run else 'edited'} ({args.scope}): {shown}")
    sys.stdout.writelines(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=str(path), tofile=str(path), n=1,
    ))
    if args.dry_run:
        return 0
    try:
        write(target, new)
    except OSError as e:
        print(f"cperm: cannot write {target}: {e.strerror}. {SANDBOX_HINT}", file=sys.stderr)
        return 1
    return 0


def show(args) -> int:
    rows: dict[tuple[str, ...], list[tuple[str, str]]] = {}
    for scope in [args.scope] if args.scope else SCOPES:
        path = settings_file(scope)
        try:
            text = read(path)
            settings = Settings(text) if text is not None else None
        except (json.JSONDecodeError, UsageError) as e:
            print(f"{scope:<8}{path}  ({e})")
            continue
        print(f"{scope:<8}{path}{'' if settings else '  (missing)'}")
        if not settings:
            continue
        for key in SHOWN:
            value = settings.get(key)
            if isinstance(value, list):
                entries = [e if isinstance(e, str) else json.dumps(e) for e in value]
            elif value is not None:
                entries = [json.dumps(value)]
            else:
                entries = []
            rows.setdefault(key, []).extend((scope, e) for e in entries)
    for key, entries in rows.items():
        if entries:
            print(f"\n{dotted(key)}")
            for scope, e in entries:
                print(f"  {scope:<8}{e}")
    return 0


# --- command line ------------------------------------------------------------


SCOPE_HELP = "settings file: user, project or local (default: local; for show: all)"
DRY_RUN_HELP = "print the diff without writing"


def options(p: argparse.ArgumentParser, default) -> None:
    """Adds --scope and --dry-run, which are accepted before and after the command."""
    p.add_argument("--scope", choices=SCOPES, default=default, help=SCOPE_HELP)
    p.add_argument("-n", "--dry-run", action="store_true",
                   default=False if default is None else default, help=DRY_RUN_HELP)


def add_edit_commands(sub, leaf: argparse.ArgumentParser) -> None:
    sandbox = sub.add_parser("sandbox", help="sandbox filesystem and network lists")
    what = sandbox.add_subparsers(dest="what", required=True, metavar="LIST")
    for name, metavar, text in [
        ("ro", "PATH", "read access (allowRead)"),
        ("rw", "PATH", "read and write access (allowRead + allowWrite)"),
        ("host", "HOST", "network access (allowedDomains)"),
        ("socket", "PATH", "Unix socket access (allowUnixSockets)"),
        ("mach", "NAME", "Mach service lookup (allowMachLookup)"),
    ]:
        q = what.add_parser(name, parents=[leaf], help=text)
        q.add_argument("values", nargs="+", metavar=metavar)
    what.add_parser("local-binding", parents=[leaf], help="binding to local ports (allowLocalBinding)")

    allow = sub.add_parser("allow", parents=[leaf], help="permission rules (permissions.allow)")
    allow.add_argument("values", nargs="+", metavar="RULE")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cperm",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    options(p, None)
    p.set_defaults(remove=False)
    # Leaves repeat the options; SUPPRESS keeps them from overriding a value given earlier.
    leaf = argparse.ArgumentParser(add_help=False)
    options(leaf, argparse.SUPPRESS)

    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    add_edit_commands(sub, leaf)
    rm = sub.add_parser("rm", parents=[leaf], help="remove what the command would add")
    rm.set_defaults(remove=True)
    add_edit_commands(rm.add_subparsers(dest="cmd", required=True, metavar="COMMAND"), leaf)
    sub.add_parser("show", parents=[leaf], help="list the entries of every scope")
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        if args.cmd == "show":
            return show(args)
        args.scope = args.scope or "local"
        return run_edit(args)
    except UsageError as e:
        print(f"cperm: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
