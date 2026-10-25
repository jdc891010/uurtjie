"""Small git helpers: which repo/branch a cwd is on, and commits made in a time window."""
from __future__ import annotations

import subprocess
import time

_cache: dict[str, tuple[float, dict | None]] = {}


def _git(cwd: str, *args: str, timeout: float = 2.0) -> str | None:
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def repo_info(cwd: str | None, ttl: float = 60) -> dict | None:
    """{'repo': toplevel path, 'branch': name} for cwd, cached for ttl seconds."""
    if not cwd:
        return None
    hit = _cache.get(cwd)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    top = _git(cwd, "rev-parse", "--show-toplevel")
    info = None
    if top:
        info = {"repo": top, "branch": _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "?"}
    _cache[cwd] = (time.monotonic(), info)
    return info


def default_author(repo: str) -> str | None:
    return _git(repo, "config", "user.email")


def commits(repo: str, since: int, until: int, author: str | None = None) -> list[dict]:
    """Commits on any branch committed in [since, until]: sha, ts, branch-ish refs, subject."""
    args = ["log", "--all", "--no-merges", f"--since=@{since}", f"--until=@{until}",
            "--format=%H%x1f%ct%x1f%D%x1f%s"]
    author = author or default_author(repo)
    if author:
        args.append(f"--author={author}")
    out = _git(repo, *args, timeout=5)
    result = []
    for line in (out or "").splitlines():
        sha, ts, refs, subject = (line.split("\x1f") + ["", "", "", ""])[:4]
        if sha and since <= int(ts) <= until:
            result.append({"sha": sha[:10], "ts": int(ts), "refs": refs, "subject": subject})
    return sorted(result, key=lambda c: c["ts"])
