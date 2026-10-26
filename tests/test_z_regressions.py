"""Regression scenarios from the security and accounting review."""
from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from argparse import Namespace
from contextlib import contextmanager, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from uurtjie import cli, config, config_edit, daemon, db, invoice, report, storage

REPO = Path(__file__).resolve().parents[1]


@contextmanager
def timezone(name):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


class ReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "data/uurtjie.db"
        self.con = db.connect(self.path)
        self.addCleanup(self.con.close)
        self.cfg = config.Config(projects=[config.Project("a", "A", rate=100), config.Project("b", "B")])
        self.start = report.local_midnight(date(2026, 9, 1)) + 9 * 3600

    def add(self, a=0, b=3600, project="a", **kw):
        return db.open_span(self.con, project, "manual", "test", self.start + a, self.start + b, **kw)

    def make_invoice(self, **kw):
        return invoice.create(self.con, self.cfg, "a", date(2026, 9, 1), date(2026, 9, 30),
                              today=date(2026, 10, 2), backup=False, with_git=False, **kw)

    def test_private_files_and_config_replacement_under_public_umask(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        extra = db.connect(self.root / "public-default/db")
        self.addCleanup(extra.close)
        backup = db.backup(extra, self.root / "backups")
        path = self.root / "config/config.toml"
        config_edit.write(path, "sleep_minutes=5\n")
        path.chmod(0o600)
        config_edit.write(path, "sleep_minutes=6\n")
        self.add()
        page, _ = invoice.write_files(self.make_invoice(), self.root / "invoices")
        for file in [self.root / "public-default/db", backup, path, path.with_suffix(".toml.bak"), page]:
            self.assertEqual(file.stat().st_mode & 0o777, 0o600, str(file))
        self.assertEqual((self.root / "public-default").stat().st_mode & 0o777, 0o700)

    def test_runtime_named_symlinks_cannot_clobber_files(self):
        runtime = self.root / "run/uurtjie"
        runtime.mkdir(parents=True)
        runtime.parent.chmod(0o700)
        victim = self.root / "victim"
        victim.write_text("untouched")
        (runtime / "state.json.tmp").symlink_to(victim)
        (runtime / "state.json").symlink_to(victim)
        d = daemon.Daemon(self.cfg, self.con, None, None)
        with patch.object(daemon, "RUNTIME_DIR", runtime):
            d.write_state(self.start, None)
        self.assertEqual(victim.read_text(), "untouched")
        self.assertFalse((runtime / "state.json").is_symlink())
        self.assertEqual(json.loads((runtime / "state.json").read_text())["ts"], self.start)
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)

    def test_pdf_output_is_private_and_failed_render_does_not_claim_stale_file(self):
        self.add()
        snap = self.make_invoice()
        out = self.root / "invoices"
        out.mkdir()
        existing = out / f"{snap['number']}.pdf"
        existing.write_bytes(b"old PDF")
        with patch.object(invoice, "html_to_pdf", return_value=False):
            _, pdf = invoice.write_files(snap, out, pdf=True)
        self.assertIsNone(pdf)
        self.assertEqual(existing.read_bytes(), b"old PDF")

        def render(html, target):
            target.write_bytes(b"new PDF")
            target.chmod(0o644)
            return True

        with patch.object(invoice, "html_to_pdf", render):
            _, pdf = invoice.write_files(snap, out, pdf=True)
        self.assertEqual(pdf.read_bytes(), b"new PDF")
        self.assertEqual(pdf.stat().st_mode & 0o777, 0o600)

    def test_runtime_rejects_shared_root_and_symlinked_directory(self):
        shared = self.root / "shared"
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        with self.assertRaises(PermissionError):
            storage.runtime_dir(shared / "uurtjie")
        (self.root / "link").symlink_to(self.root / "data", target_is_directory=True)
        with self.assertRaises(OSError):
            storage.runtime_dir(self.root / "link")

    def test_database_rejects_symlink(self):
        victim = self.root / "victim"
        victim.write_text("untouched")
        link = self.root / "data/link.db"
        link.symlink_to(victim)
        with self.assertRaises(OSError):
            db.connect(link)
        self.assertEqual(victim.read_text(), "untouched")

    def test_idle_trims_completed_chunks_and_contexts_but_not_manual_or_calls(self):
        for switch in [False, True]:
            with self.subTest(switch=switch):
                self.con.execute("DELETE FROM spans")
                d = daemon.Daemon(self.cfg, self.con, None, None)
                d.write_state = Mock()
                for sec in range(0, 1046, 5):
                    ctx = daemon.Context("a", "window", "other" if switch and sec >= 850 else "window")
                    d._record("current", ctx, self.start + sec)
                manual = self.add(0, 1045)
                call = db.open_span(self.con, "a", "meeting", "call", self.start, self.start + 1045)
                d.on_idle(self.start + 1050)
                seconds = self.con.execute("SELECT SUM(end_ts-start_ts) FROM spans WHERE source='window'").fetchone()[0]
                self.assertEqual(seconds, 750)
                for sid in [manual, call]:
                    self.assertEqual(self.con.execute("SELECT end_ts FROM spans WHERE id=?", (sid,)).fetchone()[0],
                                     self.start + 1045)

    def test_nonbillable_spans_block_bridges_and_override_same_project_overlap(self):
        for project in ["a", "b"]:
            with self.subTest(project=project):
                self.con.execute("DELETE FROM spans")
                self.add(0, 3600)
                self.add(3600, 3840, project=project, billable=False)
                self.add(3840, 7200)
                sessions = report.sessions_between(self.con, self.cfg, self.start, self.start + 7200, billable_only=True)
                self.assertEqual(sum(s["seconds"] for s in sessions), 6960)
        self.con.execute("DELETE FROM spans")
        self.add(0, 7200)
        self.add(3600, 3840, billable=False)
        sessions = report.sessions_between(self.con, self.cfg, self.start, self.start + 7200, billable_only=True)
        self.assertEqual([s["seconds"] for s in sessions], [3600, 3360])

    def test_concurrent_invoice_creation_claims_period_once(self):
        self.add()
        barrier = threading.Barrier(2)
        original = report.range_report
        outcomes = []

        def prepare(*args, **kw):
            self.assertFalse(args[0].in_transaction)
            result = original(*args, **kw)
            barrier.wait(timeout=5)
            return result

        def worker():
            con = db.connect(self.path)
            try:
                result = invoice.create(con, self.cfg, "a", date(2026, 9, 1), date(2026, 9, 30),
                                        today=date(2026, 10, 2), backup=False, with_git=False)
                outcomes.append(result["number"])
            except invoice.InvoiceError:
                outcomes.append("overlap")
            finally:
                con.close()

        with patch.object(report, "range_report", prepare):
            workers = [threading.Thread(target=worker) for _ in range(2)]
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
        self.assertCountEqual(outcomes, ["INV-2026-001", "overlap"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 1)

    def test_invoice_refuses_a_report_changed_during_enrichment(self):
        self.add()
        original = report.range_report

        def prepare(*args, **kw):
            result = original(*args, **kw)
            self.add(7200, 10800)
            return result

        with patch.object(report, "range_report", prepare), self.assertRaisesRegex(invoice.InvoiceError, "changed"):
            self.make_invoice()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM invoices").fetchone()[0], 0)

    def test_timer_stop_respects_locks_and_explicit_force(self):
        self.add()
        self.make_invoice()
        db.timer_start(self.con, "a", at=self.start + 7200)
        args = Namespace(at=self.start + 10800, note=None, discard=False, force=False)
        with self.assertRaises(invoice.InvoiceError):
            cli.cmd_stop(args, self.cfg, self.con)
        self.assertIsNotNone(db.timer_get(self.con))
        args.force = True
        with redirect_stdout(io.StringIO()):
            cli.cmd_stop(args, self.cfg, self.con)
        self.assertIsNone(db.timer_get(self.con))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM spans WHERE ctx_key='timer'").fetchone()[0], 1)

    def test_failed_timer_stop_rolls_back_span_and_keeps_timer(self):
        db.timer_start(self.con, "a", at=self.start)
        original = db.open_span

        def crash(*a, **kw):
            original(*a, **kw)
            raise RuntimeError("interrupted stop")

        with patch.object(db, "open_span", crash), self.assertRaises(RuntimeError):
            db.timer_stop(self.con, at=self.start + 3600)
        self.assertIsNotNone(db.timer_get(self.con))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM spans").fetchone()[0], 0)
        self.assertIsNotNone(db.timer_stop(self.con, at=self.start + 3600))
        self.assertIsNone(db.timer_stop(self.con, at=self.start + 3600))

    def test_discarding_short_or_long_timer_cannot_bypass_invoice_lock(self):
        self.add()
        self.make_invoice()
        db.timer_start(self.con, "a", at=self.start)
        for duration, discard in [(30, False), (3600, True)]:
            args = Namespace(at=self.start + duration, note=None, discard=discard, force=False)
            with self.assertRaises(invoice.InvoiceError):
                cli.cmd_stop(args, self.cfg, self.con)
            self.assertIsNotNone(db.timer_get(self.con))

    def test_two_simultaneous_stops_save_one_span(self):
        db.timer_start(self.con, "a", at=self.start)
        first_ready, second_attempted, release = threading.Event(), threading.Event(), threading.Event()
        original_open, original_get = db.open_span, db.timer_get
        results, errors = [], []

        def insert(*args, **kw):
            if threading.current_thread().name == "first-stop":
                first_ready.set()
                if not release.wait(5):
                    raise RuntimeError("test failed to release first stop")
            return original_open(*args, **kw)

        def get(con):
            result = original_get(con)
            if threading.current_thread().name == "second-stop":
                second_attempted.set()
            return result

        def worker():
            con = db.connect(self.path)
            con.set_trace_callback(lambda sql: second_attempted.set()
                                   if threading.current_thread().name == "second-stop" and sql == "BEGIN IMMEDIATE" else None)
            try:
                results.append(db.timer_stop(con, at=self.start + 3600))
            except Exception as exc:
                errors.append(exc)
            finally:
                con.close()

        with patch.object(db, "open_span", insert), patch.object(db, "timer_get", get):
            first = threading.Thread(target=worker, name="first-stop")
            second = threading.Thread(target=worker, name="second-stop")
            first.start()
            try:
                self.assertTrue(first_ready.wait(5))
                second.start()
                self.assertTrue(second_attempted.wait(5))
            finally:
                release.set()
                first.join(timeout=10)
                if second.ident is not None:
                    second.join(timeout=10)
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(results.count(None), 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM spans").fetchone()[0], 1)

    def test_exact_minutes_and_daily_money_are_shared_by_invoice_and_earnings(self):
        self.cfg.rounding_minutes = 0
        self.cfg.projects[0].rate = 600
        self.add(0, 60)
        snap = self.make_invoice(dry_run=True)
        self.assertAlmostEqual(snap["hours"], 1 / 60)
        self.assertEqual(snap["subtotal"], 10)
        self.con.execute("DELETE FROM spans")
        self.cfg.projects[0].rate = 101.01
        for day in range(3):
            self.add(day * 86400, day * 86400 + 1800)
        snap = self.make_invoice(dry_run=True)
        self.assertEqual([d["amount"] for d in snap["days"]], [50.51] * 3)
        self.assertEqual(snap["subtotal"], 151.53)
        bounds = report.month_bounds(2026, 9)
        self.assertEqual(report.earned(self.con, self.cfg, *bounds), {"ZAR": snap["subtotal"]})

    def test_literal_and_multiline_toml_strings_preserve_unrelated_settings(self):
        path = self.root / "config.toml"
        original = "backup_dir = '~/Sync/[backups'\nsleep_minutes = 9\n[[projects]]\nid = 'a'\npaths = ['/code']\n"
        path.write_text(original)
        config_edit.update(path, "backup_dir", ["~/New"])
        parsed = tomllib.loads(path.read_text())
        expected = tomllib.loads(original)
        expected["backup_dir"] = "~/New"
        self.assertEqual(parsed, expected)
        path.write_text('sleep_minutes=5\n[invoice.from]\nfooter="""hello\n[invoice]\nworld"""\n')
        config_edit.update(path, "invoice.vat_rate", ["15"])
        parsed = tomllib.loads(path.read_text())
        self.assertEqual(parsed["invoice"]["from"]["footer"], "hello\n[invoice]\nworld")
        self.assertEqual(parsed["invoice"]["vat_rate"], 15)

    def test_terminal_uses_foreground_group_instead_of_newer_background_job(self):
        stats = {100: "100 (bash) S 10 100 100 42 100", 200: "200 (sleep) S 100 200 100 42 100"}
        with patch.object(daemon, "descendants", return_value=[(100, "bash", False), (200, "sleep", True)]), \
                patch.object(Path, "read_text", lambda p: stats[int(p.parent.name)]), \
                patch.object(daemon, "proc_cwd", side_effect=lambda pid: {100: "/code/b", 200: "/code/a"}[pid]):
            self.assertEqual(daemon.terminal_kind(10), ("plain", "/code/b"))

    def test_herdr_server_pane_ptys_do_not_hide_herdr_terminal(self):
        # foot(10) -> bash(100) -> herdr client(200) -> herdr server(300) -> pane shells on other PTYs
        children = {10: [100], 100: [200], 200: [300], 300: [400, 500]}
        comm = {100: "bash", 200: "herdr", 300: "herdr", 400: "bash", 500: "bash"}
        stats = {100: "100 (bash) S 10 100 100 42 200", 200: "200 (herdr) S 100 200 100 42 200",
                 300: "300 (herdr) S 200 300 300 0 -1", 400: "400 (bash) S 300 400 400 43 400",
                 500: "500 (bash) S 300 500 500 44 500"}
        with patch.object(daemon, "_proc_table", return_value=(children, comm)), \
                patch.object(Path, "read_text", lambda p: stats[int(p.parent.name)]):
            self.assertEqual(daemon.terminal_kind(10), ("herdr", None))

    def test_two_configured_clients_in_shared_browser_require_focus(self):
        self.cfg.projects[0].windows = [config.WindowRule(cls="browser-a")]
        self.cfg.projects[1].windows = [config.WindowRule(cls="browser-b")]
        windows = [{"pid": 5, "address": "A", "class": "browser-a"},
                   {"pid": 5, "address": "B", "class": "browser-b"}]
        hypr = Namespace(clients=AsyncMock(return_value=windows), active_window=AsyncMock(return_value=windows[1]))
        audio = Namespace(mic_streams=AsyncMock(return_value=[{"pid": 6, "app": "Browser", "binary": "chrome", "media": "Call"}]))
        d = daemon.Daemon(self.cfg, self.con, hypr, None, audio)
        with patch.object(daemon, "proc_ancestors", return_value=[6, 5]):
            self.assertEqual(asyncio.run(d.detect_meeting()).project, "b")
            hypr.active_window.return_value = {"address": "unrelated"}
            self.assertIsNone(asyncio.run(d.detect_meeting()))

    def test_plain_terminal_cannot_bypass_required_agent(self):
        self.cfg.projects[0].paths = [Path("/code/a")]
        self.cfg.projects[0].require_agent = True
        d = daemon.Daemon(self.cfg, self.con, None, None)
        with patch.object(daemon, "terminal_kind", return_value=("plain", "/code/a")):
            self.assertIsNone(asyncio.run(d.resolve({"class": "foot", "pid": 10})))

    def test_delete_or_reassign_active_span_recovers_on_next_sample(self):
        for edit in [False, True]:
            with self.subTest(edit=edit):
                self.con.execute("DELETE FROM spans")
                d = daemon.Daemon(self.cfg, self.con, None, None)
                ctx = daemon.Context("a", "window", "w")
                d._record("current", ctx, self.start)
                d._record("current", ctx, self.start + 5)
                sid = d.current["id"]
                if edit:
                    self.con.execute("UPDATE spans SET project='b' WHERE id=?", (sid,))
                else:
                    self.con.execute("DELETE FROM spans WHERE id=?", (sid,))
                d._record("current", ctx, self.start + 10)
                d._record("current", ctx, self.start + 15)
                row = self.con.execute("SELECT * FROM spans WHERE id=?", (d.current["id"],)).fetchone()
                self.assertEqual((row["project"], row["start_ts"], row["end_ts"]), ("a", self.start + 10, self.start + 15))

    def test_edit_rejects_inverted_interval_without_writing(self):
        with timezone("Africa/Johannesburg"):
            sid = self.add()
            args = Namespace(id=sid, project=None, note=None, start=None, end="08:00", billable=None, force=False)
            with self.assertRaises(ValueError):
                cli.cmd_edit(args, self.cfg, self.con)
            self.assertEqual(self.con.execute("SELECT end_ts-start_ts FROM spans WHERE id=?", (sid,)).fetchone()[0], 3600)

    def test_overnight_ranges_and_hour_chart_preserve_dst_elapsed_time(self):
        with timezone("Europe/Berlin"):
            for day, duration in [(date(2026, 10, 24), 6 * 3600), (date(2026, 3, 28), 4 * 3600)]:
                result = cli.parse_quick("a 23:00-04:00", self.cfg, today=day)
                self.assertEqual(result["end"] - result["start"], duration)
                self.assertEqual(cli.ts_local(result["end"], "%H:%M"), "04:00")
                transition = day + timedelta(days=1)
                a, b = report.local_midnight(transition), report.local_midnight(transition + timedelta(days=1))
                chart = report.hourly_minutes([{"start": a, "end": b}], transition)
                self.assertEqual(sum(chart), (b - a) / 60)
            with self.assertRaises(ValueError):
                cli.parse_clock("02:30", date(2026, 10, 25))
            with self.assertRaises(ValueError):
                cli.parse_clock("02:30", date(2026, 3, 29))
            after_transition = cli.parse_quick("a 23:00-02:30", self.cfg, today=date(2026, 10, 25))
            self.assertEqual(after_transition["end"] - after_transition["start"], 3.5 * 3600)
            self.assertEqual(cli.parse_clock("2026-10-25T02:30+01:00", date(2026, 10, 25)),
                             cli.parse_clock("2026-10-25T01:30+00:00", date(2026, 10, 25)))

    def test_short_separate_microphone_captures_do_not_confirm_meeting(self):
        d = daemon.Daemon(self.cfg, self.con, None, None, object())
        ctx = daemon.Context("a", "meeting", "call")
        d.detect_meeting = AsyncMock(side_effect=[ctx, None, ctx])
        for sec in [0, 5, 30]:
            asyncio.run(d.meeting_tick(self.start + sec))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM spans").fetchone()[0], 0)

    def test_unbilled_subtracts_union_of_partial_invoices(self):
        self.add()
        self.add(19 * 86400, 19 * 86400 + 3600)
        invoice.create(self.con, self.cfg, "a", date(2026, 9, 1), date(2026, 9, 15),
                       today=date(2026, 10, 2), backup=False, with_git=False)
        remaining = invoice.unbilled(self.con, self.cfg, today=date(2026, 10, 2))
        self.assertEqual((remaining[0]["hours"], remaining[0]["amount"]), (1, 100))
        invoice.create(self.con, self.cfg, "a", date(2026, 9, 16), date(2026, 9, 30),
                       today=date(2026, 10, 2), backup=False, with_git=False)
        self.assertEqual(invoice.unbilled(self.con, self.cfg, today=date(2026, 10, 2)), [])

    def test_reload_restarts_idle_watcher_and_ignores_old_events(self):
        async def scenario():
            class Process:
                def __init__(self):
                    self.returncode = None
                    self.queue = asyncio.Queue()
                    self.stdout = self

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    value = await self.queue.get()
                    if value is None:
                        raise StopAsyncIteration
                    return value

                def terminate(self):
                    self.returncode = 0
                    self.queue.put_nowait(None)

                async def wait(self):
                    return self.returncode

            processes, commands = [], []
            created = [asyncio.Event(), asyncio.Event()]

            async def spawn(*args, **kw):
                processes.append(Process())
                commands.append(args)
                created[len(processes) - 1].set()
                return processes[-1]

            d = daemon.Daemon(self.cfg, self.con, None, None)
            idle = asyncio.Event()
            d.on_idle = Mock(side_effect=lambda *args: idle.set())
            changed = config.Config(sleep_minutes=1)
            with patch.object(daemon.shutil, "which", return_value="swayidle"), \
                    patch.object(daemon.asyncio, "create_subprocess_exec", spawn), \
                    patch.object(daemon.config_mod, "load", return_value=changed):
                task = asyncio.create_task(d.idle_watch())
                try:
                    await asyncio.wait_for(created[0].wait(), 2)
                    processes[0].queue.put_nowait(b"idle\n")
                    d.reload()
                    await asyncio.wait_for(created[1].wait(), 2)
                    self.assertEqual(commands[0][3], "300")
                    self.assertEqual(commands[1][3], "60")
                    d.on_idle.assert_not_called()
                    processes[1].queue.put_nowait(b"idle\n")
                    await asyncio.wait_for(idle.wait(), 2)
                    self.assertEqual(d.on_idle.call_args.args[1], 60)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertTrue(all(proc.returncode is not None for proc in processes))

        asyncio.run(scenario())


class ShellRegressionTests(unittest.TestCase):
    def test_launcher_ignores_current_directory_and_inherited_pythonpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "uurtjie.py").write_text('raise RuntimeError("untrusted module executed")')
            (root / "csv.py").write_text('raise RuntimeError("untrusted import executed")')
            env = {**os.environ, "UURTJIE_HOME": str(REPO), "PYTHONPATH": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([str(REPO / "bin/uurtjie"), "--version"], cwd=root, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.startswith("uurtjie "))

    def test_quick_popup_preserves_literal_wildcards_and_option_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / ".local/bin"
            bindir.mkdir(parents=True)
            fake = bindir / "uurtjie"
            fake.write_text(f'''#!{sys.executable}
import json,os,sys
from pathlib import Path
if sys.argv[1]=='status': print('{{"today":{{"hm":"0h00"}}}}')
elif sys.argv[1]=='projects': print('* a A')
else:
    with open(os.environ['CALLS'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')
    print('saved')
''')
            fake.chmod(0o755)
            for name in ["sleep", "notify-send"]:
                file = bindir / name
                file.write_text("#!/bin/sh\nexit 0\n")
                file.chmod(0o755)
            (root / "must-not-expand").touch()
            line = "a 30m fix * --force"
            calls = root / "calls"
            env = {**os.environ, "HOME": str(root), "CALLS": str(calls), "PATH": f"{bindir}:{os.environ['PATH']}"}
            result = subprocess.run([str(REPO / "bin/uurtjie-quick"), "--prompt"], cwd=root, env=env,
                                    input=line + "\n", text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual([json.loads(s) for s in calls.read_text().splitlines()],
                             [["quick", "--dry-run", "--", line], ["quick", "--", line]])

    def test_installer_first_install_and_active_upgrade_in_temporary_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "stubs"
            bindir.mkdir()
            systemctl = bindir / "systemctl"
            systemctl.write_text('''#!/bin/sh
echo "$*" >> "$SERVICE_LOG"
case "$*" in
  *is-active*) test -e "$ACTIVE_FILE" ;;
  *stop*) rm -f "$ACTIVE_FILE" ;;
  *restart*) touch "$ACTIVE_FILE" ;;
esac
''')
            systemctl.chmod(0o755)
            for name, code in [("swayidle", 0), ("omarchy", 1)]:
                file = bindir / name
                file.write_text(f"#!/bin/sh\nexit {code}\n")
                file.chmod(0o755)
            run = root / "run"
            run.mkdir(mode=0o700)
            home = root / "user"
            env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}",
                   "SERVICE_LOG": str(root / "calls"), "ACTIVE_FILE": str(root / "active"),
                   "UURTJIE_DATA": str(home / ".local/share/uurtjie"),
                   "UURTJIE_CONFIG": str(home / ".config/uurtjie/config.toml"),
                   "XDG_RUNTIME_DIR": str(run), "PYTHONDONTWRITEBYTECODE": "1"}
            for upgrading in [False, True]:
                (root / "calls").write_text("")
                result = subprocess.run(["bash", str(REPO / "install.sh")], env=env, cwd=root,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = (root / "calls").read_text().splitlines()
                self.assertIn("--user restart uurtjie.service", calls)
                if upgrading:
                    self.assertLess(calls.index("--user stop uurtjie.service"), calls.index("--user restart uurtjie.service"))
                self.assertEqual((home / ".config/uurtjie/config.toml").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
