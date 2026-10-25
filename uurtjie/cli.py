"""uurtjie CLI — status for the widget, manual entries, logs and monthly invoice reports."""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

from . import __version__
from . import config as config_mod
from . import db, report
from . import config_edit
from . import storage
from . import invoice as invoice_mod
from .config_edit import ConfigError
from .invoice import InvoiceError
from .config import RUNTIME_DIR
from .report import fmt_hm, local_midnight


# --------------------------------------------------------------------------- parsing helpers

def parse_duration(s: str) -> int:
    s = s.strip().lower()
    if m := re.fullmatch(r"(\d+):(\d{2})", s):
        return int(m[1]) * 3600 + int(m[2]) * 60
    if m := re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+)m)?", s):
        if m[1] or m[2]:
            return int(float(m[1] or 0) * 3600) + int(m[2] or 0) * 60
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return int(float(s) * 3600)          # bare number = hours
    raise argparse.ArgumentTypeError(f"bad duration: {s!r} (try 1h30m, 90m, 1.5h, 1:30)")


def parse_date(s: str | None) -> date:
    if not s or s == "today":
        return date.today()
    if s == "yesterday":
        return date.today() - timedelta(days=1)
    return date.fromisoformat(s)


def parse_clock(s: str, on: date) -> int:
    """'09:30' (on the given date) or '2026-10-21 09:30' -> unix ts."""
    s = s.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        h, m = map(int, s.split(":"))
        value = datetime(on.year, on.month, on.day, h, m)
    else:
        value = datetime.fromisoformat(s)
    if value.tzinfo is not None:
        return int(value.timestamp())
    # Refuse ambiguous/nonexistent local wall times; an explicit ISO offset resolves them.
    candidates = {int(value.replace(fold=fold).timestamp()) for fold in (0, 1)}
    candidates = {ts for ts in candidates if datetime.fromtimestamp(ts).replace(fold=0) == value.replace(fold=0)}
    if len(candidates) != 1:
        raise ValueError("ambiguous or nonexistent local time; give an ISO datetime with a UTC offset")
    return candidates.pop()


def clock_range(start: str, end: str, day: date) -> tuple[int, int]:
    a = parse_clock(start, day)
    if re.fullmatch(r"\d{1,2}:\d{2}", end):
        local_start = datetime.fromtimestamp(a)
        h, m = map(int, end.split(":"))
        end_clock = datetime(day.year, day.month, day.day, h, m).time()
        end_day = local_start.date() + timedelta(days=end_clock <= local_start.time())
        b = parse_clock(end, end_day)
    else:
        b = parse_clock(end, day)
    if b <= a:
        raise ValueError("end time must be after start time")
    return a, b


def ts_local(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts).strftime(fmt)


def require_project(cfg, pid: str | None, force: bool = False) -> str:
    pid = pid or cfg.default_project
    if not pid:
        sys.exit("no project given and no default_project in config")
    if not cfg.project(pid) and not force:
        known = ", ".join(p.id for p in cfg.projects) or "none configured"
        sys.exit(f"unknown project {pid!r} (known: {known}); use --force to log anyway")
    return pid


# --------------------------------------------------------------------------- status

