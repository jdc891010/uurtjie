"""uurtjie — samples focus + input activity every few seconds and writes spans.

Signals:
  * Hyprland IPC  -> which window is focused (class, initialClass, title, pid)
  * swayidle      -> keyboard/mouse/scroll activity (ext-idle-notify, no root needed)
  * Herdr socket  -> focused workspace/pane, its cwd and the agent running in it
  * /proc         -> cwd of plain (non-Herdr) terminals

A sample counts only when you are NOT idle and the focused thing maps to a project.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import config as config_mod
from . import db, gitinfo
from .config import RUNTIME_DIR, Config

log = logging.getLogger("uurtjie")


@dataclass
class Context:
    project: str
    source: str            # terminal | chrome | window
    key: str               # what makes this span distinct (herdr workspace, window class, cwd)
    detail: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- Hyprland

class Hyprland:
    def __init__(self):
        self._sock: Path | None = None

    def socket(self) -> Path:
        if self._sock and self._sock.exists():
            return self._sock
        base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "hypr"
        sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        cand = base / sig / ".socket.sock" if sig else None
        if not cand or not cand.exists():
            # systemd units don't always inherit the signature: use the newest instance
            socks = sorted(base.glob("*/.socket.sock"), key=lambda p: p.stat().st_mtime, reverse=True)
            cand = socks[0] if socks else None
        if not cand:
            raise RuntimeError("Hyprland socket not found")
        self._sock = cand
        return cand

    async def request(self, cmd: str):
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.socket())), 2)
        writer.write(f"j/{cmd}".encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 2)
        writer.close()
        try:
            return json.loads(data or b"{}")
        except json.JSONDecodeError:
            return {}

    async def active_window(self) -> dict | None:
        w = await self.request("activewindow")
        return w if isinstance(w, dict) and w.get("address") else None

    async def clients(self) -> list[dict]:
        c = await self.request("clients")
        return c if isinstance(c, list) else []


# --------------------------------------------------------------------------- microphone (calls)

def parse_mic_streams(outputs: list, sources: list) -> list[dict]:
    """pactl JSON -> real microphone captures. Capturing a speaker monitor (screen recorder, screen share)
    shows up as a source-output too, but it is not a call: those sources are named '*.monitor'."""
    monitors = {s.get("index") for s in sources
                if str(s.get("name", "")).endswith(".monitor") or s.get("monitor_of_sink") is not None}
    streams = []
    for o in outputs:
        if o.get("corked") or o.get("source") in monitors:
            continue
        pr = o.get("properties") or {}
        try:
            pid = int(pr.get("application.process.id"))
        except (TypeError, ValueError):
            pid = None
        streams.append({"pid": pid, "app": pr.get("application.name") or "",
                        "binary": pr.get("application.process.binary") or "", "media": pr.get("media.name") or ""})
    return streams


class AudioProbe:
    """Open microphone capture streams (PipeWire/PulseAudio): which apps are listening to the mic right now."""

    def __init__(self):
        self.available = shutil.which("pactl") is not None

    async def _pactl(self, what: str) -> list:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "pactl", "-f", "json", "list", what,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 3)
            data = json.loads(out or b"[]")
            return data if isinstance(data, list) else []
        except Exception as e:
            log.debug("pactl list %s failed: %s", what, e)
            if proc and proc.returncode is None:
                proc.kill()
            return []

    async def mic_streams(self) -> list[dict]:
        if not self.available:
            return []
        outputs = await self._pactl("source-outputs")
        if not outputs:
            return []
        return parse_mic_streams(outputs, await self._pactl("sources"))


def proc_ancestors(pid: int, limit: int = 40) -> list[int]:
    """pid, its parent, its grandparent … (stops at init)."""
    chain: list[int] = []
    while pid and pid > 1 and len(chain) < limit:
        chain.append(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError):
            break
    return chain


# --------------------------------------------------------------------------- Herdr

class HerdrError(Exception):
    pass


class Herdr:
    def __init__(self, sock: Path):
        self.sock = sock
        self._ws_cache: dict[str, tuple[float, dict]] = {}

    async def call(self, method: str, params: dict | None = None, timeout: float = 2.0) -> dict:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self.sock)), timeout)
        try:
            req = {"id": "uurtjie", "method": method, "params": params or {}}
            writer.write((json.dumps(req) + "\n").encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout)
        finally:
            writer.close()
        msg = json.loads(line or b"{}")
        if "error" in msg:
            raise HerdrError(msg["error"].get("message", str(msg["error"])))
        return msg.get("result", {})

    async def workspace(self, ws_id: str) -> dict:
        hit = self._ws_cache.get(ws_id)
        if hit and time.monotonic() - hit[0] < 30:
            return hit[1]
        try:
            res = await self.call("workspace.get", {"workspace_id": ws_id})
            ws = res.get("workspace", res)
        except Exception:
            ws = {}
        self._ws_cache[ws_id] = (time.monotonic(), ws)
        return ws

    async def focused(self) -> dict | None:
        """Focused pane of the Herdr server, normalised. None if Herdr isn't reachable."""
        if not self.sock.exists():
            return None
        try:
            res = await self.call("pane.current")
        except Exception as e:  # server down, old version, etc.
            log.debug("herdr pane.current failed: %s", e)
            return None
        pane = res.get("pane", res)
        ws_id = pane.get("workspace_id")
        ws = await self.workspace(ws_id) if ws_id else {}
        agent = pane.get("agent")
        if isinstance(agent, dict):
            agent = agent.get("agent") or agent.get("name")
        return {
            "pane_id": pane.get("pane_id"),
            "workspace_id": ws_id,
            "workspace_label": ws.get("label") or ws.get("name") or ws_id,
            "cwds": [c for c in (pane.get("cwd"), pane.get("foreground_cwd"),
                                 ws.get("cwd"), ws.get("root")) if c],
            "agent": agent,
            "agent_status": pane.get("agent_status"),
        }


