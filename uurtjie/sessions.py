"""Post-processing: raw entries -> human work sessions.

Raw entries are deliberately fine-grained (15-min chunks, one per Herdr workspace / Chrome
window / timer). For billing they collapse into sessions per project:

  * overlapping entries (parallel Herdr workspaces, timer running during terminal work) merge
  * gaps up to `bridge_minutes` (default = sleep_minutes) are counted as work — switching from
    Herdr to Chrome and back is one session — unless another project was active in that gap
  * human hours = session length, so parallel tasks never double count

Each session carries its tasks (workspaces, branches, page titles, notes), the git commits
made during it, and a one-line summary.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from collections import defaultdict
from datetime import date, datetime

from . import gitinfo
from .config import Config


def split_by_day(start: int, end: int):
    from .report import split_by_day as _s   # local import: report imports this module
    return _s(start, end)


def _detail(r) -> dict:
    d = r.get("detail")
    if isinstance(d, str):
        try:
            return json.loads(d)
        except json.JSONDecodeError:
            return {}
    return d or {}


def _overlaps(intervals: list[tuple[int, int]], s: int, e: int) -> bool:
    return any(a < e and b > s for a, b in intervals)


def build_sessions(rows: list[dict], cfg: Config, clip: tuple[int, int] | None = None) -> list[dict]:
    """rows: spans (dicts) of any projects. Returns sessions sorted by start."""
    pieces: dict[date, list[dict]] = defaultdict(list)
    for r in rows:
        s, e = r["start_ts"], r["end_ts"]
        if clip:
            s, e = max(s, clip[0]), min(e, clip[1])
        if e <= s:
            continue
        for d, ps, pe in split_by_day(s, e):
            pieces[d].append({**r, "start_ts": ps, "end_ts": pe})

    bridge = cfg.bridge_seconds
    sessions = []
    for d, day_rows in sorted(pieces.items()):
        by_proj: dict[str, list[dict]] = defaultdict(list)
        for r in day_rows:
            by_proj[r["project"]].append(r)
        for pid, prow in by_proj.items():
            others = [(r["start_ts"], r["end_ts"]) for r in day_rows if r["project"] != pid]
            prow.sort(key=lambda r: (r["start_ts"], r["end_ts"]))
            cur = None
            for r in prow:
                if cur and (r["start_ts"] <= cur["end"] or
                            (r["start_ts"] - cur["end"] <= bridge
                             and not _overlaps(others, cur["end"], r["start_ts"]))):
                    cur["end"] = max(cur["end"], r["end_ts"])
                    cur["rows"].append(r)
                else:
                    cur = {"project": pid, "date": d, "start": r["start_ts"], "end": r["end_ts"], "rows": [r]}
                    sessions.append(cur)
    for s in sessions:
        s["seconds"] = s["end"] - s["start"]
        _describe(s)
    sessions.sort(key=lambda s: s["start"])
    return sessions


def _describe(s: dict):
    """Collect tasks from the session's entries."""
    tasks: dict[str, dict] = {}
    notes, repos, sources = [], set(), defaultdict(int)
    for r in s["rows"]:
        det = _detail(r)
        sources[r["source"]] += r["end_ts"] - r["start_ts"]
        if r.get("note") and r["note"] not in notes:
            notes.append(r["note"])
        if r["source"] == "terminal":
            label = det.get("workspace_label") or det.get("cwd") or r.get("ctx_key")
            t = tasks.setdefault(f"term:{label}", {"kind": "terminal", "label": label,
                                                   "branches": [], "agents": [], "seconds": 0})
            for b in det.get("branches") or ([det["branch"]] if det.get("branch") else []):
                if b not in t["branches"]:
                    t["branches"].append(b)
            if det.get("agent") and det["agent"] not in t["agents"]:
                t["agents"].append(det["agent"])
            if det.get("repo"):
                repos.add(det["repo"])
        elif r["source"] in ("chrome", "window"):
            label = det.get("class") or r.get("ctx_key")
            t = tasks.setdefault(f"win:{label}", {"kind": r["source"], "label": label,
                                                  "titles": [], "seconds": 0})
            for title in det.get("windows") or ([det["window"]] if det.get("window") else []):
                title = _clean_title(title)
                if title and title not in t["titles"]:
                    t["titles"].append(title)
        elif r["source"] == "meeting":
            app = det.get("app") or det.get("class") or "call"
            t = tasks.setdefault(f"call:{app}", {"kind": "call", "label": f"call ({app})", "titles": [], "seconds": 0})
        else:
            continue
        t["seconds"] += r["end_ts"] - r["start_ts"]
    s["tasks"] = sorted(tasks.values(), key=lambda t: -t["seconds"])
    s["notes"] = notes
    s["repos"] = sorted(repos)
    s["sources"] = dict(sources)
    s["entry_ids"] = [r["id"] for r in s["rows"] if r.get("id")]


