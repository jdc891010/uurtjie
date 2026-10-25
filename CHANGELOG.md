# Changelog

Each `## [x.y.z]` section becomes the notes of that GitHub release (see README › Releasing).
The version must match `uurtjie/__init__.py`, the widget manifest, and the panel's version.

## [0.5.0] - 2026-10-25

### Added
- A project logo: a clock face with only the first hour lit. SVG mark and wordmarks (light and dark) plus PNGs in `docs/images/`.

### Removed
- `human_factor` no longer changes time or money. Old config values are ignored with a warning; already-issued invoices retain their frozen figures and original factor notes.

### Changed
- Human time is the union of recorded focus, meeting and manual evidence. Each overlapping second belongs to one project: manual time and timers take precedence over meetings, then focused windows.
- Gap billing (`bridge_minutes`) and time rounding (`rounding_minutes`) now default to 0 and affect invoices only. A gap with no sample in it (under two `tick`s) still counts as work, so entries recorded by 0.4.x lose nothing at window switches; a gap that held a sample of something untracked never counts.
- The widget shows exact human time and earnings. Its tooltip and panel explain when configured invoice adjustments may produce different amounts.
- **Action:** hours may drop compared with 0.4.x because gaps and cross-project overlaps no longer inflate human time. Remove `human_factor`; set `bridge_minutes` and `rounding_minutes` to 0 to adopt the exact defaults. Existing explicit invoice settings remain configured. Already-issued invoices are frozen and are not recalculated.

### Fixed
- Context switches close the previous span and open the next at one sample boundary. Microphone grace retains call identity without billing absent capture time.
- Sessions, hourly charts, daily totals and report raw hours use the same covered intervals, including day boundaries.

## [0.4.1] - 2026-10-25

### Added
- `uurtjie export`: the full log as CSV (default: your Documents folder; `--project`, `--to`).
- Hours-by-day chart: the bar tooltip shows minutes worked per clock hour today, with the busiest hour;
  `status --json` carries the same data as `hourly` for the panel.

### Packaging
- First release built and published by GitHub Actions from the `release` branch.
- `INSTALL.md`: step-by-step install guide, written so a coding agent can follow it.

## [0.3.0] - 2026-10-21

### Changed
- Renamed from tally to uurtjie. `install.sh` migrates data, config, service, commands and the bar widget in place.

### Added
- Calls and meetings: an open microphone in a project's window counts as a `meeting` entry, even while idle.
- Invoices: issue, lock the billed period, frozen figures, HTML/PDF, payments and overdue tracking.
- Month-to-date earnings on the bar widget.
- `uurtjie config set|show|invoice|edit`: change settings without hand-editing the TOML.
- Daily verified database backups.
