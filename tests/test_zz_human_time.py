"""Strict human-time accounting and upgrade compatibility."""
import io
import asyncio
import json
import random
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from uurtjie import cli, config, config_edit, daemon, db, invoice, report, sessions


class HumanTimeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.con = db.connect(self.root / "data/uurtjie.db")
        self.addCleanup(self.con.close)
        self.cfg = config.Config(projects=[config.Project("acme", "Acme", rate=100),
                                           config.Project("other", "Other", rate=200)])
        self.day = date(2026, 9, 1)
        self.start = report.local_midnight(self.day) + 9 * 3600

    def add(self, start, end, project="acme", source="terminal", **kw):
        return db.open_span(self.con, project, source, source, self.start + start, self.start + end, **kw)

    def issue(self, **kw):
        return invoice.create(self.con, self.cfg, "acme", self.day, self.day,
                              today=date(2026, 10, 2), backup=False, with_git=False, **kw)

    def test_legacy_factor_is_ignored_and_warned_once_on_every_surface(self):
        path = self.root / "config.toml"
        path.write_text('[[projects]]\nid="acme"\nrate=100\nhuman_factor=0.1\n'
                        '[[projects]]\nid="other"\nhuman_factor="ignored even if invalid"\n')
        self.cfg = config.load(path)
        self.assertEqual(self.cfg.warnings, ["human_factor is no longer used"])
        self.add(0, 3600)
        snap = self.issue()
        self.assertEqual((snap["hours"], snap["subtotal"]), (1, 100))
        self.assertNotIn("human_factor", snap)
        self.assertNotIn("human_factor", report.month_report(self.con, self.cfg, 2026, 9, "acme", False))
        with self.assertLogs("uurtjie", level="WARNING") as logs:
            d = daemon.Daemon(self.cfg, self.con, None, None)
            with patch.object(daemon.config_mod, "load", return_value=self.cfg):
                d.reload()
                d.reload()
        self.assertEqual(sum("human_factor is no longer used" in line for line in logs.output), 1)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_projects(None, self.cfg, self.con)
        self.assertEqual(out.getvalue().count("human_factor is no longer used"), 1)
        with patch.object(cli, "read_state", return_value=None):
            status = cli.build_status(self.cfg, self.con)
        self.assertEqual(status["tooltip"].count("human_factor is no longer used"), 1)

    def test_config_set_factor_refused_without_changing_file(self):
        path = self.root / "config.toml"
        path.write_text('[[projects]]\nid="acme"\n')
        before = path.read_text()
        for key in ["human_factor", "projects.acme.human_factor"]:
            with self.assertRaisesRegex(config_edit.ConfigError, "human_factor is no longer used"):
                config_edit.update(path, key, ["0.8"], "acme")
        self.assertEqual(path.read_text(), before)

    def test_old_frozen_invoice_keeps_factor_and_renders_its_note(self):
        self.add(0, 3600)
        snap = self.issue()
        snap["human_factor"] = 0.8
        snap["hours"], snap["subtotal"], snap["total"] = 0.8, 80, 80
        encoded = json.dumps(snap)
        self.con.execute("UPDATE invoices SET snapshot=? WHERE number=?", (encoded, snap["number"]))
        self.cfg.projects[0].rate = 500
        stored = invoice.snapshot(invoice.find(self.con, snap["number"]))
        html = invoice.render_html(stored)
        self.assertIn("factor 0.8", html)
        self.assertEqual(stored["total"], 80)
        self.assertEqual(self.con.execute("SELECT snapshot FROM invoices").fetchone()[0], encoded)

    def test_precedence_allocates_each_second_to_one_project(self):
        self.add(0, 100)
        self.add(20, 80, "other", "meeting")
        self.add(40, 60, source="manual")
        result = report.sessions_between(self.con, self.cfg, self.start, self.start + 100)
        by_project = {pid: sum(s["seconds"] for s in result if s["project"] == pid)
                      for pid in ["acme", "other"]}
        self.assertEqual(by_project, {"acme": 60, "other": 40})
        self.assertEqual(report.daily_totals(self.con, self.start, self.start + 100,
                                            project="other")[("other", self.day)]["seconds"], 40)

    def test_running_timer_wins_over_meeting_and_focus(self):
        self.add(0, 100)
        self.add(0, 100, source="meeting")
        db.timer_start(self.con, "other", at=self.start + 20)
        result = report.sessions_between(self.con, self.cfg, self.start, self.start + 100, now=self.start + 80)
        self.assertEqual(sum(s["seconds"] for s in result if s["project"] == "other"), 60)
        self.assertEqual(sum(s["seconds"] for s in result), 100)

    def test_sample_tolerance_groups_without_adding_seconds(self):
        self.add(0, 20)
        self.add(25, 40)
        result = report.sessions_between(self.con, self.cfg, self.start, self.start + 40)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["seconds"], 35)
        self.assertEqual(result[0]["end"] - result[0]["start"], 40)
        self.assertAlmostEqual(sum(report.hourly_minutes(result, self.day)) * 60, 35)

    def test_configured_bridge_is_invoice_only_and_does_not_change_raw_time(self):
        self.cfg.bridge_minutes = 5
        self.cfg.rounding_minutes = 0
        self.add(0, 60)
        self.add(180, 240)
        strict = report.sessions_between(self.con, self.cfg, self.start, self.start + 240)
        self.assertEqual([s["seconds"] for s in strict], [60, 60])
        rep = report.range_report(self.con, self.cfg, self.day, self.day, "acme", False)
        self.assertEqual((rep["total_raw_seconds"], rep["days"][0]["billing_seconds"]), (120, 240))
        self.assertAlmostEqual(rep["total_hours"], 240 / 3600)
        self.add(80, 100, "other", "manual")
        rep = report.range_report(self.con, self.cfg, self.day, self.day, "acme", False)
        self.assertEqual(rep["days"][0]["billing_seconds"], 120)

    def test_random_evidence_obeys_union_ownership_and_all_aggregation_invariants(self):
        rng = random.Random(500)
        priority = {"manual": 3, "meeting": 2, "terminal": 1, "chrome": 1, "window": 1}
        base = report.local_midnight(self.day) + 23 * 3600 + 45 * 60
        end = base + 1800
        for case in range(60):
            with self.subTest(case=case):
                self.con.execute("DELETE FROM spans")
                self.cfg.bridge_minutes = rng.choice([0, 5])
                for _ in range(rng.randrange(1, 25)):
                    start = base + rng.randrange(1800)
                    stop = min(end, start + rng.randrange(301))
                    db.open_span(self.con, rng.choice(["acme", "other"]), rng.choice([*priority, "idle", "untracked"]),
                                 "test", start, stop, billable=rng.choice([True, True, False]))
                rows = [dict(r) for r in db.spans_between(self.con, base, end)]
                expected = {}
                for second in range(base, end):
                    active = [r for r in rows if r["source"] in priority and r["start_ts"] <= second < r["end_ts"]]
                    if active:
                        expected[second] = min(active, key=lambda r: (-priority[r["source"]], r["start_ts"],
                                                                     r["project"], r["id"]))["project"]
                result = sessions.build_sessions(rows, self.cfg, (base, end))
                actual = {}
                for session in result:
                    self.assertEqual(session["seconds"], sum(b-a for a, b in session["intervals"]))
                    for a, b in session["intervals"]:
                        for second in range(a, b):
                            self.assertNotIn(second, actual)
                            actual[second] = session["project"]
                self.assertEqual(actual, expected)
                seconds = sum(s["seconds"] for s in result)
                self.assertLessEqual(seconds, end - base)
                daily = report.daily_totals(self.con, base, end, include_timer=False)
                self.assertEqual(sum(d["seconds"] for d in daily.values()), seconds)
                self.assertEqual(sum(sum(d["sources"].values()) for d in daily.values()), seconds)
                hourly = sum(sum(report.hourly_minutes(result, day)) for day in [self.day, self.day + timedelta(days=1)])
                self.assertAlmostEqual(hourly * 60, seconds)
                raw = 0
                for pid in ["acme", "other"]:
                    rep = report.range_report(self.con, self.cfg, self.day, self.day + timedelta(days=1), pid, False)
                    self.assertAlmostEqual(rep["total_raw_hours"] * 3600, rep["total_raw_seconds"])
                    raw += rep["total_raw_seconds"]
                self.assertEqual(raw, seconds)

    def test_widget_uses_exact_union_and_earnings_even_with_invoice_adjustments(self):
        self.start = report.local_midnight(date.today()) + 9 * 3600
        self.cfg.rounding_minutes, self.cfg.bridge_minutes = 6, 5
        self.add(0, 3600)
        self.add(600, 1800, "other", "meeting")
        self.add(900, 1200, source="manual")
        with patch.object(cli.time, "time", return_value=self.start + 3600), \
                patch.object(cli, "read_state", return_value=None):
            status = cli.build_status(self.cfg, self.con)
            out = io.StringIO()
            with redirect_stdout(out):
                cli.cmd_status(Namespace(waybar=True, json=False), self.cfg, self.con)
        self.assertEqual(status["today"]["seconds"], 3600)
        self.assertEqual(sum(p["seconds"] for p in status["today"]["projects"]), 3600)
        self.assertAlmostEqual(sum(status["hourly"]["minutes"]) * 60, 3600)
        self.assertEqual(status["earnings"]["today"], {"ZAR": 125})
        self.assertIn("Human time", status["tooltip"])
        self.assertIn("rounding 6 min/day", status["tooltip"])
        self.assertIn("bridging 5 min", status["tooltip"])
        self.assertEqual(json.loads(out.getvalue())["tooltip"], status["tooltip"])

    def test_zero_defaults_make_widget_earnings_equal_invoice(self):
        self.assertEqual((self.cfg.bridge_minutes, self.cfg.rounding_minutes), (0, 0))
        self.add(0, 61)
        self.add(66, 127)
        earned = report.earned(self.con, self.cfg, *report.month_bounds(2026, 9))
        snap = self.issue()
        self.assertEqual(earned, {"ZAR": snap["subtotal"]})
        self.assertAlmostEqual(snap["hours"] * 3600, 122)
        self.assertIn("Human time, no time rounding", invoice.render_html(snap))

    def tracker(self):
        hypr = Mock(active_window=AsyncMock(return_value={"address": "1"}))
        d = daemon.Daemon(self.cfg, self.con, hypr, None)
        d.write_state = Mock()
        d.enrich = Mock()
        return d

    def tick(self, d, second, context):
        with patch.object(daemon.time, "time", return_value=self.start + second), \
                patch.object(d, "resolve", new=AsyncMock(return_value=context)):
            asyncio.run(d.tick())

    def test_context_switch_and_untracked_window_share_exact_sample_boundaries(self):
        d = self.tracker()
        a, b = daemon.Context("acme", "window", "a"), daemon.Context("other", "window", "b")
        for second, ctx in [(0, a), (5, a), (10, b), (15, b), (20, None), (25, None), (30, a), (35, a)]:
            self.tick(d, second, ctx)
        rows = self.con.execute("SELECT * FROM spans ORDER BY start_ts").fetchall()
        self.assertEqual([(r["project"], r["start_ts"]-self.start, r["end_ts"]-self.start) for r in rows],
                         [("acme", 0, 10), ("other", 10, 20), ("acme", 30, 35)])
        result = report.sessions_between(self.con, self.cfg, self.start, self.start + 40)
        self.assertEqual(sum(s["seconds"] for s in result), 25)

    def test_chunk_rollover_then_idle_never_retains_idle_lookback(self):
        d = self.tracker()
        ctx = daemon.Context("acme", "window", "a")
        self.cfg.chunk_minutes = 1
        self.cfg.sleep_minutes = 0.5
        for second in range(0, 81, 5):
            self.tick(d, second, ctx)
        rows = self.con.execute("SELECT start_ts,end_ts FROM spans ORDER BY id").fetchall()
        self.assertEqual(rows[0][1], rows[1][0])
        d.on_idle(self.start + 85)
        result = report.sessions_between(self.con, self.cfg, self.start, self.start + 100)
        self.assertEqual(sum(s["seconds"] for s in result), 55)
        self.tick(d, 95, ctx)
        self.assertEqual(sum(r[0] for r in self.con.execute("SELECT end_ts-start_ts FROM spans")), 55)

    def test_meeting_while_idle_and_mute_gap_only_count_observed_capture(self):
        d = self.tracker()
        d.audio = object()
        ctx = daemon.Context("acme", "meeting", "call")
        d.detect_meeting = AsyncMock(return_value=ctx)
        d.on_idle(self.start)
        for second in range(0, 61, 5):
            with patch.object(daemon.time, "time", return_value=self.start + second):
                asyncio.run(d.tick())
        d.detect_meeting.return_value = None
        for second in [65, 70, 90, 120]:
            with patch.object(daemon.time, "time", return_value=self.start + second):
                asyncio.run(d.tick())
        d.detect_meeting.return_value = ctx
        for second in [125, 130, 135]:
            with patch.object(daemon.time, "time", return_value=self.start + second):
                asyncio.run(d.tick())
        d.detect_meeting.return_value = None
        with patch.object(daemon.time, "time", return_value=self.start + 140):
            asyncio.run(d.tick())
        result = report.sessions_between(self.con, self.cfg, self.start, self.start + 200)
        self.assertEqual(sum(s["seconds"] for s in result), 80)
        self.assertEqual([(a-self.start,b-self.start) for s in result for a,b in s["intervals"]], [(0,65),(125,140)])
        self.assertTrue(d.idle)

    def test_zero_meeting_grace_still_confirms_continuous_capture(self):
        d = self.tracker()
        d.audio = object()
        self.cfg.meeting_grace_seconds = 0
        d.detect_meeting = AsyncMock(return_value=daemon.Context("acme", "meeting", "call"))
        for second in range(0, 61, 5):
            asyncio.run(d.meeting_tick(self.start + second))
        self.assertEqual(sum(r[0] for r in self.con.execute("SELECT end_ts-start_ts FROM spans")), 60)
