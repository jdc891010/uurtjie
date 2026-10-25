# uurtjie

Client time tracking for Omarchy that counts only the time you're actually at the keyboard,
inside the client's Herdr workspaces, Claude Code / Codex terminals or Chrome profile — plus
manual entries, a bar widget, and a SQLite database you can query for monthly invoices.

*uurtjie* is Afrikaans for "little hour" (this tool was called tally before).

## How it decides what counts

Every 5 s the daemon (`uurtjie daemon`) takes one sample:

1. **Are you active?** `swayidle` (Wayland idle-notify) reports keyboard, mouse and scroll
   activity. After `sleep_minutes` (default 5) with no input you are asleep, the current entry is
   trimmed back to your last input, and nothing accrues until you touch something again.
2. **What's focused?** Hyprland IPC gives the focused window's class, title and pid.
3. **Which project is it?**
   - A window rule matches (the client's Chrome profile) → that project, source `chrome`.
   - A terminal running a `herdr` client → Herdr's socket API (`pane.current`) returns the
     focused workspace and pane, its cwd and the detected agent (claude, codex…). The cwd is
     matched against each project's `paths` (longest prefix wins) → source `terminal`,
     one entry per Herdr workspace.
   - A plain terminal → cwd of its foreground process via `/proc`.
   - Anything else → not tracked.

### Entries vs sessions

The daemon writes fine-grained **entries**: one per Herdr workspace, Chrome window or timer, and
long stretches on the same task are cut every `chunk_minutes` (15) so nothing is lost if anything
crashes. Pauses shorter than `sleep_minutes` (5) never stop tracking; after 5 minutes without
input it stops at your last input.

Reports never bill raw entries. They're collapsed into **sessions** per project:

- overlapping entries (parallel Herdr workspaces, a timer during terminal work) merge
- gaps up to `bridge_minutes` (default = `sleep_minutes`) count as work, so Herdr → Chrome →
  Herdr is one session; a gap is never bridged if another project was active in it
- human hours = session length, so parallel tasks are never double counted
- each session lists its tasks (workspaces, branches, page titles, notes) and the git commits
  made during it (any branch, your author email), and gets a one-line summary
- set `summary_command` (e.g. `claude -p '…'`) for an LLM-written summary; it's cached per session

```bash
uurtjie sessions                       # today's sessions with summaries and commits
uurtjie sessions --date yesterday --json
uurtjie report -f csv --by session     # one row per session instead of per day
```

```
Hyprland focus ─┐
swayidle input ─┼─► uurtjie ─► ~/.local/share/uurtjie/uurtjie.db ◄─ uurtjie CLI ◄─ bar widget
Herdr socket ───┘     │                                         (add/start/stop/report)
                      └─► $XDG_RUNTIME_DIR/uurtjie/state.json (live "what am I tracking")
```

## Install

Step-by-step guide (also written for coding agents to follow): **[INSTALL.md](INSTALL.md)**.
Short version, from the latest release:

```bash
curl -fsSL https://github.com/jdc891010/uurtjie/releases/latest/download/uurtjie.tar.gz | tar xz
cd uurtjie && ./install.sh
```

From a clone or an extracted tarball:

```bash
./install.sh            # copies the app, installs swayidle if missing, enables uurtjie.service,
                        # and installs + enables the Quattro bar widget when available
$EDITOR ~/.config/uurtjie/config.toml
uurtjie reload
uurtjie status
```

Requires Python 3.11+ (stdlib only, no pip packages) and `swayidle`.

Pre-Quattro Omarchy (Waybar): paste `waybar/uurtjie.jsonc` into your Waybar config instead.

### Herdr

Nothing to install — uurtjie talks to `~/.config/herdr/herdr.sock`. Check what it will see with
`herdr api snapshot` or `herdr pane list`. If you use a named Herdr session, set
`herdr_socket = "~/.config/herdr/sessions/<name>/herdr.sock"`.

To bill only when Claude Code/Codex is the agent in the focused pane, set
`require_agent = true` on the project.

### The client's Chrome profile

Chrome profiles that share one Chrome process all report the same window class, so Hyprland
can't tell them apart. Two options:

- **Distinct class (reliable):** run the client profile from its own data dir with its own
  class — copy `chrome/chrome-client.desktop` to `~/.local/share/applications/`, edit the
  names, and add `class = "chrome-acme"` under the project's `[[projects.windows]]`. This is a
  fresh profile, so you sign in once.
- **Title match (no migration):** keep the existing profile and match a title regex, e.g.
  `title = "acme\\.atlassian\\.net|Acme"`. Works if the client's tools have recognisable
  titles. Check titles with `hyprctl activewindow`.

## Daily use

```bash
uurtjie status                  # today / week / month, what's being tracked now
uurtjie today | uurtjie week
uurtjie log --date yesterday    # raw entries with ids
uurtjie add acme --from 09:00 --to 10:15 --note "standup + planning"
uurtjie add acme -d 45m --date 2026-10-20 --note "call with PM"
uurtjie start acme -n "whiteboarding"   # manual timer…
uurtjie stop                            # …saved as an entry
uurtjie edit 42 --to 17:30 --note "deploy"
uurtjie edit 43 --billable no
uurtjie rm 44
```

### Keyboard shortcuts and quick add

On Omarchy's Lua Hyprland config, `install.sh` drops `~/.config/hypr/uurtjie.lua`; add
`require("hypr.uurtjie")` to `~/.config/hypr/bindings.lua`. On a classic `.conf` setup it drops
`~/.config/hypr/uurtjie-bindings.conf` instead; add `source = ~/.config/hypr/uurtjie-bindings.conf`
to `~/.config/hypr/bindings.conf`. Check SUPER+K for clashes first.

| Keys | Action |
|---|---|
| SUPER+ALT+T | Timer: 1st press starts it, 2nd press opens a stop popup (a timer under a minute is discarded without asking) |
| SUPER+ALT+U | Today/week/month as a notification (S is Omarchy's scratchpad) |
| SUPER+ALT+Z | Undo the last manual entry |

The timer needs no typing. Press 1 starts it for whatever project you're focused on (falls
back to `default_project`) and shows a notification. Press 2 opens a small popup:
**Enter** stops it at the moment you pressed the shortcut, **k** keeps it running, **d** then
**y** discards it. After stopping you can type a description, or press Enter to skip. The bar
widget's right click and the panel's timer button use the same flow. The manual timer runs
regardless of idle, so it covers meetings and calls away from the keyboard.

**Voice descriptions.** At the description step press **v**, speak, press any key to stop;
Omarchy's dictation (Voxtype) types the text into the line so you can fix it before Enter.
Holding SUPER+CTRL+X while the line is open works too. If dictation isn't installed yet:
Omarchy menu › Install › AI › Dictation. Voxtype must be in its default "type" output mode.

Typed one-line entries are still available for back-filling (`uurtjie q y 2h review`, or
uncomment SUPER+ALT+A in the bindings file).

Quick-add lines (also `uurtjie q …` from any terminal), tokens in any order:

```
acme 1h30 call with PM          # 1.5 h ending now
09:00-10:30 standup             # range today, default project
y 2h code review                # yesterday, ends 17:00
fri 45m docs nb                 # last Friday, non-billable
acme-i 30m fix deploy           # unique project prefix
```

Bad input shows the error in the popup and lets you retype; ↑ recalls previous lines.

Widget: left click opens the panel (today per project, week/month, live tracking, timer
button); right click starts/stops the manual timer for `default_project`.

## Earnings on the widget

The bar shows hours today and **money earned so far this month**, e.g. `󰥔 1h05 acme · R1,234`, so you can
watch the total grow. The panel adds the month figure with today's increase, and the tooltip repeats it.
It is calculated exactly like an invoice (per day: session hours × `human_factor`, rounded up to
`rounding_minutes`, × `rate`), so it matches what you would bill, and it needs a `rate` on the project.
Hide it from the bar with `uurtjie config set show_money_in_bar false` (panel and tooltip keep it).

## Changing settings

No need to hand-edit the TOML (your comments and layout are kept):

```bash
uurtjie config show                                   # effective settings + what invoicing is still missing
uurtjie config set rate 650                           # your default project; --project for another
uurtjie config set invoice.vat_rate 15
uurtjie config set invoice.from.name "Your Name"
uurtjie config set invoice.from.bank "Bank: …" "Account name: …" "Account no.: …" "Branch code: …"
uurtjie config set client_address "12 Sample Street" "Cape Town, 8001" --project acme
uurtjie config invoice                                # guided prompts: your details + the client's (Enter keeps, - clears)
uurtjie config edit                                   # open the file in $EDITOR; an invalid edit is rolled back
```

Per-project settings: `name client rate currency human_factor client_address client_vat_number
client_reference require_agent`. Also `invoice.vat_rate`, `invoice.payment_terms_days`,
`invoice.number_format`, `invoice.dir`, `invoice.from.{name,address,email,phone,vat_number,reg_number,bank,footer}`
and the top-level options (`rounding_minutes`, `sleep_minutes`, `show_money_in_bar`, `meeting_*` …).
List settings (address, bank) take one argument per line. Typos are rejected with the list of valid names, and nothing is
saved unless the result still parses and loads; the previous file is kept as `config.toml.bak`. The daemon
reloads automatically.

## Calls and meetings

Tracking stops after `sleep_minutes` without keyboard, mouse or scroll input, which would drop a
40-minute call. So uurtjie also watches the **microphone**: when a project's window (or the process
behind it) has a microphone capture stream open for `meeting_min_seconds` (default 30), that time is
recorded as a `meeting` entry for the project, **even while you are idle**.

