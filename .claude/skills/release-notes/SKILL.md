---
name: release-notes
description: Prepare a uurtjie release - pick the next version, write the CHANGELOG.md section (which becomes the GitHub release notes) from the commits since the last release, bump the version in both files and validate everything. Use when the user asks to cut/prepare a release, bump the version, write release notes or update the changelog.
---

# Release notes and changelog for uurtjie

The `CHANGELOG.md` section for a version **is** the public GitHub release text: merging into `release`
publishes it verbatim (`.github/workflows/release.yml`). Write it for people who use uurtjie, not for
the people who wrote it.

All mechanics go through `scripts/changelog.py` (stdlib, tested). It refuses malformed sections, bad
versions, and anything that looks like a secret or a local home path. **Never hand-edit
the version numbers or the section heading.** Use the commands below.

## 1. Gather

```bash
git fetch --tags origin            # release tags are created on GitHub, not locally
git status --short                 # must be clean; stop and ask if not
python3 scripts/changelog.py version
python3 scripts/changelog.py commits          # JSON: since, version, commits[{sha, subject, body, category_hint, secrets}]
```

- If `since` is `null`, there is no release tag yet: everything is listed. Ask the user what the last
  released commit was and re-run with `--since <sha>`.
- If **any commit has a non-empty `secrets` list**, stop. Tell the user which commit (sha + kind only,
  never the matched text) and don't copy anything from it.
- `category_hint` is only a guess from the subject. Decide from what the change does.
- Read the diff (`git show --stat <sha>`, or the code) when a subject doesn't say what changed for the user.

## 2. Choose the version

Current version is `X.Y.Z` (pre-1.0 while `X` is 0):

| Since the last release | Bump |
|---|---|
| Only fixes, docs, packaging, internal refactors | `patch` |
| New command, setting, widget feature, or behaviour change | `minor` |
| Config or database change that needs the user to act, removed command/setting | `minor` before 1.0, `major` after |

Propose the bump with a one-line reason, and let the user override it. If nothing user-visible changed
(only tests/CI), say so and ask whether a release is wanted at all.

## 3. Write the section body

Write it to a scratch file (not in the repo), e.g. `$TMPDIR/notes-X.Y.Z.md`:

```markdown
### Added
- `uurtjie setup`: a guided first-run wizard for folders, browser windows, rate and timezone.

### Changed
- Days are counted in the configured `timezone` (default: the system's). **Action:** none, unless your
  clients are billed in another timezone; then set `timezone` in `~/.config/uurtjie/config.toml`.

### Fixed
- The bar widget no longer shows `uurtjie?` right after login.
```

Rules (`changelog.py add` enforces the ones marked ✱):
- ✱ Only `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, `### Security`, `### Packaging`,
  in that order, and omit empty ones. ✱ No `#`/`##` headings. ✱ At least one bullet.
- One bullet per user-visible change. Merge commits that belong to one change. Leave out refactors, test-only
  and review-fixup commits unless they change behaviour.
- Say what the user can now do or will notice, in plain words, present tense. Name commands, settings and keys
  in backticks exactly as typed (`uurtjie config set rate 650`, `show_money_in_bar`).
- Anything the user must do after upgrading goes in the bullet as **Action:**.
- Security fixes go under `### Security`: state the impact and who is affected, without a working exploit.
- `### Packaging`: installer, release, CI and dependency changes that matter to someone installing.
- ✱ No secrets, tokens or absolute home paths (`/home/<name>/…`); use `~/…`. Also no client or company
  names, people's names, emails, hostnames or window titles from real work. Use `acme` if an example is needed.
- No commit shas, PR numbers or internal jargon. Keep it short: a typical release is 3–10 bullets.

## 4. Apply and validate

```bash
python3 scripts/changelog.py bump <patch|minor|major|X.Y.Z>     # writes uurtjie/__init__.py + manifest.json
python3 scripts/changelog.py add <new> --notes-file "$TMPDIR/notes-<new>.md"   # dated today; --date YYYY-MM-DD to override
python3 scripts/changelog.py check            # versions agree, newest section first, well formed, no secrets
python3 scripts/changelog.py notes <new>      # exactly what GitHub will show
scripts/build-release.sh --check
python3 -m unittest discover -s tests && python3 -m unittest discover -s scripts/tests
```

If `add` refuses the notes, fix the notes file and run `add` again. Don't work around the check.
`bump` can be undone with `git checkout uurtjie/__init__.py omarchy-plugin/jd.uurtjie/manifest.json`.

## 5. Hand over

Show the user the `notes` output and `git diff --stat`, then stop. Commit (`Release X.Y.Z`), push and open
the `main` → `release` PR only when the user asks. Merging that PR publishes the release. Merging the same
version twice fails on purpose.
