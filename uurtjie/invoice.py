"""Invoices: issue a billing period (which locks its entries), track sent/paid, render HTML/PDF.

An invoice covers whole days d_from..d_to of one project. Issuing it:
  * numbers it (INV-2026-001, never reused, even after a void)
  * freezes the figures (hours, rate, VAT, daily breakdown) as a JSON snapshot, so a later rate or
    config change never alters an invoice that was already sent
  * locks the period: entries of that project inside it can't be edited, deleted or added without --force
Voiding an invoice releases the lock. Payments are recorded separately (part payments are fine).
"""
from __future__ import annotations

import html
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import date, timedelta
from contextlib import closing
from pathlib import Path

from . import db, money, report, storage
from .config import Config


class InvoiceError(Exception):
    pass


def fmt_money(cur: str, x: float) -> str:
    return f"{cur} {x:,.2f}".strip()


def fmt_hours(h: float) -> str:
    """Decimal hours without a misleading trailing zero: 7.3 (not 7.30, which reads like 7 h 30), 6.25."""
    t = f"{h:,.6f}".rstrip("0")
    return t + "0" if t.endswith(".") else t


def _day(s: str) -> date:
    return date.fromisoformat(s)


def period_bounds(d_from: date, d_to: date) -> tuple[int, int]:
    return report.local_midnight(d_from), report.local_midnight(d_to + timedelta(days=1))


# --------------------------------------------------------------------------- locking

def active_invoices(con, project: str | None = None):
    q = "SELECT * FROM invoices WHERE number IS NOT NULL AND status != 'void'"
    args: list = []
    if project:
        q += " AND project = ?"
        args.append(project)
    return con.execute(q + " ORDER BY period_from, id", args).fetchall()


def locked_by(con, project: str, start_ts: int, end_ts: int) -> str | None:
    """Number of the active invoice whose period overlaps [start_ts, end_ts) for this project."""
    end_ts = max(end_ts, start_ts + 1)
    for inv in active_invoices(con, project):
        s, e = period_bounds(_day(inv["period_from"]), _day(inv["period_to"]))
        if start_ts < e and end_ts > s:
            return inv["number"]
    return None


def check_unlocked(con, project: str, start_ts: int, end_ts: int, force: bool = False) -> None:
    """Exit with a helpful message if the time falls inside an invoiced period."""
    num = None if force else locked_by(con, project, start_ts, end_ts)
    if num:
        raise InvoiceError(f"that time is inside invoice {num} (already billed); "
                           f"use --force to change it anyway, or `uurtjie invoice void {num}` first")


# --------------------------------------------------------------------------- lookup / state

def find(con, ref: str):
    """Invoice by number (case-insensitive), or by a unique ending such as '001'."""
    ref = ref.strip()
    rows = con.execute("SELECT * FROM invoices WHERE number IS NOT NULL AND upper(number) = upper(?)", (ref,)).fetchall()
    if not rows:
        rows = con.execute("SELECT * FROM invoices WHERE number IS NOT NULL AND upper(number) LIKE upper(?)",
                           (f"%{ref}",)).fetchall()
    if not rows:
        raise InvoiceError(f"no invoice matches {ref!r} (see `uurtjie invoice list --all`)")
    if len(rows) > 1:
        raise InvoiceError(f"{ref!r} is ambiguous: " + ", ".join(r["number"] for r in rows))
    return rows[0]


def paid_total(con, invoice_id: int) -> float:
    return round(con.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE invoice_id=?",
                             (invoice_id,)).fetchone()[0], 2)


def state(inv, paid: float, today: date) -> str:
    if inv["status"] == "void":
        return "void"
    if paid + 0.005 >= inv["total"]:
        return "paid"
    if _day(inv["due_date"]) < today:
        return "overdue"
    if paid > 0:
        return "part-paid"
    return "sent" if inv["status"] == "sent" else "issued"


