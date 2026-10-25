"""Run: python3 -m unittest discover -s tests  (from the repo root)."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ["UURTJIE_DATA"] = TMP
os.environ["XDG_RUNTIME_DIR"] = TMP
os.environ["TZ"] = "Africa/Johannesburg"
time.tzset()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uurtjie import config, db, report, daemon  # noqa: E402

CFG_TOML = """
idle_timeout = 60
rounding_minutes = 6
default_project = "acme"
[[projects]]
id = "acme"
name = "Acme"
rate = 1000
paths = ["/home/alice/code/acme"]
[[projects.windows]]
class = "chrome-acme"
[[projects]]
id = "acme-infra"
name = "Acme infra"
rate = 500
paths = ["/home/alice/code/acme/infra"]
require_agent = true
"""


def ts(s):
    return int(datetime.fromisoformat(s).astimezone().timestamp())


def load_cfg():
    p = Path(TMP) / "c.toml"
    p.write_text(CFG_TOML)
    return config.load(p)


class ConfigTests(unittest.TestCase):
    def test_path_longest_prefix(self):
        cfg = load_cfg()
        self.assertEqual(cfg.project_for_path("/home/alice/code/acme/web").id, "acme")
        self.assertEqual(cfg.project_for_path("/home/alice/code/acme/infra/tf").id, "acme-infra")
        self.assertIsNone(cfg.project_for_path("/home/alice/code/acmeX"))
        self.assertIsNone(cfg.project_for_path(None))

    def test_window_rule(self):
        cfg = load_cfg()
        self.assertEqual(cfg.project_for_window("chrome-acme", "", "Jira").id, "acme")
        self.assertIsNone(cfg.project_for_window("google-chrome", "google-chrome", "Jira"))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(Path(TMP) / f"r{time.time_ns()}.db")
        self.cfg = load_cfg()

    def test_union_and_rounding(self):
        # 09:00-10:00 terminal, overlapping manual 09:30-10:30 -> 1.5 h, not 2 h
        db.open_span(self.con, "acme", "terminal", "w1", ts("2026-10-05 09:00"), ts("2026-10-05 10:00"))
        db.open_span(self.con, "acme", "manual", "manual", ts("2026-10-05 09:30"), ts("2026-10-05 10:30"), note="call")
        # 7 minutes on another day -> rounds up to 12 minutes = 0.2 h
        db.open_span(self.con, "acme", "chrome", "c", ts("2026-10-06 14:00"), ts("2026-10-06 14:07"))
        # non-billable ignored
        db.open_span(self.con, "acme", "manual", "m", ts("2026-10-06 15:00"), ts("2026-10-06 16:00"), billable=False)
        rep = report.month_report(self.con, self.cfg, 2026, 10, "acme")
        self.assertEqual([d["hours"] for d in rep["days"]], [1.5, 0.2])
        self.assertEqual(rep["total_hours"], 1.7)
        self.assertEqual(rep["total_amount"], 1700.0)
        self.assertIn("call", rep["days"][0]["summary"])
        for fmt in ("md", "csv", "json"):
            self.assertIn("2026-10-05", report.render(rep, fmt))

    def test_midnight_split(self):
        db.open_span(self.con, "acme", "terminal", "w1", ts("2026-10-07 23:30"), ts("2026-10-08 00:45"))
        rep = report.month_report(self.con, self.cfg, 2026, 10, "acme")
        self.assertEqual([(d["date"], d["raw_hours"]) for d in rep["days"]],
                         [("2026-10-07", 0.5), ("2026-10-08", 0.75)])

    def test_month_boundary(self):
        db.open_span(self.con, "acme", "terminal", "w1", ts("2026-09-30 23:00"), ts("2026-10-01 01:00"))
        rep = report.month_report(self.con, self.cfg, 2026, 10, "acme")
        self.assertEqual(rep["total_raw_hours"], 1.0)

    def test_timer(self):
        db.timer_start(self.con, "acme", at=ts("2026-10-09 08:00"))
        with self.assertRaises(RuntimeError):
            db.timer_start(self.con, "acme")
        sid = db.timer_stop(self.con, at=ts("2026-10-09 08:40"))
        r = self.con.execute("SELECT * FROM spans WHERE id=?", (sid,)).fetchone()
        self.assertEqual(r["end_ts"] - r["start_ts"], 2400)
        self.assertIsNone(db.timer_get(self.con))


class QuickTests(unittest.TestCase):
    def setUp(self):
        from uurtjie.cli import parse_quick
        self.p = parse_quick
        self.cfg = load_cfg()
        self.today = date(2026, 10, 21)           # Wednesday
        self.now = ts("2026-10-21 12:00")

    def q(self, line):
        return self.p(line, self.cfg, today=self.today, now=self.now)

    def test_duration_ends_now(self):
        e = self.q("acme 1h30 call with PM")
        self.assertEqual((e["project"], e["end"] - e["start"], e["end"], e["note"]),
                         ("acme", 5400, self.now, "call with PM"))

    def test_range_and_default_project(self):
        e = self.q("09:00-10:30 standup")
        self.assertEqual((e["project"], e["start"], e["end"]),
                         ("acme", ts("2026-10-21 09:00"), ts("2026-10-21 10:30")))

    def test_dates_and_flags(self):
        e = self.q("acme-infra y 2h review nb")
        self.assertEqual(e["end"], ts("2026-10-20 17:00"))
        self.assertFalse(e["billable"])
        self.assertEqual(self.q("fri 45m docs")["start"], ts("2026-10-16 16:15"))
        self.assertEqual(self.q("2026-10-01 90m x")["end"] - self.q("2026-10-01 90m x")["start"], 5400)

    def test_prefix_project_and_note_words_untouched(self):
        e = self.q("acme-i 30m fix today's bug")
        self.assertEqual((e["project"], e["note"]), ("acme-infra", "fix today's bug"))

    def test_errors(self):
        with self.assertRaises(ValueError):
            self.q("acme just a note")


class FakeHypr:
    def __init__(self):
        self.win = None

    async def active_window(self):
        return self.win


class FakeHerdr:
    def __init__(self, pane):
        self.pane = pane

    async def focused(self):
        return self.pane


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(Path(TMP) / f"d{time.time_ns()}.db")
        self.cfg = load_cfg()
        self.hypr = FakeHypr()
        self.herdr = FakeHerdr({"pane_id": "w1:p1", "workspace_id": "w1", "workspace_label": "acme-web",
                                "cwds": ["/home/alice/code/acme/web"], "agent": "claude", "agent_status": "working"})
        self.d = daemon.Daemon(self.cfg, self.con, self.hypr, self.herdr)
        self._orig = daemon.terminal_kind
        daemon.terminal_kind = lambda pid: ("herdr", None)

    def tearDown(self):
        daemon.terminal_kind = self._orig

    def run_tick(self, at):
        orig = time.time
        time.time = lambda: at
        try:
            asyncio.run(self.d.tick())
        finally:
            time.time = orig

    def spans(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM spans ORDER BY id")]

    def test_herdr_workspace_tracking_and_switch(self):
        self.hypr.win = {"address": "0x1", "class": "Alacritty", "initialClass": "Alacritty", "title": "herdr", "pid": 1}
        t = 1_800_000_000
        for i in range(4):
            self.run_tick(t + 5 * i)
        s = self.spans()
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]["project"], s[0]["ctx_key"]), ("acme", "herdr:w1"))
        self.assertEqual(s[0]["end_ts"] - s[0]["start_ts"], 15)
        self.assertEqual(json.loads(s[0]["detail"])["agent"], "claude")

        # switch to client Chrome profile -> new span
        self.hypr.win = {"address": "0x2", "class": "chrome-acme", "initialClass": "chrome-acme", "title": "Jira", "pid": 2}
        self.run_tick(t + 20)
        self.run_tick(t + 25)
        s = self.spans()
        self.assertEqual(len(s), 2)
        self.assertEqual(s[1]["source"], "chrome")

        # unrelated window -> nothing new, chain broken
        self.hypr.win = {"address": "0x3", "class": "Spotify", "initialClass": "Spotify", "title": "x", "pid": 3}
        self.run_tick(t + 30)
        self.assertEqual(len(self.spans()), 2)

        st = json.loads((Path(TMP) / "uurtjie" / "state.json").read_text())
        self.assertIsNone(st["active"])

    def test_require_agent(self):
        self.herdr.pane = {"pane_id": "w2:p1", "workspace_id": "w2", "workspace_label": "infra",
                           "cwds": ["/home/alice/code/acme/infra"], "agent": None, "agent_status": None}
        self.hypr.win = {"address": "0x1", "class": "Alacritty", "initialClass": "Alacritty", "title": "", "pid": 1}
        self.run_tick(1_800_000_000)
        self.assertEqual(self.spans(), [])
        self.herdr.pane["agent"] = "codex"
        self.run_tick(1_800_000_005)
        self.assertEqual(self.spans()[0]["project"], "acme-infra")

    def test_idle_trims_back(self):
        self.hypr.win = {"address": "0x1", "class": "Alacritty", "initialClass": "Alacritty", "title": "", "pid": 1}
        t = 1_800_000_000
        for i in range(0, 125, 5):              # 2 minutes of samples
            self.run_tick(t + i)
        self.d.on_idle(t + 120)                 # idle fired; last input was 60 s earlier
        s = self.spans()[0]
        self.assertEqual(s["end_ts"], t + 60)
        self.run_tick(t + 130)                  # idle: no new span
        self.assertEqual(len(self.spans()), 1)
        self.d.on_active()
        self.run_tick(t + 200)
        self.assertEqual(len(self.spans()), 2)


class ChunkTests(DaemonTests):
    def test_15_min_chunks_contiguous(self):
        self.hypr.win = {"address": "0x1", "class": "Alacritty", "initialClass": "Alacritty", "title": "", "pid": 1}
        t = 1_800_000_000
        for i in range(0, 40 * 60 + 1, 5):        # 40 minutes on one Herdr workspace
            self.run_tick(t + i)
        s = self.spans()
        self.assertEqual([x["end_ts"] - x["start_ts"] for x in s], [900, 900, 600])
        self.assertEqual(s[0]["end_ts"], s[1]["start_ts"])   # no gaps between chunks
        self.assertEqual({x["ctx_key"] for x in s}, {"herdr:w1"})


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(Path(TMP) / f"s{time.time_ns()}.db")
        self.cfg = load_cfg()
        self.cfg.sleep_minutes = 5
        self.cfg.bridge_minutes = 0

    def add(self, pid, src, key, a, b, detail=None, note=None):
        db.open_span(self.con, pid, src, key, ts(f"2026-10-12 {a}"), ts(f"2026-10-12 {b}"), detail, note)

    def sess(self):
        return report.sessions_between(self.con, self.cfg, ts("2026-10-12 00:00"), ts("2026-10-13 00:00"))

    def test_parallel_herdr_and_chrome_collapse_into_one(self):
        self.add("acme", "terminal", "herdr:w1", "09:00", "09:15", {"workspace_label": "api", "branch": "feat/auth"})
        self.add("acme", "terminal", "herdr:w2", "09:15", "09:30", {"workspace_label": "web", "branch": "main"})
        self.add("acme", "chrome", "chrome:chrome-acme", "09:32", "09:40",
                 {"class": "chrome-acme", "window": "AUTH-12 - Jira - Google Chrome"})       # 2 min gap: bridged
        self.add("acme", "terminal", "herdr:w1", "09:41", "10:00", {"workspace_label": "api"})
        self.add("acme", "manual", "timer", "09:50", "10:05", note="call with PM")           # overlaps: no double count
        s = self.sess()
        self.assertEqual(len(s), 3)
        self.assertEqual(sum(x["seconds"] for x in s), 62 * 60)
        self.assertEqual([t["label"] for t in s[0]["tasks"]][:2], ["api", "web"])
        from uurtjie import sessions as S
        S.summarize(s, self.cfg)
        self.assertIn("call with PM", s[-1]["summary"])
        self.assertIn("AUTH-12 - Jira", s[1]["summary"])
        self.assertNotIn("Google Chrome", s[1]["summary"])

    def test_sleep_gap_splits_and_other_project_blocks_bridge(self):
        self.add("acme", "terminal", "herdr:w1", "09:00", "09:30")
        self.add("acme", "terminal", "herdr:w1", "09:40", "10:00")    # 10 min gap > 5: new session
        self.add("acme", "terminal", "herdr:w1", "10:03", "10:10")    # 3 min gap, but…
        self.add("internal", "terminal", "term:x", "10:00", "10:02")  # …another project was active
        s = [x for x in self.sess() if x["project"] == "acme"]
        self.assertEqual([x["seconds"] // 60 for x in s], [30, 20, 7])

    def test_commits_attached_once(self):
        import subprocess as sp
        repo = Path(tempfile.mkdtemp())
        env = {**os.environ, "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_EMAIL": "dev@example.com",
               "GIT_AUTHOR_NAME": "JD", "GIT_COMMITTER_NAME": "JD"}
        sp.run(["git", "init", "-q", str(repo)], check=True)
        for when, msg in (("2026-10-12T09:10:00", "Add OAuth callback"), ("2026-10-12T09:55:00", "Fix token refresh"),
                          ("2026-10-12T14:00:00", "Afternoon work")):
            e = {**env, "GIT_AUTHOR_DATE": when + "+02:00", "GIT_COMMITTER_DATE": when + "+02:00"}
            sp.run(["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", msg], check=True, env=e)
        det = {"workspace_label": "api", "repo": str(repo), "branch": "main", "cwd": str(repo)}
        self.add("acme", "terminal", "herdr:w1", "09:00", "10:00", det)
        self.add("acme", "terminal", "herdr:w1", "13:50", "14:30", det)
        self.cfg.git_author = "dev@example.com"
        rep = report.month_report(self.con, self.cfg, 2026, 10, "acme")
        ss = rep["days"][0]["sessions"]
        self.assertEqual([[c["subject"] for c in s["commits"]] for s in ss],
                         [["Add OAuth callback", "Fix token refresh"], ["Afternoon work"]])
        self.assertIn("commits: Add OAuth callback; Fix token refresh", ss[0]["summary"])
        self.assertAlmostEqual(rep["days"][0]["raw_hours"], 100 / 60)
        for fmt, by in (("md", "day"), ("csv", "session"), ("json", "day")):
            self.assertIn("Fix token refresh", report.render(rep, fmt, by))

    def test_summary_command_cached(self):
        self.add("acme", "terminal", "herdr:w1", "09:00", "09:30", {"workspace_label": "api"})
        counter = Path(tempfile.mkdtemp()) / "n"
        self.cfg.summary_command = ["sh", "-c", f"cat >/dev/null; echo x >> {counter}; echo 'Built auth API'"]
        from uurtjie import sessions as S
        for _ in range(2):
            s = self.sess()
            S.summarize(s, self.cfg, self.con)
            self.assertEqual(s[0]["summary"], "Built auth API")
        self.assertEqual(counter.read_text().count("x"), 1)


class BackupTests(unittest.TestCase):
    def test_backup_verified_rotated_and_daily_once(self):
        import sqlite3
        con = db.connect(Path(TMP) / f"b{time.time_ns()}.db")
        db.open_span(con, "acme", "manual", "m", 1_800_000_000, 1_800_003_600, note="kept")
        bdir = Path(tempfile.mkdtemp())
        for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
            (bdir / f"uurtjie-{day}.db").write_text("old")
        dest = db.backup(con, bdir, keep=2)
        copy = sqlite3.connect(dest)
        self.assertEqual(copy.execute("SELECT note FROM spans").fetchone()[0], "kept")
        self.assertEqual(len(list(bdir.glob("uurtjie-????-??-??.db"))), 2)       # pruned to keep=2
        self.assertIsNone(db.backup_if_due(con, bdir, 2))                       # today's already done


class HerdrSocketTests(unittest.TestCase):
    def test_call_against_fake_server(self):
        sock = Path(TMP) / "herdr.sock"

        async def handler(reader, writer):
            req = json.loads(await reader.readline())
            if req["method"] == "pane.current":
                res = {"type": "pane_info", "pane": {"pane_id": "w3:p2", "workspace_id": "w3",
                                                     "cwd": "/home/alice/code/acme", "agent": "claude",
                                                     "agent_status": "idle"}}
            else:
                res = {"type": "workspace_info", "workspace": {"workspace_id": "w3", "label": "acme"}}
            writer.write((json.dumps({"id": req["id"], "result": res}) + "\n").encode())
            await writer.drain()
            writer.close()

        async def main():
            server = await asyncio.start_unix_server(handler, path=str(sock))
            async with server:
                return await daemon.Herdr(sock).focused()

        pane = asyncio.run(main())
        self.assertEqual(pane["workspace_label"], "acme")
        self.assertEqual(pane["cwds"], ["/home/alice/code/acme"])
        self.assertEqual(pane["agent"], "claude")

    def test_terminal_kind_plain(self):
        kind, cwd = daemon.terminal_kind(os.getpid())
        self.assertEqual(kind, "plain")


class LastAreaTests(unittest.TestCase):
    def test_terminal_area(self):
        from uurtjie.cli import describe_area
        d = {"workspace_label": "acme-config", "cwd": "/p/acme/acme-config-admin",
             "branch": "main", "agent": "codex"}
        self.assertEqual(describe_area("terminal", d), "acme-config · acme-config-admin @ main (codex)")

    def test_terminal_area_dedupes_and_falls_back(self):
        from uurtjie.cli import describe_area
        self.assertEqual(describe_area("terminal", {"workspace_label": "acme", "cwd": "/x/acme"}), "acme")
        self.assertEqual(describe_area("terminal", {}), "terminal")

    def test_chrome_area_strips_browser_suffix(self):
        from uurtjie.cli import describe_area
        d = {"window": "Acme | Dashboard - Google Chrome", "class": "chrome-acme"}
        self.assertEqual(describe_area("chrome", d), "Acme | Dashboard")
        self.assertEqual(describe_area("chrome", {"class": "chrome-acme"}), "chrome-acme")

    def test_log_rows_show_start_end_and_minutes(self):
        import argparse
        import contextlib
        import io
        from uurtjie.cli import cmd_log
        con = db.connect(Path(TMP) / f"g{time.time_ns()}.db")
        cfg = load_cfg()
        db.open_span(con, "acme", "terminal", "herdr:w1", ts("2026-10-05 09:00:10"), ts("2026-10-05 09:01:40"),
                     {"workspace_label": "api"})
        db.open_span(con, "acme", "chrome", "c", ts("2026-10-05 23:59:00"), ts("2026-10-06 00:03:00"), {"class": "chrome-acme"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_log(argparse.Namespace(date="2026-10-05", days=1, project=None, json=False), cfg, con)
        out = buf.getvalue().splitlines()
        self.assertIn("minutes", out[0])
        self.assertIn("09:00:10 – 09:01:40", out[1])
        self.assertIn("1.5 min", out[1])
        self.assertIn("api", out[1])
        self.assertIn("23:59:00 – 00:03:00+1d", out[2])
        self.assertIn("4.0 min", out[2])

    def _stop(self, con, cfg, **kw):
        import argparse
        import contextlib
        import io
        from uurtjie.cli import cmd_stop
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_stop(argparse.Namespace(at=kw.get("at"), discard=False, note=None), cfg, con)
        return buf.getvalue()

    def test_timer_under_a_minute_is_discarded(self):
        con = db.connect(Path(TMP) / f"s{time.time_ns()}.db")
        cfg = load_cfg()
        now = int(time.time())
        db.timer_start(con, "acme", at=now - 30)
        self.assertIn("discarded", self._stop(con, cfg))
        self.assertIsNone(db.timer_get(con))
        self.assertEqual(con.execute("SELECT count(*) FROM spans").fetchone()[0], 0)

    def test_timer_stop_time_decides_not_wall_clock(self):
        con = db.connect(Path(TMP) / f"s{time.time_ns()}.db")
        cfg = load_cfg()
        now = int(time.time())
        db.timer_start(con, "acme", at=now - 600)                    # pressed 9 min ago -> kept
        self.assertIn("stopped", self._stop(con, cfg, at=now - 60))
        self.assertEqual(con.execute("SELECT count(*) FROM spans").fetchone()[0], 1)
        db.timer_start(con, "acme", at=now - 600)                    # stop time 59 s after start -> dropped
        self.assertIn("discarded", self._stop(con, cfg, at=now - 541))

    def test_timer_of_a_minute_or_more_is_kept(self):
        con = db.connect(Path(TMP) / f"s{time.time_ns()}.db")
        cfg = load_cfg()
        now = int(time.time())
        db.timer_start(con, "acme", at=now - 60)
        self.assertIn("stopped", self._stop(con, cfg))
        self.assertEqual(con.execute("SELECT count(*) FROM spans").fetchone()[0], 1)

    def test_fmt_ago(self):
        from uurtjie.cli import fmt_ago
        self.assertEqual([fmt_ago(s) for s in (5, 120, 3900, 90000)], ["just now", "2 min ago", "1h05 ago", "1 d ago"])

    def test_tooltip_shows_last_area_when_nothing_is_active(self):
        from unittest import mock
        from uurtjie.cli import build_status
        con = db.connect(Path(TMP) / f"t{time.time_ns()}.db")
        cfg = load_cfg()
        now = int(time.time())
        db.open_span(con, "acme", "chrome", "c", now - 600, now - 300, {"window": "Jira - Google Chrome"})
        with mock.patch("uurtjie.cli.read_state", return_value=None):      # other tests leave a state.json behind
            self.assertIn("Last: chrome · Jira · 5 min ago", build_status(cfg, con)["tooltip"])

    def test_last_tracked_skips_manual_and_picks_latest(self):
        from uurtjie.cli import last_tracked
        con = db.connect(Path(TMP) / f"l{time.time_ns()}.db")
        cfg = load_cfg()
        self.assertIsNone(last_tracked(con, cfg, 1000))
        db.open_span(con, "acme", "terminal", "w1", 100, 200, {"workspace_label": "old"})
        db.open_span(con, "acme", "chrome", "c", 300, 400, {"window": "Jira - Google Chrome"})
        db.open_span(con, "acme", "manual", "manual", 500, 900, note="call")
        last = last_tracked(con, cfg, 520)
        self.assertEqual((last["source"], last["label"], last["name"], last["ago"]),
                         ("chrome", "Jira", "Acme", "2 min ago"))


# ---------------------------------------------------------------------------------------------- calls

class FakeHyprCalls(FakeHypr):
    def __init__(self):
        super().__init__()
        self.windows = []

    async def clients(self):
        return self.windows


class FakeAudio:
    def __init__(self):
        self.streams = []

    async def mic_streams(self):
        return list(self.streams)


CHROME_STREAM = {"pid": 900, "app": "Google Chrome", "binary": "chrome", "media": "Meet"}
ACME_WIN = {"address": "0xa", "class": "chrome-acme", "initialClass": "chrome-acme", "title": "Meet - weekly", "pid": 800}
OTHER_WIN = {"address": "0xb", "class": "google-chrome", "initialClass": "google-chrome", "title": "YouTube", "pid": 800}


class CallTests(unittest.TestCase):
    T = 1_800_000_000

    def setUp(self):
        self.con = db.connect(Path(TMP) / f"c{time.time_ns()}.db")
        self.addCleanup(self.con.close)
        self.cfg = load_cfg()
        self.hypr, self.audio = FakeHyprCalls(), FakeAudio()
        self.hypr.windows = [dict(ACME_WIN)]
        self.d = daemon.Daemon(self.cfg, self.con, self.hypr, FakeHerdr(None), self.audio)
        self._anc = daemon.proc_ancestors
        daemon.proc_ancestors = lambda pid: {900: [900, 850, 800, 1]}.get(pid, [pid])

    def tearDown(self):
        daemon.proc_ancestors = self._anc

    def tick(self, at):
        orig = time.time
        time.time = lambda: at
        try:
            asyncio.run(self.d.tick())
        finally:
            time.time = orig

    def calls(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM spans WHERE source='meeting' ORDER BY id")]

    def test_call_counts_while_idle_and_bridges_a_mute(self):
        t = self.T
        self.d.on_idle(t)                                   # nobody touches keyboard or mouse
        self.audio.streams = [CHROME_STREAM]
        for i in range(0, 65, 5):
            self.tick(t + i)
        st = json.loads((Path(TMP) / "uurtjie" / "state.json").read_text())
        self.assertEqual(st["meeting"]["project"], "acme")
        self.audio.streams = []                              # muted: mic released for a minute
        for i in range(65, 130, 5):
            self.tick(t + i)
        self.audio.streams = [CHROME_STREAM]                 # unmuted: same call
        self.tick(t + 130)
        c = self.calls()
        self.assertEqual(len(c), 1)
        self.assertEqual((c[0]["project"], c[0]["start_ts"], c[0]["end_ts"]), ("acme", t, t + 130))
        self.assertEqual(json.loads(c[0]["detail"])["app"], "Google Chrome")
        self.audio.streams = []                              # hang up: over after the grace period
        for i in range(135, 300, 5):
            self.tick(t + i)
        self.assertEqual(self.calls()[0]["end_ts"], t + 130)
        self.assertIsNone(json.loads((Path(TMP) / "uurtjie" / "state.json").read_text())["meeting"])

    def test_brief_mic_use_is_not_a_call(self):
        self.audio.streams = [CHROME_STREAM]
        for i in range(0, 25, 5):
            self.tick(self.T + i)
        self.audio.streams = []
        self.tick(self.T + 30)
        self.assertEqual(self.calls(), [])

    def test_dictation_and_ignored_apps_are_not_calls(self):
        self.audio.streams = [{"pid": 900, "app": "voxtype", "binary": "voxtype", "media": "record"}]
        for i in range(0, 90, 5):
            self.tick(self.T + i)
        self.assertEqual(self.calls(), [])

    def test_unrelated_app_is_not_a_call(self):
        self.hypr.windows = [{"address": "0xc", "class": "zoom", "initialClass": "zoom", "title": "Zoom", "pid": 800}]
        self.audio.streams = [CHROME_STREAM]
        for i in range(0, 90, 5):
            self.tick(self.T + i)
        self.assertEqual(self.calls(), [])

    def test_shared_browser_process_needs_the_project_window_focused(self):
        self.hypr.windows = [dict(ACME_WIN), dict(OTHER_WIN)]      # one Chrome process, two profiles
        self.audio.streams = [CHROME_STREAM]
        self.hypr.win = dict(OTHER_WIN)                            # looking at the unrelated profile
        for i in range(0, 90, 5):
            self.tick(self.T + i)
        self.assertEqual(self.calls(), [])
        self.hypr.win = dict(ACME_WIN)
        for i in range(90, 150, 5):
            self.tick(self.T + i)
        self.assertEqual(len(self.calls()), 1)

    def test_pactl_json_keeps_microphones_and_drops_monitors_and_paused_streams(self):
        sources = [{"index": 59, "name": "alsa_output.hdmi-stereo.monitor", "monitor_of_sink": None},
                   {"index": 61, "name": "alsa_input.pci-analog-stereo", "monitor_of_sink": None}]
        props = lambda pid, name: {"application.process.id": str(pid), "application.name": name,
                                   "application.process.binary": "chrome", "media.name": "Meet"}
        outputs = [{"index": 1, "source": 61, "corked": False, "properties": props(900, "Google Chrome")},
                   {"index": 2, "source": 59, "corked": False, "properties": props(901, "OBS")},        # speaker capture
                   {"index": 3, "source": 61, "corked": True, "properties": props(902, "Paused")},
                   {"index": 4, "source": 61, "corked": False, "properties": {"application.name": "NoPid"}}]
        got = daemon.parse_mic_streams(outputs, sources)
        self.assertEqual([(g["app"], g["pid"]) for g in got], [("Google Chrome", 900), ("NoPid", None)])

    def test_detection_can_be_switched_off(self):
        self.cfg.meeting_detect = False
        self.audio.streams = [CHROME_STREAM]
        for i in range(0, 90, 5):
            self.tick(self.T + i)
        self.assertEqual(self.calls(), [])

    def test_call_shows_in_sessions_and_status_text(self):
        from uurtjie import sessions as S
        from uurtjie.cli import build_status, describe_area
        self.audio.streams = [CHROME_STREAM]
        for i in range(0, 125, 5):
            self.tick(self.T + i)
        rows = [dict(r) for r in self.con.execute("SELECT * FROM spans")]
        sess = [x for x in S.build_sessions(rows, self.cfg) if x["project"] == "acme"]
        self.assertIn("call (Google Chrome)", S.default_summary(sess[0]))
        self.assertEqual(describe_area("meeting", {"app": "Google Chrome"}), "call in Google Chrome")
        from unittest import mock
        state = {"running": True, "idle": True, "active": None,
                 "meeting": {"project": "acme", "app": "Google Chrome", "since": int(time.time()) - 600}}
        with mock.patch("uurtjie.cli.read_state", return_value=state):
            st = build_status(self.cfg, self.con)
        self.assertEqual(st["class"], "meeting")
        self.assertIn("Call: Acme via Google Chrome (10 min)", st["tooltip"])
        self.assertEqual(st["meeting"]["minutes"], 10)


# ---------------------------------------------------------------------------------------------- invoices

class InvoiceTests(unittest.TestCase):
    TODAY = date(2026, 11, 2)

    def setUp(self):
        from uurtjie import invoice
        self.inv = invoice
        self.con = db.connect(Path(TMP) / f"i{time.time_ns()}.db")
        self.addCleanup(self.con.close)
        self.cfg = load_cfg()
        self.cfg.invoice.vat_rate = 15.0
        self.cfg.invoice.dir = Path(TMP) / "invoices"
        self.cfg.backup_dir = Path(TMP) / f"bk{time.time_ns()}"
        self.cfg.invoice.seller = {"name": "Jane Doe", "address": ["1 Main Rd"], "bank": ["Bank X", "Acc 123"],
                                   "email": "", "phone": "", "vat_number": "", "reg_number": "", "footer": "Thanks"}
        db.open_span(self.con, "acme", "terminal", "w1", ts("2026-10-05 09:00"), ts("2026-10-05 10:30"),
                     {"workspace_label": "api"})
        db.open_span(self.con, "acme", "chrome", "c", ts("2026-10-06 14:00"), ts("2026-10-06 14:07"),
                     {"window": "Secret page - Google Chrome", "class": "chrome-acme"})

    def make(self, **kw):
        return self.inv.create(self.con, self.cfg, "acme", date(2026, 10, 1), date(2026, 10, 31),
                               today=self.TODAY, with_git=False, **kw)

    def test_create_numbers_totals_and_due_date(self):
        snap = self.make()
        self.assertEqual(snap["number"], "INV-2026-001")
        self.assertEqual((snap["hours"], snap["subtotal"], snap["vat_amount"], snap["total"]), (1.7, 1700.0, 255.0, 1955.0))
        self.assertEqual(snap["due_date"], "2026-11-16")
        db.open_span(self.con, "acme", "terminal", "w1", ts("2026-11-03 09:00"), ts("2026-11-03 10:00"))
        nxt = self.inv.create(self.con, self.cfg, "acme", date(2026, 11, 1), date(2026, 11, 30),
                              today=date(2026, 12, 2), with_git=False)
        self.assertEqual(nxt["number"], "INV-2026-002")

    def test_dry_run_saves_and_locks_nothing(self):
        snap = self.make(dry_run=True)
        self.assertIsNone(snap["number"])
        self.assertEqual(self.inv.rows(self.con, self.TODAY), [])
        self.assertIsNone(self.inv.locked_by(self.con, "acme", ts("2026-10-05 09:00"), ts("2026-10-05 10:00")))

    def test_refusals(self):
        with self.assertRaises(self.inv.InvoiceError):          # period not over
            self.inv.create(self.con, self.cfg, "acme", date(2026, 10, 1), date(2026, 11, 2), today=self.TODAY)
        with self.assertRaises(self.inv.InvoiceError):          # nothing tracked
            self.inv.create(self.con, self.cfg, "acme", date(2026, 8, 1), date(2026, 8, 31), today=self.TODAY, with_git=False)
        self.cfg.project("acme").rate = 0
        with self.assertRaises(self.inv.InvoiceError):          # no rate
            self.make()
        self.cfg.project("acme").rate = 1000
        self.make()
        with self.assertRaises(self.inv.InvoiceError):          # double billing
            self.make()

    def test_issued_period_is_locked_until_voided(self):
        from uurtjie import cli
        snap = self.make()
        a, b = ts("2026-10-05 09:00"), ts("2026-10-05 10:00")
        self.assertEqual(self.inv.locked_by(self.con, "acme", a, b), snap["number"])
        self.assertIsNone(self.inv.locked_by(self.con, "acme-infra", a, b))
        self.assertIsNone(self.inv.locked_by(self.con, "acme", ts("2026-11-03 09:00"), ts("2026-11-03 10:00")))
        sid = self.con.execute("SELECT id FROM spans ORDER BY id").fetchone()[0]
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.cmd_rm(argparse_ns(ids=[sid], force=False), self.cfg, self.con)
        self.assertIn("kept", buf.getvalue())
        self.assertEqual(self.con.execute("SELECT count(*) FROM spans WHERE id=?", (sid,)).fetchone()[0], 1)
        with self.assertRaises(self.inv.InvoiceError):
            cli.cmd_add(argparse_ns(project="acme", date="2026-10-07", duration="1h", start="09:00", end=None,
                                    note=None, non_billable=False, force=False), self.cfg, self.con)
        with self.assertRaises(self.inv.InvoiceError):
            cli.cmd_edit(argparse_ns(id=sid, project=None, note="x", start=None, end=None, billable=None, force=False),
                         self.cfg, self.con)
        with contextlib.redirect_stdout(buf):
            cli.cmd_rm(argparse_ns(ids=[sid], force=True), self.cfg, self.con)
        self.assertEqual(self.con.execute("SELECT count(*) FROM spans WHERE id=?", (sid,)).fetchone()[0], 0)
        self.inv.void(self.con, snap["number"])
        self.assertIsNone(self.inv.locked_by(self.con, "acme", a, b))
        again = self.make()                                      # re-issue; the voided number is not reused
        self.assertEqual(again["number"], "INV-2026-002")

    def test_payments_status_and_outstanding(self):
        snap = self.make()
        num = snap["number"]
        row = lambda today: self.inv.rows(self.con, today)[0]
        self.assertEqual(row(self.TODAY)["state"], "issued")
        self.inv.mark_sent(self.con, num, self.TODAY)
        self.assertEqual(row(self.TODAY)["state"], "sent")
        self.assertEqual(row(date(2026, 11, 17))["state"], "overdue")
        r = self.inv.record_payment(self.con, "001", 1000, date(2026, 11, 5))     # unique ending is enough
        self.assertEqual(r["outstanding"], 955.0)
        self.assertEqual(row(self.TODAY)["state"], "part-paid")
        self.assertIn("ZAR 955.00 outstanding", self.inv.outstanding_summary(self.con, self.TODAY)["text"])
        self.assertIn("1 overdue", self.inv.outstanding_summary(self.con, date(2026, 12, 1))["text"])
        self.inv.record_payment(self.con, num)                                    # default: the rest
        self.assertEqual(row(self.TODAY)["state"], "paid")
        self.assertEqual(self.inv.outstanding_summary(self.con, self.TODAY)["text"], "")
        with self.assertRaises(self.inv.InvoiceError):
            self.inv.record_payment(self.con, num)
        with self.assertRaises(self.inv.InvoiceError):                            # payments recorded: needs --force
            self.inv.void(self.con, num)

    def test_figures_are_frozen_in_the_snapshot(self):
        snap = self.make()
        self.cfg.project("acme").rate = 5000                     # later rate change must not touch the invoice
        stored = self.inv.snapshot(self.inv.find(self.con, snap["number"]))
        self.assertEqual((stored["rate"], stored["total"]), (1000, 1955.0))

    def test_html_is_escaped_and_client_safe_by_default(self):
        self.cfg.project("acme").client = "A&B <Ltd>"
        snap = self.make()
        page = self.inv.render_html(snap)
        for needle in ("INV-2026-001", "ZAR 1,955.00", "VAT 15%", "Bank X", "Thanks", "Timesheet", "A&amp;B &lt;Ltd&gt;"):
            self.assertIn(needle, page)
        self.assertNotIn("<Ltd>", page)
        self.assertNotIn("Secret page", page)                    # window titles never leak by default
        self.assertIn("Secret page", self.inv.render_html(snap, "sessions"))

    def test_unbilled_months(self):
        db.open_span(self.con, "acme", "terminal", "w1", ts("2026-09-10 09:00"), ts("2026-09-10 10:00"))
        self.make()
        months = {(r["project"], r["month"]) for r in self.inv.unbilled(self.con, self.cfg, self.TODAY)}
        self.assertIn(("acme", "2026-09"), months)
        self.assertNotIn(("acme", "2026-10"), months)

    def test_old_databases_are_migrated_without_losing_rows(self):
        import sqlite3
        path = Path(TMP) / f"legacy{time.time_ns()}.db"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, project TEXT NOT NULL, period TEXT NOT NULL,"
                    " hours REAL NOT NULL, amount REAL NOT NULL, currency TEXT, file TEXT, generated_at INTEGER NOT NULL)")
        old.execute("INSERT INTO invoices(project, period, hours, amount, currency, generated_at) VALUES ('acme','2026-09',5,5000,'ZAR',1)")
        old.commit()
        old.close()
        con = db.connect(path)
        self.addCleanup(con.close)
        db.connect(path).close()                                 # a second process migrating again is harmless
        cols = {r["name"] for r in con.execute("PRAGMA table_info(invoices)")}
        self.assertTrue({"number", "status", "total", "snapshot", "period_from"} <= cols)
        self.assertEqual(con.execute("SELECT count(*) FROM invoices").fetchone()[0], 1)
        self.assertEqual(self.inv.rows(con, self.TODAY), [])     # old log rows aren't treated as issued invoices

    def test_cli_flow_create_list_paid_void(self):
        import contextlib
        import io
        from uurtjie import cli
        db.open_span(self.con, "acme", "terminal", "w1", ts("2020-01-15 09:00"), ts("2020-01-15 11:00"))

        def run(**kw):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.cmd_invoice(argparse_ns(**kw), self.cfg, self.con)
            return buf.getvalue()

        base = dict(invoice_cmd="create", project="acme", month="2020-01", start=None, end=None, vat=None,
                    due_days=None, pdf=False, detail="days", dry_run=True, force=False)
        self.assertIn("DRAFT", run(**base))
        out = run(**{**base, "dry_run": False})
        self.assertIn("INV-2026-001", out)                        # numbered by issue year, not billed year
        self.assertIn("locked", out)
        self.assertTrue((self.cfg.invoice.dir / "INV-2026-001.html").exists())
        self.assertIn("issued", run(invoice_cmd="list", project=None, all=False))
        self.assertIn("paid in full", run(invoice_cmd="paid", number="INV-2026-001", amount=None, date=None, note=None))
        self.assertIn("paid", run(invoice_cmd="list", project=None, all=False))
        with self.assertRaises(SystemExit):                       # voiding needs --yes
            run(invoice_cmd="void", number="INV-2026-001", yes=False, force=False)


def argparse_ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


# ---------------------------------------------------------------------------------------------- config editing

EDIT_CFG = """# my config
tick = 5
sleep_minutes = 5        # idle after this
default_project = "acme"

