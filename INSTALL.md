# Installing uurtjie

This guide is for people **and coding agents**. If you are an agent: follow the steps in order, run
each *Check*, and stop and report to the user when a check fails. Don't skip the parts marked
**Ask the user**.

Everything is installed for the current user. The only system-wide change is installing `swayidle`
with `sudo pacman` if it is missing (step 3).

---

## 1. Check prerequisites

| Need | Check | Expected | If missing |
|---|---|---|---|
| Hyprland (Wayland) | `hyprctl version` | prints a version | **Required.** uurtjie reads window focus from Hyprland. Stop. |
| Python 3.11+ | `python3 -c 'import sys; print(sys.version_info >= (3, 11))'` | `True` | **Required.** Install a newer Python. Stop. |
| swayidle | `command -v swayidle` | a path | `install.sh` installs it with `sudo pacman` (Arch/Omarchy). On other distros install it yourself first. |
| systemd user session | `systemctl --user is-system-running` | `running` or `degraded` | **Required** for the background service. |
| pactl (PipeWire/PulseAudio) | `command -v pactl` | a path | Optional. Without it calls and meetings aren't detected. |
| Herdr | `test -S ~/.config/herdr/herdr.sock && echo yes` | `yes` | Optional. Plain terminals are still tracked by working directory. |
| Omarchy Quattro bar | `omarchy plugin list --json` | JSON | Optional. Without it there is no bar widget. For Waybar, use `waybar/uurtjie.jsonc`. |

uurtjie has no pip dependencies (Python standard library only).

## 2. Download

### Option A: latest release (recommended)

```bash
work=$(mktemp -d) && cd "$work"
curl -fsSLO https://github.com/jdc891010/uurtjie/releases/latest/download/uurtjie.tar.gz
curl -fsSLO https://github.com/jdc891010/uurtjie/releases/latest/download/SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS
tar xzf uurtjie.tar.gz
cd uurtjie
```

*Check:* `sha256sum` prints `uurtjie.tar.gz: OK`, and `./install.sh` exists.

Pin a specific version instead (the last 3 releases are hosted, see
<https://github.com/jdc891010/uurtjie/releases>):

```bash
v=0.4.1
curl -fsSLO "https://github.com/jdc891010/uurtjie/releases/download/v$v/uurtjie-$v.tar.gz"
```

With the GitHub CLI: `gh release download --repo jdc891010/uurtjie -p uurtjie.tar.gz -p SHA256SUMS`.

### Option B: from source

```bash
git clone https://github.com/jdc891010/uurtjie.git
cd uurtjie
```

## 3. Install

Optional but quick: `python3 -m unittest discover -s tests`. *Check:* the last line is `OK`.

**Ask the user** before this step if `swayidle` is missing, because `install.sh` will run `sudo pacman -S swayidle`.

```bash
./install.sh
```

What it does (re-running it upgrades in place, and it never overwrites an existing config or database):

| Path | What |
|---|---|
| `~/.local/share/uurtjie/app/` | the Python package |
| `~/.local/bin/uurtjie`, `uurtjie-timer`, `uurtjie-quick` | commands (make sure `~/.local/bin` is on `PATH`) |
| `~/.config/uurtjie/config.toml` | your settings (created from `config.example.toml` only if absent) |
| `~/.config/systemd/user/uurtjie.service` | background tracker, enabled and started |
| `~/.config/hypr/uurtjie.lua` or `uurtjie-bindings.conf` | keybinds. The installer prints a line for you to add to your bindings file. It doesn't edit that file itself. |
| `~/.config/omarchy/plugins/jd.uurtjie/` | bar widget (Omarchy Quattro only) |
| `~/.local/share/uurtjie/uurtjie.db` | your tracked time (SQLite) |

*Check:* `systemctl --user is-active uurtjie` prints `active`.

If the installer printed a `require("hypr.uurtjie")` or `source = …` line, **ask the user** whether to
add it to their Hyprland bindings (SUPER+ALT+T/U/Z must not clash with their own binds).

## 4. Run the setup wizard

```bash
uurtjie setup
```

The wizard is interactive. **Agents: don't script the answers.** Hand it to the user (in Claude Code,
ask them to type `! uurtjie setup`). It walks through:

1. **How tracking works**: idle cut-off, folders vs windows, calls, sessions and rounding.
2. **Your timezone / UTC offset**: day boundaries for daily hours and earnings.
3. **Projects**: name, client, hourly **rate** and currency.
4. **Folders to watch**: terminal, Claude Code, Codex and Herdr work under these paths counts for the project.
   It suggests folders you've recently used.
5. **Browser windows**: which Chrome or other browser profiles or windows count, matched by window class or title.
   It lists the windows open right now so you can pick.
6. **Review**: shows the config it will write, saves it, reloads the tracker and runs a live check.

> **If `uurtjie setup` reports "invalid choice"**, your version predates the wizard. Configure by hand:
> ```bash
> $EDITOR ~/.config/uurtjie/config.toml     # projects, paths, [[projects.windows]], rate
> uurtjie reload
> uurtjie config show                        # effective settings + what invoicing still needs
> uurtjie config invoice                     # guided prompts for your and the client's details
> ```
> `config.example.toml` and the README explain every setting. Use `hyprctl activewindow` to see a
> window's class and title for `[[projects.windows]]`.

## 5. Verify

```bash
systemctl --user is-active uurtjie     # active
uurtjie projects                       # each project with its rate, folders and window rules
uurtjie status                         # focus a folder/window you configured, wait ~10 s, run again: it shows that project as tracked now
uurtjie where                          # where the config, database and backups live
journalctl --user -u uurtjie -n 50     # no tracebacks
```

## Upgrade

Download a newer release (step 2) and run its `./install.sh`. Your config, database and backups stay.
Then check with `uurtjie status`; the version is in `uurtjie status --json`.

## Uninstall

```bash
systemctl --user disable --now uurtjie
rm -f ~/.config/systemd/user/uurtjie.service ~/.local/bin/uurtjie ~/.local/bin/uurtjie-timer ~/.local/bin/uurtjie-quick
rm -f ~/.config/hypr/uurtjie.lua ~/.config/hypr/uurtjie-bindings.conf      # and remove the line you added to bindings
command -v omarchy >/dev/null && omarchy plugin remove jd.uurtjie
rm -rf ~/.config/omarchy/plugins/jd.uurtjie ~/.local/share/uurtjie/app
```

**Ask the user** before deleting their data: `~/.local/share/uurtjie/` (database, backups, invoices)
and `~/.config/uurtjie/` (settings).

## Troubleshooting

- `journalctl --user -u uurtjie -f`. Set `UURTJIE_LOG=DEBUG` in the unit for per-sample detail.
- Nothing tracked in a terminal: the terminal's class must be in `terminal_classes` (`hyprctl activewindow`),
  and its working directory (or the Herdr pane's cwd) must be under a project path.
- Widget shows `uurtjie?`: `~/.local/bin/uurtjie status --json` must run cleanly.