- It is matched to a project through the window rules, so a dedicated client Chrome counts the moment
  you join a call there, whichever window is focused. A browser shared with unrelated windows (your
  everyday Chrome) only counts while the project's window is the focused one.
- A mic that goes quiet for less than `meeting_grace_seconds` (default 120, e.g. mute toggles) is the
  same call. The entry ends when the mic has been gone that long, and starts when it opened.
- Capturing your *speakers* (screen recorders, screen sharing) and dictation (`voxtype`) don't count;
  extend `meeting_ignore` for other apps. Join pages that open the mic before you enter the call
  (lobby previews) count once the mic has been open for `meeting_min_seconds`.
- The widget shows `In a call` with the project and minutes; set `meeting_detect = false` to turn it off.
- Calls overlap your normal entries, and sessions never double count, so a call during typing is
  billed once. Needs `pactl` (PipeWire/PulseAudio, installed with Omarchy).

## Monthly log and invoice

```bash
uurtjie report --month 2026-10 --project acme                 # markdown timesheet
uurtjie report --month 2026-10 --project acme -f csv -o ~/invoices/{project}-{period}.csv --record
uurtjie report --month 2026-10 --project acme -f json         # feed this to your invoice tooling
```

Per day: union of billable time × `human_factor`, rounded up to `rounding_minutes`, × `rate`.
`--record` logs the report in the `invoices` table. The JSON output is the stable interface for
mapping into the client's timesheet template (e.g. let Claude derive the column mapping once and
keep it as a skill).

