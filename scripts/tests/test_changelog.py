"""Unit tests for scripts/changelog.py (release notes, changelog sections, version bumps)."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))       # repo root: import scripts.changelog
from scripts import changelog as cl  # noqa: E402

GOOD = """# Changelog

Intro text.

## [0.4.1] - 2026-10-25

### Added
- Export as CSV.

### Fixed
- Off-by-one in the hourly chart.

## [0.3.0] - 2026-10-21

### Changed
- Renamed from tally.
"""

BODY = "### Added\n- Setup wizard.\n"


def make_repo(tmp: Path, version: str = "0.4.1", changelog: str = GOOD) -> Path:
    (tmp / "uurtjie").mkdir()
    (tmp / "uurtjie/__init__.py").write_text(f'"""uurtjie."""\n__version__ = "{version}"\n')
    (tmp / "omarchy-plugin/jd.uurtjie").mkdir(parents=True)
    (tmp / cl.MANIFEST).write_text(json.dumps({"id": "jd.uurtjie", "version": version}, indent=2) + "\n")
    (tmp / cl.CHANGELOG).write_text(changelog)
    return tmp


def run(root: Path, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cl.main(list(argv), root=root)
    return code, out.getvalue(), err.getvalue()


class VersionTests(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(cl.parse_version("0.4.1"), (0, 4, 1))
        self.assertEqual(cl.parse_version("10.0.12"), (10, 0, 12))

    def test_parse_rejects(self):
        for bad in ["", "0.4", "0.4.1.2", "v0.4.1", "01.2.3", "0.4.1-rc1", " 0.4.1", "0.4.1\n", "a.b.c", None]:
            with self.subTest(bad=bad), self.assertRaises(cl.ReleaseError):
                cl.parse_version(bad)

    def test_bump_parts(self):
        self.assertEqual(cl.bump("0.4.1", "patch"), "0.4.2")
        self.assertEqual(cl.bump("0.4.1", "minor"), "0.5.0")
        self.assertEqual(cl.bump("0.4.1", "major"), "1.0.0")
        self.assertEqual(cl.bump("0.4.1", "0.10.0"), "0.10.0")

    def test_bump_explicit_must_increase(self):
        for v in ("0.4.1", "0.4.0", "0.3.9"):
            with self.subTest(v=v), self.assertRaises(cl.ReleaseError):
                cl.bump("0.4.1", v)

    def test_bump_unknown_part(self):
        with self.assertRaises(cl.ReleaseError):
            cl.bump("0.4.1", "huge")

    def test_versions_must_agree(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_repo(Path(d))
            self.assertEqual(cl.current_version(root), "0.4.1")
            (root / cl.MANIFEST).write_text('{"version": "0.4.2"}')
            with self.assertRaisesRegex(cl.ReleaseError, "mismatch"):
                cl.current_version(root)

    def test_agreeing_but_invalid_version(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_repo(Path(d), version="0.4")
            with self.assertRaisesRegex(cl.ReleaseError, "invalid version"):
                cl.current_version(root)

    def test_missing_init_version(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_repo(Path(d))
            (root / cl.INIT).write_text("x = 1\n")
            with self.assertRaisesRegex(cl.ReleaseError, "__version__"):
                cl.read_versions(root)

    def test_write_version_keeps_layout(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_repo(Path(d))
            before = (root / cl.MANIFEST).read_text()
            cl.write_version("0.5.0", root)
            self.assertEqual(cl.current_version(root), "0.5.0")
            self.assertEqual((root / cl.MANIFEST).read_text(), before.replace("0.4.1", "0.5.0"))
            self.assertIn('"""uurtjie."""', (root / cl.INIT).read_text())

    def test_write_version_two_init_lines(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_repo(Path(d))
            (root / cl.INIT).write_text('__version__ = "0.4.1"\n__version__ = "0.4.1"\n')
            with self.assertRaises(cl.ReleaseError):
                cl.write_version("0.5.0", root)
            self.assertEqual(json.loads((root / cl.MANIFEST).read_text())["version"], "0.4.1")  # nothing half-written

    def test_write_version_no_manifest_version(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_repo(Path(d))
            (root / cl.MANIFEST).write_text('{"id": "x"}')
            with self.assertRaises(cl.ReleaseError):
                cl.write_version("0.5.0", root)
            self.assertIn('"0.4.1"', (root / cl.INIT).read_text())


class ParseTests(unittest.TestCase):
    def test_sections(self):
        pre, secs = cl.parse_changelog(GOOD)
        self.assertIn("Intro text.", pre)
        self.assertEqual([(s.version, s.date) for s in secs], [("0.4.1", "2026-10-25"), ("0.3.0", "2026-10-21")])
        self.assertEqual(secs[0].line, 5)
        self.assertTrue(secs[0].body.startswith("### Added"))
        self.assertTrue(secs[0].body.endswith("hourly chart."))
        self.assertEqual(secs[1].body, "### Changed\n- Renamed from tally.")

    def test_no_sections(self):
        self.assertEqual(cl.parse_changelog("# Changelog\n")[1], [])

    def test_heading_without_date(self):
        _, secs = cl.parse_changelog("## [1.0.0]\n- x\n")
        self.assertIsNone(secs[0].date)

    def test_malformed_heading(self):
        with self.assertRaisesRegex(cl.ReleaseError, ":3: malformed"):
            cl.parse_changelog("# C\n\n## [1.0.0 - 2026-01-01\n")

    def test_level3_heading_is_body(self):
        _, secs = cl.parse_changelog("## [1.0.0] - 2026-01-01\n### [not a version]\n- x\n")
        self.assertEqual(len(secs), 1)


class NotesTests(unittest.TestCase):
    def test_notes(self):
        self.assertEqual(cl.notes(GOOD, "0.3.0"), "### Changed\n- Renamed from tally.\n")
        self.assertTrue(cl.notes(GOOD, "0.4.1").startswith("### Added\n- Export as CSV.\n\n### Fixed"))

    def test_missing(self):
        with self.assertRaisesRegex(cl.ReleaseError, r"\[9\.9\.9\]"):
            cl.notes(GOOD, "9.9.9")

    def test_empty_section_counts_as_missing(self):
        with self.assertRaises(cl.ReleaseError):
            cl.notes("## [1.0.0] - 2026-01-01\n\n", "1.0.0")

    def test_prefix_does_not_match(self):
        with self.assertRaises(cl.ReleaseError):
            cl.notes(GOOD, "0.4.10")
        with self.assertRaises(cl.ReleaseError):
            cl.notes(GOOD.replace("[0.4.1]", "[10.4.1]"), "0.4.1")


class CheckTests(unittest.TestCase):
    def test_good(self):
        self.assertEqual(cl.check(GOOD), [])
        self.assertEqual(cl.check(GOOD, "0.4.1"), [])

    def test_version_missing(self):
        errs = cl.check(GOOD, "0.5.0")
        self.assertTrue(any("no section for [0.5.0]" in e for e in errs))

    def test_version_not_newest(self):
        errs = cl.check(GOOD, "0.3.0")
        self.assertEqual(len(errs), 1)
        self.assertIn("newest section is [0.4.1]", errs[0])

    def test_order(self):
        text = "## [0.3.0] - 2026-10-21\n- a\n\n## [0.4.1] - 2026-10-25\n- b\n"
        self.assertTrue(any("newest first" in e for e in cl.check(text)))

    def test_equal_versions_are_out_of_order_and_duplicate(self):
        text = "## [0.3.0] - 2026-10-21\n- a\n\n## [0.3.0] - 2026-10-21\n- b\n"
        errs = cl.check(text)
        self.assertTrue(any("duplicate" in e for e in errs))
        self.assertTrue(any("newest first" in e for e in errs))

    def test_dates(self):
        for d, ok in [("2026-10-25", True), ("2026-13-01", False), ("2026-1-5", False), ("20261025", False)]:
            with self.subTest(d=d):
                errs = cl.check(f"## [1.0.0] - {d}\n- x\n")
                self.assertEqual(errs == [], ok, errs)
        self.assertTrue(any("missing date" in e for e in cl.check("## [1.0.0]\n- x\n")))

    def test_bad_version_in_heading(self):
        errs = cl.check("## [Unreleased] - 2026-01-01\n- x\n")
        self.assertTrue(any("invalid version" in e for e in errs))

    def test_malformed_reported_not_raised(self):
        self.assertTrue(cl.check("## [1.0.0 oops\n")[0].endswith("'## [X.Y.Z] - YYYY-MM-DD'"))

    def test_error_carries_line(self):
        errs = cl.check(GOOD.replace("2026-10-21", "2026-02-30"))
        self.assertTrue(any(e.startswith("CHANGELOG.md:13 [0.3.0]") for e in errs), errs)


class BodyTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(cl.validate_body(BODY), [])
        self.assertEqual(cl.validate_body("* plain bullet\n"), [])
        self.assertEqual(cl.validate_body("### Security\n  - nested bullet\n"), [])

    def test_categories(self):
        for c in cl.CATEGORIES:
            self.assertEqual(cl.validate_body(f"### {c}\n- x\n"), [], c)
        self.assertTrue(cl.validate_body("### Misc\n- x\n")[0].startswith("notes: unknown category"))

    def test_problems(self):
        cases = {"": "empty", "   \n": "empty", "## [9.9.9]\n- x\n": "level-2", "# Title\n- x\n": "level-1",
                 "Just prose.\n": "no bullet"}
        for body, msg in cases.items():
            with self.subTest(body=body):
                self.assertTrue(any(msg in e for e in cl.validate_body(body)), cl.validate_body(body))

    def test_where_prefix(self):
        self.assertEqual(cl.validate_body("", "X"), ["X: empty"])


class AddTests(unittest.TestCase):
    def test_inserts_above_newest(self):
        out = cl.add_section(GOOD, "0.5.0", BODY, "2026-11-01")
        _, secs = cl.parse_changelog(out)
        self.assertEqual([s.version for s in secs], ["0.5.0", "0.4.1", "0.3.0"])
        self.assertIn("Intro text.\n\n## [0.5.0] - 2026-11-01\n\n### Added\n- Setup wizard.\n\n## [0.4.1]", out)
        self.assertEqual(cl.notes(out, "0.4.1"), cl.notes(GOOD, "0.4.1"))      # existing sections untouched

    def test_first_section(self):
        out = cl.add_section("# Changelog\n", "0.1.0", BODY, "2026-11-01")
        self.assertEqual(out, "# Changelog\n\n## [0.1.0] - 2026-11-01\n\n### Added\n- Setup wizard.\n")

    def test_refusals(self):
        cases = [("0.4.1", BODY, "2026-11-01", "already has"), ("0.4.0", BODY, "2026-11-01", "newer than"),
                 ("0.5.0", "", "2026-11-01", "empty"), ("0.5", BODY, "2026-11-01", "invalid version")]
        for v, body, on, msg in cases:
            with self.subTest(v=v), self.assertRaisesRegex(cl.ReleaseError, msg):
                cl.add_section(GOOD, v, body, on)
        with self.assertRaisesRegex(cl.ReleaseError, "invalid date"):
            cl.add_section(GOOD, "0.5.0", BODY, "next tuesday")

    def test_strips_body(self):
        out = cl.add_section(GOOD, "0.5.0", "\n\n" + BODY + "\n\n\n", "2026-11-01")
        self.assertEqual(cl.notes(out, "0.5.0"), BODY)


class CategoryHintTests(unittest.TestCase):
    def test_hints(self):
        cases = {"Fix crash on empty config": "Fixed", "fix: idle detection": "Fixed", "Hotfix widget": "Fixed",
                 "Fixes #12": "Fixed", "Bug in rounding": "Fixed",
                 "Add setup wizard": "Added", "feat: timezone": "Added", "New panel": "Added",
                 "Introduce clock module": "Added", "Security: escape titles": "Security",
                 "Patch CVE-2026-1 in invoice": "Security", "Close vulnerability in export": "Security",
                 "Remove tally migration": "Removed", "Drop py310": "Removed",
                 "Deprecate idle_timeout": "Deprecated", "ci: cache pip": "Packaging", "build: tarball": "Packaging",
                 "Release 0.5.0": "Packaging", "Refactor sessions": "Changed", "Address review": "Changed",
                 "Prefix fix later": "Changed", "Additional docs": "Changed"}
        for subject, cat in cases.items():
            with self.subTest(subject=subject):
                self.assertEqual(cl.category_hint(subject), cat)


class GitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_repo(Path(self.tmp.name))
        env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull}
        self.git = lambda *a: subprocess.run(["git", *a], cwd=self.root, env=env, check=True, capture_output=True)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Initial import")
        self.git("tag", "v0.4.1")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, msg: str):
        self.git("commit", "-q", "--allow-empty", "-m", msg)

    def test_since_last_tag(self):
        self.commit("Add setup wizard\n\nAsks for folders.\n\nCo-Authored-By: Claude <noreply@anthropic.com>")
        self.commit("Fix idle cut-off")
        cs = cl.commits(root=self.root)
        self.assertEqual([c["subject"] for c in cs], ["Add setup wizard", "Fix idle cut-off"])
        self.assertEqual(cs[0]["body"], "Asks for folders.")                     # trailer stripped
        self.assertEqual(cs[1]["body"], "")
        self.assertEqual([c["category_hint"] for c in cs], ["Added", "Fixed"])
        self.assertEqual(len(cs[0]["sha"]), 12)
        self.assertEqual(cs[0]["secrets"], [])
        self.assertEqual(cl.last_tag(self.root), "v0.4.1")

    def test_no_tag_lists_everything(self):
        self.git("tag", "-d", "v0.4.1")
        self.assertIsNone(cl.last_tag(self.root))
        self.assertEqual([c["subject"] for c in cl.commits(root=self.root)], ["Initial import"])

    def test_explicit_since(self):
        self.commit("Later")
        self.assertEqual([c["subject"] for c in cl.commits("v0.4.1", self.root)], ["Later"])

    def test_merges_skipped(self):
        self.git("checkout", "-q", "-b", "side")
        self.commit("Side work")
        self.git("checkout", "-q", "main")
        self.commit("Main work")
        self.git("merge", "-q", "--no-ff", "-m", "Merge side", "side")
        self.assertNotIn("Merge side", [c["subject"] for c in cl.commits(root=self.root)])

    def test_unknown_ref(self):
        with self.assertRaises(cl.ReleaseError):
            cl.commits("v9.9.9", self.root)

    def test_cli_commits(self):
        self.commit("Add thing")
        code, out, _ = run(self.root, "commits")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith('{\n  "since": "v0.4.1",\n  "version": "0.4.1",\n  "commits": [\n    {\n'))
        data = json.loads(out)
        self.assertEqual((data["since"], data["version"]), ("v0.4.1", "0.4.1"))
        self.assertEqual(data["commits"][0]["subject"], "Add thing")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_repo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_and_check(self):
        self.assertEqual(run(self.root, "version"), (0, "0.4.1\n", ""))
        self.assertEqual(run(self.root, "check")[:2], (0, "ok: CHANGELOG.md has [0.4.1]\n"))

    def test_check_explicit_version(self):
        self.assertEqual(run(self.root, "check", "--version", "0.4.1")[0], 0)
        self.assertEqual(run(self.root, "check", "--version", "0.3.0")[0], 1)

    def test_check_fails_after_bump_without_notes(self):
        self.assertEqual(run(self.root, "bump", "minor")[:2], (0, "0.5.0\n"))
        code, _, err = run(self.root, "check")
        self.assertEqual(code, 1)
        self.assertIn("no section for [0.5.0]", err)

    def test_full_release_flow(self):
        run(self.root, "bump", "patch")
        notes = self.root / "notes.md"
        notes.write_text(BODY)
        code, out, err = run(self.root, "add", "0.4.2", "--notes-file", str(notes), "--date", "2026-11-02")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "added [0.4.2] - 2026-11-02 to CHANGELOG.md\n")
        self.assertEqual(run(self.root, "check")[0], 0)
        self.assertEqual(run(self.root, "notes", "0.4.2")[1], BODY)

    def test_add_default_date_is_today(self):
        notes = self.root / "n.md"
        notes.write_text(BODY)
        self.assertIn(date.today().isoformat(), run(self.root, "add", "0.5.0", "--notes-file", str(notes))[1])

    def test_add_from_stdin(self):
        old = sys.stdin
        sys.stdin = io.StringIO(BODY)
        try:
            self.assertEqual(run(self.root, "add", "0.5.0", "--notes-file", "-", "--date", "2026-11-01")[0], 0)
        finally:
            sys.stdin = old
        self.assertEqual(cl.notes((self.root / cl.CHANGELOG).read_text(), "0.5.0"), BODY)

    def test_errors_exit_1_without_traceback(self):
        for argv in (["notes", "9.9.9"], ["bump", "0.1.0"], ["check", "--version", "x"],
                     ["add", "0.5.0", "--notes-file", "/nonexistent/notes.md"]):
            with self.subTest(argv=argv):
                code, out, err = run(self.root, *argv)
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("error: "), err)

    def test_failed_add_leaves_file_untouched(self):
        before = (self.root / cl.CHANGELOG).read_text()
        notes = self.root / "n.md"
        notes.write_text("## [0.5.0]\n- sneaky\n")
        self.assertEqual(run(self.root, "add", "0.5.0", "--notes-file", str(notes))[0], 1)
        self.assertEqual((self.root / cl.CHANGELOG).read_text(), before)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".")], [])   # no temp file left

    def test_atomic_write_keeps_mode(self):
        p = self.root / cl.CHANGELOG
        p.chmod(0o600)
        cl.atomic_write(p, "x")
        self.assertEqual((p.read_text(), p.stat().st_mode & 0o777), ("x", 0o600))
        q = self.root / "new.md"
        cl.atomic_write(q, "y")
        self.assertEqual(q.stat().st_mode & 0o777, 0o644)

    def test_atomic_write_cleans_up_on_failure(self):
        p = self.root / cl.CHANGELOG
        with self.assertRaises(TypeError):
            cl.atomic_write(p, 123)  # type: ignore[arg-type]
        self.assertEqual([x for x in self.root.iterdir() if x.name.startswith(".CHANGELOG")], [])
        self.assertEqual(p.read_text(), GOOD)


class ShellWrapperTests(unittest.TestCase):
    """The release workflow calls these scripts; they must agree with the module."""
    SCRIPTS = Path(__file__).resolve().parent.parent
    # The wrapper runs `python3`: make that the interpreter running these tests (a venv, mutmut's runner).
    ENV = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}

    def wrapper(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.SCRIPTS / "release-notes.sh"), *args], capture_output=True, text=True,
                              env=self.ENV)

    def test_release_notes_sh(self):
        repo = self.SCRIPTS.parent
        v = cl.current_version(repo)
        r = self.wrapper(v)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, cl.notes((repo / cl.CHANGELOG).read_text(), v))

    def test_release_notes_sh_missing(self):
        self.assertEqual(self.wrapper("9.9.9").returncode, 1)
        self.assertNotEqual(self.wrapper().returncode, 0)

    def test_repo_changelog_is_valid(self):
        repo = self.SCRIPTS.parent
        self.assertEqual(cl.check((repo / cl.CHANGELOG).read_text(), cl.current_version(repo)), [])


class EdgeCaseTests(unittest.TestCase):
    """Behaviour pinned down by mutation testing (mutmut): messages, exact edits, argument handling."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_repo(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_error_messages(self):
        with self.assertRaisesRegex(cl.ReleaseError, r"^new version 0\.4\.0 must be greater than 0\.4\.1$"):
            cl.bump("0.4.1", "0.4.0")
        (self.root / cl.INIT).write_text('__version__ = "0.4.1"\n__version__ = "0.4.1"\n')
        with self.assertRaisesRegex(cl.ReleaseError, r"^expected exactly one __version__ line in uurtjie/__init__\.py$"):
            cl.write_version("0.5.0", self.root)
        with self.assertRaisesRegex(cl.ReleaseError, r"must be newer than the newest section \[0\.4\.1\]$"):
            cl.add_section(GOOD, "0.3.5", BODY, "2026-11-01")
        self.assertIn("use one of Added, Changed, Deprecated, Removed, Fixed, Security, Packaging",
                      cl.validate_body("### Misc\n- x\n")[0])

    def test_add_joins_all_body_errors(self):
        with self.assertRaises(cl.ReleaseError) as e:
            cl.add_section(GOOD, "0.5.0", "## Oops\nprose\n", "2026-11-01")
        self.assertIn("would split the section; use '### Category'; notes: no bullet points", str(e.exception))

    def test_manifest_only_top_level_version_changes(self):
        m = self.root / cl.MANIFEST
        m.write_text('{\n  "version": "0.4.1",\n  "deps": {"version": "9.9.9"}\n}\n')
        cl.write_version("0.5.0", self.root)
        self.assertEqual(json.loads(m.read_text()), {"version": "0.5.0", "deps": {"version": "9.9.9"}})

    def test_manifest_nested_version_first_is_refused(self):
        m = self.root / cl.MANIFEST
        m.write_text('{\n  "deps": {"version": "0.4.1"},\n  "version": "0.4.1"\n}\n')
        before = m.read_text()
        with self.assertRaisesRegex(cl.ReleaseError, r"^couldn't update the version in omarchy-plugin/"):
            cl.write_version("0.5.0", self.root)
        self.assertEqual(m.read_text(), before)
        self.assertIn('"0.4.1"', (self.root / cl.INIT).read_text())

    def test_first_section_keeps_preamble_whitespace(self):
        out = cl.add_section("# Changelog\n\nIntro by X  \n\n\n", "0.1.0", BODY, "2026-11-01")
        self.assertEqual(out, "# Changelog\n\nIntro by X  \n\n## [0.1.0] - 2026-11-01\n\n### Added\n- Setup wizard.\n")
        out = cl.add_section("# Changelog by X", "0.1.0", BODY, "2026-11-01")
        self.assertTrue(out.startswith("# Changelog by X\n\n## [0.1.0]"))

    def test_check_continues_after_bad_heading(self):
        errs = cl.check("## [Unreleased] - 2026-01-01\n- x\n\n## [1.0.0] - 2026-02-30\n- y\n")
        self.assertEqual(len(errs), 2, errs)
        self.assertIn("invalid date '2026-02-30'", errs[1])

    def test_dates_are_strict(self):
        self.assertEqual(cl.parse_date("2026-11-01"), date(2026, 11, 1))
        for bad in ("20261101", "2026-W44-1", "2026-02-30", "2026-11-01T00:00", "", "٢٠٢٦-١١-٠١"):
            with self.subTest(bad=bad), self.assertRaisesRegex(cl.ReleaseError, "expected YYYY-MM-DD"):
                cl.parse_date(bad)
        with self.assertRaisesRegex(cl.ReleaseError, "invalid date '20261101'"):
            cl.add_section(GOOD, "0.5.0", BODY, "20261101")
        self.assertIn("CHANGELOG.md:1 [1.0.0]: invalid date '20261101'", cl.check("## [1.0.0] - 20261101\n- x\n")[0])

    def test_git_error_names_command(self):
        with self.assertRaisesRegex(cl.ReleaseError, r"^git log --no-such-flag: "):
            cl.git("log", "--no-such-flag", root=self.root)

    def test_temp_file_is_a_hidden_sibling(self):
        made = []
        real = os.fdopen

        def spy(fd, *a, **k):
            made.append(Path(os.readlink(f"/proc/self/fd/{fd}")))
            return real(fd, *a, **k)
        os.fdopen = spy
        try:
            cl.atomic_write(self.root / cl.CHANGELOG, "x")
        finally:
            os.fdopen = real
        self.assertEqual(made[0].parent, self.root.resolve())          # same filesystem: os.replace is atomic
        self.assertTrue(made[0].name.startswith(".CHANGELOG.md."))

    def test_cli_requires_command_and_notes_file(self):
        for argv in ([], ["add", "0.5.0"]):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as e:
                cl.main(argv, root=self.root)
            self.assertEqual(e.exception.code, 2)

    def test_cli_check_prints_one_error_per_line(self):
        (self.root / cl.CHANGELOG).write_text("## [0.4.1]\nprose\n")
        code, _, err = run(self.root, "check")
        self.assertEqual(code, 1)
        self.assertEqual(err.splitlines(), ["CHANGELOG.md:1 [0.4.1]: missing date ('## [0.4.1] - YYYY-MM-DD')",
                                            "CHANGELOG.md:1 [0.4.1]: no bullet points"])


class GitEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_repo(Path(self.tmp.name))
        env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull}
        self.git = lambda *a: subprocess.run(["git", *a], cwd=self.root, env=env, check=True,
                                             capture_output=True, text=True).stdout.strip()
        self.git("init", "-q", "-b", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_file_named_like_a_ref_is_not_ambiguous(self):
        (self.root / "HEAD").write_text("a file called HEAD\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Initial import")
        self.assertEqual([c["subject"] for c in cl.commits(root=self.root)], ["Initial import"])

    def test_cli_since_is_used(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Initial import")
        self.git("tag", "v0.4.1")
        self.git("commit", "-q", "--allow-empty", "-m", "First")
        first = self.git("rev-parse", "HEAD")
        self.git("commit", "-q", "--allow-empty", "-m", "Second")
        subjects = lambda *a: [c["subject"] for c in json.loads(run(self.root, "commits", *a)[1])["commits"]]
        self.assertEqual(subjects(), ["First", "Second"])
        self.assertEqual(subjects("--since", first), ["Second"])
        self.assertEqual(json.loads(run(self.root, "commits", "--since", first)[1])["since"], first)

    def test_subject_and_body_edges_survive_parsing(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "\tLeading tab subject X\n\nBody ending in X\n\nCo-Authored-By: A <a@x>")
        self.git("commit", "-q", "--allow-empty", "-m", "Second, no body")
        cs = cl.commits(root=self.root)
        self.assertEqual([(c["subject"], c["body"]) for c in cs],
                         [("Leading tab subject X", "Body ending in X"), ("Second, no body", "")])
        self.assertTrue(all(len(c["sha"]) == 12 and c["sha"].isalnum() for c in cs))

    def test_secret_split_across_subject_and_body(self):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Rotate password:\n\nhunter2222")
        self.assertEqual(cl.commits(root=self.root)[0]["secrets"], ["password assignment"])


if __name__ == "__main__":
    unittest.main()
