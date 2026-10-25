#!/usr/bin/env python3
"""Release bookkeeping: version numbers, CHANGELOG.md sections and the commits that feed them.

Used by scripts/build-release.sh, scripts/release-notes.sh, CI and the `release-notes` Claude skill
(.claude/skills/release-notes/SKILL.md). Standard library only, like the app.

    changelog.py version                         current version (fails if app/widget versions disagree)
    changelog.py check [--version X.Y.Z]         CHANGELOG.md is well formed and has the current version
    changelog.py notes X.Y.Z                     one section's body (the GitHub release notes)
    changelog.py commits [--since vX.Y.Z]        commits since the last release tag, as JSON
    changelog.py bump major|minor|patch|X.Y.Z    write the app, manifest and panel versions
    changelog.py add X.Y.Z --notes-file F [--date YYYY-MM-DD]   insert a section at the top

Versions are strict X.Y.Z: they end up in git tags, file names and shell commands in the release
workflow, so nothing else is accepted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = "CHANGELOG.md"
INIT = "uurtjie/__init__.py"
MANIFEST = "omarchy-plugin/jd.uurtjie/manifest.json"
PANEL = "omarchy-plugin/jd.uurtjie/Panel.qml"

# ASCII digits only (\d also matches e.g. Arabic-Indic digits), used with fullmatch ($ allows a trailing \n).
VERSION_RE = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})")
HEADING_RE = re.compile(r"^## \[(?P<version>[^\]]*)\](?: - (?P<date>\S+))?\s*$")
INIT_RE = re.compile(r'^__version__ = "([^"]*)"$', re.M)
PANEL_RE = re.compile(r'(readonly property string widgetVersion:\s*)"([^"]*)"')
CATEGORIES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security", "Packaging")

# Things that must never reach public release notes.
SECRET_PATTERNS = [
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("password assignment", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key)\s*[:=]\s*\S{6,}")),
    ("absolute home path", re.compile(r"(?<![\w~])/(home|Users)/[A-Za-z0-9._-]+")),
]
TRAILER_RE = re.compile(r"^(Co-Authored-By|Signed-off-by|Reviewed-by|Change-Id):", re.I)


class ReleaseError(Exception):
    pass


# --------------------------------------------------------------------------- versions

def parse_version(s: str) -> tuple[int, int, int]:
    m = VERSION_RE.fullmatch(s) if isinstance(s, str) else None
    if not m:
        raise ReleaseError(f"invalid version {s!r}: expected X.Y.Z (digits only, no leading zeros)")
    return int(m[1]), int(m[2]), int(m[3])


def bump(current: str, part: str) -> str:
    """'0.4.1' + 'minor' -> '0.5.0'. An explicit X.Y.Z must be greater than the current version."""
    major, minor, patch = parse_version(current)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if parse_version(part) <= (major, minor, patch):
        raise ReleaseError(f"new version {part} must be greater than {current}")
    return part


def read_versions(root: Path = ROOT) -> tuple[str, str]:
    m = INIT_RE.search((root / INIT).read_text())
    if not m:
        raise ReleaseError(f"no __version__ line in {INIT}")
    return m[1], str(json.loads((root / MANIFEST).read_text()).get("version", ""))


def current_version(root: Path = ROOT) -> str:
    init_v, manifest_v = read_versions(root)
    if init_v != manifest_v:
        raise ReleaseError(f"version mismatch: {INIT} says {init_v}, {MANIFEST} says {manifest_v}")
    parse_version(init_v)
    panel = PANEL_RE.search((root / PANEL).read_text())
    if not panel or panel[2] != init_v:
        raise ReleaseError(f"version mismatch: {PANEL} must agree with {INIT}")
    return init_v


def write_version(new: str, root: Path = ROOT) -> None:
    parse_version(new)
    init = root / INIT
    text, n = INIT_RE.subn(f'__version__ = "{new}"', init.read_text())
    if n != 1:
        raise ReleaseError(f"expected exactly one __version__ line in {INIT}")
    manifest = root / MANIFEST
    mtext = manifest.read_text()
    data = json.loads(mtext)
    data["version"] = new
    # Keep the manifest's layout: replace the one value rather than re-dumping the file.
    mnew, n = re.subn(r'("version"\s*:\s*)"[^"]*"', rf'\g<1>"{new}"', mtext, count=1)
    if n != 1 or json.loads(mnew) != data:
        raise ReleaseError(f"couldn't update the version in {MANIFEST}")
    panel = root / PANEL
    pnew, count = PANEL_RE.subn(lambda m: f'{m[1]}"{new}"', panel.read_text())
    if count != 1:
        raise ReleaseError(f"expected exactly one widgetVersion in {PANEL}")
    atomic_write(init, text)
    atomic_write(manifest, mnew)
    atomic_write(panel, pnew)


# --------------------------------------------------------------------------- changelog

@dataclass
class Section:
    version: str
    date: str | None
    body: str
    line: int            # 1-based line of the heading


def parse_changelog(text: str) -> tuple[str, list[Section]]:
    """-> (preamble, sections in file order)."""
    lines = text.splitlines()
    preamble: list[str] = []
    sections: list[Section] = []
    cur: Section | None = None
    buf: list[str] = []
    for i, line in enumerate(lines, 1):
        m = HEADING_RE.match(line) if line.startswith("## [") else None
        if line.startswith("## [") and not m:
            raise ReleaseError(f"{CHANGELOG}:{i}: malformed heading {line!r}, expected '## [X.Y.Z] - YYYY-MM-DD'")
        if m:
            if cur:
                cur.body = "\n".join(buf).strip("\n")
                sections.append(cur)
            cur, buf = Section(m["version"], m["date"], "", i), []
        elif cur:
            buf.append(line)
        else:
            preamble.append(line)
    if cur:
        cur.body = "\n".join(buf).strip("\n")
        sections.append(cur)
    return "\n".join(preamble), sections


def parse_date(s: str) -> date:
    """Strict YYYY-MM-DD (date.fromisoformat alone also takes '20261101' and week dates)."""
    try:
        if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", s):
            return date.fromisoformat(s)
    except ValueError:
        pass
    raise ReleaseError(f"invalid date {s!r}: expected YYYY-MM-DD")


def find_secrets(text: str) -> list[str]:
    return [name for name, rx in SECRET_PATTERNS if rx.search(text)]


def validate_body(body: str, where: str = "notes") -> list[str]:
    errors = []
    if not body.strip():
        errors.append(f"{where}: empty")
    for line in body.splitlines():
        if line.startswith("## "):
            errors.append(f"{where}: level-2 heading {line!r} would split the section; use '### Category'")
        elif line.startswith("### ") and line[4:].strip() not in CATEGORIES:
            errors.append(f"{where}: unknown category {line!r}; use one of {', '.join(CATEGORIES)}")
        elif line.startswith("# "):
            errors.append(f"{where}: level-1 heading {line!r} not allowed in a section")
    if body.strip() and not any(l.lstrip().startswith(("- ", "* ")) for l in body.splitlines()):
        errors.append(f"{where}: no bullet points")
    for name in find_secrets(body):
        errors.append(f"{where}: looks like it contains a secret ({name}); remove it before publishing")
    return errors


def check(text: str, version: str | None = None) -> list[str]:
    """All problems with CHANGELOG.md, empty if it is fine."""
    try:
        _, sections = parse_changelog(text)
    except ReleaseError as e:
        return [str(e)]
    errors: list[str] = []
    seen: set[str] = set()
    prev: tuple[int, int, int] | None = None
    for s in sections:
        where = f"{CHANGELOG}:{s.line} [{s.version}]"
        try:
            v = parse_version(s.version)
        except ReleaseError as e:
            errors.append(f"{where}: {e}")
            continue
        if s.version in seen:
            errors.append(f"{where}: duplicate section")
        seen.add(s.version)
        if prev is not None and v >= prev:
            errors.append(f"{where}: sections must be newest first")
        prev = v
        if s.date is None:
            errors.append(f"{where}: missing date ('## [{s.version}] - YYYY-MM-DD')")
        else:
            try:
                parse_date(s.date)
            except ReleaseError as e:
                errors.append(f"{where}: {e}")
        errors += validate_body(s.body, where)
    if version is not None and version not in seen:
        errors.append(f"{CHANGELOG}: no section for [{version}]; add '## [{version}] - YYYY-MM-DD'")
    if version is not None and sections and sections[0].version != version:
        errors.append(f"{CHANGELOG}: the newest section is [{sections[0].version}], expected [{version}] first")
    return errors


def notes(text: str, version: str) -> str:
    parse_version(version)
    _, sections = parse_changelog(text)
    for s in sections:
        if s.version == version:
            if not s.body.strip():
                break
            return s.body + "\n"
    raise ReleaseError(f"no {CHANGELOG} section for [{version}]")


def add_section(text: str, version: str, body: str, on: str) -> str:
    """Insert '## [version] - on' + body above the newest section (or at the end of the preamble)."""
    parse_version(version)
    parse_date(on)
    if errors := validate_body(body):
        raise ReleaseError("; ".join(errors))
    _, sections = parse_changelog(text)
    if any(s.version == version for s in sections):
        raise ReleaseError(f"{CHANGELOG} already has a section for [{version}]")
    if sections and parse_version(version) < parse_version(sections[0].version):
        raise ReleaseError(f"[{version}] must be newer than the newest section [{sections[0].version}]")
    new = f"## [{version}] - {on}\n\n{body.strip()}\n"
    lines = text.splitlines(keepends=True)
    if sections:
        at = sections[0].line - 1
        out = "".join(lines[:at]) + new + "\n" + "".join(lines[at:])
    else:
        out = text.rstrip("\n") + "\n\n" + new
    return out


# --------------------------------------------------------------------------- git

def git(*args: str, root: Path = ROOT) -> str:
    r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def last_tag(root: Path = ROOT) -> str | None:
    try:
        tag = git("describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", root=root).strip()
    except ReleaseError:
        return None
    return tag or None


def category_hint(subject: str) -> str:
    s = subject.lower()
    if re.match(r"^(fix|bug|hotfix)\b|^fix(es|ed)?\b", s):
        return "Fixed"
    if re.match(r"^(sec|security)\b", s) or "vulnerab" in s or "cve-" in s:
        return "Security"
    if re.match(r"^(feat|add|adds|added|new|introduce)\b", s):
        return "Added"
    if re.match(r"^(remove|removes|removed|drop|delete)\b", s):
        return "Removed"
    if re.match(r"^(deprecate)", s):
        return "Deprecated"
    if re.match(r"^(ci|build|release|chore\(release\)|package|packaging)\b", s):
        return "Packaging"
    return "Changed"


def commits(since: str | None = None, root: Path = ROOT) -> list[dict]:
    """Non-merge commits after `since` (default: the last v* tag), oldest first, trailers stripped."""
    # Strict ref syntax: no option injection (`--output=…`) or revision expressions reach git.
    if since is not None and not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{7,40}", since):
        raise ReleaseError(f"invalid --since {since!r}: a vX.Y.Z tag or a commit sha")
    since = since or last_tag(root)
    rng = [f"{since}..HEAD"] if since else ["HEAD"]
    out = git("log", "--no-merges", "--reverse", "--format=%H%x1f%s%x1f%b%x1e", *rng, "--", root=root)
    result = []
    for rec in filter(str.strip, out.split("\x1e")):          # one record per commit, the last one is empty
        sha, subject, body = rec.lstrip("\n").split("\x1f", 2)
        subject = subject.strip()
        body = "\n".join(l for l in body.strip().splitlines() if not TRAILER_RE.match(l)).strip()
        result.append({"sha": sha[:12], "subject": subject, "body": body,
                       "category_hint": category_hint(subject),
                       "secrets": find_secrets(subject + "\n" + body)})
    return result


# --------------------------------------------------------------------------- io + cli

def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    ap = argparse.ArgumentParser(prog="changelog.py", description=__doc__.split("\n\n")[0])  # pragma: no mutate (help text)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version")
    c = sub.add_parser("check")
    c.add_argument("--version", help="require this version's section (default: current version)")  # pragma: no mutate (help text)
    c = sub.add_parser("notes")
    c.add_argument("version")
    c = sub.add_parser("commits")
    c.add_argument("--since")
    c = sub.add_parser("bump")
    c.add_argument("part", help="major, minor, patch or an explicit X.Y.Z")  # pragma: no mutate (help text)
    c = sub.add_parser("add")
    c.add_argument("version")
    notes_help = "markdown body: '### Added' etc. with bullets ('-' = stdin)"  # pragma: no mutate (help text)
    c.add_argument("--notes-file", required=True, help=notes_help)
    c.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")  # pragma: no mutate (help text)
    args = ap.parse_args(argv)
    path = root / CHANGELOG
    try:
        if args.cmd == "version":
            print(current_version(root))
        elif args.cmd == "check":
            v = args.version or current_version(root)
            parse_version(v)
            if errors := check(path.read_text(), v):
                print("\n".join(errors), file=sys.stderr)
                return 1
            print(f"ok: {CHANGELOG} has [{v}]")
        elif args.cmd == "notes":
            sys.stdout.write(notes(path.read_text(), args.version))
        elif args.cmd == "commits":
            print(json.dumps({"since": args.since or last_tag(root), "version": current_version(root),
                              "commits": commits(args.since, root)}, indent=2))
        elif args.cmd == "bump":
            new = bump(current_version(root), args.part)
            write_version(new, root)
            print(new)
        elif args.cmd == "add":
            body = sys.stdin.read() if args.notes_file == "-" else Path(args.notes_file).read_text()
            on = args.date or date.today().isoformat()
            atomic_write(path, add_section(path.read_text(), args.version, body, on))
            print(f"added [{args.version}] - {on} to {CHANGELOG}")
    except (ReleaseError, ValueError, OSError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