terminal_classes = ["foot"]

[[projects]]
id = "acme"
name = "Acme"
rate = 0                 # set your hourly rate
paths = ["/code/acme"]
# client_address = ["example"]

[[projects.windows]]
class = "chrome-acme"

[[projects]]
id = "other"
name = "Other"
rate = 500
bank_note = "keep me"
paths = ["/code/other"]
"""


class ConfigEditTests(unittest.TestCase):
    def setUp(self):
        from uurtjie import config_edit
        self.ce = config_edit
        self.path = Path(TMP) / f"edit{time.time_ns()}.toml"
        self.path.write_text(EDIT_CFG)

    def toml(self):
        import tomllib
        return tomllib.loads(self.path.read_text())

    def test_replaces_the_right_projects_value_and_keeps_everything_else(self):
        self.ce.update(self.path, "rate", ["650"], "acme")
        text, d = self.path.read_text(), self.toml()
        self.assertEqual([p["rate"] for p in d["projects"]], [650, 500])
        self.assertIn("# my config", text)
        self.assertIn("sleep_minutes = 5        # idle after this", text)
        self.assertNotIn("set your hourly rate", text)                # the stale comment goes with the old value
        self.assertEqual(d["projects"][1]["bank_note"], "keep me")

    def test_other_project_is_selected_by_id(self):
        self.ce.update(self.path, "rate", ["650.5"], "other")
        self.assertEqual([p["rate"] for p in self.toml()["projects"]], [0, 650.5])

    def test_missing_project_key_is_inserted_in_that_block_only(self):
        self.ce.update(self.path, "client_vat_number", ["4123456789"], "other")
        d = self.toml()
        self.assertEqual(d["projects"][1]["client_vat_number"], "4123456789")
        self.assertNotIn("client_vat_number", d["projects"][0])
        self.assertEqual(d["projects"][0]["windows"][0]["class"], "chrome-acme")

    def test_new_top_level_key_lands_before_the_first_table(self):
        self.ce.update(self.path, "meeting_min_seconds", ["45"])
        d = self.toml()
        self.assertEqual(d["meeting_min_seconds"], 45)
        self.assertNotIn("meeting_min_seconds", d["projects"][0])
        self.ce.update(self.path, "rounding_minutes", ["15"])
        self.assertEqual(self.toml()["rounding_minutes"], 15)

    def test_creates_the_invoice_tables_and_reuses_them(self):
        from uurtjie import config
        self.ce.update(self.path, "invoice.from.name", ["Jane Doe"])
        self.ce.update(self.path, "invoice.from.email", ["jane@example.com"])
        self.ce.update(self.path, "invoice.vat_rate", ["15"])
        self.assertEqual(self.path.read_text().count("[invoice.from]"), 1)
        cfg = config.load(self.path)
        self.assertEqual((cfg.invoice.seller["name"], cfg.invoice.seller["email"], cfg.invoice.vat_rate),
                         ("Jane Doe", "jane@example.com", 15.0))

    def test_multiline_arrays_are_replaced_whole(self):
        self.path.write_text(EDIT_CFG + '\n[invoice.from]\nbank = [\n  "old 1",\n  "old 2"\n]\nname = "keep"\n')
        self.ce.update(self.path, "invoice.from.bank", ["Bank: X", "Account: 1"])
        d = self.toml()
        self.assertEqual(d["invoice"]["from"]["bank"], ["Bank: X", "Account: 1"])
        self.assertEqual(d["invoice"]["from"]["name"], "keep")

    def test_quotes_unicode_and_lists(self):
        self.ce.update(self.path, "invoice.from.name", ['O"Brien & Söns \\ Co'])
        self.ce.update(self.path, "client_address", ["1 Main Rd", "Pretoria 0001"], "acme")
        d = self.toml()
        self.assertEqual(d["invoice"]["from"]["name"], 'O"Brien & Söns \\ Co')
        self.assertEqual(d["projects"][0]["client_address"], ["1 Main Rd", "Pretoria 0001"])

    def test_bad_input_changes_nothing(self):
        before = self.path.read_text()
        for key, vals, proj in (("rte", ["1"], "acme"), ("rate", ["abc"], "acme"), ("rate", ["1", "2"], "acme"),
                                ("rate", ["1"], None), ("rate", ["1"], "ghost"), ("meeting_detect", ["maybe"], None)):
            with self.assertRaises(self.ce.ConfigError, msg=key):
                self.ce.update(self.path, key, vals, proj)
        self.assertEqual(self.path.read_text(), before)
        self.assertFalse(self.path.with_name(self.path.name + ".bak").exists())

    def test_unparseable_config_is_never_made_worse(self):
        self.path.write_text("this is = = not toml\n")
        with self.assertRaises(self.ce.ConfigError):
            self.ce.update(self.path, "rounding_minutes", ["6"])
        self.assertEqual(self.path.read_text(), "this is = = not toml\n")

    def test_backup_keeps_the_state_before_a_batch(self):
        self.ce.update_many(self.path, [("rate", ["650"], "acme"), ("invoice.vat_rate", ["15"], None),
                                        ("currency", ["ZAR"], "acme")])
        self.assertEqual(self.path.with_name(self.path.name + ".bak").read_text(), EDIT_CFG)
        d = self.toml()
        self.assertEqual((d["projects"][0]["rate"], d["projects"][0]["currency"], d["invoice"]["vat_rate"]), (650, "ZAR", 15))

    def test_cli_set_show_and_wizard(self):
        import contextlib
        import io
        from unittest import mock
        from uurtjie import cli, config as config_mod

        def run(fn, *a, **kw):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), mock.patch.object(config_mod, "CONFIG_PATH", self.path), \
                    mock.patch.object(cli, "reload_daemon") as reload:
                fn(*a, **kw)
            return buf.getvalue(), reload

        cfg = lambda: config_mod.load(self.path)
        out, reload = run(cli.cmd_config, argparse_ns(config_cmd="set", key="rate", value=["650"], project=None), cfg(), None)
        self.assertIn("rate (acme): 0 → 650", out)                    # default project is acme
        reload.assert_called_once()
        out, _ = run(cli.cmd_config, argparse_ns(config_cmd="show"), cfg(), None)
        self.assertIn("ZAR 650.00 / hour", out)
        self.assertIn("not yet, still missing invoice.from.name, invoice.from.bank", out)
        # in prompt order: name | address (2 lines + blank) | email | phone | VAT no | reg no | bank (2 lines + blank)
        # | footer | VAT % | terms | client | client address ('-' clears) | client VAT no | client reference
        answers = iter(["Jane Doe", "1 Main Rd", "Joburg", "", "jane@example.com", "", "4987654321", "",
                        "Bank: X", "Acc: 1", "", "Thanks", "15", "", "Client Co", "-", "", ""])
        out, reload = run(cli._config_wizard, cfg(), self.path, "acme", ask=lambda prompt: next(answers))
        d = self.toml()
        self.assertEqual(d["invoice"]["from"]["name"], "Jane Doe")
        self.assertEqual(d["invoice"]["from"]["bank"], ["Bank: X", "Acc: 1"])
        self.assertEqual(d["invoice"]["vat_rate"], 15)
        self.assertEqual(d["projects"][0]["client"], "Client Co")
        reload.assert_called_once()
        out, _ = run(cli.cmd_config, argparse_ns(config_cmd="show"), cfg(), None)
        self.assertIn("ready to invoice: yes", out)


class EarningsTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(Path(TMP) / f"e{time.time_ns()}.db")
        self.addCleanup(self.con.close)
        self.cfg = load_cfg()

    def test_matches_what_the_invoice_would_charge(self):
        from uurtjie import report as R
        db.open_span(self.con, "acme", "terminal", "w1", ts("2026-10-05 09:00"), ts("2026-10-05 10:30"))
        db.open_span(self.con, "acme", "chrome", "c", ts("2026-10-06 14:00"), ts("2026-10-06 14:07"))   # 7 min -> 12 min
        db.open_span(self.con, "acme", "manual", "m", ts("2026-10-07 09:00"), ts("2026-10-07 10:00"), billable=False)
        start, end = R.month_bounds(2026, 10)
        got = R.earned(self.con, self.cfg, start, end, now=ts("2026-10-31 12:00"))
        rep = R.month_report(self.con, self.cfg, 2026, 10, "acme", with_git=False)
        self.assertEqual(got, {"ZAR": rep["total_amount"]})
        self.assertEqual(got, {"ZAR": 1700.0})

    def test_unrated_projects_and_other_months_add_nothing(self):
        from uurtjie import report as R
        self.cfg.project("acme").rate = 0
        db.open_span(self.con, "acme", "terminal", "w1", ts("2026-10-05 09:00"), ts("2026-10-05 10:00"))
        db.open_span(self.con, "acme-infra", "terminal", "w2", ts("2026-09-05 09:00"), ts("2026-09-05 10:00"))
        start, end = R.month_bounds(2026, 10)
        self.assertEqual(R.earned(self.con, self.cfg, start, end, now=ts("2026-10-31 12:00")), {})

    def test_widget_shows_month_to_date_and_it_can_be_hidden_in_the_bar(self):
        from unittest import mock
        from uurtjie import report as R
        from uurtjie.cli import build_status, money_label
        now = int(time.time())
        db.open_span(self.con, "acme", "terminal", "w1", now - 1800, now - 60)
        with mock.patch("uurtjie.cli.read_state", return_value=None):
            st = build_status(self.cfg, self.con)
            self.cfg.show_money_in_bar = False
            quiet = build_status(self.cfg, self.con)
        month = R.earned(self.con, self.cfg, R.local_midnight(date.today().replace(day=1)), now + 1, now)
        self.assertTrue(month)
        self.assertEqual(st["earnings"]["month"], month)
        self.assertIn(" · " + money_label(month, cents=False), st["bar_text"])
        self.assertIn("Earned: " + money_label(month) + " this month", st["tooltip"])
        self.assertNotIn(money_label(month, cents=False), quiet["bar_text"])
        self.assertIn("Earned:", quiet["tooltip"])                  # hidden from the bar only

    def test_money_label(self):
        from uurtjie.cli import money_label
        self.assertEqual(money_label({"ZAR": 1234.5}), "R1,234.50")
        self.assertEqual(money_label({"ZAR": 1234.5}, cents=False), "R1,234")
        self.assertEqual(money_label({"ZAR": 10.0, "CHF": 5.0}), "CHF 5.00 + R10.00")


if __name__ == "__main__":
    unittest.main()
