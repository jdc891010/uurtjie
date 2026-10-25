"""Config loading: projects, path rules, window rules (Chrome profiles)."""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("UURTJIE_CONFIG", "~/.config/uurtjie/config.toml")).expanduser()
DATA_DIR = Path(os.environ.get("UURTJIE_DATA", "~/.local/share/uurtjie")).expanduser()
_runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
if not os.environ.get("XDG_RUNTIME_DIR") and not _runtime_root.exists():
    _runtime_root = DATA_DIR / "run"
RUNTIME_DIR = _runtime_root / "uurtjie"

DEFAULT_TERMINALS = [
    "Alacritty", "alacritty",
    "com.mitchellh.ghostty", "ghostty",
    "kitty", "org.wezfurlong.wezterm", "foot",
]


@dataclass
class WindowRule:
    """Matches a Hyprland window by class (or initialClass) and/or title regex."""
    cls: str | None = None
    title: re.Pattern | None = None

    def matches(self, wclass: str, initial_class: str, title: str) -> bool:
        if self.cls is None and self.title is None:
            return False
        if self.cls is not None and self.cls not in (wclass, initial_class):
            return False
        if self.title is not None and not self.title.search(title or ""):
            return False
        return True


@dataclass
class Project:
    id: str
    name: str
    client: str = ""
    rate: float = 0.0
    currency: str = "ZAR"
    paths: list[Path] = field(default_factory=list)
    windows: list[WindowRule] = field(default_factory=list)
    require_agent: bool = False          # only count terminal time when an agent runs in the pane
    agents: list[str] = field(default_factory=lambda: ["claude", "codex"])
    client_address: list[str] = field(default_factory=list)   # shown on invoices
    client_vat_number: str = ""
    client_reference: str = ""           # e.g. their PO / contract number

    def path_score(self, cwd: Path) -> int:
        """Length of the longest configured path that contains cwd; 0 = no match."""
        best = 0
        for p in self.paths:
            if cwd == p or cwd.is_relative_to(p):
                best = max(best, len(p.parts))
        return best


@dataclass
class InvoiceSettings:
    number_format: str = "INV-{year}-{seq:03d}"     # {year} {seq} {project}
    payment_terms_days: int = 14
    vat_rate: float = 0.0                           # percent, e.g. 15 for South African VAT
    dir: Path = DATA_DIR / "invoices"               # where invoice HTML/PDF files are written
    seller: dict = field(default_factory=dict)      # name, address[], email, phone, vat_number, reg_number, bank[], footer


DEFAULT_MEETING_IGNORE = ["voxtype", "pavucontrol", "speech-dispatcher"]


@dataclass
class Config:
    tick: int = 5                   # seconds between samples
    sleep_minutes: float = 5        # no keyboard/mouse/scroll this long = asleep: tracking stops
    chunk_minutes: int = 15         # long same-task entries are cut into chunks this size
    bridge_minutes: float = 0       # invoice-only gap billing; never affects human time
    merge_gap: int = 120            # same-context samples closer than this extend one entry
    rounding_minutes: int = 6       # per-day rounding on reports (0 = none)
    git_author: str | None = None   # commits to attribute; default `git config user.email`
    summary_command: list[str] | None = None  # optional: gets session JSON on stdin, prints 1 line
    backup_dir: Path = DATA_DIR / "backups"   # point at a synced folder (Syncthing, Drive) for off-machine copies
    backup_keep: int = 30                     # daily backups to keep
    terminal_classes: list[str] = field(default_factory=lambda: list(DEFAULT_TERMINALS))
    herdr_socket: Path = Path("~/.config/herdr/herdr.sock").expanduser()
    default_project: str | None = None
    projects: list[Project] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Calls: a microphone capture stream open this long counts as a meeting, even with no keyboard input.
    show_money_in_bar: bool = True        # month-to-date earnings next to the hours in the bar widget
    meeting_detect: bool = True
    meeting_min_seconds: int = 30         # shorter mic use (dictation, a voice search) is not a call
    meeting_grace_seconds: int = 120      # mic gone this long = call over (bridges mute toggles)
    meeting_ignore: list[str] = field(default_factory=lambda: list(DEFAULT_MEETING_IGNORE))
    invoice: InvoiceSettings = field(default_factory=InvoiceSettings)

    @property
    def idle_timeout(self) -> int:
        return max(30, int(self.sleep_minutes * 60))

    @property
    def bridge_seconds(self) -> int:
        return max(0, int(self.bridge_minutes * 60))

    def project(self, pid: str) -> Project | None:
        return next((p for p in self.projects if p.id == pid), None)

    def project_for_path(self, cwd: str | Path | None) -> Project | None:
        if not cwd:
            return None
        cwd = Path(cwd).expanduser()
        scored = [(p.path_score(cwd), p) for p in self.projects]
        scored = [s for s in scored if s[0] > 0]
        return max(scored, key=lambda s: s[0])[1] if scored else None

    def project_for_window(self, wclass: str, initial_class: str, title: str) -> Project | None:
        for p in self.projects:
            if any(r.matches(wclass, initial_class, title) for r in p.windows):
                return p
        return None

    def is_terminal(self, wclass: str, initial_class: str) -> bool:
        return wclass in self.terminal_classes or initial_class in self.terminal_classes