def rows(con, today: date | None = None, project: str | None = None, include_void: bool = False) -> list[dict]:
    today = today or date.today()
    q = "SELECT * FROM invoices WHERE number IS NOT NULL"
    args: list = []
    if project:
        q += " AND project = ?"
        args.append(project)
    out = []
    for inv in con.execute(q + " ORDER BY issue_date, id", args):
        paid = paid_total(con, inv["id"])
        st = state(inv, paid, today)
        if st == "void" and not include_void:
            continue
        out.append({"id": inv["id"], "number": inv["number"], "project": inv["project"],
                    "period_from": inv["period_from"], "period_to": inv["period_to"],
                    "issue_date": inv["issue_date"], "due_date": inv["due_date"], "currency": inv["currency"] or "",
                    "hours": inv["hours"], "subtotal": inv["amount"], "vat_amount": inv["vat_amount"],
                    "total": inv["total"], "paid": paid, "outstanding": 0.0 if st in ("paid", "void") else round(inv["total"] - paid, 2),
                    "state": st})
    return out


def outstanding_summary(con, today: date | None = None) -> dict:
    """What clients still owe: {'text': 'ZAR 4,200.00 outstanding · 1 overdue', 'overdue': n, 'by_currency': {...}}."""
    owed: dict[str, float] = {}
    overdue = 0
    for r in rows(con, today):
        if r["outstanding"] > 0:
            owed[r["currency"]] = round(owed.get(r["currency"], 0) + r["outstanding"], 2)
            overdue += r["state"] == "overdue"
    if not owed:
        return {"text": "", "overdue": 0, "by_currency": {}}
    text = " + ".join(fmt_money(c, a) for c, a in sorted(owed.items())) + " outstanding"
    if overdue:
        text += f" · {overdue} overdue"
    return {"text": text, "overdue": overdue, "by_currency": owed}


# --------------------------------------------------------------------------- create

