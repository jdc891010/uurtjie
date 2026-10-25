"""Regression checks for release version consistency and failed mutation tooling."""
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import changelog, check_mutations
from test_changelog import make_repo


class ReleaseRegressionTests(unittest.TestCase):
    def test_version_bump_updates_panel_and_detects_stale_or_missing_panel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(Path(tmp))
            changelog.write_version("0.4.2", root)
            self.assertIn('widgetVersion: "0.4.2"', (root / changelog.PANEL).read_text())
            self.assertEqual(changelog.current_version(root), "0.4.2")
            (root / changelog.PANEL).write_text('readonly property string widgetVersion: "0.4.1"')
            with self.assertRaisesRegex(changelog.ReleaseError, "mismatch"):
                changelog.current_version(root)
            (root / changelog.PANEL).write_text("no version")
            with self.assertRaises(changelog.ReleaseError):
                changelog.write_version("0.4.3", root)
            self.assertIn('"0.4.2"', (root / changelog.INIT).read_text())
            with self.assertRaises(changelog.ReleaseError):
                changelog.current_version(root)

    def test_mutation_gate_rejects_missing_incomplete_or_excess_results(self):
        for text in ["", "one: survived", "one: not checked", "one: timeout", "broken",
                     "one: killed\none: killed", "one: killed\ntwo: survived\nthree: survived"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                check_mutations.check_results(text, 1)
        result = check_mutations.check_results("one: killed\ntwo: survived\n", 1)
        self.assertEqual(dict(result), {"killed": 1, "survived": 1})

    def test_mutation_gate_rejects_failed_run_failed_results_and_missing_executable(self):
        ok = subprocess.CompletedProcess([], 0)
        cases = [[subprocess.CalledProcessError(1, "mutmut")],
                 [ok, subprocess.CalledProcessError(1, "mutmut results")],
                 [FileNotFoundError("mutmut")],
                 [ok, subprocess.CompletedProcess([], 0, stdout="")]]
        for case in cases:
            with self.subTest(case=case), patch.object(check_mutations.subprocess, "run", side_effect=case), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(check_mutations.main(), 1)
        with patch.object(check_mutations.subprocess, "run", side_effect=[
                ok, subprocess.CompletedProcess([], 0, stdout="one: killed")]), redirect_stdout(io.StringIO()):
            self.assertEqual(check_mutations.main(), 0)


if __name__ == "__main__":
    unittest.main()
