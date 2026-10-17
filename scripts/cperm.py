#!/usr/bin/env python3
"""Edit Claude Code permission and sandbox settings.

    cperm sandbox ro PATH...      sandbox.filesystem.allowRead
    cperm sandbox rw PATH...      sandbox.filesystem.allowRead + allowWrite
    cperm sandbox host HOST...    sandbox.network.allowedDomains
    cperm sandbox socket PATH...  sandbox.network.allowUnixSockets
    cperm sandbox mach NAME...    sandbox.network.allowMachLookup
    cperm sandbox local-binding   sandbox.network.allowLocalBinding
    cperm allow RULE...           permissions.allow, e.g. 'Bash(nix:*)'
    cperm basic                   Edit(/**) and WebSearch in permissions.allow,
                                  . in sandbox.filesystem.allowRead
    cperm rm COMMAND...           removes what COMMAND would add
    cperm show                    lists the entries of every scope
    cperm serve                   runs the broker (see below)

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

The Claude Code sandbox keeps commands from reading or writing the settings
files. An edit that fails there goes to the broker, a `cperm serve` process
outside the sandbox listening on /private/tmp/cperm-UID/broker.sock; add that
directory to sandbox.network.allowUnixSockets. The broker repeats the edit
itself and applies it only once it is allowed in a dialog. macOS only.
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import functools
import io
import json
import json.scanner
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
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

BASIC = [
    (PERMISSIONS_ALLOW, "Edit(/**)"),
    (PERMISSIONS_ALLOW, "WebSearch"),
    (ALLOW_READ, "."),
]

SHOWN = [
    ("permissions", "allow"),
    ("permissions", "ask"),
    ("permissions", "deny"),
    ("permissions", "additionalDirectories"),
    *[("sandbox", "filesystem", k) for k in ("allowRead", "denyRead", "allowWrite", "denyWrite")],
    *[("sandbox", "network", k) for k in (
        "allowedDomains", "deniedDomains", "allowUnixSockets", "allowMachLookup", "allowLocalBinding")],
]


SOCKET_DIR = Path(f"/private/tmp/cperm-{os.getuid()}")
SOCKET = SOCKET_DIR / "broker.sock"
DIALOG_TIMEOUT = 120
SOL_LOCAL, LOCAL_PEERPID = 0, 2  # <sys/un.h>


class UsageError(Exception):
    pass


class Denied(UsageError):
    """Raised when a settings file cannot be read or written for lack of permission."""


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
    return process_cwd(pid) if pid else Path.cwd()


def process_cwd(pid: str | int) -> Path:
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        raise UsageError(f"cannot find the working directory of pid {pid}: {e}")
    for line in out.splitlines():
        if line.startswith("n"):
            return Path(line[1:])
    raise UsageError(f"cannot find the working directory of pid {pid}")


def settings_file(scope: str, root: Callable[[], Path] = project_root) -> Path:
    if scope == "user":
        return config_dir() / "settings.json"
    name = "settings.json" if scope == "project" else "settings.local.json"
    return root() / ".claude" / name


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
    for v in getattr(args, "values", []):
        if any(unprintable(c) for c in v):
            raise UsageError(f"{v!r} contains control or format characters")
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
    if args.cmd == "basic":
        for path, entry in BASIC:
            edit.entry(path, entry)
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


SANDBOX_HINT = (
    "Settings files are protected inside the Claude Code sandbox; "
    "run cperm outside it, or run the broker (cperm serve) to have edits approved in a dialog."
)


def read(path: Path) -> str | None:
    """Returns the file's text, or None if it does not exist."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except PermissionError as e:
        raise Denied(f"cannot read {path}: {e.strerror}. {SANDBOX_HINT}")
    except OSError as e:
        raise UsageError(f"cannot read {path}: {e.strerror}")


@dataclasses.dataclass
class Change:
    scope: str
    path: Path
    target: Path
    old: str
    new: str
    notes: list[str]

    @property
    def shown(self) -> str:
        return str(self.path) if self.path == self.target else f"{self.path} -> {self.target}"

    def diff(self) -> str:
        return "".join(difflib.unified_diff(
            self.old.splitlines(keepends=True), self.new.splitlines(keepends=True),
            fromfile=str(self.path), tofile=str(self.path), n=1,
        ))

    def report(self, dry_run: bool) -> str:
        lines = [f"{note}\n" for note in self.notes]
        if self.new == self.old:
            lines.append(f"no change: {self.shown}\n")
        else:
            lines.append(f"{'would edit' if dry_run else 'edited'} ({self.scope}): {self.shown}\n")
            lines.append(self.diff())
        return "".join(lines)

    def commit(self) -> None:
        if self.new == self.old:
            return
        try:
            write(self.target, self.new)
        except PermissionError as e:
            raise Denied(f"cannot write {self.target}: {e.strerror}. {SANDBOX_HINT}")
        except OSError as e:
            raise UsageError(f"cannot write {self.target}: {e.strerror}")


def plan(args, root: Callable[[], Path] = project_root) -> Change:
    validate(args)
    path = settings_file(args.scope, root)
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
    new = settings.dumps()
    if not old.strip() and settings.data == {}:
        new = old
    return Change(args.scope, path, target, old, new, edit.notes)


def run_edit(args) -> int:
    try:
        change = plan(args)
        if not args.dry_run:
            change.commit()
    except Denied:
        if args.dry_run or not SOCKET.exists():
            raise
        return delegate()
    print(change.report(args.dry_run), end="")
    return 0


# --- broker ------------------------------------------------------------------


