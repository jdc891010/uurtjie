"""Aggregation: per-day union of spans (no double counting), rounding, monthly invoice data."""
from __future__ import annotations

import csv
import io
import json
import math
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta

from . import db, money
from .config import Config


def local_midnight(d: date) -> int:
    return int(datetime.combine(d, dtime.min).astimezone().timestamp())


def split_by_day(start: int, end: int):
    """Yield (date, start, end) pieces of [start, end) split at local midnight."""
    cur = start
    while cur < end:
        d = datetime.fromtimestamp(cur).astimezone().date()
        nxt = local_midnight(d + timedelta(days=1))
        piece_end = min(end, nxt)
        yield d, cur, piece_end
        cur = piece_end


def union_seconds(intervals: list[tuple[int, int]]) -> int:
    total, cur_s, cur_e = 0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def round_up(seconds: int, minutes: int) -> int:
    if minutes <= 0 or seconds <= 0:
        return seconds
    step = minutes * 60
    return math.ceil(seconds / step) * step


def _virtual_timer_span(con, now: int):
    t = db.timer_get(con)
    if not t:
        return None
    return {"id": None, "project": t["project"], "source": "manual", "ctx_key": "timer",
            "start_ts": t["start_ts"], "end_ts": now, "detail": None, "note": t["note"], "billable": 1}


def daily_totals(con, start: int, end: int, project: str | None = None,
                 billable_only: bool = False, include_timer: bool = True, now: int | None = None):
    """Return {(project, date): {"seconds": int, "sources": {src: s}, "contexts": set, "notes": list}}."""
    from .sessions import evidence_segments
    now = int(datetime.now().timestamp()) if now is None else now
    rows = [dict(r) for r in db.spans_between(con, start, end)]
    if include_timer:
        timer = _virtual_timer_span(con, now)
        if timer:
            rows.append(timer)
    buckets = defaultdict(lambda: {"intervals": [], "by_source": defaultdict(list),
                                   "contexts": set(), "notes": []})
    for segment in evidence_segments(rows, (start, end)):
        if (project is not None and segment["project"] != project) or (billable_only and not segment["billable"]):
            continue
        for day, lo, hi in split_by_day(segment["start"], segment["end"]):
            bucket = buckets[(segment["project"], day)]
            bucket["intervals"].append((lo, hi))
            for row in segment["rows"]:
                if row["credited"]:
                    bucket["by_source"][row["source"]].append((lo, hi))
                detail = json.loads(row["detail"]) if row.get("detail") else {}
                label = detail.get("workspace_label") or detail.get("window") or row.get("ctx_key")
                if label and row["source"] != "manual":
                    bucket["contexts"].add(f'{row["source"]}:{label}')
                if row.get("note") and row["note"] not in bucket["notes"]:
                    bucket["notes"].append(row["note"])

    out = {}
    for key, b in buckets.items():
        out[key] = {
            "seconds": union_seconds(b["intervals"]),
            "sources": {src: union_seconds(iv) for src, iv in b["by_source"].items()},
            "contexts": sorted(b["contexts"]),
            "notes": b["notes"],
        }
    return out


def earned(con, cfg: Config, start: int, end: int, now: int | None = None) -> dict[str, float]:
    """Billable money in [start, end) per currency, worked out the way invoices do it: per project and
    day, session hours rounded up to rounding_minutes, hours x rate. Includes a running timer."""
    now = now or int(datetime.now().timestamp())
    per_day: dict = defaultdict(int)
    for s in sessions_between(con, cfg, start, end, billable_only=True, now=now):
        per_day[(s["project"], s["date"])] += s["seconds"]
    out = defaultdict(lambda: money.decimal(0))
    for (pid, _day), secs in per_day.items():
        p = cfg.project(pid)
        if p and p.rate > 0:
            _, amount = money.bill_day(secs, cfg.rounding_minutes, p.rate)
            out[p.currency] += money.decimal(amount)
    return {cur: money.cents(v) for cur, v in out.items() if v}


def month_bounds(year: int, month: int) -> tuple[int, int]:
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return local_midnight(first), local_midnight(nxt)


def sessions_between(con, cfg: Config, start: int, end: int, billable_only: bool = False,
                     with_commits: bool = False, summaries: bool = False, now: int | None = None,
                     invoice: bool = False):
    """Sessions (all projects) in [start, end), including a running manual timer."""
    from . import sessions as S
    now = int(datetime.now().timestamp()) if now is None else now
    rows = [dict(r) for r in db.spans_between(con, start, end)]
    v = _virtual_timer_span(con, now)
    if v and v["end_ts"] > start:
        rows.append(v)
    out = S.build_sessions(rows, cfg, clip=(start, end), billable_only=billable_only, invoice=invoice)
    if with_commits:
        S.attach_commits(out, cfg)
    if summaries:
        S.summarize(out, cfg, con)
    return out


def hourly_minutes(sessions: list[dict], day: date) -> list[float]:
    """Minutes worked in each local clock hour of `day` (repeated hours combined). Built from the same
    sessions as the day total, so the buckets add up to it."""
    edges = [int(datetime.combine(day, dtime(h)).astimezone().timestamp()) for h in range(24)]
    edges.append(local_midnight(day + timedelta(days=1)))
    out = [0.0] * 24
    for s in sessions:
        for h in range(24):
            for start, end in s.get("intervals", [(s["start"], s["end"])]):
                lo, hi = max(start, edges[h]), min(end, edges[h + 1])
                if hi > lo:
                    out[h] += (hi - lo) / 60
    return out