def create(con, cfg: Config, project_id: str, d_from: date, d_to: date, *, today: date | None = None,
           vat_rate: float | None = None, due_days: int | None = None, force: bool = False,
           backup: bool = True, dry_run: bool = False, with_git: bool = True) -> dict:
    """Issue an invoice for whole days d_from..d_to. Returns the snapshot dict (with 'number')."""
    today = today or date.today()
    proj = cfg.project(project_id)
    if not proj:
        raise InvoiceError(f"unknown project {project_id!r}")
    if proj.rate <= 0:
        raise InvoiceError(f"project {project_id!r} has no hourly rate; set `rate = …` (and currency) in the config, "
                           "then `uurtjie reload`")
    if d_to < d_from:
        raise InvoiceError("the end date is before the start date")
    if d_to >= today and not force:
        raise InvoiceError(f"{d_to} isn't finished yet; invoice up to {today - timedelta(days=1)} "
                           "or use --force to include today (time tracked later today would stay unbilled)")
    for other in active_invoices(con, project_id):
        if _day(other["period_from"]) <= d_to and _day(other["period_to"]) >= d_from and not force:
            raise InvoiceError(f"{other['period_from']}..{other['period_to']} overlaps invoice {other['number']}; "
                               "void it first or choose another range (--force to override)")

    bounds = period_bounds(d_from, d_to)
    # A coherent SQLite snapshot avoids mixing a timer read with spans from another
    # instant. Enrichment runs on the copy without holding up daemon/CLI writes.
    with closing(sqlite3.connect(":memory:", isolation_level=None)) as view:
        con.backup(view)
        view.row_factory = sqlite3.Row
        before = db.accounting_state(view, *bounds)
        cached = {r["key"] for r in view.execute("SELECT key FROM summaries")}
        rep = report.range_report(view, cfg, d_from, d_to, project_id, with_git=with_git)
        for row in view.execute("SELECT * FROM summaries"):
            if row["key"] not in cached:
                con.execute("INSERT OR IGNORE INTO summaries(key, summary, created_at) VALUES (?,?,?)", tuple(row))
    if rep["total_hours"] <= 0:
        raise InvoiceError("no billable time in that period")
    iv = cfg.invoice
    vat = iv.vat_rate if vat_rate is None else vat_rate
    terms = iv.payment_terms_days if due_days is None else due_days
    subtotal = money.cents(rep["total_amount"])
    vat_amount = money.cents(money.decimal(subtotal) * money.decimal(vat) / 100)
    total = money.cents(money.decimal(subtotal) + money.decimal(vat_amount))
    issue = today
    due = issue + timedelta(days=terms)
    month_label = rep["period"] if len(rep["period"]) == 7 else f"{d_from}..{d_to}"

    snap = {
        "number": None, "issue_date": issue.isoformat(), "due_date": due.isoformat(), "terms_days": terms,
        "period_from": d_from.isoformat(), "period_to": d_to.isoformat(), "period": month_label,
        "project": {"id": proj.id, "name": proj.name},
        "seller": iv.seller,
        "client": {"name": proj.client or proj.name, "address": proj.client_address,
                   "vat_number": proj.client_vat_number, "reference": proj.client_reference},
        "currency": proj.currency, "rate": proj.rate, "human_factor": rep["human_factor"],
        "rounding_minutes": rep["rounding_minutes"], "hours": rep["total_hours"],
        "subtotal": subtotal, "vat_rate": vat, "vat_amount": vat_amount, "total": total,
        "days": rep["days"],
    }
    if dry_run:
        return snap

    if backup:
        try:
            db.backup(con, cfg.backup_dir, cfg.backup_keep,
                      name=f"uurtjie-{time.strftime('%Y-%m-%d_%H%M%S')}-pre-invoice.db")
        except Exception:                      # a failed safety copy must not block billing
            pass

    year = issue.year
    with db.transaction(con):
        # Git and summary commands ran without a write lock. The accounting inputs must
        # still agree before the snapshot is issued; otherwise ask for a fresh report.
        if before != db.accounting_state(con, *bounds):
            raise InvoiceError("time entries changed while preparing the invoice; retry the command")
        for other in active_invoices(con, project_id):
            if _day(other["period_from"]) <= d_to and _day(other["period_to"]) >= d_from and not force:
                raise InvoiceError(f"that period overlaps invoice {other['number']}; void it first")
        try:
            seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM invoices WHERE substr(issue_date,1,4)=?",
                              (str(year),)).fetchone()[0]
            number = iv.number_format.format(year=year, seq=seq, project=proj.id)
            snap["number"] = number
            con.execute(
                "INSERT INTO invoices(project, period, hours, amount, currency, file, generated_at, number, seq,"
                " period_from, period_to, status, issue_date, due_date, vat_rate, vat_amount, total, snapshot)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, month_label, rep["total_hours"], subtotal, proj.currency, None, int(time.time()),
                 number, seq, snap["period_from"], snap["period_to"], "issued", snap["issue_date"],
                 snap["due_date"], vat, vat_amount, total, json.dumps(snap, ensure_ascii=False)))
        except sqlite3.IntegrityError as exc:
            raise InvoiceError("couldn't allocate an invoice number (number_format may not be unique)") from exc
    return snap


def locked_entries(con, project: str, d_from: date, d_to: date) -> int:
    s, e = period_bounds(d_from, d_to)
    return con.execute("SELECT count(*) FROM spans WHERE project=? AND end_ts > ? AND start_ts < ?",
                       (project, s, e)).fetchone()[0]


def snapshot(inv) -> dict:
    snap = json.loads(inv["snapshot"] or "{}")
    snap["number"] = inv["number"]
    return snap


# --------------------------------------------------------------------------- lifecycle

def mark_sent(con, ref: str, on: date | None = None) -> str:
    inv = find(con, ref)
    if inv["status"] == "void":
        raise InvoiceError(f"{inv['number']} is void")
    ts = int(time.mktime((on or date.today()).timetuple()))
    con.execute("UPDATE invoices SET status='sent', sent_at=? WHERE id=?", (ts, inv["id"]))
    return inv["number"]


