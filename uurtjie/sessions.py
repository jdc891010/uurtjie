"""Resolve evidence ownership, then group strict human intervals into work sessions.

Manual time takes precedence over meetings, then focused windows. Short sampling gaps
can group sessions without adding time. Configured gap billing is invoice-only.
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


def evidence_segments(rows: list[dict], clip: tuple[int, int] | None = None) -> list[dict]:
    """Disjoint evidence intervals. Ties use earliest start, then project and entry ID."""
    priority = {"manual": 3, "meeting": 2, "terminal": 1, "chrome": 1, "window": 1}
    events = defaultdict(list)
    ranks = {}
    for index, row in enumerate(rows):
        if row["source"] not in priority:
            continue
        start, end = row["start_ts"], row["end_ts"]
        ranks[index] = (-priority[row["source"]], start, row["project"], row.get("id") or 0, index)
        if clip:
            start, end = max(start, clip[0]), min(end, clip[1])
        if end > start:
            events[start].append((index, row))
            events[end].append((index, None))
    active, segments = {}, []
    edges = sorted(events)
    for start, end in zip(edges, edges[1:]):
        for index, row in events[start]:
            if row is None:
                active.pop(index, None)
            else:
                active[index] = row
        if not active:
            continue
        winner = min(active, key=ranks.__getitem__)
        project = active[winner]["project"]
        contributing = [{**r, "start_ts": start, "end_ts": end, "credited": i == winner}
                        for i, r in active.items() if r["project"] == project]
        segments.append({"project": project, "start": start, "end": end, "rows": contributing,
                         "billable": all(r.get("billable", 1) for r in contributing)})
    return segments


def _append_interval(intervals: list, start: int, end: int):
    if intervals and intervals[-1][1] == start:
        intervals[-1] = (intervals[-1][0], end)
    else:
        intervals.append((start, end))


def build_sessions(rows: list[dict], cfg: Config, clip: tuple[int, int] | None = None,
                   billable_only: bool = False, invoice: bool = False) -> list[dict]:
    """Group resolved evidence; only invoice billing intervals may include configured gaps."""
    pieces = defaultdict(list)
    for segment in evidence_segments(rows, clip):
        for day, start, end in split_by_day(segment["start"], segment["end"]):
            pieces[day].append({**segment, "start": start, "end": end,
                                "rows": [{**r, "start_ts": start, "end_ts": end} for r in segment["rows"]]})
    bridge = cfg.bridge_seconds if invoice else 0
    tolerance = max(0, cfg.tick, bridge)
    sessions = []
    for day, segments in sorted(pieces.items()):
        by_project = defaultdict(list)
        for segment in segments:
            if not billable_only or segment["billable"]:
                by_project[segment["project"]].append(segment)
        for project, own in by_project.items():
            blockers = [(s["start"], s["end"]) for s in segments
                        if s["project"] != project or (billable_only and not s["billable"])]
            current = None
            for segment in own:
                start, end = segment["start"], segment["end"]
                joins = (current and start - current["end"] <= tolerance
                         and not _overlaps(blockers, current["end"], start))
                if not joins:
                    current = {"project": project, "date": day, "start": start, "end": start,
                               "rows": [], "intervals": [], "billing_intervals": []}
                    sessions.append(current)
                elif 0 < start - current["end"] <= bridge:
                    _append_interval(current["billing_intervals"], current["end"], start)
                _append_interval(current["intervals"], start, end)
                _append_interval(current["billing_intervals"], start, end)
                current["end"] = end
                current["rows"].extend(segment["rows"])
    for session in sessions:
        session["seconds"] = sum(end - start for start, end in session["intervals"])
        session["billing_seconds"] = sum(end - start for start, end in session["billing_intervals"])
        _describe(session)
    sessions.sort(key=lambda s: (s["start"], s["project"]))
    return sessions


def _describe(s: dict):
    """Collect tasks from the session's entries."""
    tasks: dict[str, dict] = {}
    notes, repos, sources = [], set(), defaultdict(int)
    for r in s["rows"]:
        det = _detail(r)
        seconds = r["end_ts"] - r["start_ts"] if r.get("credited", True) else 0
        sources[r["source"]] += seconds
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
        t["seconds"] += seconds
    s["tasks"] = sorted(tasks.values(), key=lambda t: -t["seconds"])
    s["notes"] = notes
    s["repos"] = sorted(repos)
    s["sources"] = dict(sources)
    s["entry_ids"] = list(dict.fromkeys(r["id"] for r in s["rows"] if r.get("id")))


def _clean_title(t: str) -> str:
    for suffix in (" - Google Chrome", " – Google Chrome", " - Chromium"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    return t.strip()[:80]


def attach_commits(sessions: list[dict], cfg: Config, invoice: bool = False):
    """Give each commit to the one session (same project) whose time window contains it."""
    seen: set[str] = set()
    for s in sessions:
        s["commits"] = []
        for repo in s["repos"]:
            for c in gitinfo.commits(repo, s["start"] - 60,
                                     s["end"] + (cfg.bridge_seconds if invoice else 0), cfg.git_author):
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
        "hours": s["seconds"] / 3600, "seconds": s["seconds"],
        "intervals": s["intervals"], "billing_seconds": s["billing_seconds"],
        "notes": s["notes"],
        "tasks": [{k: v for k, v in t.items() if k != "seconds"} | {"hours": round(t["seconds"] / 3600, 2)}
                  for t in s["tasks"]],
        "commits": [{k: c[k] for k in ("sha", "repo", "subject")} for c in s.get("commits", [])],
        "entry_ids": s["entry_ids"],
        **({"summary": s["summary"]} if "summary" in s else {}),
    }
