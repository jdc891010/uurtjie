#!/usr/bin/env python3
"""Regenerates the README images in docs/images/ from the real code, using made-up demo data.

    python3 scripts/screenshots.py            # needs git; Chrome/Chromium for the invoice PNG

Everything runs against a throwaway config + database in a temp dir (UURTJIE_CONFIG / UURTJIE_DATA /
XDG_RUNTIME_DIR), with a fictional client (Acme) and freelancer, so no real tracking data can end up
in the repo. Terminal images are the real CLI output; the bar widget/panel image is drawn from the real
`uurtjie status --json` with the same layout as omarchy-plugin/jd.uurtjie/Panel.qml. The two diagrams are
static. Re-run after changing output formats, the widget, or how tracking works.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "docs/images"
MONO = "'JetBrains Mono','DejaVu Sans Mono','Liberation Mono',ui-monospace,Menlo,Consolas,monospace"
SANS = "-apple-system,'Segoe UI','Helvetica Neue',Arial,sans-serif"
CW = 8.4          # monospace advance at 14px, generous so text never overflows its box
# Nerd Font glyphs used by the widget don't exist in normal fonts; draw stand-ins instead.
GLYPHS = {"󰥔": "◷", "󰔛": "⏱", "󰍬": "🎙", "󰒲": "☾", "󰚌": "✕"}

THEME = """<style>
  .bg{fill:#ffffff} .card{fill:#f6f8fa;stroke:#d0d7de} .fg{fill:#1f2328} .mute{fill:#59636e}
  .line{stroke:#59636e} .acc{fill:#0969da} .soft{fill:#ddf4ff} .softg{fill:#dafbe1} .softy{fill:#fff8c5}
  .softr{fill:#ffebe9}
  @media (prefers-color-scheme: dark){
    .bg{fill:#0d1117} .card{fill:#161b22;stroke:#30363d} .fg{fill:#e6edf3} .mute{fill:#9198a1}
    .line{stroke:#9198a1} .acc{fill:#4493f8} .soft{fill:#0c2d6b} .softg{fill:#0f3d1d} .softy{fill:#3b2e0a}
    .softr{fill:#4a1418}
  }
</style>"""


def glyphs(s: str) -> str:
    for k, v in GLYPHS.items():
        s = s.replace(k, v)
    return s


def write(name: str, svg: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(svg)
    print(f"wrote docs/images/{name}")


# --------------------------------------------------------------------------- demo data

def demo_env(tmp: Path) -> dict:
    return {**os.environ, "UURTJIE_CONFIG": str(tmp / "config.toml"), "UURTJIE_DATA": str(tmp / "data"),
            "XDG_RUNTIME_DIR": str(tmp / "run"), "PYTHONPATH": str(REPO), "GIT_CONFIG_GLOBAL": os.devnull}


def git_repo(path: Path, commits: list[tuple[datetime, str]]) -> None:
    path.mkdir(parents=True)
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "Jane Doe",
           "GIT_AUTHOR_EMAIL": "jane@example.com", "GIT_COMMITTER_NAME": "Jane Doe",
           "GIT_COMMITTER_EMAIL": "jane@example.com"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, env=env, check=True)
    for when, msg in commits:
        stamp = when.astimezone().isoformat()
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", msg], cwd=path, check=True,
                       env={**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp})


def seed(tmp: Path) -> dict:
    """Config + a month and a half of plausible tracking for Acme. Returns facts the renderers need."""
    code = tmp / "code"
    today = date.today()
    at = lambda d, hm: datetime.combine(d, datetime.strptime(hm, "%H:%M").time())  # noqa: E731
    git_repo(code / "acme-api", [(at(today, "08:20"), "Add OAuth callback for SSO login"),
                                 (at(today, "10:31"), "Fix token refresh race")])
    (tmp / "config.toml").write_text(f"""
tick = 5
sleep_minutes = 5
rounding_minutes = 6
default_project = "acme"
git_author = "jane@example.com"
[invoice]
vat_rate = 15
payment_terms_days = 14
dir = "{tmp / 'invoices'}"
[invoice.from]
name = "Jane Doe Consulting"
address = ["1 Example Road", "Cape Town, 8001"]
email = "jane@example.com"
vat_number = "4000000000"
bank = ["Bank: Example Bank", "Account name: Jane Doe Consulting", "Account no.: 000 000 000", "Branch code: 000000"]
footer = "Thank you for your business."

[[projects]]
id = "acme"
name = "Acme platform"
client = "Acme (Pty) Ltd"
rate = 450
currency = "ZAR"
client_address = ["12 Sample Street", "Johannesburg, 2000"]
client_reference = "PO-2026-117"
paths = ["{code / 'acme-api'}", "{code / 'acme-web'}"]
[[projects.windows]]
class = "chrome-acme"

[[projects]]
id = "internal"
name = "Internal"
rate = 0
paths = ["{code / 'internal'}"]
""")
    sys.path.insert(0, str(REPO))
    os.environ.update(demo_env(tmp))
    from uurtjie import db  # noqa: E402  (paths are read from the environment at import)
    con = db.connect()
    ts = lambda d, hm: int(at(d, hm).timestamp())  # noqa: E731
    api = {"workspace_label": "acme-api", "cwd": str(code / "acme-api"), "agent": "claude", "branch": "main"}
    web = {"workspace_label": "acme-web", "cwd": str(code / "acme-web"), "agent": "codex"}
    jira = {"class": "chrome-acme", "window": "ACME-142 SSO login · Jira"}
    call = {"class": "chrome-acme", "app": "Google Chrome"}

    def day(d: date, blocks):
        for project, source, a, b, det in blocks:
            key = det.get("workspace_label") or det.get("class") or source
            db.open_span(con, project, source, key, ts(d, a), ts(d, b), det)

    # Past weekdays since the first of last month: ~6-7 h a day.
    first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    d = first
    while d < today:
        if d.weekday() < 5:
            day(d, [("acme", "terminal", "08:10", "10:20", api), ("acme", "chrome", "10:24", "10:55", jira),
                    ("acme", "terminal", "11:00", "12:40", web), ("acme", "terminal", "13:30", "15:45", api)])
            if d.day % 3 == 0:
                day(d, [("acme", "meeting", "16:00", "16:45", call)])
        d += timedelta(days=1)
    # Today, up to shortly before now: terminal, Jira, a call, a short internal task.
    end = (datetime.now() - timedelta(minutes=8)).strftime("%H:%M") if datetime.now().hour >= 12 else "11:50"
    day(today, [("acme", "terminal", "07:40", "08:55", api), ("acme", "chrome", "08:57", "09:10", jira),
                ("acme", "meeting", "09:15", "09:50", call), ("acme", "terminal", "09:52", "10:45", api),
                ("internal", "terminal", "10:50", "11:05", {"workspace_label": "dotfiles", "cwd": str(code / "internal")}),
                ("acme", "terminal", "11:08", end, web)])
    con.commit()
    run_dir = tmp / "run/uurtjie"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({     # "the daemon is running and you're in acme-web"
        "ts": int(time.time()) + 3600, "pid": 1, "idle": False, "tick": 5, "meeting": None,
        "active": {"project": "acme", "source": "terminal", "key": "herdr:2", "detail": web}}))
    return {"last_month": first}


def cli(tmp: Path, *args: str) -> str:
    r = subprocess.run([sys.executable, "-m", "uurtjie", *args], env=demo_env(tmp), capture_output=True,
                       text=True, cwd=tmp)
    if r.returncode:
        raise SystemExit(f"uurtjie {' '.join(args)} failed:\n{r.stderr}")
    return r.stdout.rstrip("\n").replace(str(tmp), "~")


# --------------------------------------------------------------------------- renderers

def terminal(name: str, title: str, blocks: list[tuple[str, str]]) -> None:
    """A dark terminal window showing `$ command` + its real output for each block."""
    lines: list[tuple[str, str]] = []
    for cmd, out in blocks:
        lines.append(("cmd", cmd))
        lines += [("out", l) for l in glyphs(out).splitlines()]
        lines.append(("out", ""))
    lines.pop()
    width = max(560, int(max(len(l) + 2 for _, l in lines) * CW + 44))
    height = 44 + len(lines) * 20 + 18
    rows = []
    for i, (kind, text) in enumerate(lines):
        y = 58 + i * 20
        if kind == "cmd":
            rows.append(f'<text x="22" y="{y}" fill="#7ee787" xml:space="preserve">$ <tspan fill="#e6edf3" '
                        f'font-weight="600">{escape(text)}</tspan></text>')
        else:
            rows.append(f'<text x="22" y="{y}" fill="#c9d1d9" xml:space="preserve">{escape(text)}</text>')
    write(name, f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">
<rect width="{width}" height="{height}" rx="10" fill="#0d1117" stroke="#30363d"/>
<path d="M10 0H{width - 10}A10 10 0 0 1 {width} 10V30H0V10A10 10 0 0 1 10 0Z" fill="#161b22"/>
<circle cx="18" cy="15" r="5.5" fill="#ff5f57"/><circle cx="36" cy="15" r="5.5" fill="#febc2e"/><circle cx="54" cy="15" r="5.5" fill="#28c840"/>
<text x="{width / 2}" y="20" fill="#8b949e" font-family="{SANS}" font-size="12" text-anchor="middle">{escape(title)}</text>
<g font-family="{MONO}" font-size="14">{''.join(rows)}</g>
</svg>
""")


def widget(status: dict) -> None:
    """Bar strip + the panel that opens on click, laid out like Panel.qml."""
    W, pad, fg = 540, 24, "#cdd6f4"
    bar = glyphs(status["bar_text"])
    head = [f'<rect width="{W}" height="34" rx="8" fill="#11111b"/>',
            f'<text x="14" y="22" fill="#6c7086" font-family="{SANS}" font-size="13">1  2  3</text>',
            f'<rect x="{W - 300}" y="5" width="288" height="24" rx="6" fill="#313244"/>',
            f'<text x="{W - 156}" y="22" fill="{fg}" font-family="{MONO}" font-size="13" '
            f'text-anchor="middle">{escape(bar)}</text>']
    y = 52
    body: list[str] = []

    def line(text, size=14, opacity=1.0, weight=400):
        nonlocal y
        y += size + 10
        body.append(f'<text x="{pad}" y="{y}" fill="{fg}" fill-opacity="{opacity}" font-family="{MONO}" '
                    f'font-size="{size}" font-weight="{weight}" xml:space="preserve">{escape(glyphs(text))}</text>')

    s = status
    line(f"Today {s['today']['hm']}", 26, weight=600)
    line(f"Week {s['week']['hm']}   ·   Month {s['month']['hm']}", opacity=0.8)
    e = s["earnings"]
    if e["month_text"]:
        line(f"Earned this month  {e['month_text']}" + (f"   (+{e['today_text']} today)" if e["today_text"] else ""))
    for p in s["today"]["projects"]:
        line(f"  {p['name']}   {p['hm']}")
    line("Today by hour · minutes worked", 12, 0.6)
    axis, ph = 26, 72                       # hour chart: 24 columns of 0-60 min, gridlines at 60/30/0
    top = y + 8
    cw = (W - 2 * pad - axis) / 24
    for m in (60, 30, 0):
        gy = top + ph * (1 - m / 60)
        body.append(f'<text x="{pad + axis - 6}" y="{gy + 4}" fill="{fg}" fill-opacity=".6" font-family="{MONO}" '
                    f'font-size="10" text-anchor="end">{m}</text>'
                    f'<rect x="{pad + axis}" y="{gy}" width="{W - 2 * pad - axis}" height="1" fill="{fg}" fill-opacity=".15"/>')
    for h, mins in enumerate(s["hourly"]["minutes"]):
        x, bw = pad + axis + h * cw + cw * 0.14, cw * 0.72
        now = h == s["hourly"]["now_hour"]
        body.append(f'<rect x="{x:.1f}" y="{top}" width="{bw:.1f}" height="{ph}" fill="{fg}" fill-opacity="{.2 if now else .07}"/>')
        if mins > 0:
            bh = max(2, ph * min(60, mins) / 60)
            body.append(f'<rect x="{x:.1f}" y="{top + ph - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="{fg}" fill-opacity=".8"/>')
        if h % 3 == 0:
            body.append(f'<text x="{x + bw / 2:.1f}" y="{top + ph + 14}" fill="{fg}" fill-opacity="{1 if now else .6}" '
                        f'font-family="{MONO}" font-size="10" text-anchor="middle">{h}</text>')
    y = top + ph + 14
    a = s.get("active")
    if a:
        line(f"● {a['name']} · {a['source']} · {a['label']}" + (f" ({a['agent']})" if a.get("agent") else ""))
    if (s.get("invoices") or {}).get("text"):
        line(f"Invoices: {s['invoices']['text']}", opacity=0.6)
    y += 14
    x = pad
    for label in ("Start timer", "Week in terminal", "Export", "Live logs"):
        w = len(label) * 7.6 + 22
        body.append(f'<rect x="{x}" y="{y}" width="{w:.0f}" height="28" rx="6" fill="none" stroke="{fg}" stroke-opacity=".75"/>'
                    f'<text x="{x + w / 2:.0f}" y="{y + 19}" fill="{fg}" font-family="{SANS}" font-size="13" '
                    f'text-anchor="middle">{label}</text>')
        x += w + 8
    y += 28
    line(f"uurtjie v{s['version']}", 12, 0.5)
    H = y + 22
    panel = f'<rect y="40" width="{W}" height="{H - 40}" rx="12" fill="#1e1e2e" stroke="#45475a"/>'
    write("widget.svg", f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="The uurtjie bar widget and the panel it opens">
{panel}{''.join(head)}{''.join(body)}
</svg>
""")


def how_it_works() -> None:
    def box(x, y, w, h, title, sub, cls="card"):
        return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" class="{cls}" stroke-width="1.2"/>'
                f'<text x="{x + w / 2}" y="{y + 26}" class="fg" font-weight="600" text-anchor="middle">{title}</text>'
                + "".join(f'<text x="{x + w / 2}" y="{y + 46 + i * 17}" class="mute" font-size="12.5" '
                          f'text-anchor="middle">{t}</text>' for i, t in enumerate(sub)))

    def arrow(x1, y1, x2, y2):
        return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="line" stroke-width="1.6" marker-end="url(#a)"/>'

    inputs = [("Keyboard / mouse", ["swayidle: are you at the keys?"]),
              ("Focused window", ["Hyprland: class, title, pid"]),
              ("Terminal folder", ["Herdr pane or /proc cwd + agent"]),
              ("Microphone", ["PipeWire: is a call open?"])]
    parts = [box(20, 20 + i * 84, 240, 70, t, s) for i, (t, s) in enumerate(inputs)]
    parts += [arrow(260, 55 + i * 84, 318, 196) for i in range(4)]
    parts += [box(320, 20, 230, 86, "config.toml", ["projects: folders, windows,", "rate, currency"]),
              arrow(435, 106, 435, 128),
              box(320, 130, 230, 132, "uurtjie daemon", ["one sample every 5 s", "matches a project by",
                                                         "folder or window rule", "idle 5 min → stops"], "soft"),
              arrow(550, 196, 608, 196),
              box(610, 150, 210, 92, "SQLite database", ["entries, timers,", "invoices, payments"]),
              arrow(715, 150, 715, 108),
              box(610, 20, 210, 86, "Bar widget", ["today, earnings,", "panel + timer"]),
              arrow(715, 242, 715, 270),
              box(610, 272, 210, 86, "CLI", ["sessions, timesheets,", "invoices (HTML / PDF)"])]
    W, H = 840, 378
    write("how-it-works.svg", f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{SANS}" font-size="14" role="img" aria-label="How uurtjie works: activity inputs feed the daemon, which writes a database read by the bar widget and the CLI">
{THEME}
<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="mute"/></marker></defs>
<rect width="{W}" height="{H}" rx="12" class="bg"/>
{''.join(parts)}
</svg>
""")


def timeline() -> None:
    """One morning: what you do → entries written → sessions billed."""
    W, x0, x1 = 900, 110, 880
    t0, t1 = 8 * 60, 12 * 60

    def X(hm):
        return x0 + (int(hm[:2]) * 60 + int(hm[3:]) - t0) / (t1 - t0) * (x1 - x0)

    def seg(y, a, b, cls, label="", h=26):
        out = f'<rect x="{X(a):.1f}" y="{y}" width="{X(b) - X(a):.1f}" height="{h}" rx="5" class="{cls}"/>'
        if label:
            out += (f'<text x="{(X(a) + X(b)) / 2:.1f}" y="{y + h / 2 + 4.5}" class="fg" font-size="12" '
                    f'text-anchor="middle">{label}</text>')
        return out

    rows = [
        (40, "You", [("08:00", "09:00", "softg", "terminal · acme-api"), ("09:00", "09:03", "softy", ""),
                     ("09:03", "09:25", "softg", "Acme Jira"), ("09:25", "10:00", "softr", "away · no input"),
                     ("10:00", "10:40", "softg", "call · no typing"), ("10:40", "11:10", "card", "personal Slack"),
                     ("11:10", "12:00", "softg", "terminal · acme-web")]),
        (100, "Entries", [("08:00", "08:15", "soft"), ("08:15", "08:30", "soft"), ("08:30", "08:45", "soft"),
                          ("08:45", "09:00", "soft"), ("09:03", "09:25", "soft", "chrome"),
                          ("10:00", "10:40", "soft", "meeting"), ("11:10", "11:25", "soft"),
                          ("11:25", "11:40", "soft"), ("11:40", "11:55", "soft")]),
        (160, "Sessions", [("08:00", "09:25", "acc"), ("10:00", "10:40", "acc"), ("11:10", "11:55", "acc")]),
    ]
    parts = []
    for y, label, segs in rows:
        parts.append(f'<text x="20" y="{y + 18}" class="fg" font-weight="600">{label}</text>')
        parts += [seg(y, *s) for s in segs]
    for a, b, t in (("08:00", "09:25", "1h25"), ("10:00", "10:40", "0h40"), ("11:10", "11:55", "0h45")):
        parts.append(f'<text x="{(X(a) + X(b)) / 2:.1f}" y="178" fill="#ffffff" font-size="12.5" font-weight="600" '
                     f'text-anchor="middle">{t}</text>')
    for h in range(8, 13):
        x = X(f"{h:02d}:00")
        parts.append(f'<line x1="{x:.1f}" y1="30" x2="{x:.1f}" y2="196" class="line" stroke-opacity=".25" stroke-dasharray="3 4"/>'
                     f'<text x="{x:.1f}" y="214" class="mute" font-size="12" text-anchor="middle">{h:02d}:00</text>')
    for x, y, n in ((X("09:01"), 34, "①"), (X("09:27"), 94, "②"), (X("10:20"), 34, "③"), (X("10:55"), 34, "④")):
        parts.append(f'<text x="{x:.1f}" y="{y}" class="acc" font-size="13" text-anchor="middle">{n}</text>')
    notes = ["① A 3-minute switch to Chrome is shorter than sleep_minutes: still one session.",
             "② After 5 minutes without input (09:30) the entry is cut back to your last keypress at 09:25.",
             "③ A call counts without typing, because the Acme window has the microphone open.",
             "④ Windows and folders that match no project, like personal Slack, are never tracked.",
             "Entries are cut into 15-minute pieces so a crash loses nothing; reports only use sessions.",
             "Morning total 2h50 → rounded up to rounding_minutes (6) → 2.9 h × your rate on the invoice."]
    for i, n in enumerate(notes):
        strong = i == len(notes) - 1
        parts.append(f'<text x="20" y="{246 + i * 21}" class="{"fg" if strong else "mute"}" font-size="13"'
                     f'{" font-weight=\"600\"" if strong else ""}>{escape(n)}</text>')
    H = 246 + len(notes) * 21 + 6
    write("timeline.svg", f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="{SANS}" font-size="14" role="img" aria-label="Timeline of one morning: your activity, the entries uurtjie writes, and the sessions it bills">
{THEME}
<rect width="{W}" height="{H}" rx="12" class="bg"/>
{''.join(parts)}
</svg>
""")


def timer_popup() -> None:
    """The SUPER+ALT+T stop prompt (bin/uurtjie-timer), as printed in its popup terminal."""
    terminal("timer.svg", "Uurtjie timer", [
        ("uurtjie-timer          # 2nd press of SUPER+ALT+T",
         "Stop timer?  acme\nstarted 14:02 · 0h47\n\n[Enter] stop   [k] keep running   [d] discard\n"
         "✓ stopped #812 acme 0h47\n\n[v] speak   or start typing   [Enter] skip\n"
         "Description: architecture call with the PM")])


def invoice_png(tmp: Path, last_month: date) -> None:
    exe = next((p for p in map(shutil.which, ("google-chrome-stable", "chromium", "google-chrome")) if p), None)
    if not exe:
        print("skipped invoice.png: no Chrome/Chromium")
        return
    cli(tmp, "invoice", "create", "--month", f"{last_month:%Y-%m}", "--project", "acme")
    html = next((tmp / "invoices").glob("*.html"))
    png = OUT / "invoice.png"
    with tempfile.TemporaryDirectory() as prof:
        subprocess.run([exe, "--headless=new", "--disable-gpu", f"--user-data-dir={prof}", "--hide-scrollbars",
                        "--force-device-scale-factor=1", "--window-size=860,1180", f"--screenshot={png}",
                        html.as_uri()], capture_output=True, timeout=90, check=False)
    print("wrote docs/images/invoice.png" if png.exists() else "invoice.png: Chrome produced nothing")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="uurtjie-shots-") as d:
        tmp = Path(d)
        facts = seed(tmp)
        widget(json.loads(cli(tmp, "status", "--json")))
        terminal("cli-status.svg", "uurtjie status · today · sessions", [
            ("uurtjie status", cli(tmp, "status")),
            ("uurtjie today", cli(tmp, "today")),
            ("uurtjie sessions", cli(tmp, "sessions"))])
        month = f"{facts['last_month']:%Y-%m}"
        report = [l if len(l) <= 96 else l[:95] + "…"
                  for l in cli(tmp, "report", "--month", month, "--project", "acme").splitlines()]
        terminal("cli-report.svg", "uurtjie report", [
            (f"uurtjie report --month {month} --project acme", "\n".join(report[:13] + ["…"] + report[-3:]))])
        invoice_png(tmp, facts["last_month"])
        terminal("cli-invoice.svg", "uurtjie invoice", [("uurtjie invoice list", cli(tmp, "invoice", "list"))])
    how_it_works()
    timeline()
    timer_popup()
    leaked = [p.name for p in OUT.iterdir() if p.suffix == ".svg"
              and re.search(r"/home/|/Users/|/tmp/|uurtjie-shots-", p.read_text())]
    if leaked:
        raise SystemExit(f"local paths leaked into {leaked}")


if __name__ == "__main__":
    main()