### Querying the database directly

```bash
sqlite3 ~/.local/share/uurtjie/uurtjie.db \
  "SELECT date(start_local) d, project, round(sum(hours),2) FROM spans_local
   WHERE billable GROUP BY d, project ORDER BY d"
```

Tables: `spans` (every entry; `detail` JSON carries Herdr workspace, pane, cwd and agent),
`timer` (running manual timer), `invoices` (issued invoices with their frozen snapshot, plus the old report log),
`payments`, view `spans_local`.
Note that summing `spans` directly can double count overlaps; `uurtjie report` doesn't.

## Invoices

```bash
uurtjie invoice create --month 2026-10 --dry-run   # figures only: nothing saved, nothing locked
uurtjie invoice create --month 2026-10 --pdf       # issue INV-2026-001, write HTML (+ PDF)
uurtjie invoice list                               # numbers, paid / owed, due dates, state
uurtjie invoice sent 001                           # mark as sent
uurtjie invoice paid 001 [--amount 5000] [--date 2026-11-12]   # default: the full outstanding amount
uurtjie invoice void 001 --yes                     # cancel and unlock; the number is never reused
uurtjie invoice unbilled                           # months with tracked time that no invoice covers
uurtjie invoice render 001 --pdf --detail sessions # rewrite the files from the frozen figures
```

Without `--month`, `create` bills last calendar month. `--from/--to` bills any range of whole days.

- **Locking.** Issuing an invoice locks its period for that project: `edit`, `rm`, `undo`, `add` and
  `quick` refuse to touch time inside it (`--force` overrides, or void the invoice first). Overlapping
  an existing invoice is refused, and so is a period that includes today, so nothing tracked later
  slips in unbilled. A project without a `rate` can't be invoiced.