def record_payment(con, ref: str, amount: float | None = None, on: date | None = None,
                   note: str | None = None) -> dict:
    inv = find(con, ref)
    if inv["status"] == "void":
        raise InvoiceError(f"{inv['number']} is void")
    paid = paid_total(con, inv["id"])
    due = round(inv["total"] - paid, 2)
    if due <= 0:
        raise InvoiceError(f"{inv['number']} is already fully paid")
    amount = due if amount is None else round(amount, 2)
    if amount <= 0:
        raise InvoiceError("amount must be positive")
    day = (on or date.today()).isoformat()
    con.execute("INSERT INTO payments(invoice_id, paid_on, amount, note, created_at) VALUES (?,?,?,?,?)",
                (inv["id"], day, amount, note, int(time.time())))
    if inv["status"] == "issued":
        con.execute("UPDATE invoices SET status='sent' WHERE id=?", (inv["id"],))   # paying implies it was sent
    return {"number": inv["number"], "amount": amount, "currency": inv["currency"] or "",
            "outstanding": round(due - amount, 2)}


def void(con, ref: str, force: bool = False) -> str:
    inv = find(con, ref)
    if inv["status"] == "void":
        raise InvoiceError(f"{inv['number']} is already void")
    if paid_total(con, inv["id"]) > 0 and not force:
        raise InvoiceError(f"{inv['number']} has payments recorded; use --force to void it anyway")
    con.execute("UPDATE invoices SET status='void', void_at=? WHERE id=?", (int(time.time()), inv["id"]))
    return inv["number"]


def unbilled(con, cfg: Config, today: date | None = None, project: str | None = None) -> list[dict]:
    """Months with billable time that no active invoice fully covers (the current month is 'open')."""
    today = today or date.today()
    out = []
    for p in cfg.projects:
        if project and p.id != project:
            continue
        r = con.execute("SELECT MIN(start_ts), MAX(end_ts) FROM spans WHERE project=? AND billable=1", (p.id,)).fetchone()
        if not r or r[0] is None:
            continue
        cur = date.fromtimestamp(r[0]).replace(day=1)
        last = date.fromtimestamp(r[1]).replace(day=1)
        invs = [(_day(i["period_from"]), _day(i["period_to"])) for i in active_invoices(con, p.id)]
        while cur <= last:
            nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
            m_end = nxt - timedelta(days=1)
            covered = set()
            for a, b in invs:
                day = max(a, cur)
                while day <= min(b, m_end):
                    covered.add(day)
                    day += timedelta(days=1)
            if len(covered) < (m_end - cur).days + 1:
                rep = report.range_report(con, cfg, cur, m_end, p.id, with_git=False,
                                          period=cur.strftime("%Y-%m"), excluded_days=covered)
                if rep["total_hours"] > 0:
                    out.append({"project": p.id, "month": rep["period"], "hours": rep["total_hours"],
                                "amount": rep["total_amount"], "currency": rep["currency"],
                                "open": m_end >= today,
                                "partly_invoiced": any(a <= m_end and b >= cur for a, b in invs)})
            cur = nxt
    return out