def _clean_title(t: str) -> str:
    for suffix in (" - Google Chrome", " – Google Chrome", " - Chromium"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t.strip()[:80]


def attach_commits(sessions: list[dict], cfg: Config):
    """Give each commit to the one session (same project) whose time window contains it."""
    seen: set[str] = set()
    for s in sessions:
        s["commits"] = []
        for repo in s["repos"]:
            for c in gitinfo.commits(repo, s["start"] - 60, s["end"] + cfg.bridge_seconds, cfg.git_author):
                if c["sha"] in seen:
                    continue
                seen.add(c["sha"])
                c["repo"] = repo.rstrip("/").rsplit("/", 1)[-1]
                s["commits"].append(c)
        s["commits"].sort(key=lambda c: c["ts"])


def default_summary(s: dict) -> str:
    parts = list(s["notes"])
    for t in s["tasks"]:
        if t["kind"] == "terminal":
            parts.append(t["label"] + (f' ({", ".join(t["branches"][:2])})' if t["branches"] else ""))
        else:
            parts.append(f'{t["label"]}: ' + ", ".join(t["titles"][:3]) if t["titles"] else t["label"])
    commits = s.get("commits", [])
    if commits:
        subj = [c["subject"] for c in commits[:4]]
        more = f" (+{len(commits) - 4})" if len(commits) > 4 else ""
        parts.append("commits: " + "; ".join(subj) + more)
    return " · ".join(p for p in parts if p) or "—"


def summarize(sessions: list[dict], cfg: Config, con=None):
    """Fill s['summary']. With summary_command, results are cached in the db."""
    for s in sessions:
        s["summary"] = default_summary(s)
        if not cfg.summary_command:
            continue
        payload = session_json(s)
        key = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if con is not None:
            row = con.execute("SELECT summary FROM summaries WHERE key=?", (key,)).fetchone()
            if row:
                s["summary"] = row["summary"]
                continue
        try:
            r = subprocess.run(cfg.summary_command, input=json.dumps(payload), capture_output=True,
                               text=True, timeout=90)
            line = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else ""
        except (OSError, subprocess.TimeoutExpired):
            line = ""
        if line:
            s["summary"] = line[:300]
            if con is not None:
                con.execute("INSERT OR REPLACE INTO summaries(key, summary, created_at) VALUES (?,?,strftime('%s'))",
                            (key, s["summary"]))


def session_json(s: dict) -> dict:
    return {
        "project": s["project"], "date": s["date"].isoformat(),
        "start": datetime.fromtimestamp(s["start"]).strftime("%H:%M"),
        "end": datetime.fromtimestamp(s["end"]).strftime("%H:%M"),
        "hours": round(s["seconds"] / 3600, 2),
        "notes": s["notes"],
        "tasks": [{k: v for k, v in t.items() if k != "seconds"} | {"hours": round(t["seconds"] / 3600, 2)}
                  for t in s["tasks"]],
        "commits": [{k: c[k] for k in ("sha", "repo", "subject")} for c in s.get("commits", [])],
        "entry_ids": s["entry_ids"],
        **({"summary": s["summary"]} if "summary" in s else {}),
    }