- **Frozen figures.** Hours, rate, VAT and the daily breakdown are stored with the invoice, so changing
  the rate or config later never alters one that was sent. A backup is taken before each issue.
- **Money.** Per day: session hours × `human_factor`, rounded up to `rounding_minutes`; subtotal
  = hours × rate; VAT = subtotal × `[invoice] vat_rate`%. Part payments are fine; the state is
  `issued` → `sent` → `part-paid` / `overdue` → `paid`. Outstanding and overdue amounts show in the
  widget panel and tooltip.
- **What the client sees.** Page 1 is the invoice, page 2 the timesheet (hours per day). Window titles and
  work summaries are only included with `--detail sessions`, so nothing from your browsing leaks by default.
  Hours are decimal (7.5 = 7 h 30 min), and the page says so.
- **Your details.** Put your name, address, VAT and bank details under `[invoice.from]` and the client's
  address under the project (see `config.example.toml`). Files go to `[invoice] dir`
  (default `~/.local/share/uurtjie/invoices`). `--pdf` prints the HTML with headless Chrome/Chromium;
  without one, open the HTML and print to PDF.

## Backups

The daemon writes a verified copy of the database once a day (`uurtjie-YYYY-MM-DD.db`) and keeps
`backup_keep` (30) of them. Set `backup_dir` to a synced folder for off-machine copies.

```bash
uurtjie backup              # today's daily backup now
uurtjie backup --now        # extra timestamped copy, outside the rotation (e.g. before month-end)
uurtjie backup --list
```

Restore: `systemctl --user stop uurtjie`, copy a backup over `~/.local/share/uurtjie/uurtjie.db`,
`systemctl --user start uurtjie`.

## Invoice server (planned)

SQLite stays the store. A local invoice server can come later and connect to this without changes:
read the database read-only (WAL mode allows a reader alongside the daemon), or call
`uurtjie sessions --json` / `uurtjie report -f json`, which already carry hours, amounts, session
summaries and commits per day.

## Upgrading from tally

uurtjie is the new name for tally (0.2.x). Run `./install.sh` and it migrates in place: your database,
backups and config move to `~/.local/share/uurtjie` and `~/.config/uurtjie`, the old `tally` service,
commands and bar widget are removed, and the `source =` line in `~/.config/hypr/bindings.conf` is
rewritten (the original is kept as `bindings.conf.tally-bak`; Lua configs get the hint to add the `require` line). If both tally and uurtjie data exist,
it stops and asks you to merge by hand. A custom `backup_dir` keeps its path, but its
`tally-*.db` files are renamed to `uurtjie-*.db`. Afterwards use `uurtjie` where you used `tally`,
and `UURTJIE_*` for the old `TALLY_*` environment variables.

## Releasing

Merging into the `release` branch publishes a GitHub Release (`.github/workflows/release.yml`):
tests run, `scripts/build-release.sh` builds `uurtjie-<version>.tar.gz` (plus `uurtjie.tar.gz` for the
stable "latest" link and `SHA256SUMS`), and the matching `CHANGELOG.md` section becomes the release
notes. Only the newest 3 releases stay hosted; older ones are deleted, their git tags kept.

1. Bump the version in `uurtjie/__init__.py` **and** `omarchy-plugin/jd.uurtjie/manifest.json`.
2. Add a `## [x.y.z] - YYYY-MM-DD` section to `CHANGELOG.md`.
3. `scripts/build-release.sh --check` (CI runs the same check on every PR).
4. Open a PR from `main` into `release` and merge it.

Re-merging without a version bump fails instead of overwriting a published release.

## Troubleshooting

- `journalctl --user -u uurtjie -f` — set `UURTJIE_LOG=DEBUG` in the unit for per-sample detail.
- Nothing tracked in a terminal: check the terminal's class is in `terminal_classes`
  (`hyprctl activewindow`) and the Herdr pane cwd is under a project path.
- Widget shows `uurtjie?`: `~/.local/bin/uurtjie status --json` must run cleanly.

## Possible next steps

- Supabase sync of `spans` alongside the existing Claude Code ledger.
- Claude Code `UserPromptSubmit` hook calling a `uurtjie ping` to credit remote/SSH sessions.
- Agent working-time (Herdr `agent_status = working`) as a separate non-billable series.
