# Changelog

Each `## [x.y.z]` section becomes the notes of that GitHub release (see README › Releasing).
The version must match `uurtjie/__init__.py` and `omarchy-plugin/jd.uurtjie/manifest.json`.

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
