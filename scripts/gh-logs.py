#!/usr/bin/env python3
"""Download a GitHub Actions run - logs, metadata, and jobs - into a local dir.

Picks the most recent run matching the filters and downloads the per-attempt log
zip (one compressed request per attempt). The zip's filenames can't be mapped to
job ids directly (the prefix is an opaque ordinal - see cli/cli's logs.go), so
each entry is matched to a job by string similarity and saved as <job_id>.txt.
Low-confidence matches keep the raw zip name and print a warning.

A run must be selected: give a run id, or narrow with -b/-n (latest match wins).

Examples:
    gh-logs 26841725448             # a specific run by id
    gh-logs -b main                 # latest run on branch main
    gh-logs -n Deploy               # latest run whose name/workflow has "Deploy"
    gh-logs -b main -n CI           # both filters
    gh-logs -R owner/repo -b dev    # a different repo

Output: <outdir>/<owner>/<repo>/run-<id>/
    summary.md                      # human-readable run + per-attempt summary
    run.json                        # run metadata
    jobs.json                       # all jobs + steps across attempts
    attempt-<N>/run.json            # that attempt's metadata
    attempt-<N>/<job_id>.txt        # job logs, named by job id
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

MATCH_THRESHOLD = 0.6  # min similarity to rename a zip log to <job_id>.txt


class GhError(subprocess.CalledProcessError):
    """A gh invocation that exited non-zero, carrying its stderr text."""


def gh(*args: str) -> bytes:
    """Run a gh CLI command and return its raw stdout bytes."""
    result = subprocess.run(["gh", *args], capture_output=True)
    if result.returncode != 0:
        raise GhError(result.returncode, ["gh", *args], result.stdout, result.stderr.decode(errors="replace"))
    return result.stdout


def gh_json(path: str, paginate: bool = False) -> object:
    args = ["api"]
    if paginate:
        args += ["--paginate", "--slurp"]
    args.append(path)
    return json.loads(gh(*args))


def write_json(path: Path, obj: object) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def match_logs(entries: list[str], candidates: list[dict]) -> list[tuple[str, dict | None, float]]:
    """Greedily pair each zip entry with its most similar job (unique 1:1).

    Zip names are "<ordinal>_<sanitized job name>.txt"; the sanitization (truncation,
    char-stripping) is undocumented, so we compare on string similarity rather than
    trying to reproduce it. Returns (entry, job_or_None, ratio) for every entry.
    """
    cores = {e: _norm(e[:-4].split("_", 1)[-1]) for e in entries}
    njobs = [(j, _norm(j["name"])) for j in candidates]
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, cores[e], nj).ratio(), e, j)
            for e in entries
            for j, nj in njobs
        ),
        key=lambda t: t[0],
        reverse=True,  # highest ratio first
    )
    best: dict[str, tuple[dict, float]] = {}
    used_entries: set[str] = set()
    used_jobs: set[int] = set()
    for ratio, e, j in scored:
        if e in used_entries or j["id"] in used_jobs:
            continue
        best[e] = (j, ratio)
        used_entries.add(e)
        used_jobs.add(j["id"])
    return [(e, *best.get(e, (None, 0.0))) for e in entries]


def start_times(jobs: list[dict]) -> dict:
    """(attempt, job name) -> start time, used to detect carried-over jobs."""
    return {(j["run_attempt"], j["name"]): j.get("started_at") for j in jobs}


def carried_over(started: dict, n: int, job: dict) -> bool:
    """True if the job is unchanged from the previous attempt (same start time)."""
    prev = (n - 1, job["name"])
    return n > 1 and prev in started and job.get("started_at") == started[prev]


def pick_run(repo: str, branch: str | None, name: str | None) -> dict:
    fields = "databaseId,displayTitle,workflowName,headBranch,createdAt"
    args = ["run", "list", "-R", repo, "-L", "100", "--json", fields]
    if branch:
        args += ["--branch", branch]
    runs = json.loads(gh(*args))  # gh returns newest-first
    for run in runs:
        if name and name not in run["displayTitle"] and name not in run["workflowName"]:
            continue
        return run
    sys.exit("no run matched those filters")


ICONS = {
    "success": "✅", "failure": "❌", "cancelled": "🚫", "skipped": "⏭️",
    "timed_out": "⏱️", "neutral": "➖", "startup_failure": "💥", "action_required": "⚠️",
}


def icon(conclusion: str | None, status: str | None) -> str:
    if status and status != "completed":
        return "🟡"  # queued / in_progress
    return ICONS.get(conclusion, "❔")


def fmt_dt(ts: str | None) -> str:
    return ts.replace("T", " ")[:16] + " UTC" if ts else "-"


def duration(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "-"
    delta = datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))
    secs = int(delta.total_seconds())
    if secs < 0:  # skipped jobs carry bogus start/end timestamps
        return "-"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m}m" if h else f"{m}m{s}s" if m else f"{s}s"


def cell(text: str) -> str:
    return (text or "").replace("|", "\\|")


def write_summary(run_dir: Path, run_meta: dict, jobs: list[dict]) -> None:
    actor = (run_meta.get("triggering_actor") or run_meta.get("actor") or {}).get("login", "?")
    repo_url = run_meta.get("repository", {}).get("html_url", "")
    branch = run_meta.get("head_branch")
    full_sha = run_meta.get("head_sha") or ""
    verdict = run_meta.get("conclusion") or run_meta["status"]
    out = [
        f"# {run_meta.get('display_title') or run_meta.get('name')}",
        "",
        f"**{icon(run_meta.get('conclusion'), run_meta['status'])} {verdict}** · {run_meta['run_attempt']} attempt(s)",
        "",
        f"- **Run:** [{run_meta['id']}]({run_meta['html_url']})",
        f"- **Trigger:** `{run_meta['event']}` by @{actor}",
        f"- **Branch:** [`{branch}`]({repo_url}/tree/{branch}) @ [`{full_sha[:8]}`]({repo_url}/commit/{full_sha})",
        f"- **Ran:** {fmt_dt(run_meta.get('run_started_at'))} → {fmt_dt(run_meta.get('updated_at'))}",
        "",
    ]
    by_attempt: dict[int, list[dict]] = {}
    for j in jobs:
        by_attempt.setdefault(j["run_attempt"], []).append(j)
    started = start_times(jobs)

    for n in sorted(by_attempt):
        attempt_dir = run_dir / f"attempt-{n}"
        am = json.loads((attempt_dir / "run.json").read_text()) if (attempt_dir / "run.json").exists() else {}
        verdict = am.get("conclusion") or am.get("status") or ""
        out.append(f"## [Attempt {n}]({run_meta['html_url']}/attempts/{n}) — {icon(am.get('conclusion'), am.get('status'))} {verdict}".rstrip())
        if am.get("run_started_at"):
            out.append(f"started {fmt_dt(am['run_started_at'])}")
        out += ["", "| Job | Result | Duration | Failing steps |", "|---|---|---|---|"]
        for j in sorted(by_attempt[n], key=lambda j: j["id"]):
            # In successive attempts, show only what actually re-ran:
            # drop jobs carried over unchanged (same start time as the prior attempt)
            # and drop skipped jobs entirely.
            if n > 1 and (carried_over(started, n, j) or j.get("conclusion") == "skipped"):
                continue
            failing = [s["name"] for s in j.get("steps") or [] if s.get("conclusion") not in (None, "success", "skipped")]
            log_link = f" ([log](attempt-{n}/{j['id']}.txt))" if (attempt_dir / f"{j['id']}.txt").exists() else ""
            out.append(
                f"| [{cell(j['name'])}]({j['html_url']}){log_link} "
                f"| {icon(j['conclusion'], j['status'])} {j.get('conclusion') or j['status']} "
                f"| {duration(j.get('started_at'), j.get('completed_at'))} "
                f"| {cell(', '.join(failing))} |"
            )
        out.append("")
    (run_dir / "summary.md").write_text("\n".join(out) + "\n")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("run_id", nargs="?", type=int, help="run id (default: pick by filters)")
    p.add_argument("-b", "--branch", help="filter by head branch")
    p.add_argument("-n", "--name", help="substring of run name or workflow name")
    p.add_argument("-R", "--repo", help="owner/name (default: current repo)")
    p.add_argument("-f", "--force", action="store_true", help="re-download attempts already saved as completed")
    p.add_argument(
        "-o",
        "--outdir",
        default=os.path.join(
            os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"),
            "gh-logs",
        ),
        help="output base dir (default: $XDG_CACHE_HOME/gh-logs or ~/.cache/gh-logs)",
    )
    args = p.parse_args()

    if not args.run_id and not args.branch and not args.name:
        p.error("select a run: give a run id, or narrow with -b/-n")

    repo = args.repo
    if not repo:
        try:
            repo = gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").decode().strip()
        except GhError as e:
            sys.exit(f"could not determine the repo - pass -R owner/repo\n  {e.stderr.strip()}")

    if args.run_id:
        run_id = args.run_id
    else:
        run_id = pick_run(repo, args.branch, args.name)["databaseId"]

    run_meta = gh_json(f"repos/{repo}/actions/runs/{run_id}")

    run_dir = Path(args.outdir) / repo / f"run-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Run metadata.
    write_json(run_dir / "run.json", run_meta)

    # Jobs + steps (every job across every attempt).
    pages = gh_json(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100&filter=all", paginate=True)
    jobs = [job for page in pages for job in page["jobs"]]
    # Stable order: by attempt, then by job id (creation order within an attempt).
    jobs.sort(key=lambda j: (j["run_attempt"], j["id"]))
    write_json(run_dir / "jobs.json", jobs)

    # Per attempt: metadata + the log zip, each entry matched to a job and named by id.
    # A completed attempt's logs are immutable, so skip it if already saved. run.json is
    # the "done" marker, written ONLY after the logs land - so its presence with status
    # "completed" truly means the logs are on disk. A failed download leaves no marker,
    # so the attempt keeps showing as incomplete and is retried next run.
    for n in range(1, run_meta["run_attempt"] + 1):
        dest_dir = run_dir / f"attempt-{n}"
        meta_path = dest_dir / "run.json"
        if not args.force and meta_path.exists() and json.loads(meta_path.read_text()).get("status") == "completed":
            continue
        dest_dir.mkdir(exist_ok=True)
        meta = gh_json(f"repos/{repo}/actions/runs/{run_id}/attempts/{n}")
        try:
            data = gh("api", f"repos/{repo}/actions/runs/{run_id}/attempts/{n}/logs")
        except GhError as e:
            print(f"  attempt-{n}: no logs ({e.stderr.strip()})", file=sys.stderr)
            continue
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            entries = [x for x in zf.namelist() if "/" not in x]  # top-level = per-job
            candidates = [j for j in jobs if j["run_attempt"] == n]
            for entry, job, ratio in match_logs(entries, candidates):
                if job and ratio >= MATCH_THRESHOLD:
                    (dest_dir / f"{job['id']}.txt").write_bytes(zf.read(entry))
                else:
                    (dest_dir / entry).write_bytes(zf.read(entry))  # keep raw name
        write_json(meta_path, meta)  # marker last: a skip now means logs are present

    write_summary(run_dir, run_meta, jobs)
    print(run_dir)


if __name__ == "__main__":
    try:
        main()
    except GhError as e:
        msg = (e.stderr or "").strip() or f"exited {e.returncode}"
        sys.exit(f"{' '.join(e.cmd)}\n  {msg}")
    except KeyboardInterrupt:
        sys.exit(130)