# --------------------------------------------------------------------------- rendering

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #1c1f23; margin: 0; background: #fff; }
.page { max-width: 780px; margin: 0 auto; padding: 40px 36px; }
h1 { font-size: 30px; letter-spacing: .04em; margin: 0; }
.muted { color: #5b6370; }
.head { display: flex; justify-content: space-between; gap: 24px; align-items: flex-start; }
.meta td { padding: 1px 0 1px 18px; text-align: right; }
.meta td:first-child { color: #5b6370; }
.parties { display: flex; gap: 40px; margin: 28px 0 8px; }
.parties div { flex: 1; }
.label { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: #5b6370; margin-bottom: 4px; }
table.items { width: 100%; border-collapse: collapse; margin-top: 18px; }
table.items th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: #5b6370;
  border-bottom: 2px solid #1c1f23; padding: 6px 8px; }
table.items td { padding: 8px; border-bottom: 1px solid #e3e6ea; vertical-align: top; }
.num, table.items th.num { text-align: right; white-space: nowrap; }
table.totals { margin: 14px 0 0 auto; border-collapse: collapse; min-width: 280px; }
table.totals td { padding: 4px 8px; text-align: right; }
table.totals tr.grand td { font-weight: 700; font-size: 16px; border-top: 2px solid #1c1f23; padding-top: 8px; }
.box { border: 1px solid #e3e6ea; border-radius: 6px; padding: 12px 14px; margin-top: 22px; }
.sessions { font-size: 12px; color: #3d4450; }
.foot { margin-top: 28px; font-size: 12px; }
.pagebreak { page-break-before: always; }
@page { size: A4; margin: 16mm; }
@media print { .page { padding: 0; } }
"""


def _fmt_day(s: str) -> str:
    return _day(s).strftime("%-d %b %Y")


def _period_label(snap: dict) -> str:
    a, b = _day(snap["period_from"]), _day(snap["period_to"])
    first = a.replace(day=1)
    last = (date(a.year + (a.month == 12), a.month % 12 + 1, 1) - timedelta(days=1))
    if a == first and b == last:
        return a.strftime("%B %Y")
    return f"{_fmt_day(snap['period_from'])} – {_fmt_day(snap['period_to'])}"


def render_html(snap: dict, detail: str = "days") -> str:
    """Printable invoice + timesheet. detail='sessions' adds per-session work summaries (may contain page titles)."""
    e = html.escape
    cur = snap["currency"]
    seller, client = snap.get("seller") or {}, snap.get("client") or {}

    def block(lines) -> str:
        return "<br>".join(e(x) for x in lines if x)

    seller_lines = [seller.get("name", "")] + list(seller.get("address") or []) + \
        [x for x in (seller.get("email", ""), seller.get("phone", "")) if x]
    if seller.get("reg_number"):
        seller_lines.append(f"Reg. no. {seller['reg_number']}")
    if seller.get("vat_number"):
        seller_lines.append(f"VAT no. {seller['vat_number']}")
    client_lines = [client.get("name", "")] + list(client.get("address") or [])
    if client.get("vat_number"):
        client_lines.append(f"VAT no. {client['vat_number']}")

    meta = [("Invoice no.", snap["number"]), ("Issued", _fmt_day(snap["issue_date"])),
            ("Due", _fmt_day(snap["due_date"]))]
    if client.get("reference"):
        meta.append(("Reference", client["reference"]))
    meta_rows = "".join(f"<tr><td>{e(k)}</td><td><b>{e(str(v))}</b></td></tr>" for k, v in meta)

    desc = f'{e(snap["project"]["name"])} — {e(_period_label(snap))}'
    item = (f'<tr><td>{desc}<div class="muted">Hours as tracked, rounded up to {snap["rounding_minutes"]} min per day'
            f'{"; factor " + str(snap["human_factor"]) if snap["human_factor"] != 1 else ""}</div></td>'
            f'<td class="num">{fmt_hours(snap["hours"])}</td><td class="num">{e(fmt_money(cur, snap["rate"]))}</td>'
            f'<td class="num">{e(fmt_money(cur, snap["subtotal"]))}</td></tr>')

    totals = [("Subtotal", fmt_money(cur, snap["subtotal"]))]
    if snap["vat_rate"]:
        totals.append((f'VAT {snap["vat_rate"]:g}%', fmt_money(cur, snap["vat_amount"])))
    total_rows = "".join(f'<tr><td>{e(k)}</td><td>{e(v)}</td></tr>' for k, v in totals)
    total_rows += f'<tr class="grand"><td>Total due</td><td>{e(fmt_money(cur, snap["total"]))}</td></tr>'

    pay_lines = [f'Please pay <b>{e(fmt_money(cur, snap["total"]))}</b> by <b>{e(_fmt_day(snap["due_date"]))}</b> '
                 f'({snap["terms_days"]} days), quoting <b>{e(snap["number"])}</b>.']
    if seller.get("bank"):
        pay_lines.append("<br>".join(e(x) for x in seller["bank"]))
    pay = '<div class="box">' + "<br><br>".join(pay_lines) + "</div>"

    day_rows = []
    for d in snap["days"]:
        work = ""
        if detail == "sessions":
            work = '<div class="sessions">' + "<br>".join(
                f'{e(s["start"])}–{e(s["end"])} · {fmt_hours(s["hours"])} h · {e(s.get("summary", ""))}'
                for s in d.get("sessions", [])) + "</div>"
        day_rows.append(f'<tr><td>{e(_fmt_day(d["date"]))}<span class="muted"> {e(d["weekday"])}</span>{work}</td>'
                        f'<td class="num">{fmt_hours(d["hours"])}</td><td class="num">{e(fmt_money(cur, d["amount"]))}</td></tr>')

    footer = f'<div class="foot muted">{e(seller["footer"])}</div>' if seller.get("footer") else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invoice {e(snap["number"])}</title><style>{CSS}</style></head>
<body><div class="page">
<div class="head"><div><h1>INVOICE</h1><div class="muted">{e(snap["number"])}</div></div>
<table class="meta">{meta_rows}</table></div>
<div class="parties"><div><div class="label">From</div>{block(seller_lines)}</div>
<div><div class="label">Bill to</div>{block(client_lines)}</div></div>
<table class="items"><tr><th>Description</th><th class="num">Hours</th><th class="num">Rate</th><th class="num">Amount</th></tr>{item}</table>
<table class="totals">{total_rows}</table>
{pay}{footer}
<div class="pagebreak"></div>
<h2>Timesheet — {e(_period_label(snap))}</h2>
<div class="muted">{e(snap["project"]["name"])} · invoice {e(snap["number"])} · hours are decimal (7.5 = 7 h 30 min)</div>
<table class="items"><tr><th>Date</th><th class="num">Hours</th><th class="num">Amount</th></tr>{"".join(day_rows)}
<tr><td><b>Total</b></td><td class="num"><b>{fmt_hours(snap["hours"])}</b></td><td class="num"><b>{e(fmt_money(cur, snap["subtotal"]))}</b></td></tr></table>
</div></body></html>
"""


def html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """Print the HTML with headless Chrome/Chromium (separate profile, so a running browser isn't touched)."""
    exe = next((p for p in (shutil.which(n) for n in ("google-chrome-stable", "chromium", "google-chrome")) if p), None)
    if not exe:
        return False
    with tempfile.TemporaryDirectory(prefix="uurtjie-pdf-", ignore_cleanup_errors=True) as tmp:
        cmd = [exe, "--headless=new", "--disable-gpu", f"--user-data-dir={tmp}", "--no-pdf-header-footer",
               f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri()]
        try:
            subprocess.run(cmd, capture_output=True, timeout=90, umask=0o077)
        except (OSError, subprocess.TimeoutExpired):
            return False
    return pdf_path.exists() and pdf_path.stat().st_size > 0


def write_files(snap: dict, out_dir: Path, pdf: bool = False, detail: str = "days") -> tuple[Path, Path | None]:
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = snap["number"].replace("/", "-")
    html_path = out_dir / f"{base}.html"
    storage.atomic_write(html_path, render_html(snap, detail))
    pdf_path = None
    if pdf:
        target = out_dir / f"{base}.pdf"
        with tempfile.TemporaryDirectory(prefix=".uurtjie-render-", dir=out_dir) as tmp:
            fresh = Path(tmp) / "invoice.pdf"
            if html_to_pdf(html_path, fresh):
                storage.private_file(fresh)
                fresh.replace(target)
                pdf_path = target
    return html_path, pdf_path