def month_report(con, cfg: Config, year: int, month: int, project_id: str, with_git: bool = True) -> dict:
    first = date(year, month, 1)
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return range_report(con, cfg, first, last, project_id, with_git, period=f"{year:04d}-{month:02d}")


def range_report(con, cfg: Config, d_from: date, d_to: date, project_id: str, with_git: bool = True,
                 period: str | None = None, excluded_days: set[date] | None = None) -> dict:
    """Billable days/sessions for whole days d_from..d_to inclusive."""
    from . import sessions as S
    proj = cfg.project(project_id)
    rate = proj.rate if proj else 0.0
    start, end = local_midnight(d_from), local_midnight(d_to + timedelta(days=1))
    human = [s for s in sessions_between(con, cfg, start, end)
             if s["project"] == project_id and s["date"] not in (excluded_days or ())]
    sess = [s for s in sessions_between(con, cfg, start, end, billable_only=True, with_commits=with_git, invoice=True)
            if s["project"] == project_id and s["date"] not in (excluded_days or ())]
    S.summarize(sess, cfg, con)

    by_day: dict = defaultdict(list)
    for s in sess:
        by_day[s["date"]].append(s)
    raw_by_day = defaultdict(int)
    for s in human:
        raw_by_day[s["date"]] += s["seconds"]
    days = []
    for d in sorted(raw_by_day):
        ss = by_day[d]
        raw = raw_by_day[d]
        billing = sum(s["billing_seconds"] for s in ss)
        hours, amount = money.bill_day(billing, cfg.rounding_minutes, rate)
        days.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%a"),
            "raw_hours": raw / 3600, "raw_seconds": raw,
            "billing_seconds": billing,
            "hours": hours,
            "amount": amount,
            "summary": " | ".join(s["summary"] for s in ss),
            "commits": sum(len(s.get("commits", [])) for s in ss),
            "sessions": [S.session_json(s) for s in ss],
        })
    total_hours = float(sum(money.decimal(d["hours"]) for d in days))
    return {
        "project": project_id,
        "name": proj.name if proj else project_id,
        "client": proj.client if proj else "",
        "period": period or f"{d_from.isoformat()}..{d_to.isoformat()}",
        "rate": rate,
        "currency": proj.currency if proj else "",
        "rounding_minutes": cfg.rounding_minutes,
        "bridge_minutes": cfg.bridge_minutes,
        "days": days,
        "total_raw_hours": sum(raw_by_day.values()) / 3600,
        "total_raw_seconds": sum(raw_by_day.values()),
        "total_hours": total_hours,
        "total_amount": money.cents(sum(money.decimal(d["amount"]) for d in days)),
    }


def render(rep: dict, fmt: str, detail: str = "day") -> str:
    if fmt == "json":
        return json.dumps(rep, indent=2, ensure_ascii=False)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        if detail == "session":
            w.writerow(["date", "start", "end", "hours", "summary", "commits"])
            for d in rep["days"]:
                for s in d["sessions"]:
                    w.writerow([d["date"], s["start"], s["end"], f'{s["hours"]:.2f}', s["summary"],
                                " ".join(c["sha"] for c in s["commits"])])
            return buf.getvalue()
        w.writerow(["date", "weekday", "hours", "raw_hours", "amount", "work", "commits"])
        for d in rep["days"]:
            w.writerow([d["date"], d["weekday"], f'{d["hours"]:.2f}', f'{d["raw_hours"]:.2f}',
                        f'{d["amount"]:.2f}', d["summary"], d["commits"]])
        w.writerow(["TOTAL", "", f'{rep["total_hours"]:.2f}', f'{rep["total_raw_hours"]:.2f}',
                    f'{rep["total_amount"]:.2f}', "", ""])
        return buf.getvalue()
    # markdown: invoice table, then session detail
    cur = rep["currency"]
    lines = [
        f'# Timesheet — {rep["name"]} ({rep["period"]})',
        "",
        f'Client: {rep["client"] or "—"} · Rate: {cur} {rep["rate"]:,.2f}/h'
        f' · Rounding: {rep["rounding_minutes"]} min/day',
        "",
        "| Date | Day | Hours | Amount | Work |",
        "|---|---|---:|---:|---|",
    ]
    for d in rep["days"]:
        lines.append(f'| {d["date"]} | {d["weekday"]} | {d["hours"]:.2f} | {cur} {d["amount"]:,.2f} | {d["summary"]} |')
    lines.append(f'| **Total** | | **{rep["total_hours"]:.2f}** | **{cur} {rep["total_amount"]:,.2f}** | |')
    if detail != "day-only":
        lines += ["", "## Sessions", ""]
        for d in rep["days"]:
            for s in d["sessions"]:
                lines.append(f'- **{d["date"]} {s["start"]}–{s["end"]}** ({s["hours"]:.2f} h) — {s["summary"]}')
                for c in s["commits"]:
                    lines.append(f'  - `{c["sha"][:7]}` {c["repo"]}: {c["subject"]}')
    return "\n".join(lines) + "\n"


def fmt_hm(seconds: int) -> str:
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h{m:02d}"
