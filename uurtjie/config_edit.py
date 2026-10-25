"""Change settings in config.toml from the command line, keeping your comments and layout.

There is no TOML writer in the standard library, so this edits the text itself: it finds the right
table (top level, [invoice], [invoice.from] or one [[projects]] block by id), replaces or inserts
the one line, then refuses to save anything that no longer parses, or where the value didn't land
where intended. The previous file is kept as config.toml.bak.
"""
from __future__ import annotations

import copy
import json
import re
import tempfile
import tomllib
from pathlib import Path

from . import config as config_mod
from . import storage


class ConfigError(Exception):
    pass


# table -> {key: type}.  "project" keys live in one [[projects]] block.
SCHEMA: dict[str, dict[str, str]] = {
    "": {"default_project": "str", "rounding_minutes": "int", "sleep_minutes": "float", "chunk_minutes": "int",
         "bridge_minutes": "float", "git_author": "str", "backup_dir": "str", "backup_keep": "int",
         "meeting_detect": "bool", "show_money_in_bar": "bool", "meeting_min_seconds": "int", "meeting_grace_seconds": "int",
         "meeting_ignore": "list", "terminal_classes": "list"},
    "invoice": {"vat_rate": "float", "payment_terms_days": "int", "number_format": "str", "dir": "str"},
    "invoice.from": {"name": "str", "email": "str", "phone": "str", "vat_number": "str", "reg_number": "str",
                     "footer": "str", "address": "list", "bank": "list"},
    "project": {"name": "str", "client": "str", "rate": "float", "currency": "str", "human_factor": "float",
                "client_address": "list", "client_vat_number": "str", "client_reference": "str",
                "require_agent": "bool"},
}


def valid_keys() -> list[str]:
    out = list(SCHEMA[""]) + [f"invoice.{k}" for k in SCHEMA["invoice"]] + \
        [f"invoice.from.{k}" for k in SCHEMA["invoice.from"]] + list(SCHEMA["project"])
    return out


def resolve(key: str) -> tuple[str, str, str]:
    """'invoice.from.bank' -> ('invoice.from', 'bank', 'list'); 'rate' -> ('project', 'rate', 'float')."""
    for prefix, table in (("invoice.from.", "invoice.from"), ("invoice.", "invoice")):
        if key.startswith(prefix):
            name = key[len(prefix):]
            if name in SCHEMA[table]:
                return table, name, SCHEMA[table][name]
            break
    else:
        for table in ("", "project"):
            if key in SCHEMA[table]:
                return table, key, SCHEMA[table][key]
    raise ConfigError(f"unknown setting {key!r}. Valid settings: " + ", ".join(valid_keys()))


def coerce(kind: str, values: list[str]):
    if kind == "list":
        lines = [ln.strip() for v in values for ln in v.splitlines()]
        return [ln for ln in lines if ln]
    if len(values) != 1:
        raise ConfigError("give exactly one value (quote it if it has spaces)")
    v = values[0].strip()
    try:
        if kind == "int":
            return int(v)
        if kind == "float":
            return float(v)
        if kind == "bool":
            if v.lower() in ("true", "yes", "on", "1"):
                return True
            if v.lower() in ("false", "no", "off", "0"):
                return False
            raise ValueError
    except ValueError:
        raise ConfigError(f"{v!r} is not a valid {kind}") from None
    return v


def toml_literal(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v == int(v) else repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False)       # JSON string escapes are valid TOML basic strings


HEADER = re.compile(r"^\s*(\[\[?)\s*([^\]\[]+?)\s*\]\]?\s*(?:#.*)?$")


def _headers(lines: list[str]) -> list[tuple[int, bool, str]]:
    heads = []
    i = 0
    while i < len(lines):
        if m := HEADER.match(lines[i]):
            heads.append((i, m.group(1) == "[[", m.group(2)))
        elif re.match(r"^\s*[A-Za-z0-9_\-\"']+\s*=", lines[i]):
            i = _value_end(lines, i)
            continue
        i += 1
    return heads


def _value_end(lines: list[str], i: int) -> int:
    """Use TOML's parser to delimit a complete assignment, including every string form."""
    for end in range(i + 1, len(lines) + 1):
        try:
            tomllib.loads("\n".join(lines[i:end]))
        except tomllib.TOMLDecodeError:
            continue
        return end
    raise ConfigError("couldn't safely delimit a setting; edit the file by hand")


