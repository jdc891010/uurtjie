"""SQLite store. One table of time spans; everything else is derived."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    id         INTEGER PRIMARY KEY,
    project    TEXT    NOT NULL,
    source     TEXT    NOT NULL,          -- terminal | chrome | window | manual
    ctx_key    TEXT,                      -- e.g. herdr workspace id, window class
    start_ts   INTEGER NOT NULL,          -- unix seconds
    end_ts     INTEGER NOT NULL,
    detail     TEXT,                      -- JSON: workspace label, cwd, agent, window title
    note       TEXT,
    billable   INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS spans_start ON spans(start_ts);
CREATE INDEX IF NOT EXISTS spans_project ON spans(project, start_ts);

CREATE TABLE IF NOT EXISTS timer (            -- at most one running manual timer
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    project  TEXT    NOT NULL,
    start_ts INTEGER NOT NULL,
    note     TEXT
);

CREATE TABLE IF NOT EXISTS invoices (         -- log of generated monthly reports
    id           INTEGER PRIMARY KEY,
    project      TEXT    NOT NULL,
    period       TEXT    NOT NULL,           -- YYYY-MM
    hours        REAL    NOT NULL,
    amount       REAL    NOT NULL,
    currency     TEXT,
    file         TEXT,
    generated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (        -- money received against an invoice
    id         INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL,
    paid_on    TEXT    NOT NULL,             -- YYYY-MM-DD
    amount     REAL    NOT NULL,
    note       TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (        -- cached summary_command output per session
    key        TEXT PRIMARY KEY,
    summary    TEXT NOT NULL,
    created_at INTEGER
);

CREATE VIEW IF NOT EXISTS spans_local AS
SELECT id, project, source, ctx_key,
       datetime(start_ts, 'unixepoch', 'localtime') AS start_local,
       datetime(end_ts,   'unixepoch', 'localtime') AS end_local,
       round((end_ts - start_ts) / 3600.0, 3)      AS hours,
       detail, note, billable
FROM spans;
"""


# Columns added to the original `invoices` log table (issued invoices, see invoice.py).
INVOICE_COLUMNS = [
    ("number", "TEXT"), ("seq", "INTEGER"), ("period_from", "TEXT"), ("period_to", "TEXT"),
    ("status", "TEXT"), ("issue_date", "TEXT"), ("due_date", "TEXT"), ("vat_rate", "REAL"),
    ("vat_amount", "REAL"), ("total", "REAL"), ("sent_at", "INTEGER"), ("void_at", "INTEGER"),
    ("snapshot", "TEXT"), ("notes", "TEXT"),
]


def _migrate(con) -> None:
    """Additive only, safe to run from several processes at once."""
    have = {r["name"] for r in con.execute("PRAGMA table_info(invoices)")}
    for name, decl in INVOICE_COLUMNS:
        if name not in have:
            try:
                con.execute(f"ALTER TABLE invoices ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    raise
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS invoices_number ON invoices(number)")
    con.execute("CREATE INDEX IF NOT EXISTS invoices_project ON invoices(project, period_from)")
    con.execute("CREATE INDEX IF NOT EXISTS payments_invoice ON payments(invoice_id)")


def db_path() -> Path:
    return DATA_DIR / "uurtjie.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10, isolation_level=None)  # autocommit
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def open_span(con, project: str, source: str, ctx_key: str | None, start: int, end: int | None = None,
              detail: dict | None = None, note: str | None = None, billable: bool = True) -> int:
    now = int(time.time())
    cur = con.execute(
        "INSERT INTO spans(project, source, ctx_key, start_ts, end_ts, detail, note, billable, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (project, source, ctx_key, start, end if end is not None else start,
         json.dumps(detail) if detail else None, note, int(billable), now, now),
    )
    return cur.lastrowid


def extend_span(con, span_id: int, end: int, detail: dict | None = None) -> None:
    if detail is None:
        con.execute("UPDATE spans SET end_ts=MAX(end_ts, ?), updated_at=? WHERE id=?",
                    (end, int(time.time()), span_id))
    else:
        con.execute("UPDATE spans SET end_ts=MAX(end_ts, ?), detail=?, updated_at=? WHERE id=?",
                    (end, json.dumps(detail), int(time.time()), span_id))


def truncate_span(con, span_id: int, end: int) -> None:
    """Pull a span's end back (used when idle is detected after the fact)."""
    con.execute("UPDATE spans SET end_ts=MAX(start_ts, MIN(end_ts, ?)), updated_at=? WHERE id=?",
                (end, int(time.time()), span_id))


def spans_between(con, start: int, end: int, project: str | None = None, billable_only: bool = False):
    q = "SELECT * FROM spans WHERE end_ts > ? AND start_ts < ?"
    args: list = [start, end]
    if project:
        q += " AND project = ?"
        args.append(project)
    if billable_only:
        q += " AND billable = 1"
    return con.execute(q + " ORDER BY start_ts", args).fetchall()


# --- backups --------------------------------------------------------------

def backup(con, backup_dir: Path, keep: int = 30, name: str | None = None) -> Path:
    """Consistent online copy (safe while the daemon writes), verified, then old ones pruned."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    name = name or f"uurtjie-{time.strftime('%Y-%m-%d')}.db"
    dest = backup_dir / name
    tmp = dest.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(tmp)
    try:
        con.backup(out)
        ok = out.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        out.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed integrity check: {ok}")
    tmp.replace(dest)
    daily = sorted(backup_dir.glob("uurtjie-????-??-??.db"))
    for old in daily[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    return dest


def backup_if_due(con, backup_dir: Path, keep: int) -> Path | None:
    dest = backup_dir / f"uurtjie-{time.strftime('%Y-%m-%d')}.db"
    return None if dest.exists() else backup(con, backup_dir, keep)


# --- manual timer ---------------------------------------------------------

def timer_get(con):
    return con.execute("SELECT * FROM timer WHERE id=1").fetchone()


def timer_start(con, project: str, note: str | None = None, at: int | None = None):
    if timer_get(con):
        raise RuntimeError("a timer is already running; stop it first")
    con.execute("INSERT INTO timer(id, project, start_ts, note) VALUES (1,?,?,?)",
                (project, at or int(time.time()), note))


def timer_stop(con, at: int | None = None, note: str | None = None) -> int | None:
    t = timer_get(con)
    if not t:
        return None
    end = at or int(time.time())
    sid = open_span(con, t["project"], "manual", "timer", t["start_ts"], end,
                    note=note or t["note"])
    con.execute("DELETE FROM timer WHERE id=1")
    return sid