def delegate() -> int:
    """Hands this invocation to the broker and relays its output."""
    request = {"argv": sys.argv[1:], "claude_pid": os.environ.get("CLAUDE_PID")}
    print(f"cperm: sandboxed; waiting up to {DIALOG_TIMEOUT}s for approval in the broker's dialog",
          file=sys.stderr)
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(DIALOG_TIMEOUT + 30)
            s.connect(str(SOCKET))
            s.sendall(json.dumps(request).encode() + b"\n")
            s.shutdown(socket.SHUT_WR)
            with s.makefile("rb") as f:
                reply = json.loads(f.read())
    except (OSError, ValueError) as e:
        raise UsageError(
            f"cannot reach the broker at {SOCKET}: {e}. "
            f"Is {SOCKET_DIR} in sandbox.network.allowUnixSockets?"
        )
    sys.stdout.write(reply["stdout"])
    sys.stderr.write(reply["stderr"])
    return reply["code"]


def serve(args) -> int:
    if sys.platform != "darwin":
        raise UsageError("the broker needs macOS")
    try:
        SOCKET_DIR.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = SOCKET_DIR.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise UsageError(f"{SOCKET_DIR} is not a directory owned by you")
    SOCKET_DIR.chmod(0o700)
    SOCKET.unlink(missing_ok=True)

    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(SOCKET))
        server.listen()
        inode = SOCKET.stat().st_ino
        server.settimeout(3600)
        log(f"listening on {SOCKET}")
        while True:
            try:
                conn, _ = server.accept()
            except TimeoutError:
                # Lets launchd restart a broker whose socket was cleaned out of /tmp.
                try:
                    if SOCKET.stat().st_ino == inode:
                        continue
                except FileNotFoundError:
                    pass
                log(f"{SOCKET} is gone; exiting")
                return 1
            with conn:
                conn.settimeout(10)
                try:
                    code, out, err = handle(conn)
                except Exception as e:  # keeps one bad request from stopping the broker
                    code, out, err = 2, "", f"cperm broker: {type(e).__name__}: {e}\n"
                log(f"exit {code}: {(out + err).strip()}")
                try:
                    conn.sendall(json.dumps({"code": code, "stdout": out, "stderr": err}).encode())
                except OSError as e:
                    log(f"cannot reply: {e}")


def handle(conn: socket.socket) -> tuple[int, str, str]:
    """Runs one delegated edit and returns its exit code, stdout and stderr."""
    peer = struct.unpack("i", conn.getsockopt(SOL_LOCAL, LOCAL_PEERPID, 4))[0]
    with conn.makefile("rb") as f:
        request = json.loads(f.readline(1 << 16))
    argv = request.get("argv")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return 2, "", "cperm broker: malformed request\n"
    log(f"pid {peer}: cperm {' '.join(argv)}")

    err = io.StringIO()
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            args = parser().parse_args(argv)
    except SystemExit:
        return 2, "", err.getvalue()
    if args.cmd in ("show", "serve") or args.dry_run:
        return 2, "", "cperm broker: only edits can be delegated\n"
    args.scope = args.scope or "local"

    try:
        root = requester_root(request.get("claude_pid"), peer)
        change = plan(args, lambda: root)
        if change.new != change.old:
            if not approve(dialog_text(change, root)):
                return 1, "", "cperm: not allowed in the broker's dialog\n"
            change.commit()
    except UsageError as e:
        return 2, "", f"cperm: {e}\n"
    return 0, change.report(dry_run=False), ""


def requester_root(claude_pid, peer: int) -> Path:
    """Returns the project root of the requester, determined from its process tree.

    The claimed Claude Code pid counts only if it is an ancestor of the
    connecting process; otherwise the connecting process's own directory is
    used.
    """
    out = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True,
    ).stdout
    parent = dict(tuple(map(int, line.split())) for line in out.splitlines())
    lineage, pid = set(), peer
    while pid > 1 and pid not in lineage:
        lineage.add(pid)
        pid = parent.get(pid, 0)
    if isinstance(claude_pid, str) and claude_pid.isdigit() and int(claude_pid) in lineage:
        return process_cwd(int(claude_pid))
    return process_cwd(peer)


def unprintable(c: str) -> bool:
    return unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp")


def dialog_text(change: Change, root: Path) -> str:
    hunks = [line for line in change.diff().splitlines() if not line.startswith(("---", "+++"))]
    text = "\n".join([
        f"A sandboxed command wants to edit the {change.scope} settings.",
        "",
        f"Requested from: {root}",
        f"File: {change.shown}",
        "",
        *hunks,
    ])
    return "".join(c if c == "\n" or not unprintable(c) else f"\\x{{{ord(c):x}}}" for c in text)


DIALOG = """on run argv
  activate
  set answer to display dialog (item 1 of argv) with title "cperm" buttons {"Deny", "Allow"} default button "Deny" with icon caution giving up after (item 2 of argv as integer)
  return button returned of answer
end run"""


def approve(text: str) -> bool:
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", DIALOG, text, str(DIALOG_TIMEOUT)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "Allow"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


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

    sub.add_parser("basic", parents=[leaf],
                   help="project editing and web search (Edit(/**), WebSearch, allowRead .)")


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
    sub.add_parser("serve", help="run the broker that applies edits from sandboxed commands")
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        if args.cmd == "show":
            return show(args)
        if args.cmd == "serve":
            return serve(args)
        args.scope = args.scope or "local"
        return run_edit(args)
    except UsageError as e:
        print(f"cperm: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
