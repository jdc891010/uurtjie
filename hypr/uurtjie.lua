-- uurtjie keybinds + popup window rule for Omarchy's Lua Hyprland config.
-- Loaded from bindings.lua with:  require("hypr.uurtjie")
-- (SUPER+ALT+S is already Omarchy's scratchpad/secret binding, so status is on U.)
local bin = os.getenv("HOME") .. "/.local/bin/uurtjie"

-- Start the timer; press again to stop it (popup asks to confirm + describe).
o.bind("SUPER + ALT + T", "Uurtjie timer", bin .. "-timer")

-- Today / week / month as a notification.
o.bind("SUPER + ALT + U", "Uurtjie status", "sh -c 'notify-send -a uurtjie Uurtjie \"$(" .. bin .. " status 2>&1)\"'")

-- Undo the last manual entry.
o.bind("SUPER + ALT + Z", "Uurtjie undo", "sh -c 'notify-send -a uurtjie Uurtjie \"$(" .. bin .. " undo 2>&1)\"'")

-- Optional quick back-fill popup:
-- o.bind("SUPER + ALT + A", "Uurtjie quick add", bin .. "-quick")

-- Float and centre the small timer / quick-add popups.
o.window("^uurtjie\\.(timer|quick)$", { float = true, center = true, size = { 560, 200 } })