def _range(lines: list[str], table: str, project: str | None) -> tuple[int, int, int]:
    """(first body line, end, header line or -1) of the target section."""
    heads = _headers(lines)
    ends = [h[0] for h in heads] + [len(lines)]
    if table == "":
        return 0, ends[0], -1
    if table == "project":
        for n, (i, is_arr, name) in enumerate(heads):
            if is_arr and name == "projects":
                body = lines[i + 1:ends[n + 1]]
                if tomllib.loads("\n".join(body)).get("id") == project:
                    return i + 1, ends[n + 1], i
        raise ConfigError(f"no project with id {project!r} in the config")
    for n, (i, is_arr, name) in enumerate(heads):
        if not is_arr and name == table:
            return i + 1, ends[n + 1], i
    return -1, -1, -1


def edit_text(text: str, table: str, name: str, literal: str, project: str | None = None) -> str:
    lines = text.split("\n")
    start, end, header = _range(lines, table, project)
    new_line = f"{name} = {literal}"
    if start == -1:                                    # table doesn't exist yet: add it at the end
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["", f"[{table}]", new_line, ""]
        return "\n".join(lines)
    last_assign = None
    i = start
    while i < end:
        m = re.match(rf"^\s*{re.escape(name)}\s*=", lines[i])
        stop = _value_end(lines, i) if re.match(r"^\s*[A-Za-z0-9_\-\"']+\s*=", lines[i]) else i + 1
        if m:                                          # replace the whole (maybe multi-line) assignment
            lines[i:stop] = [new_line]
            return "\n".join(lines)
        if re.match(r"^\s*[A-Za-z0-9_\-\"']+\s*=", lines[i]):
            last_assign = stop
        i = stop
    at = last_assign if last_assign is not None else (header + 1 if header >= 0 else end)
    lines.insert(at, new_line)
    if header == -1 and last_assign is None:
        lines.insert(at + 1, "")
    return "\n".join(lines)


def _lookup(parsed: dict, table: str, name: str, project: str | None):
    node = parsed
    if table == "project":
        node = next((p for p in parsed.get("projects", []) if p.get("id") == project), {})
    elif table:
        for part in table.split("."):
            node = node.get(part, {})
    return node.get(name)


def update(path: Path, key: str, values: list[str], project: str | None = None) -> tuple[str, str]:
    """Set one setting. Returns (old_value, new_value_as_toml); raises ConfigError."""
    (_, old, new), = update_many(path, [(key, values, project)])
    return old, new


def update_many(path: Path, changes: list[tuple[str, list[str], str | None]]) -> list[tuple[str, str, str]]:
    """Apply several (key, values, project) changes with ONE validated write. Returns (key, old, new) per change."""
    text = path.read_text() if path.exists() else ""
    try:
        old_parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"the current config doesn't parse ({e}); fix it first (`uurtjie config edit`)") from None
    done, expect = [], []
    expected = copy.deepcopy(old_parsed)
    for key, values, project in changes:
        table, name, kind = resolve(key)
        value = coerce(kind, values)
        if table == "project" and not project:
            raise ConfigError(f"{key!r} belongs to a project: pass --project")
        old = _lookup(old_parsed, table, name, project)
        text = edit_text(text, table, name, toml_literal(value), project)
        done.append((key, "" if old is None else str(old), toml_literal(value)))
        expect.append((table, name, project, value))
        node = expected
        if table == "project":
            node = next(p for p in node["projects"] if p.get("id") == project)
        elif table:
            for part in table.split("."):
                node = node.setdefault(part, {})
        node[name] = value
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"refusing to save: the edit would break the config ({e})") from None
    for table, name, project, value in expect:
        if _lookup(parsed, table, name, project) != value:
            raise ConfigError("refusing to save: a value didn't land where intended; edit the file by hand")
    if parsed != expected:
        raise ConfigError("refusing to save: the edit would change unrelated settings")
    with tempfile.TemporaryDirectory() as tmp:         # the app itself must still load it
        probe = Path(tmp) / "config.toml"
        probe.write_text(text)
        try:
            config_mod.load(probe)
        except Exception as e:
            raise ConfigError(f"refusing to save: the app couldn't load the result ({e})") from None
    write(path, text)
    return done


def write(path: Path, text: str) -> None:
    """Keep the previous file as config.toml.bak, then replace atomically."""
    storage.private_dir(path.parent)
    if path.exists():
        storage.atomic_write(path.with_name(path.name + ".bak"), path.read_text())
    storage.atomic_write(path, text)