def _lines(v) -> list[str]:
    """A TOML string (with newlines) or a list of strings -> list of non-empty lines."""
    if isinstance(v, str):
        v = v.splitlines()
    return [str(x).strip() for x in (v or []) if str(x).strip()]


def _seller(d: dict) -> dict:
    out = {k: str(d.get(k, "")) for k in ("name", "email", "phone", "vat_number", "reg_number", "footer")}
    out["address"] = _lines(d.get("address"))
    out["bank"] = _lines(d.get("bank"))
    return out


def _window_rule(d: dict) -> WindowRule:
    title = d.get("title")
    return WindowRule(cls=d.get("class") or None, title=re.compile(title) if title else None)


def load(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        return Config()
    raw = tomllib.loads(path.read_text())
    cfg = Config()
    if "human_factor" in raw or any("human_factor" in p for p in raw.get("projects", [])):
        cfg.warnings.append("human_factor is no longer used")
    for key in ("tick", "sleep_minutes", "chunk_minutes", "bridge_minutes", "merge_gap",
                "rounding_minutes", "default_project", "git_author"):
        if key in raw:
            setattr(cfg, key, raw[key])
    if "idle_timeout" in raw and "sleep_minutes" not in raw:   # older configs, in seconds
        cfg.sleep_minutes = raw["idle_timeout"] / 60
    if "backup_dir" in raw:
        cfg.backup_dir = Path(raw["backup_dir"]).expanduser()
    if "backup_keep" in raw:
        cfg.backup_keep = int(raw["backup_keep"])
    if raw.get("summary_command"):
        sc = raw["summary_command"]
        cfg.summary_command = sc if isinstance(sc, list) else ["sh", "-c", sc]
    if "terminal_classes" in raw:
        cfg.terminal_classes = list(raw["terminal_classes"])
    if "herdr_socket" in raw:
        cfg.herdr_socket = Path(raw["herdr_socket"]).expanduser()
    for key in ("meeting_detect", "show_money_in_bar"):
        if key in raw:
            setattr(cfg, key, bool(raw[key]))
    for key in ("meeting_min_seconds", "meeting_grace_seconds"):
        if key in raw:
            setattr(cfg, key, int(raw[key]))
    if "meeting_ignore" in raw:
        cfg.meeting_ignore = [str(x) for x in raw["meeting_ignore"]]
    if "invoice" in raw:
        iv = raw["invoice"]
        cfg.invoice = InvoiceSettings(
            number_format=iv.get("number_format", InvoiceSettings.number_format),
            payment_terms_days=int(iv.get("payment_terms_days", 14)),
            vat_rate=float(iv.get("vat_rate", 0)),
            dir=Path(iv["dir"]).expanduser() if "dir" in iv else InvoiceSettings().dir,
            seller=_seller(iv.get("from", {})),
        )
    for p in raw.get("projects", []):
        windows = p.get("windows", [])
        if isinstance(windows, dict):
            windows = [windows]
        cfg.projects.append(Project(
            id=p["id"],
            name=p.get("name", p["id"]),
            client=p.get("client", ""),
            rate=float(p.get("rate", 0)),
            currency=p.get("currency", "ZAR"),
            paths=[Path(x).expanduser() for x in p.get("paths", [])],
            windows=[_window_rule(w) for w in windows],
            require_agent=bool(p.get("require_agent", False)),
            agents=list(p.get("agents", ["claude", "codex"])),
            client_address=_lines(p.get("client_address")),
            client_vat_number=str(p.get("client_vat_number", "")),
            client_reference=str(p.get("client_reference", "")),
        ))
    return cfg