def read_state() -> dict | None:
    try:
        st = json.loads((RUNTIME_DIR / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    st["running"] = time.time() - st.get("ts", 0) < max(30, 4 * st.get("tick", 5))
    return st


def fmt_ago(seconds: int) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d} ago"
    return f"{seconds // 86400} d ago"


def describe_area(source: str, detail: dict) -> str:
    """Where the work happened: Herdr workspace + folder (+ branch, agent), or the Chrome page."""
    if source == "terminal":
        folder = Path(detail["cwd"]).name if detail.get("cwd") else ""
        label = " · ".join(dict.fromkeys(x for x in (detail.get("workspace_label"), folder) if x))
        if detail.get("branch"):
            label += f" @ {detail['branch']}"
        if detail.get("agent"):
            label += f" ({detail['agent']})"
        return label or "terminal"
    if source == "meeting":
        return "call in " + (detail.get("app") or detail.get("class") or "an app")
    page = re.sub(r"\s+[-–]\s+Google Chrome$", "", detail.get("window") or "")
    return page or detail.get("class") or source


def last_tracked(con, cfg, now: int) -> dict | None:
    """The most recent automatically tracked area (manual entries have no 'where')."""
    row = con.execute("SELECT project, source, detail, end_ts FROM spans WHERE source != 'manual' "
                      "ORDER BY end_ts DESC LIMIT 1").fetchone()
    if not row:
        return None
    try:
        detail = json.loads(row["detail"] or "{}")
    except json.JSONDecodeError:
        detail = {}
    p = cfg.project(row["project"])
    label = describe_area(row["source"], detail)
    if len(label) > 60:
        label = label[:59] + "…"
    return {"project": row["project"], "name": p.name if p else row["project"], "source": row["source"],
            "label": label, "ago": fmt_ago(max(0, now - row["end_ts"]))}


CURRENCY_SYMBOLS = {"ZAR": "R", "USD": "$", "EUR": "€", "GBP": "£"}


def money_label(by_currency: dict, cents: bool = True) -> str:
    parts = []
    for cur, amt in sorted(by_currency.items()):
        num = f"{amt:,.2f}" if cents else f"{amt:,.0f}"
        sym = CURRENCY_SYMBOLS.get(cur)
        parts.append(f"{sym}{num}" if sym else f"{cur} {num}")
    return " + ".join(parts)


SPARK = "▁▂▃▄▅▆▇█"      # 8 levels across 0-60 minutes in an hour


def hourly_tip(hourly: dict) -> list[str]:
    """Plain-text bar chart for the bar tooltip (it can't draw): one block per hour from the first
    worked hour to the last, taller = more of that hour worked, '·' = nothing that hour."""
    mins = hourly["minutes"]
    worked = [h for h, m in enumerate(mins) if m > 0]
    if not worked:
        return []
    lo, hi = worked[0], worked[-1]
    scale = max(60, max(mins))
    bars = "".join(SPARK[min(7, int(mins[h] / scale * 8))] if mins[h] > 0 else "·" for h in range(lo, hi + 1))
    return [f"By hour {lo:02d}h–{hi:02d}h (full block = {scale:g} min)", f"  {bars}",
            f"  busiest {hourly['peak_hour']:02d}h: {mins[hourly['peak_hour']]:.0f} min"]


def build_status(cfg, con) -> dict:
    now = int(time.time())
    today = date.today()
    t0 = local_midnight(today)
    week0 = local_midnight(today - timedelta(days=today.weekday()))
    month0 = local_midnight(today.replace(day=1))

    def total(start):
        return sum(s["seconds"] for s in report.sessions_between(con, cfg, start, now + 1, now=now))

    by_proj = {}
    today_sessions = report.sessions_between(con, cfg, t0, now + 1, now=now)
    for s in today_sessions:
        by_proj[s["project"]] = by_proj.get(s["project"], 0) + s["seconds"]
    hour_minutes = report.hourly_minutes(today_sessions, today)
    peak = max(range(24), key=lambda h: hour_minutes[h])
    hourly = {"minutes": hour_minutes, "peak_hour": peak if hour_minutes[peak] > 0 else None,
              "now_hour": datetime.fromtimestamp(now).hour}
    projects = [{"id": pid, "name": (cfg.project(pid).name if cfg.project(pid) else pid),
                 "seconds": s, "hm": fmt_hm(s)} for pid, s in sorted(by_proj.items(), key=lambda kv: -kv[1])]

    st = read_state()
    active = None
    if st and st["running"] and st.get("active"):
        a = st["active"]
        p = cfg.project(a["project"])
        d = a.get("detail") or {}
        active = {"project": a["project"], "name": p.name if p else a["project"], "source": a["source"],
                  "label": d.get("workspace_label") or d.get("class") or a["key"], "agent": d.get("agent")}
    meeting = None
    if st and st["running"] and st.get("meeting"):
        m = st["meeting"]
        mp = cfg.project(m["project"])
        meeting = {"project": m["project"], "name": mp.name if mp else m["project"], "app": m.get("app") or "",
                   "since": m["since"], "minutes": max(0, (now - m["since"]) // 60)}
    timer = db.timer_get(con)
    timer_d = None if not timer else {
        "project": timer["project"], "start": timer["start_ts"], "elapsed": now - timer["start_ts"],
        "hm": fmt_hm(now - timer["start_ts"]), "note": timer["note"]}

    today_total = sum(p["seconds"] for p in projects)
    month_money = report.earned(con, cfg, month0, now + 1, now)
    today_money = report.earned(con, cfg, t0, now + 1, now)
    earnings = {"month": month_money, "today": today_money, "month_text": money_label(month_money),
                "today_text": money_label(today_money), "bar_text": money_label(month_money, cents=False)}
    if timer_d:
        cls, icon = "timer", "󰔛"
    elif meeting:
        cls, icon = "meeting", "󰍬"
    elif active:
        cls, icon = "active", "󰥔"
    elif st and st["running"] and st.get("idle"):
        cls, icon = "idle", "󰒲"
    elif st and st["running"]:
        cls, icon = "untracked", "󰥔"
    else:
        cls, icon = "stopped", "󰚌"
    who = timer_d["project"] if timer_d else (meeting["project"] if meeting else (active["project"] if active else ""))
    bar_text = f"{icon} {fmt_hm(today_total)}" + (f" {who}" if who else "")
    if month_money and cfg.show_money_in_bar:
        bar_text += f" · {earnings['bar_text']}"

    last = last_tracked(con, cfg, now)
    tip = [f"Today {fmt_hm(today_total)} · week {fmt_hm(total(week0))} · month {fmt_hm(total(month0))}"]
    tip += [f"  {p['name']}: {p['hm']}" for p in projects]
    tip += hourly_tip(hourly)
    if month_money:
        tip.append(f"Earned: {earnings['month_text']} this month"
                   + (f" · {earnings['today_text']} today" if today_money else ""))
    if meeting:
        tip.append(f"Call: {meeting['name']}" + (f" via {meeting['app']}" if meeting["app"] else "")
                   + f" ({meeting['minutes']} min)")
    if active:
        tip.append(f"Tracking: {active['name']} via {active['source']} ({active['label']})")
    elif last and not meeting:
        tip.append(f"Last: {last['source']} · {last['label']} · {last['ago']}")
    if timer_d:
        tip.append(f"Timer: {timer_d['project']} {timer_d['hm']}")
    owed = invoice_mod.outstanding_summary(con)
    if owed["text"]:
        tip.append(f"Invoices: {owed['text']}")
    if not (st and st["running"]):
        tip.append("uurtjie is not running")
    tip += [f"Warning: {warning}" for warning in cfg.warnings]

    return {
        "running": bool(st and st["running"]), "idle": bool(st and st.get("idle")), "class": cls,
        "active": active, "timer": timer_d,
        "today": {"seconds": today_total, "hm": fmt_hm(today_total), "projects": projects},
        "hourly": hourly,
        "week": {"seconds": total(week0), "hm": fmt_hm(total(week0))},
        "month": {"seconds": total(month0), "hm": fmt_hm(total(month0))},
        "bar_text": bar_text, "tooltip": "\n".join(tip),
        "default_project": cfg.default_project,
        "project_ids": [p.id for p in cfg.projects],
        "version": __version__,
        "last": last,
        "meeting": meeting,
        "invoices": owed,
        "earnings": earnings,
        "warnings": cfg.warnings,
    }


# --------------------------------------------------------------------------- commands

def cmd_daemon(args, cfg, con):
    from .daemon import run
    con.close()                         # run() owns the daemon's connection
    asyncio.run(run())


def cmd_status(args, cfg, con):
    s = build_status(cfg, con)
    if args.waybar:
        print(json.dumps({"text": s["bar_text"], "tooltip": s["tooltip"], "class": s["class"], "alt": s["class"]}, ensure_ascii=False))
    elif args.json:
        print(json.dumps(s, ensure_ascii=False))
    else:
        print(s["tooltip"])


def cmd_summary(args, cfg, con, days: int):
    today = date.today()
    start_d = today - timedelta(days=today.weekday()) if days == 7 else today
    sess = report.sessions_between(con, cfg, local_midnight(start_d), int(time.time()) + 1)
    if not sess:
        print("nothing tracked yet")
        return
    by_day: dict = {}
    for s in sess:
        by_day.setdefault(s["date"], {}).setdefault(s["project"], []).append(s)
    for d, projs in by_day.items():
        print(f"{d:%a %d %b}  {fmt_hm(sum(s['seconds'] for ss in projs.values() for s in ss))}")
        for pid, ss in projs.items():
            print(f"   {pid:<16} {fmt_hm(sum(s['seconds'] for s in ss)):>6}   {len(ss)} session(s)")


def cmd_sessions(args, cfg, con):
    from . import sessions as S
    d = parse_date(args.date)
    start = local_midnight(d - timedelta(days=args.days - 1))
    end = local_midnight(d + timedelta(days=1))
    sess = [s for s in report.sessions_between(con, cfg, start, end, with_commits=True, summaries=True)
            if not args.project or s["project"] == args.project]
    if args.json:
        print(json.dumps([S.session_json(s) for s in sess], indent=2, ensure_ascii=False))
        return
    if not sess:
        print("no sessions")
    for s in sess:
        print(f"{s['date']:%a %d %b} {ts_local(s['start'], '%H:%M')}–{ts_local(s['end'], '%H:%M')}"
              f"  {fmt_hm(s['seconds']):>6}  {s['project']:<14} {s['summary']}")
        for c in s.get("commits", []):
            print(f"{'':>30}{c['sha'][:7]} {c['repo']}: {c['subject']}")


def cmd_log(args, cfg, con):
    d = parse_date(args.date)
    start = local_midnight(d - timedelta(days=args.days - 1))
    end = local_midnight(d + timedelta(days=1))
    rows = db.spans_between(con, start, end, args.project)
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return
    if rows:
        print(f'{"id":>6}  {"date":<10}  {"start":<8}   {"end":<8}  {"minutes":>9}  {"project":<14} {"source":<8} area')
    for r in rows:
        det = json.loads(r["detail"]) if r["detail"] else {}
        label = det.get("workspace_label") or det.get("class") or r["ctx_key"] or ""
        flag = "" if r["billable"] else " [non-billable]"
        end = ts_local(r["end_ts"], "%H:%M:%S")
        if ts_local(r["end_ts"], "%Y-%m-%d") != ts_local(r["start_ts"], "%Y-%m-%d"):
            end += "+1d"                      # crossed midnight
        minutes = f'{(r["end_ts"] - r["start_ts"]) / 60:.1f} min'
        print(f'{r["id"]:>6}  {ts_local(r["start_ts"], "%Y-%m-%d")}  {ts_local(r["start_ts"], "%H:%M:%S")} – {end:<8}  '
              f'{minutes:>9}  {r["project"]:<14} {r["source"]:<8} '
              f'{label}{"  — " + r["note"] if r["note"] else ""}{flag}')


def documents_dir() -> Path:
    try:
        out = subprocess.run(["xdg-user-dir", "DOCUMENTS"], capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        out = ""
    return Path(out) if out else Path.home() / "Documents"


def cmd_export(args, cfg, con):
    rows = db.spans_between(con, 0, 2**62, args.project)
    dest_dir = Path(args.to).expanduser() if args.to else documents_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"uurtjie-log-{time.strftime('%Y-%m-%d_%H%M%S')}.csv"
    with io.StringIO(newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "date", "start", "end", "minutes", "project", "source", "area", "note", "billable"])
        for r in rows:
            det = json.loads(r["detail"]) if r["detail"] else {}
            label = det.get("workspace_label") or det.get("class") or r["ctx_key"] or ""
            w.writerow([r["id"], ts_local(r["start_ts"], "%Y-%m-%d"), ts_local(r["start_ts"], "%H:%M:%S"),
                        ts_local(r["end_ts"], "%Y-%m-%d %H:%M:%S"), f'{(r["end_ts"] - r["start_ts"]) / 60:.1f}',
                        r["project"], r["source"], label, r["note"] or "", int(bool(r["billable"]))])
        storage.atomic_write(dest, f.getvalue())
    print(f"exported {len(rows)} entries: {dest}")


def entry_write(fn):
    """Keep manual lock checks and their writes atomic against invoice issuance."""
    @wraps(fn)
    def wrapped(args, cfg, con):
        with db.transaction(con):
            return fn(args, cfg, con)
    return wrapped


@entry_write
def cmd_add(args, cfg, con):
    pid = require_project(cfg, args.project, args.force)
    d = parse_date(args.date)
    if args.duration:
        secs = parse_duration(args.duration)
        if args.start:
            start = parse_clock(args.start, d)
        else:  # ends now (today) or at 17:00 on a past date
            end_ref = int(time.time()) if d == date.today() else parse_clock("17:00", d)
            start = end_ref - secs
        end = start + secs
    elif args.start and args.end:
        start, end = clock_range(args.start, args.end, d)
    else:
        sys.exit("give --duration, or --from and --to")
    if end <= start:
        raise ValueError("duration must be positive")
    invoice_mod.check_unlocked(con, pid, start, end, args.force)
    sid = db.open_span(con, pid, "manual", "manual", start, end, note=args.note, billable=not args.non_billable)
    print(f"added #{sid}: {pid} {ts_local(start)}–{ts_local(end, '%H:%M')} ({fmt_hm(end - start)})")


def auto_project(cfg) -> str | None:
    """Project you're focused on right now (from the daemon), else default_project."""
    st = read_state()
    if st and st.get("running") and st.get("active"):
        return st["active"]["project"]
    return cfg.default_project


@entry_write
def cmd_start(args, cfg, con):
    pid = require_project(cfg, args.project or auto_project(cfg), args.force)
    try:
        db.timer_start(con, pid, args.note)
    except RuntimeError as e:
        sys.exit(str(e))
    p = cfg.project(pid)
    print(f"timer started for {p.name if p else pid}")


MIN_TIMER_SECONDS = 60       # shorter manual timers are discarded on stop, without asking


@entry_write
def cmd_stop(args, cfg, con):
    at = getattr(args, "at", None)
    t = db.timer_get(con)
    end = at if at is not None else int(time.time())
    if t:
        if at is not None and at <= t["start_ts"] and not getattr(args, "discard", False):
            raise ValueError("stop time must be after the timer start")
        invoice_mod.check_unlocked(con, t["project"], t["start_ts"], end, getattr(args, "force", False))
    if getattr(args, "discard", False):
        if not t:
            sys.exit("no timer running")
        con.execute("DELETE FROM timer WHERE id=1")
        print("timer discarded")
        return
    if t and end - t["start_ts"] < MIN_TIMER_SECONDS:
        con.execute("DELETE FROM timer WHERE id=1")        # accidental start/stop: nothing worth keeping
        print("timer discarded (under a minute)")
        return
    sid = db.timer_stop(con, at=at, note=args.note, force=getattr(args, "force", False))
    if sid is None:
        sys.exit("no timer running")
    r = con.execute("SELECT * FROM spans WHERE id=?", (sid,)).fetchone()
    print(f"stopped: #{sid} {r['project']} {fmt_hm(r['end_ts'] - r['start_ts'])}")


def cmd_timer(args, cfg, con):
    """Machine-readable timer state for scripts/widgets."""
    t = db.timer_get(con)
    now = int(time.time())
    if not t:
        print(json.dumps({"running": False, "next_project": auto_project(cfg)}))
        return
    p = cfg.project(t["project"])
    print(json.dumps({"running": True, "project": t["project"], "name": p.name if p else t["project"],
                      "start": t["start_ts"], "start_hm": ts_local(t["start_ts"], "%H:%M"),
                      "elapsed": now - t["start_ts"], "elapsed_hm": fmt_hm(now - t["start_ts"])},
                     ensure_ascii=False))


def cmd_toggle(args, cfg, con):
    if db.timer_get(con):
        cmd_stop(argparse.Namespace(note=None, at=None, discard=False), cfg, con)
    else:
        cmd_start(argparse.Namespace(project=args.project, note=None, force=False), cfg, con)


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_quick(line: str, cfg, today: date | None = None, now: int | None = None) -> dict:
    """Parse a one-line entry, tokens in any order:

        [project] [date] <duration | HH:MM-HH:MM> [note …] [nb]

    project : configured id (or unique prefix); default_project if omitted
    date    : today | yesterday | y | mon..sun (most recent) | YYYY-MM-DD
    time    : 1h30m 90m 1.5h 1:30   or   09:00-10:30
    nb      : mark non-billable
    """
    today = today or date.today()
    tokens = line.split()
    project, day, secs, rng, billable, rest = None, today, None, None, True, []
    for tok in tokens:
        low = tok.lower()
        if project is None and not rest:
            match = [p.id for p in cfg.projects if p.id == low] or \
                    [p.id for p in cfg.projects if p.id.startswith(low) and len(low) >= 2]
            if len(match) == 1:
                project = match[0]
                continue
        if secs is None and rng is None:
            if m := re.fullmatch(r"(\d{1,2}:\d{2})-(\d{1,2}:\d{2})", low):
                rng = (m[1], m[2])
                continue
            if re.fullmatch(r"\d+(?:\.\d+)?h(?:\d+m?)?|\d+m|\d+:\d{2}", low):
                secs = parse_duration(low if not re.fullmatch(r"\d+h\d+", low) else low + "m")
                continue
        if not rest:
            if low in ("today", "t"):
                day = today; continue
            if low in ("yesterday", "y"):
                day = today - timedelta(days=1); continue
            if low[:3] in WEEKDAYS and low.isalpha():
                back = (today.weekday() - WEEKDAYS.index(low[:3])) % 7
                day = today - timedelta(days=back); continue
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", low):
                day = date.fromisoformat(low); continue
        if low in ("nb", "!nb", "#nb"):
            billable = False; continue
        rest.append(tok)

    project = project or cfg.default_project
    if not project:
        raise ValueError("no project (and no default_project set)")
    if rng:
        start, end = clock_range(rng[0], rng[1], day)
    elif secs:
        now = now or int(time.time())
        end = now if day == today else parse_clock("17:00", day)
        start = end - secs
    else:
        raise ValueError("need a duration (1h30) or a range (09:00-10:30)")
    return {"project": project, "start": start, "end": end,
            "note": " ".join(rest) or None, "billable": billable}


@entry_write
def cmd_quick(args, cfg, con):
    line = " ".join(args.line)
    try:
        e = parse_quick(line, cfg)
    except (ValueError, argparse.ArgumentTypeError) as ex:
        sys.exit(f"couldn't parse {line!r}: {ex}")
    if args.dry_run:
        print(json.dumps(e))
        return
    invoice_mod.check_unlocked(con, e["project"], e["start"], e["end"], getattr(args, "force", False))
    sid = db.open_span(con, e["project"], "manual", "manual", e["start"], e["end"],
                       note=e["note"], billable=e["billable"])
    print(f"#{sid} {e['project']} {ts_local(e['start'], '%a %d %b %H:%M')}–{ts_local(e['end'], '%H:%M')}"
          f" ({fmt_hm(e['end'] - e['start'])}){' — ' + e['note'] if e['note'] else ''}"
          f"{' [non-billable]' if not e['billable'] else ''}")


@entry_write
def cmd_undo(args, cfg, con):
    """Delete the most recently created manual entry (fix a fat-fingered quick add)."""
    r = con.execute("SELECT id, project, start_ts, end_ts FROM spans WHERE source='manual' "
                    "ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
    if not r:
        sys.exit("no manual entries")
    invoice_mod.check_unlocked(con, r["project"], r["start_ts"], r["end_ts"], getattr(args, "force", False))
    con.execute("DELETE FROM spans WHERE id=?", (r["id"],))
    print(f"removed #{r['id']} ({r['project']})")


@entry_write
def cmd_rm(args, cfg, con):
    for i in args.ids:
        r = con.execute("SELECT project, start_ts, end_ts FROM spans WHERE id=?", (i,)).fetchone()
        if r:
            try:
                invoice_mod.check_unlocked(con, r["project"], r["start_ts"], r["end_ts"], getattr(args, "force", False))
            except InvoiceError as ex:
                print(f"#{i} kept: {ex}")
                continue
        n = con.execute("DELETE FROM spans WHERE id=?", (i,)).rowcount
        print(f"#{i} {'deleted' if n else 'not found'}")


@entry_write
def cmd_edit(args, cfg, con):
    r = con.execute("SELECT * FROM spans WHERE id=?", (args.id,)).fetchone()
    if not r:
        sys.exit(f"#{args.id} not found")
    d = datetime.fromtimestamp(r["start_ts"]).date()
    end_day = datetime.fromtimestamp(r["end_ts"]).date()
    sets, vals = [], []
    if args.project:
        sets.append("project=?"); vals.append(require_project(cfg, args.project, args.force))
    if args.note is not None:
        sets.append("note=?"); vals.append(args.note)
    if args.start:
        sets.append("start_ts=?"); vals.append(parse_clock(args.start, d))
    if args.end:
        sets.append("end_ts=?"); vals.append(parse_clock(args.end, end_day))
    if args.billable is not None:
        sets.append("billable=?"); vals.append(1 if args.billable == "yes" else 0)
    if not sets:
        sys.exit("nothing to change")
    new_start = parse_clock(args.start, d) if args.start else r["start_ts"]
    new_end = parse_clock(args.end, end_day) if args.end else r["end_ts"]
    if new_end <= new_start:
        raise ValueError("end time must be after start time (use a full date for overnight changes)")
    for proj, a, b in ((r["project"], r["start_ts"], r["end_ts"]),
                       (args.project or r["project"], new_start, new_end)):
        invoice_mod.check_unlocked(con, proj, a, b, args.force)
    sets.append("updated_at=?"); vals.append(int(time.time()))
    con.execute(f"UPDATE spans SET {', '.join(sets)} WHERE id=?", (*vals, args.id))
    print(f"#{args.id} updated")


def cmd_report(args, cfg, con):
    if args.month:
        y, m = map(int, args.month.split("-"))
    else:
        t = date.today()
        y, m = t.year, t.month
    pids = [require_project(cfg, args.project, args.force)] if args.project else [p.id for p in cfg.projects]
    outs = []
    for pid in pids:
        rep = report.month_report(con, cfg, y, m, pid)
        if not rep["days"] and not args.project:
            continue
        text = report.render(rep, args.format, args.by)
        if args.out:
            out = Path(args.out.format(project=pid, period=rep["period"])).expanduser()
            storage.atomic_write(out, text)
            print(f"wrote {out}  ({rep['total_hours']:.2f} h, {rep['currency']} {rep['total_amount']:,.2f})")
        else:
            outs.append(text)
        if args.record:
            con.execute("INSERT INTO invoices(project, period, hours, amount, currency, file, generated_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (pid, rep["period"], rep["total_hours"], rep["total_amount"], rep["currency"],
                         args.out and str(out), int(time.time())))
    if outs:
        print("\n".join(outs))


def _invoice_project(cfg, name):
    pid = name or cfg.default_project or (cfg.projects[0].id if len(cfg.projects) == 1 else None)
    if not pid:
        sys.exit("which project? use --project")
    return require_project(cfg, pid, False)


def _print_invoice_totals(snap):
    cur = snap["currency"]
    print(f'  {snap["hours"]:.2f} h x {invoice_mod.fmt_money(cur, snap["rate"])} = {invoice_mod.fmt_money(cur, snap["subtotal"])}')
    if snap["vat_rate"]:
        print(f'  VAT {snap["vat_rate"]:g}%  {invoice_mod.fmt_money(cur, snap["vat_amount"])}')
    print(f'  total {invoice_mod.fmt_money(cur, snap["total"])} · due {snap["due_date"]} ({snap["terms_days"]} days)')


def cmd_invoice(args, cfg, con):
    c = args.invoice_cmd
    today = date.today()
    money = invoice_mod.fmt_money
    if c == "create":
        pid = _invoice_project(cfg, args.project)
        if args.month:
            y, m = map(int, args.month.split("-"))
            d_from, d_to = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        elif args.start or args.end:
            if not (args.start and args.end):
                sys.exit("give both --from and --to (or use --month)")
            d_from, d_to = parse_date(args.start), parse_date(args.end)
        else:                                             # default: the previous calendar month
            d_to = today.replace(day=1) - timedelta(days=1)
            d_from = d_to.replace(day=1)
        snap = invoice_mod.create(con, cfg, pid, d_from, d_to, today=today, vat_rate=args.vat,
                                  due_days=args.due_days, force=args.force, dry_run=args.dry_run)
        print(f'{"DRAFT (nothing saved)" if args.dry_run else snap["number"]}  {pid}  {d_from}..{d_to}')
        _print_invoice_totals(snap)
        if args.dry_run:
            return
        n = invoice_mod.locked_entries(con, pid, d_from, d_to)
        html_path, pdf_path = invoice_mod.write_files(snap, cfg.invoice.dir, pdf=args.pdf, detail=args.detail)
        con.execute("UPDATE invoices SET file=? WHERE number=?", (str(pdf_path or html_path), snap["number"]))
        print(f"  locked {n} entries in that period (void the invoice to unlock)")
        print(f"  {html_path}")
        if args.pdf:
            print(f"  {pdf_path}" if pdf_path else "  (pdf failed: needs Chrome/Chromium; print the HTML to PDF instead)")
        if not cfg.invoice.seller.get("name"):
            print("  tip: add [invoice.from] (name, address, bank) to the config so the invoice carries your details")
    elif c == "list":
        rs = invoice_mod.rows(con, today, args.project, include_void=args.all)
        if not rs:
            print("no invoices yet (uurtjie invoice create --help)")
        for r in rs:
            print(f'{r["number"]:<15} {r["project"]:<12} {r["period_from"]}..{r["period_to"]}  '
                  f'{money(r["currency"], r["total"]):>18}  paid {r["paid"]:>10,.2f}  owed {r["outstanding"]:>10,.2f}  '
                  f'due {r["due_date"]}  {r["state"]}')
        owed = invoice_mod.outstanding_summary(con, today)
        if owed["text"]:
            print(f'\n{owed["text"]}')
    elif c == "show":
        inv = invoice_mod.find(con, args.number)
        snap = invoice_mod.snapshot(inv)
        paid = invoice_mod.paid_total(con, inv["id"])
        st = invoice_mod.state(inv, paid, today)
        pays = [dict(p) for p in con.execute("SELECT paid_on, amount, note FROM payments WHERE invoice_id=? ORDER BY paid_on, id", (inv["id"],))]
        if args.json:
            print(json.dumps({**snap, "state": st, "paid": paid, "payments": pays}, indent=2, ensure_ascii=False))
            return
        print(f'{inv["number"]}  {inv["project"]}  {inv["period_from"]}..{inv["period_to"]}  [{st}]')
        _print_invoice_totals(snap)
        print(f'  issued {inv["issue_date"]} · paid {money(snap["currency"], paid)} · file {inv["file"] or "-"}')
        for p in pays:
            print(f'  payment {p["paid_on"]}  {money(snap["currency"], p["amount"])}{"  " + p["note"] if p["note"] else ""}')
    elif c == "render":
        inv = invoice_mod.find(con, args.number)
        out = Path(args.out).expanduser() if args.out else cfg.invoice.dir
        html_path, pdf_path = invoice_mod.write_files(invoice_mod.snapshot(inv), out, pdf=args.pdf, detail=args.detail)
        print(html_path)
        if args.pdf:
            print(pdf_path or "pdf failed: needs Chrome/Chromium")
    elif c == "sent":
        print(f'{invoice_mod.mark_sent(con, args.number, parse_date(args.date) if args.date else None)} marked as sent')
    elif c == "paid":
        r = invoice_mod.record_payment(con, args.number, args.amount, parse_date(args.date) if args.date else None, args.note)
        print(f'{r["number"]}: received {money(r["currency"], r["amount"])}'
              + (f' · {money(r["currency"], r["outstanding"])} still owed' if r["outstanding"] > 0 else " · paid in full"))
    elif c == "void":
        inv = invoice_mod.find(con, args.number)
        if not args.yes:
            n = invoice_mod.locked_entries(con, inv["project"], date.fromisoformat(inv["period_from"]), date.fromisoformat(inv["period_to"]))
            sys.exit(f'this voids {inv["number"]} and unlocks {n} entries ({inv["period_from"]}..{inv["period_to"]}); '
                     'the number is never reused. Re-run with --yes to confirm.')
        print(f'{invoice_mod.void(con, args.number, args.force)} voided; its period is unlocked')
    elif c == "unbilled":
        rs = invoice_mod.unbilled(con, cfg, today, args.project)
        if not rs:
            print("everything tracked is covered by an invoice")
        for r in rs:
            flag = " (month still open)" if r["open"] else (" (partly invoiced)" if r["partly_invoiced"] else "")
            print(f'{r["project"]:<12} {r["month"]}  {r["hours"]:>7.2f} h  {money(r["currency"], r["amount"]):>16}{flag}')


def cmd_projects(args, cfg, con):
    for warning in cfg.warnings:
        print(f"Warning: {warning}")
    for p in cfg.projects:
        star = "*" if p.id == cfg.default_project else " "
        print(f"{star} {p.id:<16} {p.name}  ({p.currency} {p.rate:g}/h)")
        for path in p.paths:
            print(f"      path   {path}")
        for w in p.windows:
            print(f"      window class={w.cls or '*'} title={w.title.pattern if w.title else '*'}")


def cmd_where(args, cfg, con):
    print(f"config  {config_mod.CONFIG_PATH}")
    print(f"db      {db.db_path()}")
    print(f"state   {RUNTIME_DIR / 'state.json'}")
    print(f"backups {cfg.backup_dir}")


def cmd_backup(args, cfg, con):
    if args.list:
        files = sorted(cfg.backup_dir.glob("uurtjie-*.db"))
        for f in files:
            print(f"{f}  {f.stat().st_size // 1024} KB")
        if not files:
            print(f"no backups yet in {cfg.backup_dir}")
        return
    name = None if not args.now else f"uurtjie-{time.strftime('%Y-%m-%d_%H%M%S')}-manual.db"
    dest = db.backup(con, Path(args.to).expanduser() if args.to else cfg.backup_dir, cfg.backup_keep, name)
    print(f"backup written: {dest}")


def reload_daemon():
    subprocess.run(["systemctl", "--user", "kill", "-s", "HUP", "uurtjie.service"], check=False)
    print("sent reload to uurtjie.service")


def cmd_reload(args, cfg, con):
    reload_daemon()


# --------------------------------------------------------------------------- config

def _config_project(cfg, name):
    pid = name or cfg.default_project or (cfg.projects[0].id if len(cfg.projects) == 1 else None)
    if not pid or not cfg.project(pid):
        sys.exit("which project? use --project " + "/".join(p.id for p in cfg.projects))
    return pid


def _show_config(cfg, path):
    def val(v, fmt=str):
        return fmt(v) if v not in ("", None, []) else "(not set)"

    def lines(v):
        return ("\n" + " " * 22).join(v) if v else "(not set)"

    print(f"config  {path}\n")
    for p in cfg.projects:
        star = "  (default)" if p.id == cfg.default_project else ""
        print(f"project {p.id}{star}")
        print(f"  name                {p.name}")
        print(f"  client              {val(p.client)}")
        print(f"  rate                {p.currency} {p.rate:,.2f} / hour" if p.rate else "  rate                (not set)")
        print(f"  currency            {p.currency}")
        print(f"  client_address      {lines(p.client_address)}")
        print(f"  client_vat_number   {val(p.client_vat_number)}")
        print(f"  client_reference    {val(p.client_reference)}\n")
    iv, s = cfg.invoice, cfg.invoice.seller
    print("invoice")
    print(f"  vat_rate            {iv.vat_rate:g} %")
    print(f"  payment_terms_days  {iv.payment_terms_days}")
    print(f"  number_format       {iv.number_format}")
    print(f"  dir                 {iv.dir}")
    for k in ("name", "email", "phone", "vat_number", "reg_number", "footer"):
        print(f"  from.{k:<14} {val(s.get(k))}")
    print(f"  from.address        {lines(s.get('address'))}")
    print(f"  from.bank           {lines(s.get('bank'))}")
    print(f"\nrounding_minutes {cfg.rounding_minutes} · sleep_minutes {cfg.sleep_minutes:g} · "
          f"meeting_detect {cfg.meeting_detect}")
    missing = [f"project {p.id}: rate" for p in cfg.projects if p.rate <= 0]
    missing += [f"invoice.from.{k}" for k in ("name", "bank") if not s.get(k)]
    print("\nready to invoice: " + ("yes" if not missing else "not yet, still missing " + ", ".join(missing)))


def _ask_text(ask, label, current):
    ans = ask(f"{label} [{current or 'none'}]: ").strip()
    return None if ans == "" else ("" if ans == "-" else ans)         # Enter keeps, '-' clears


def _ask_lines(ask, label, current):
    print(f"{label}: one line each, blank line to finish (Enter now keeps, '-' clears)")
    for ln in current or []:
        print(f"   current: {ln}")
    first = ask("> ").strip()
    if first == "":
        return None
    if first == "-":
        return []
    out = [first]
    while ln := ask("> ").strip():
        out.append(ln)
    return out


def _config_wizard(cfg, path, pid, ask=input):
    s, iv, p = cfg.invoice.seller, cfg.invoice, cfg.project(pid)
    print("Invoice details. Enter keeps the current value, '-' clears it.\n\nFrom (you):")
    plan = [("invoice.from.name", "text", "  Your name or company", s.get("name")),
            ("invoice.from.address", "lines", "  Address", s.get("address")),
            ("invoice.from.email", "text", "  Email", s.get("email")),
            ("invoice.from.phone", "text", "  Phone", s.get("phone")),
            ("invoice.from.vat_number", "text", "  VAT number (blank if not VAT-registered)", s.get("vat_number")),
            ("invoice.from.reg_number", "text", "  Company registration number (blank if none)", s.get("reg_number")),
            ("invoice.from.bank", "lines", "  Bank details (e.g. Bank: …, Account name: …, Account no.: …, Branch code: …)", s.get("bank")),
            ("invoice.from.footer", "text", "  Footer line", s.get("footer")),
            ("invoice.vat_rate", "text", "  VAT percent to charge (0 = none; 15 for South Africa)", f"{iv.vat_rate:g}"),
            ("invoice.payment_terms_days", "text", "  Payment terms, days", str(iv.payment_terms_days)),
            ("client", "text", f"\nBill to ({pid}):\n  Client name", p.client),
            ("client_address", "lines", "  Client address", p.client_address),
            ("client_vat_number", "text", "  Client VAT number", p.client_vat_number),
            ("client_reference", "text", "  Client reference / PO number", p.client_reference)]
    changes = []
    for key, kind, label, current in plan:
        if label.startswith("\n"):
            print(label.split("\n")[1]); label = label.split("\n")[2]
        ans = _ask_lines(ask, label, current) if kind == "lines" else _ask_text(ask, label, current)
        if ans is None:
            continue
        project = pid if config_edit.resolve(key)[0] == "project" else None
        changes.append((key, ans if kind == "lines" else [ans], project))
    if not changes:
        print("nothing changed")
        return
    for key, old, new in config_edit.update_many(path, changes):
        print(f"  {key}: {old or '(not set)'} → {new}")
    reload_daemon()


def cmd_config(args, cfg, con):
    c = args.config_cmd
    path = config_mod.CONFIG_PATH
    if c == "show":
        _show_config(cfg, path)
    elif c == "set":
        table = config_edit.resolve(args.key)[0]
        pid = _config_project(cfg, args.project) if table == "project" else None
        old, new = config_edit.update(path, args.key, args.value, pid)
        print(f"{args.key}{f' ({pid})' if pid else ''}: {old or '(not set)'} → {new}")
        reload_daemon()
    elif c == "invoice":
        if not sys.stdin.isatty():
            sys.exit("run this in a terminal, or set values one at a time: uurtjie config set invoice.from.name 'Your Name'")
        _config_wizard(cfg, path, _config_project(cfg, args.project))
    elif c == "edit":
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        before = path.read_text()
        subprocess.run([*editor.split(), str(path)], check=False)
        try:
            tomllib.loads(path.read_text())
            config_mod.load(path)
        except Exception as e:
            storage.atomic_write(path, before)
            sys.exit(f"that edit doesn't load ({e}); your previous config was restored")
        if path.read_text() != before:
            config_edit.write(path, path.read_text())
            storage.atomic_write(path.with_name(path.name + ".bak"), before)
            reload_daemon()
        else:
            print("no changes")


# --------------------------------------------------------------------------- argparse

def main(argv=None):
    ap = argparse.ArgumentParser(prog="uurtjie", description="Activity-based client time tracking")
    ap.add_argument("--version", action="version", version=f"uurtjie {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="run the tracker (systemd runs this)").set_defaults(fn=cmd_daemon)

    s = sub.add_parser("status", help="current status (for widgets)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--waybar", action="store_true")
    s.set_defaults(fn=cmd_status)

    sub.add_parser("today", help="today by project").set_defaults(fn=lambda a, c, d: cmd_summary(a, c, d, 1))
    sub.add_parser("week", help="this week by day/project").set_defaults(fn=lambda a, c, d: cmd_summary(a, c, d, 7))

    s = sub.add_parser("log", help="list raw entries (with ids for edit/rm)")
    s.add_argument("--date", help="YYYY-MM-DD | today | yesterday")
    s.add_argument("--days", type=int, default=1)
    s.add_argument("--project")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("export", help="export the full log as CSV (default: Documents folder)")
    s.add_argument("--project")
    s.add_argument("--to", help="directory (default: Documents)")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("add", help="add a manual entry")
    s.add_argument("project", nargs="?")
    s.add_argument("--date")
    s.add_argument("--from", dest="start", help="HH:MM")
    s.add_argument("--to", dest="end", help="HH:MM")
    s.add_argument("--duration", "-d", help="1h30m, 90m, 1.5h, 1:30")
    s.add_argument("--note", "-n")
    s.add_argument("--non-billable", action="store_true")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("start", help="start a manual timer")
    s.add_argument("project", nargs="?")
    s.add_argument("--note", "-n")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_start)

    s = sub.add_parser("stop", help="stop the manual timer")
    s.add_argument("--note", "-n")
    s.add_argument("--at", type=int, help="stop time as unix seconds (default: now)")
    s.add_argument("--discard", action="store_true", help="throw the running timer away")
    s.add_argument("--force", action="store_true", help="save time inside an invoiced period")
    s.set_defaults(fn=cmd_stop)

    sub.add_parser("timer", help="timer state as JSON").set_defaults(fn=cmd_timer)

    s = sub.add_parser("toggle", help="start/stop the timer (widget click)")
    s.add_argument("project", nargs="?")
    s.set_defaults(fn=cmd_toggle)

    s = sub.add_parser("quick", aliases=["q"], help='one-line entry: "acme 1h30 call with PM"')
    s.add_argument("line", nargs="+")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true", help="even if it falls inside an invoiced period")
    s.set_defaults(fn=cmd_quick)

    s = sub.add_parser("undo", help="delete the last manual entry")
    s.add_argument("--force", action="store_true", help="even if it is inside an invoiced period")
    s.set_defaults(fn=cmd_undo)

    s = sub.add_parser("rm", help="delete entries")
    s.add_argument("ids", nargs="+", type=int)
    s.add_argument("--force", action="store_true", help="also delete entries inside an invoiced period")
    s.set_defaults(fn=cmd_rm)

    s = sub.add_parser("edit", help="edit an entry")
    s.add_argument("id", type=int)
    s.add_argument("--project")
    s.add_argument("--note", "-n")
    s.add_argument("--from", dest="start")
    s.add_argument("--to", dest="end")
    s.add_argument("--billable", choices=["yes", "no"])
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_edit)

    s = sub.add_parser("report", help="monthly timesheet / invoice data")
    s.add_argument("--month", help="YYYY-MM (default: this month)")
    s.add_argument("--project")
    s.add_argument("--format", "-f", choices=["md", "csv", "json"], default="md")
    s.add_argument("--out", "-o", help="file path; {project} and {period} are substituted")
    s.add_argument("--record", action="store_true", help="log this report in the invoices table")
    s.add_argument("--by", choices=["day", "session", "day-only"], default="day",
                   help="csv rows per day or per session; md 'day-only' hides the session list")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("sessions", help="work sessions with summaries and commits")
    s.add_argument("--date", help="YYYY-MM-DD | today | yesterday")
    s.add_argument("--days", type=int, default=1)
    s.add_argument("--project")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("invoice", help="issue and track invoices (issuing locks the billed period)")
    isub = s.add_subparsers(dest="invoice_cmd", required=True)
    c = isub.add_parser("create", help="issue an invoice (default: last calendar month)")
    c.add_argument("--project")
    c.add_argument("--month", help="YYYY-MM")
    c.add_argument("--from", dest="start", help="first day, YYYY-MM-DD")
    c.add_argument("--to", dest="end", help="last day, YYYY-MM-DD")
    c.add_argument("--vat", type=float, help="VAT percent (default: [invoice] vat_rate)")
    c.add_argument("--due-days", type=int, help="payment terms (default: [invoice] payment_terms_days)")
    c.add_argument("--pdf", action="store_true", help="also write a PDF (needs Chrome/Chromium)")
    c.add_argument("--detail", choices=["days", "sessions"], default="days",
                   help="'sessions' adds per-session work summaries to the timesheet page")
    c.add_argument("--dry-run", action="store_true", help="show the figures, save nothing, lock nothing")
    c.add_argument("--force", action="store_true", help="include today / overlap an existing invoice")
    c = isub.add_parser("list", help="invoices with paid / owed / due")
    c.add_argument("--project")
    c.add_argument("--all", action="store_true", help="include voided invoices")
    c = isub.add_parser("show", help="one invoice with its payments")
    c.add_argument("number")
    c.add_argument("--json", action="store_true")
    c = isub.add_parser("render", help="rewrite the HTML/PDF of an issued invoice from its frozen figures")
    c.add_argument("number")
    c.add_argument("--pdf", action="store_true")
    c.add_argument("--detail", choices=["days", "sessions"], default="days")
    c.add_argument("--out", help="directory (default: [invoice] dir)")
    c = isub.add_parser("sent", help="mark as sent to the client")
    c.add_argument("number")
    c.add_argument("--date", help="YYYY-MM-DD (default: today)")
    c = isub.add_parser("paid", help="record a payment (default: the full outstanding amount)")
    c.add_argument("number")
    c.add_argument("--amount", type=float)
    c.add_argument("--date", help="YYYY-MM-DD (default: today)")
    c.add_argument("--note", "-n")
    c = isub.add_parser("void", help="cancel an invoice and unlock its period")
    c.add_argument("number")
    c.add_argument("--yes", action="store_true")
    c.add_argument("--force", action="store_true", help="even if payments were recorded")
    c = isub.add_parser("unbilled", help="months with tracked time that no invoice covers")
    c.add_argument("--project")
    s.set_defaults(fn=cmd_invoice)

    sub.add_parser("projects", help="list configured projects").set_defaults(fn=cmd_projects)
    sub.add_parser("where", help="print config/db paths").set_defaults(fn=cmd_where)
    s = sub.add_parser("backup", help="back up the database now (the daemon also does this daily)")
    s.add_argument("--to", help="directory (default: backup_dir from config)")
    s.add_argument("--now", action="store_true", help="timestamped extra copy, kept outside the daily rotation")
    s.add_argument("--list", action="store_true")
    s.set_defaults(fn=cmd_backup)

    s = sub.add_parser("config", help="show or change settings (rate, invoice details, ...) without hand-editing")
    csub = s.add_subparsers(dest="config_cmd", required=True)
    c = csub.add_parser("show", help="effective settings and what's still missing for invoicing")
    c = csub.add_parser("set", help="set one value, e.g.  config set rate 950   or   config set invoice.from.bank 'Bank: X' 'Acc: 1'")
    c.add_argument("key", help="rate, currency, client, client_address, invoice.vat_rate, invoice.from.name, ...")
    c.add_argument("value", nargs="+", help="a value (lists like address/bank take one argument per line)")
    c.add_argument("--project", help="for per-project settings (default: your default project)")
    c = csub.add_parser("invoice", help="guided prompts for your invoice and client details")
    c.add_argument("--project")
    c = csub.add_parser("edit", help="open the config in $EDITOR; invalid edits are rolled back")
    s.set_defaults(fn=cmd_config)

    sub.add_parser("reload", help="reload config in the running daemon").set_defaults(fn=cmd_reload)

    args = ap.parse_args(argv)
    cfg = config_mod.load()
    con = db.connect()
    try:
        args.fn(args, cfg, con)
    except BrokenPipeError:          # e.g. `uurtjie log | head`
        sys.stderr.close()
    except (InvoiceError, ConfigError, ValueError) as e:
        sys.exit(str(e))
    finally:
        con.close()


if __name__ == "__main__":
    main()
