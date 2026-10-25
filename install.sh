#!/usr/bin/env bash
# Installs uurtjie for the current user on Omarchy (Hyprland). Re-run to upgrade.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
app="$HOME/.local/share/uurtjie/app"

need() { command -v "$1" >/dev/null || { echo "missing: $1 ($2)"; exit 1; }; }
need python3 "pacman -S python"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "python 3.11+ required"; exit 1; }
command -v swayidle >/dev/null || {
  echo "installing swayidle (input-activity detection)…"
  sudo pacman -S --needed --noconfirm swayidle
}

# --- migrate from tally (<= 0.2.x): same data, new name ---------------------
old_data="$HOME/.local/share/tally"; new_data="$HOME/.local/share/uurtjie"
old_cfg="$HOME/.config/tally";      new_cfg="$HOME/.config/uurtjie"
old_unit="$HOME/.config/systemd/user/tally.service"
if [[ -e "$old_data" || -e "$old_cfg" || -e "$old_unit" ]]; then
  echo "→ migrating from tally"
  if [[ -e "$old_data" && -e "$new_data" ]] || [[ -e "$old_cfg" && -e "$new_cfg" ]]; then
    echo "both tally and uurtjie data exist; merge by hand, then remove the tally dirs and re-run"; exit 1
  fi
  systemctl --user disable --now tally.service 2>/dev/null || true
  rm -f "$old_unit"
  rename_backups() {
    local d="$1" f; [[ -d "$d" ]] || return 0
    for f in "$d"/tally-*.db; do [[ -e "$f" ]] || continue; mv -n "$f" "$d/uurtjie-${f##*/tally-}"; done
  }
  if [[ -d "$old_data" ]]; then
    mv "$old_data" "$new_data"
    rm -rf "$new_data/app"
    for ext in db db-wal db-shm; do
      [[ -f "$new_data/tally.$ext" ]] && mv "$new_data/tally.$ext" "$new_data/uurtjie.$ext"
    done
    rename_backups "$new_data/backups"
  fi
  if [[ -d "$old_cfg" ]]; then
    mv "$old_cfg" "$new_cfg"
    custom=$(sed -n 's/^backup_dir *= *"\(.*\)".*/\1/p' "$new_cfg/config.toml" 2>/dev/null || true)
    [[ -n "$custom" ]] && rename_backups "${custom/#\~/$HOME}"
  fi
  rm -f "$HOME/.local/bin/tally" "$HOME/.local/bin/tally-timer" "$HOME/.local/bin/tally-quick" \
        "$HOME/.config/hypr/tally-bindings.conf"
  if command -v omarchy >/dev/null; then omarchy plugin remove jd.tally >/dev/null 2>&1 || true; fi
  rm -rf "$HOME/.config/omarchy/plugins/jd.tally"
  b="$HOME/.config/hypr/bindings.conf"
  if grep -q "tally-bindings.conf" "$b" 2>/dev/null; then
    cp "$b" "$b.tally-bak"                       # keeps your original; delete once happy
    sed -i 's/tally-bindings\.conf/uurtjie-bindings.conf/' "$b"
  fi
  echo "  data + config moved to ~/.local/share/uurtjie and ~/.config/uurtjie; old tally files removed"
fi

echo "→ app → $app"
mkdir -p "$app" "$HOME/.local/bin" "$HOME/.config/uurtjie"
rm -rf "$app/uurtjie"
cp -r "$here/uurtjie" "$app/"
install -m 755 "$here/bin/uurtjie" "$HOME/.local/bin/uurtjie"
install -m 755 "$here/bin/uurtjie-quick" "$HOME/.local/bin/uurtjie-quick"
install -m 755 "$here/bin/uurtjie-timer" "$HOME/.local/bin/uurtjie-timer"
if [[ -f "$HOME/.config/hypr/hyprland.lua" ]]; then          # Omarchy with the Lua Hyprland config
  install -Dm 644 "$here/hypr/uurtjie.lua" "$HOME/.config/hypr/uurtjie.lua"
  if ! grep -q 'hypr.uurtjie' "$HOME/.config/hypr/bindings.lua" 2>/dev/null; then
    echo "→ keybinds: add this line to ~/.config/hypr/bindings.lua (check SUPER+K for clashes first):"
    echo '    require("hypr.uurtjie")'
  fi
else                                                           # classic hyprland.conf setup
  install -Dm 644 "$here/hypr/uurtjie-bindings.conf" "$HOME/.config/hypr/uurtjie-bindings.conf"
  if ! grep -q "uurtjie-bindings.conf" "$HOME/.config/hypr/bindings.conf" 2>/dev/null; then
    echo "→ keybinds: add this line to ~/.config/hypr/bindings.conf (check SUPER+K for clashes first):"
    echo "    source = ~/.config/hypr/uurtjie-bindings.conf"
  fi
fi

if [[ ! -f "$HOME/.config/uurtjie/config.toml" ]]; then
  cp "$here/config.example.toml" "$HOME/.config/uurtjie/config.toml"
  echo "→ wrote ~/.config/uurtjie/config.toml — edit projects/paths, then: uurtjie reload"
fi

echo "→ systemd user service"
install -Dm 644 "$here/systemd/uurtjie.service" "$HOME/.config/systemd/user/uurtjie.service"
systemctl --user daemon-reload
systemctl --user enable --now uurtjie.service

if command -v omarchy >/dev/null && omarchy plugin list --json >/dev/null 2>&1; then
  echo "→ Omarchy Quattro bar widget"
  dest="$HOME/.config/omarchy/plugins/jd.uurtjie"
  rm -rf "$dest"; mkdir -p "$dest"
  cp "$here"/omarchy-plugin/jd.uurtjie/* "$dest/"      # plugins may not be symlinks
  omarchy plugin validate "$dest"
  omarchy-shell shell rescanPlugins || true       # the shell must know the plugin before it can be enabled
  sleep 1
  omarchy plugin enable jd.uurtjie right || echo "  couldn't enable the widget; run: omarchy plugin enable jd.uurtjie right"
else
  echo "→ no Quattro plugin system found; for Waybar paste waybar/uurtjie.jsonc into your config"
fi

echo
"$HOME/.local/bin/uurtjie" status || true
echo "done. Logs: journalctl --user -u uurtjie -f"
