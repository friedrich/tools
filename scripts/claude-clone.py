#!/usr/bin/env python3
"""Copy a Claude Code conversation from one folder to another.

Claude Code stores each conversation as ``<configDir>/projects/<enc>/<sid>.jsonl``
where ``<enc>`` is the working-folder path with every non-alphanumeric character
turned into ``-`` (long names get truncated to 200 chars + a hash — see ``encode``,
which mirrors the ``claude`` binary exactly). A conversation may also have a sidecar
``<sid>/`` directory holding ``tool-results/`` and ``subagents/``.

This tool copies a conversation so it shows up (and resumes) under a different
folder: it copies the transcript and any sidecar dir into the destination folder's
project directory under a fresh session id. What attaches the conversation to the
new folder is living in that folder's project dir — so the history is left as-is
(cwd and embedded paths untouched); only the ``sessionId`` field is updated to the
new id.

It also copies ``file-history/<sid>/`` — the raw file snapshots powering
checkpoint/rewind — unless ``--no-file-history`` is given.

Both ends may be remote, given scp-style as ``[user@]host:...``:

* destination ``host:path`` — the copy is staged locally and streamed over ssh
  (``tar | zstd -1 | ssh 'zstd -d | tar -x'``) into the remote Claude config dir,
  resolved over ssh as ``${CLAUDE_CONFIG_DIR:-$HOME/.claude}``. The remote path
  may be relative to the remote home, like scp.
* source ``host:SESSION`` (or ``--from host:path``) — the conversation is pulled
  from that host: this very script is streamed to the remote's ``python3`` over
  ssh stdin and run in a hidden helper mode that finds the session in the remote
  config dir and writes it back as a tar.gz (stdlib only — the remote needs
  nothing but python3). ``--list --from host:path`` lists remote conversations
  the same way.

It never touches ``session-env/`` (ephemeral live-shell state) or a project's
``memory/`` (shared across the project, not per-conversation).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path


# --- project-dir name encoding (must match the claude binary) ----------------

_MAX = 200  # _Bt in the binary


def _java_hashcode(s: str) -> int:
    """java.lang.String.hashCode(): h = 31*h + c, truncated to signed 32-bit."""
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return h


def _base36(n: int) -> str:
    n = abs(n)
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def encode(folder_abs: str) -> str:
    """Encode an absolute folder path to its projects/ subdirectory name."""
    sanitized = "".join(c if c.isascii() and c.isalnum() else "-" for c in folder_abs)
    if len(sanitized) <= _MAX:
        return sanitized
    return f"{sanitized[:_MAX]}-{_base36(_java_hashcode(folder_abs))}"


def config_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


# --- locating the source project directory -----------------------------------

def project_dir_for(folder_abs: str, cfg: Path) -> Path:
    """Return the projects/ subdir for a folder.

    Prefers the deterministic encoding, but because that encoding is lossy
    (``/`` and ``.`` both map to ``-``), falls back to scanning existing project
    dirs for one whose sessions actually record this cwd.
    """
    candidate = cfg / "projects" / encode(folder_abs)
    if candidate.is_dir():
        return candidate
    match = _find_dir_by_cwd(folder_abs, cfg)
    return match if match is not None else candidate


def _find_dir_by_cwd(folder_abs: str, cfg: Path) -> Path | None:
    root = cfg / "projects"
    if not root.is_dir():
        return None
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        for jf in d.glob("*.jsonl"):
            if _first_cwd(jf) == folder_abs:
                return d
            break  # one session per dir is enough to identify it
    return None


def _first_cwd(jsonl: Path) -> str | None:
    try:
        with jsonl.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("cwd"):
                    return obj["cwd"]
    except OSError:
        pass
    return None


# --- session model ------------------------------------------------------------

class Session:
    def __init__(self, jsonl: Path, cfg: Path):
        self.jsonl = jsonl
        self.cfg = cfg  # config dir this session lives under (real or staged)
        self.sid = jsonl.stem
        self.sidecar = jsonl.with_suffix("")  # <dir>/<sid>/
        self.origin = str(jsonl)  # display label; "host:sid" for pulled sessions

    @property
    def mtime(self) -> float:
        return self.jsonl.stat().st_mtime

    @property
    def file_history(self) -> Path:
        return self.cfg / "file-history" / self.sid

    def first_prompt(self) -> str:
        try:
            with self.jsonl.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict) or obj.get("type") != "user":
                        continue
                    msg = obj.get("message")
                    content = msg.get("content") if isinstance(msg, dict) else None
                    text = None
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text = part.get("text")
                                break
                    if text:
                        text = " ".join(text.split())
                        if not text.startswith("<"):  # skip system-reminder-only turns
                            return text
        except OSError:
            pass
        return ""


def sessions_in(project_dir: Path, cfg: Path) -> list[Session]:
    if not project_dir.is_dir():
        return []
    return sorted(
        (Session(p, cfg) for p in project_dir.glob("*.jsonl")),
        key=lambda s: s.mtime,
    )


def all_sessions(cfg: Path) -> list[Session]:
    """Every conversation across all project dirs (session lookup isn't cwd-bound)."""
    root = cfg / "projects"
    out: list[Session] = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir():
                out.extend(sessions_in(d, cfg))
    return out


def pick_session(sessions: list[Session], selector: str) -> Session:
    if not sessions:
        sys.exit("error: no conversations found")
    matches = [s for s in sessions if s.sid == selector] or \
              [s for s in sessions if s.sid.startswith(selector)]
    if not matches:
        sys.exit(f"error: no session matching {selector!r}")
    if len(matches) > 1:
        ids = ", ".join(s.sid[:8] for s in matches)
        sys.exit(f"error: {selector!r} is ambiguous ({ids}) — give more characters")
    return matches[0]


def find_sessions(src_folder: str | None, cfg: Path) -> list[Session]:
    """Search everywhere, or narrow to a folder when one is given."""
    if src_folder is not None:
        return sessions_in(project_dir_for(resolve_folder(src_folder), cfg), cfg)
    return all_sessions(cfg)


# --- copy + rewrite -----------------------------------------------------------

def rewrite_jsonl(src: Path, dst: Path, old_sid: str, new_sid: str) -> None:
    """Copy a jsonl line-by-line, rewriting only the sessionId field.

    The cwd and any embedded absolute paths are left untouched — the conversation
    is attached to its new folder by living in that folder's project dir, not by
    relabelling its history. Unchanged lines are copied verbatim (byte-preserving)
    so only the records that actually need editing are re-serialised.
    """
    with src.open(encoding="utf-8") as fin, dst.open("w", encoding="utf-8") as fout:
        for line in fin:
            stripped = line.rstrip("\n")
            if not stripped:
                fout.write(line)
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                fout.write(line)
                continue
            if isinstance(obj, dict) and obj.get("sessionId") == old_sid:
                obj["sessionId"] = new_sid
                fout.write(json.dumps(obj, ensure_ascii=False,
                                      separators=(",", ":")) + "\n")
            else:
                fout.write(line)


def write_conversation(sess: Session, out_jsonl: Path, out_sidecar: Path,
                       out_fh: Path, new_sid: str, with_file_history: bool) -> None:
    """Materialise the copied conversation at explicit target paths.

    Used for both local writes (targets under the real config dir) and remote
    ones (targets under a temp staging tree that is then shipped over ssh).
    """
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    rewrite_jsonl(sess.jsonl, out_jsonl, sess.sid, new_sid)

    if sess.sidecar.is_dir():
        if out_sidecar.exists():
            shutil.rmtree(out_sidecar)
        shutil.copytree(sess.sidecar, out_sidecar)
        # rewrite the sessionId inside subagent transcripts too
        subdir = out_sidecar / "subagents"
        if subdir.is_dir():
            for sub in subdir.glob("*.jsonl"):
                tmp = sub.with_suffix(".jsonl.tmp")
                rewrite_jsonl(sub, tmp, sess.sid, new_sid)
                tmp.replace(sub)

    if with_file_history and sess.file_history.is_dir():
        if out_fh.exists():
            shutil.rmtree(out_fh)
        out_fh.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(sess.file_history, out_fh)


def describe_extras(sess: Session, with_file_history: bool) -> None:
    if sess.sidecar.is_dir():
        subs = sess.sidecar / "subagents"
        n = len(list(subs.glob("*.jsonl"))) if subs.is_dir() else 0
        print(f"  sidecar dir: yes ({n} subagent transcript(s))")
    if with_file_history and sess.file_history.is_dir():
        n = sum(1 for _ in sess.file_history.rglob("*@v*"))
        print(f"  file-history: yes ({n} snapshot(s))")


def copy_local(sess: Session, dst_folder: str, *,
               with_file_history: bool, dry_run: bool) -> None:
    new_sid = str(uuid.uuid4())
    dst_project = project_dir_for(dst_folder, config_dir())
    out_jsonl = dst_project / f"{new_sid}.jsonl"

    print(f"  source:      {sess.origin}")
    print(f"  destination: {out_jsonl}")
    print(f"  session id:  {sess.sid}  ->  {new_sid}")
    describe_extras(sess, with_file_history)

    if dry_run:
        print("  (dry run — nothing written)")
        return

    write_conversation(sess, out_jsonl, dst_project / new_sid,
                       config_dir() / "file-history" / new_sid,
                       new_sid, with_file_history)
    print("  done.")


def _ssh_capture(host: str, remote_cmd: str) -> str:
    try:
        r = subprocess.run(["ssh", host, remote_cmd],
                           capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("error: ssh not found on this machine")
    if r.returncode != 0:
        sys.exit(f"error: ssh {host} failed: "
                 f"{r.stderr.strip() or ('exit ' + str(r.returncode))}")
    return r.stdout


def copy_remote(sess: Session, host: str, remote_folder: str, *,
                with_file_history: bool, dry_run: bool) -> None:
    new_sid = str(uuid.uuid4())

    # One round trip: resolve the remote config dir, expand the destination folder
    # to an absolute path (relative/~ are taken against the remote home, like scp),
    # and check whether it exists.
    probe = (
        r'printf "%s\n" "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"; '
        "p=" + shlex.quote(remote_folder) + "; "
        r'case "$p" in /*) : ;; "~") p="$HOME" ;; "~/"*) p="$HOME/${p#"~/"}" ;; '
        r'*) p="$HOME/$p" ;; esac; '
        r'printf "%s\n" "$p"; '
        r'test -d "$p" && echo DIR_OK || echo DIR_NO'
    )
    lines = _ssh_capture(host, probe).splitlines()
    if len(lines) < 2 or not lines[0].strip() or not lines[1].strip():
        sys.exit(f"error: could not resolve paths on {host}")
    remote_cfg = lines[0].strip()
    remote_abs = lines[1].strip()
    enc = encode(remote_abs)
    remote_proj = f"{remote_cfg}/projects/{enc}"

    print(f"  source:        {sess.origin}")
    print(f"  remote folder: {host}:{remote_abs}")
    print(f"  destination:   {remote_proj}/{new_sid}.jsonl")
    print(f"  session id:    {sess.sid}  ->  {new_sid}")
    describe_extras(sess, with_file_history)
    if "DIR_OK" not in lines:
        print(f"warning: {host}:{remote_abs} does not exist "
              f"(the conversation will still be created for it)", file=sys.stderr)

    if dry_run:
        print("  (dry run — nothing written)")
        return

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        proj = root / "projects" / enc
        write_conversation(sess, proj / f"{new_sid}.jsonl", proj / new_sid,
                           root / "file-history" / new_sid,
                           new_sid, with_file_history)
        # tar | zstd -1 (fast) | ssh 'zstd -d | tar -x'
        remote_cmd = (f"mkdir -p {shlex.quote(remote_cfg)} && "
                      f"zstd -dc | tar -C {shlex.quote(remote_cfg)} -xf -")
        tar = subprocess.Popen(["tar", "-C", str(root), "-cf", "-", "."],
                               stdout=subprocess.PIPE)
        zst = subprocess.Popen(["zstd", "-1", "-c"],
                               stdin=tar.stdout, stdout=subprocess.PIPE)
        tar.stdout.close()  # let tar receive SIGPIPE if zstd dies
        ssh = subprocess.Popen(["ssh", host, remote_cmd], stdin=zst.stdout)
        zst.stdout.close()
        ssh_rc = ssh.wait()
        zst_rc = zst.wait()
        tar_rc = tar.wait()
        if tar_rc != 0:
            sys.exit(f"error: staging tar failed (exit {tar_rc})")
        if zst_rc != 0:
            sys.exit(f"error: zstd failed (exit {zst_rc})")
        if ssh_rc != 0:
            sys.exit(f"error: transfer to {host} failed (ssh exit {ssh_rc})")

    print("  done.")


# --- remote source (pull) -----------------------------------------------------
#
# The remote end runs this very script: it is streamed to the remote's python3
# over ssh stdin ("python3 -") and invoked in --remote-helper mode, so the pull
# needs nothing installed remotely beyond python3. "emit" writes the selected
# conversation as a tar.gz on stdout with paths relative to the remote config
# dir; "list" prints the folder's conversations for a human.

def _run_remote_helper(host: str, helper_args: list[str],
                       capture: bool) -> subprocess.Popen:
    remote_cmd = " ".join(shlex.quote(a) for a in
                          ["python3", "-", "--remote-helper", *helper_args])
    try:
        script = open(__file__, "rb")
    except OSError as e:
        sys.exit(f"error: cannot read own source to send to {host}: {e}")
    with script:
        try:
            return subprocess.Popen(
                ["ssh", host, remote_cmd], stdin=script,
                stdout=subprocess.PIPE if capture else None)
        except FileNotFoundError:
            sys.exit("error: ssh not found on this machine")


def pull_remote(host: str, selector: str, src_folder: str | None,
                with_file_history: bool, staging: Path) -> Session:
    """Fetch a conversation from a remote host into a local staging tree.

    The staging tree is shaped like a config dir (projects/..., file-history/...),
    so the returned Session behaves exactly like a local one and any destination
    (local or remote) can be fed from it.
    """
    helper = ["emit", selector]
    if src_folder is not None:
        helper += ["--from", src_folder]
    if not with_file_history:
        helper.append("--no-file-history")

    print(f"fetching from {host}:")
    proc = _run_remote_helper(host, helper, capture=True)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|gz") as tf:
            try:
                tf.extractall(staging, filter="data")
            except TypeError:  # python < 3.12: no filter=
                tf.extractall(staging)
    except tarfile.ReadError:
        proc.wait()
        sys.exit(f"error: fetch from {host} failed (ssh exit {proc.returncode})")
    if proc.wait() != 0:
        sys.exit(f"error: fetch from {host} failed (ssh exit {proc.returncode})")

    jsonls = list((staging / "projects").glob("*/*.jsonl"))
    if len(jsonls) != 1:
        sys.exit(f"error: unexpected payload from {host} "
                 f"({len(jsonls)} transcript(s) received)")
    sess = Session(jsonls[0], staging)
    sess.origin = f"{host}:{sess.sid}"
    return sess


def list_remote(host: str, folder: str) -> None:
    rc = _run_remote_helper(host, ["list", folder], capture=False).wait()
    if rc != 0:
        sys.exit(rc)


def helper_emit(selector: str, src_folder: str | None,
                with_file_history: bool) -> None:
    cfg = config_dir()
    sess = pick_session(find_sessions(src_folder, cfg), selector)
    print(f"  remote source: {sess.jsonl}", file=sys.stderr)
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tf:
        tf.add(sess.jsonl, arcname=str(sess.jsonl.relative_to(cfg)))
        if sess.sidecar.is_dir():
            tf.add(sess.sidecar, arcname=str(sess.sidecar.relative_to(cfg)))
        if with_file_history and sess.file_history.is_dir():
            tf.add(sess.file_history, arcname=f"file-history/{sess.sid}")


# --- CLI ----------------------------------------------------------------------

def resolve_folder(p: str) -> str:
    return os.path.abspath(os.path.expanduser(p))


def parse_dest(dest: str) -> tuple[str | None, str | None]:
    """Split a destination into (host, remote_path), scp-style.

    ``host:path`` or ``user@host:path`` is remote (the path is resolved on the
    remote, relative to its home like scp); anything else — including local paths,
    which contain a ``/`` before any ``:`` — is local.
    """
    head, sep, tail = dest.partition(":")
    if sep and head and "/" not in head:
        return head, tail
    return None, None


def cmd_list(src_folder: str, src_project: Path, cfg: Path) -> None:
    sessions = sessions_in(src_project, cfg)
    if not sessions:
        sys.exit(f"no conversations found for {src_folder}\n"
                 f"(looked in {src_project})")
    print(f"conversations attached to {src_folder}:\n")
    import datetime
    for s in sessions:
        when = datetime.datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M")
        prompt = s.first_prompt() or "(no user prompt)"
        if len(prompt) > 70:
            prompt = prompt[:69] + "…"
        print(f"  {s.sid}  {when}  {prompt}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="claude-clone",
        description="Clone a Claude Code conversation into another folder; "
                    "either end may be a remote host over ssh.")
    ap.add_argument("session", nargs="?", metavar="SESSION",
                    help="session id or prefix to copy, 'current' for the "
                         "running session, or [user@]host:SESSION to pull from "
                         "a remote machine (required unless --list)")
    ap.add_argument("dest", nargs="?", metavar="DEST",
                    help="destination folder, or [user@]host:path for a remote "
                         "machine over ssh (remote path may be relative to the "
                         "remote home, like scp)")
    ap.add_argument("--from", dest="src", default=None, metavar="SRC_FOLDER",
                    help="only look for the session under this folder, "
                         "[user@]host:path for a folder on a remote machine "
                         "(default: search all conversations)")
    ap.add_argument("--list", action="store_true",
                    help="list conversations for a folder (--from, or cwd) and exit")
    ap.add_argument("--no-file-history", dest="file_history", action="store_false",
                    help="don't copy file-history/ rewind snapshots (copied by default)")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="show what would happen without writing anything")
    ap.add_argument("--remote-helper", choices=("emit", "list"),
                    help=argparse.SUPPRESS)  # internal: run on the far end of a pull
    args = ap.parse_args(argv)

    if args.remote_helper == "emit":
        if not args.session:
            ap.error("--remote-helper emit requires a session selector")
        helper_emit(args.session, args.src, args.file_history)
        return
    if args.remote_helper == "list":
        folder = resolve_folder(args.session or os.getcwd())
        cmd_list(folder, project_dir_for(folder, config_dir()), config_dir())
        return

    if args.list:
        host, folder = parse_dest(args.src) if args.src is not None else (None, None)
        if host is not None:
            list_remote(host, folder)
            return
        src_folder = resolve_folder(args.src or os.getcwd())
        cmd_list(src_folder, project_dir_for(src_folder, config_dir()), config_dir())
        return

    if not args.session:
        ap.error("SESSION is required (use --list to see session ids)")
    if not args.dest:
        ap.error("DEST is required")

    # The source host may be spelled on the session (host:SESSION) or on
    # --from (host:path); they must agree if both are given.
    sess_host, sess_sel = parse_dest(args.session)
    from_host, from_path = (parse_dest(args.src) if args.src is not None
                            else (None, None))
    if sess_host and from_host and sess_host != from_host:
        ap.error(f"conflicting source hosts {sess_host!r} and {from_host!r}")
    src_host = sess_host or from_host
    selector = sess_sel if sess_host else args.session
    src_folder = from_path if from_host else args.src

    if selector == "current":
        if src_host is not None:
            sys.exit("error: SESSION 'current' refers to the local running "
                     "session and cannot be pulled from a remote host")
        selector = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not selector:
            sys.exit("error: SESSION 'current' requires CLAUDE_CODE_SESSION_ID to "
                     "be set (run from inside a Claude Code session)")

    with tempfile.TemporaryDirectory() as staging:
        if src_host is not None:
            sess = pull_remote(src_host, selector, src_folder,
                               args.file_history, Path(staging))
        else:
            sess = pick_session(find_sessions(src_folder, config_dir()), selector)

        dst_host, remote_folder = parse_dest(args.dest)
        print("cloning conversation:")
        if dst_host is not None:
            copy_remote(sess, dst_host, remote_folder,
                        with_file_history=args.file_history, dry_run=args.dry_run)
        else:
            dst_folder = resolve_folder(args.dest)
            if not os.path.isdir(dst_folder):
                print(f"warning: destination folder {dst_folder} does not exist "
                      f"(the conversation will still be created for it)",
                      file=sys.stderr)
            copy_local(sess, dst_folder,
                       with_file_history=args.file_history, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
