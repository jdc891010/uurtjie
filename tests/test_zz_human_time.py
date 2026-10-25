"""Strict human-time accounting and upgrade compatibility."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from uurtjie import cli, config, config_edit, daemon, db, invoice, report


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