# --------------------------------------------------------------------------- /proc helpers

def _proc_table() -> tuple[dict[int, list[int]], dict[int, str]]:
    children: dict[int, list[int]] = defaultdict(list)
    comm: dict[int, str] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        pid = int(entry)
        name = stat[stat.index("(") + 1: stat.rindex(")")]
        ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        comm[pid] = name
        children[ppid].append(pid)
    return children, comm


def descendants(pid: int) -> list[tuple[int, str, bool]]:
    """(pid, comm, is_leaf) for every descendant of pid."""
    children, comm = _proc_table()
    out, stack = [], list(children.get(pid, []))
    while stack:
        p = stack.pop()
        out.append((p, comm.get(p, ""), not children.get(p)))
        stack.extend(children.get(p, []))
    return out


def proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def terminal_kind(win_pid: int | None) -> tuple[str, str | None]:
    """('herdr', None) if a herdr client runs in this terminal, else ('plain', cwd)."""
    if not win_pid:
        return "plain", None
    desc = descendants(win_pid)
    if any(name == "herdr" for _, name, _ in desc):
        return "herdr", None
    leaves = [p for p, _, leaf in desc if leaf]
    target = max(leaves) if leaves else win_pid  # newest leaf ~ foreground process
    return "plain", proc_cwd(target)


# --------------------------------------------------------------------------- daemon

