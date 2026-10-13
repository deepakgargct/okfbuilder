"""YAML frontmatter parsing and OKF-style serialization.

OKF concept = UTF-8 markdown with a YAML block delimited by `---` lines at the
very top of the file, followed by a free-form markdown body (SPEC §4).
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


@dataclass
class ParsedDoc:
    has_frontmatter: bool
    frontmatter: dict | None
    body: str
    raw_frontmatter: str = ""
    error: str | None = None          # YAML parse error, if any
    not_mapping: bool = False          # parsed but not a dict
    bom: bool = False
    extra: dict = field(default_factory=dict)


def parse(text: str) -> ParsedDoc:
    bom = text.startswith("\ufeff")
    if bom:
        text = text[1:]
    m = FM_RE.match(text)
    if not m:
        return ParsedDoc(False, None, text, bom=bom)
    raw = m.group(1)
    body = text[m.end():]
    try:
        data = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as e:
        return ParsedDoc(True, None, body, raw, error=str(e).splitlines()[0], bom=bom)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return ParsedDoc(True, None, body, raw, not_mapping=True, bom=bom)
    return ParsedDoc(True, data, body, raw, bom=bom)


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
# Keys written in flow style so files look like the SPEC examples.
_FLOW_KEYS = {"generated", "tags", "usage_window"}
# Preferred key order (OKF §4.1, then §5, then extensions).
_ORDER = [
    "type", "title", "description", "resource", "tags",
    "status", "stale_after", "generated", "verified",
    "runtime", "parameters", "computation", "executor", "attester",
    "sources", "usage_window",
]


def _flow(v: Any) -> str:
    return yaml.safe_dump(v, default_flow_style=True, allow_unicode=True,
                          width=10_000, sort_keys=False).strip()


def _block(k: str, v: Any) -> str:
    return yaml.safe_dump({k: v}, default_flow_style=False, allow_unicode=True,
                          width=10_000, sort_keys=False).rstrip("\n")


def dump(fm: dict) -> str:
    keys = [k for k in _ORDER if k in fm] + [k for k in fm if k not in _ORDER]
    lines: list[str] = []
    for k in keys:
        v = fm[k]
        if v is None or v == [] or v == {}:
            continue
        if k in _FLOW_KEYS:
            lines.append(f"{k}: {_flow(v)}")
        elif k == "verified" and isinstance(v, list):
            lines.append("verified:")
            lines.extend(f"  - {_flow(e)}" for e in v)
        elif k in ("sources", "parameters") and isinstance(v, list):
            lines.append(_block(k, v))
        else:
            lines.append(_block(k, v))
    return "---\n" + "\n".join(lines) + "\n---\n"


def compose(fm: dict, body: str) -> str:
    return dump(fm) + "\n" + body.strip() + "\n"


# --------------------------------------------------------------------------- #
# Datetime helpers (SPEC §5: ISO 8601 with explicit UTC offset)
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_iso(value: Any) -> str | None:
    """Normalise many date shapes into `YYYY-MM-DDTHH:MM:SSZ` (UTC)."""
    if value is None or value == "":
        return None
    d = parse_dt(value)
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value: Any) -> _dt.datetime | None:
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None
    s = value.strip()
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime  # HTTP Last-Modified
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


def has_explicit_offset(value: Any) -> bool:
    """True when the value is a full datetime with an explicit UTC offset."""
    if isinstance(value, _dt.datetime):
        return value.tzinfo is not None
    if isinstance(value, _dt.date):
        return False
    if isinstance(value, str):
        d = parse_dt(value)
        return d is not None and d.tzinfo is not None and "T" in value
    return False