class Daemon:
    def __init__(self, cfg: Config, con, hypr: Hyprland, herdr: Herdr, audio: AudioProbe | None = None):
        self.cfg, self.con, self.hypr, self.herdr = cfg, con, hypr, herdr
        self.audio = audio                 # None = call detection off
        self.idle = False
        self.current: dict | None = None   # {"key": tuple, "id": span_id, "last": ts}
        self.meeting_current: dict | None = None   # same shape, for the call entry (ignores idle)
        self.meeting_seen: dict | None = None      # {"key", "ctx", "since", "last_seen", "confirmed"}
        self.last_ctx: Context | None = None

    def reload(self):
        self.cfg = config_mod.load()
        if self.audio is None and self.cfg.meeting_detect:
            self.audio = AudioProbe()
        self.herdr = Herdr(self.cfg.herdr_socket)
        log.info("config reloaded: %d projects", len(self.cfg.projects))

    async def resolve(self, win: dict) -> Context | None:
        cfg = self.cfg
        wclass, iclass, title = win.get("class", ""), win.get("initialClass", ""), win.get("title", "")

        # 1. explicit window rules (Chrome profile, Figma, Slack ...) win
        p = cfg.project_for_window(wclass, iclass, title)
        if p:
            src = "chrome" if "chrom" in f"{wclass}{iclass}".lower() else "window"
            return Context(p.id, src, f"{src}:{iclass or wclass}",
                           {"window": title[:160], "class": iclass or wclass})

        if not cfg.is_terminal(wclass, iclass):
            return None

        # 2. terminal running Herdr: ask Herdr which workspace/pane has focus
        kind, cwd = terminal_kind(win.get("pid"))
        if kind == "herdr":
            pane = await self.herdr.focused()
            if not pane:
                return None
            for c in pane["cwds"]:
                p = cfg.project_for_path(c)
                if p:
                    break
            else:
                return None
            agent = (pane.get("agent") or "").lower()
            if p.require_agent and not any(a in agent for a in p.agents):
                return None
            return Context(p.id, "terminal", f'herdr:{pane["workspace_id"]}', {
                "workspace_id": pane["workspace_id"],
                "workspace_label": pane["workspace_label"],
                "pane_id": pane["pane_id"],
                "cwd": pane["cwds"][0] if pane["cwds"] else None,
                "agent": pane.get("agent"),
            })

        # 3. plain terminal: cwd of its foreground process
        p = cfg.project_for_path(cwd)
        if p:
            return Context(p.id, "terminal", f"term:{cwd}", {"cwd": cwd, "workspace_label": Path(cwd).name})
        return None

    @staticmethod
    def enrich(ctx: Context):
        """Attach repo + branch to terminal contexts (cached, cheap)."""
        if ctx.source == "terminal" and ctx.detail.get("cwd"):
            info = gitinfo.repo_info(ctx.detail["cwd"])
            if info:
                ctx.detail.update(info)

    @staticmethod
    def merge_detail(c: dict, new: dict):
        """Keep the latest detail, but remember every page title / branch seen in this chunk."""
        old = c.get("detail") or {}
        merged = dict(new)
        for k, many in (("window", "windows"), ("branch", "branches")):
            seen = list(old.get(many) or ([old[k]] if old.get(k) else []))
            if new.get(k) and new[k] not in seen:
                seen.append(new[k])
            if seen:
                merged[many] = seen[-12:]
        c["detail"] = merged

    def on_idle(self, now: int):
        self.idle = True
        if self.current:
            # swayidle fires idle_timeout seconds after the last input: give that time back
            db.truncate_span(self.con, self.current["id"], now - self.cfg.idle_timeout)
            self.current = None
        self.write_state(now, None)
        log.info("idle")

    def on_active(self):
        self.idle = False
        log.info("active")

    async def tick(self):
        now = int(time.time())
        ctx = None
        if not self.idle:
            try:
                win = await self.hypr.active_window()
            except Exception as e:
                log.debug("hyprland query failed: %s", e)
                win = None
            if win:
                ctx = await self.resolve(win)

        if ctx is None:
            self.current = None
        else:
            self.enrich(ctx)
            self._record("current", ctx, now)
        await self.meeting_tick(now)
        self.last_ctx = ctx
        self.write_state(now, ctx)

    def _record(self, slot: str, ctx: Context, now: int, gap: int | None = None, start: int | None = None):
        """Extend the open entry in `slot` if it is the same task, else open a new one (and chunk long ones)."""
        gap = self.cfg.merge_gap if gap is None else gap
        key = (ctx.project, ctx.source, ctx.key)
        c = getattr(self, slot)
        if c and c["key"] == key and now - c["last"] <= gap:
            self.merge_detail(c, ctx.detail)
            db.extend_span(self.con, c["id"], now, c["detail"])
            c["last"] = now
            if now - c["start"] >= self.cfg.chunk_minutes * 60:
                # long session on the same task: close this chunk, continue in a fresh one
                sid = db.open_span(self.con, ctx.project, ctx.source, ctx.key, now, now, ctx.detail)
                setattr(self, slot, {"key": key, "id": sid, "start": now, "last": now, "detail": dict(ctx.detail)})
        else:
            first = now if start is None else start
            sid = db.open_span(self.con, ctx.project, ctx.source, ctx.key, first, now, ctx.detail)
            setattr(self, slot, {"key": key, "id": sid, "start": first, "last": now, "detail": dict(ctx.detail)})

    # ----------------------------------------------------------------- calls
    def _mic_ignored(self, s: dict) -> bool:
        hay = f'{s["app"]} {s["binary"]} {s["media"]}'.lower()
        return any(x.lower() in hay for x in self.cfg.meeting_ignore)

    async def detect_meeting(self) -> Context | None:
        """A project's window (or a process that owns one) is using the microphone right now."""
        streams = [s for s in await self.audio.mic_streams() if not self._mic_ignored(s)]
        if not streams:
            return None
        by_pid: dict[int, list[dict]] = defaultdict(list)
        for w in await self.hypr.clients():
            if w.get("pid"):
                by_pid[w["pid"]].append(w)
        focus = None
        for s in streams:
            if not s["pid"]:
                continue
            wins = next((by_pid[p] for p in proc_ancestors(s["pid"]) if p in by_pid), None)
            if not wins:
                continue
            mapped = [(w, self.cfg.project_for_window(w.get("class", ""), w.get("initialClass", ""), w.get("title", "")))
                      for w in wins]
            if all(p is None for _, p in mapped):
                continue
            if any(p is None for _, p in mapped):
                # one process owns both project and unrelated windows (your everyday Chrome):
                # only credit the call when the focused window is the project's
                if focus is None:
                    focus = await self.hypr.active_window() or {}
                mapped = [(w, p) for w, p in mapped if p and w.get("address") == focus.get("address")]
                if not mapped:
                    continue
            w, p = mapped[0]
            cls = w.get("initialClass") or w.get("class") or ""
            return Context(p.id, "meeting", f"meeting:{cls}",
                           {"app": s["app"] or s["binary"], "class": cls, "window": (w.get("title") or "")[:160], "call": True})
        return None

    async def meeting_tick(self, now: int):
        """Calls count while the mic is open, even with no keyboard or mouse input (idle doesn't apply)."""
        if not (self.audio and self.cfg.meeting_detect):
            self.meeting_seen = self.meeting_current = None
            return
        try:
            ctx = await self.detect_meeting()
        except Exception:
            log.exception("call detection failed")
            ctx = None
        m, cfg = self.meeting_seen, self.cfg
        if ctx is None:
            if m and now - m["last_seen"] > cfg.meeting_grace_seconds:
                log.info("call ended (%s)", m["ctx"].project)
                self.meeting_seen = self.meeting_current = None
            return
        key = (ctx.project, ctx.key)
        if m is None or m["key"] != key:
            m = self.meeting_seen = {"key": key, "ctx": ctx, "since": now, "last_seen": now, "confirmed": False}
        m["ctx"], m["last_seen"] = ctx, now
        if not m["confirmed"] and now - m["since"] >= cfg.meeting_min_seconds:
            m["confirmed"] = True
            log.info("call started: %s via %s", ctx.project, ctx.detail.get("app"))
        if m["confirmed"]:
            # first entry is back-dated to when the mic opened, so the opening seconds count too
            self._record("meeting_current", ctx, now, gap=max(cfg.merge_gap, cfg.meeting_grace_seconds),
                         start=m["since"] if self.meeting_current is None else None)

    def meeting_info(self, now: int) -> dict | None:
        m = self.meeting_seen
        if not m or not m["confirmed"] or now - m["last_seen"] > self.cfg.meeting_grace_seconds:
            return None
        return {"project": m["ctx"].project, "app": m["ctx"].detail.get("app"), "class": m["ctx"].detail.get("class"),
                "since": m["since"]}

    def write_state(self, now: int, ctx: Context | None):
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        state = {
            "ts": now, "pid": os.getpid(), "idle": self.idle, "tick": self.cfg.tick,
            "active": None if ctx is None else
            {"project": ctx.project, "source": ctx.source, "key": ctx.key, "detail": ctx.detail},
            "meeting": self.meeting_info(now),
        }
        tmp = RUNTIME_DIR / "state.json.tmp"
        tmp.write_text(json.dumps(state))
        tmp.replace(RUNTIME_DIR / "state.json")

    async def idle_watch(self):
        exe = shutil.which("swayidle")
        if not exe:
            log.warning("swayidle not installed — idle detection disabled (pacman -S swayidle)")
            return
        while True:
            proc = await asyncio.create_subprocess_exec(
                exe, "-w", "timeout", str(self.cfg.idle_timeout), "echo idle", "resume", "echo active",
                stdout=asyncio.subprocess.PIPE)
            assert proc.stdout
            async for raw in proc.stdout:
                line = raw.decode().strip()
                if line == "idle":
                    self.on_idle(int(time.time()))
                elif line == "active":
                    self.on_active()
            await proc.wait()
            log.warning("swayidle exited (%s); restarting", proc.returncode)
            self.idle = False
            await asyncio.sleep(5)


async def run():
    logging.basicConfig(level=os.environ.get("UURTJIE_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = config_mod.load()
    if not cfg.projects:
        log.warning("no projects configured in %s — nothing will be tracked", config_mod.CONFIG_PATH)
    audio = AudioProbe()
    if cfg.meeting_detect and not audio.available:
        log.warning("pactl not found: calls won't be detected without keyboard input")
    d = Daemon(cfg, db.connect(), Hyprland(), Herdr(cfg.herdr_socket), audio if cfg.meeting_detect else None)
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGHUP, d.reload)
    asyncio.create_task(d.idle_watch())
    log.info("uurtjie started: %d projects, tick=%ss idle=%ss", len(cfg.projects), cfg.tick, cfg.idle_timeout)
    last_backup_check = 0.0
    while True:
        try:
            await d.tick()
        except Exception:
            log.exception("tick failed")
        if time.monotonic() - last_backup_check > 600:      # daily backup, checked every 10 min
            last_backup_check = time.monotonic()
            try:
                if dest := db.backup_if_due(d.con, d.cfg.backup_dir, d.cfg.backup_keep):
                    log.info("backup written: %s", dest)
            except Exception:
                log.exception("backup failed")
        await asyncio.sleep(d.cfg.tick)
