"""Conservative, reversible fixes. Never invents trust data.

* Declares okf_version "0.2" in the root index.
* Regenerates missing / incomplete index.md files from concept frontmatter.
* Adds a `Z` offset to timezone-less datetimes in known timestamp fields.
* Re-sorts log.md date sections newest-first.
It does NOT fabricate `generated.by`, `verified`, or sources.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import defaultdict

from .frontmatter import compose, parse, to_iso

TS_KEYS = ("stale_after",)


def _fix_dt(v):
    if isinstance(v, (_dt.datetime, _dt.date)) or isinstance(v, str):
        if isinstance(v, _dt.datetime) and v.tzinfo:
            return v.isoformat().replace("+00:00", "Z"), False
        iso = to_iso(v)
        if iso and str(v) != iso:
            return iso, True
    return v, False


def apply_fixes(files: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    out = dict(files)
    log: list[str] = []
    concepts: dict[str, dict] = {}

    for path, text in files.items():
        name = path.rsplit("/", 1)[-1]
        if not path.endswith(".md") or name in ("index.md", "log.md"):
            continue
        doc = parse(text)
        if not doc.frontmatter:
            continue
        fm, changed = dict(doc.frontmatter), False
        for k in TS_KEYS:
            if k in fm:
                fm[k], c = _fix_dt(fm[k])
                changed |= c
        if isinstance(fm.get("generated"), dict) and "at" in fm["generated"]:
            fm["generated"] = dict(fm["generated"])
            fm["generated"]["at"], c = _fix_dt(fm["generated"]["at"])
            changed |= c
        v = fm.get("verified")
        if isinstance(v, (list, dict)):
            entries = [v] if isinstance(v, dict) else v
            new = []
            for e in entries:
                if isinstance(e, dict) and "at" in e:
                    e = dict(e)
                    e["at"], c = _fix_dt(e["at"])
                    changed |= c
                new.append(e)
            fm["verified"] = new
        if isinstance(fm.get("sources"), list):
            new = []
            for s in fm["sources"]:
                if isinstance(s, dict) and "last_modified" in s:
                    s = dict(s)
                    s["last_modified"], c = _fix_dt(s["last_modified"])
                    changed |= c
                new.append(s)
            fm["sources"] = new
        if changed:
            out[path] = compose(fm, doc.body)
            log.append(f"{path}: added explicit UTC offsets to timestamps")
        concepts[path] = fm

    # index.md regeneration
    by_dir: dict[str, list[str]] = defaultdict(list)
    for p in concepts:
        by_dir[p.rsplit("/", 1)[0] if "/" in p else ""].append(p)
    for d, items in by_dir.items():
        idx = f"{d}/index.md" if d else "index.md"
        existing = out.get(idx, "")
        listed = set(re.findall(r"\]\(([^)]+)\)", existing))
        missing = [p for p in items if p.rsplit("/", 1)[-1] not in listed and "/" + p not in listed]
        if not missing and existing:
            continue
        lines = [f"* [{concepts[p].get('title') or p.rsplit('/', 1)[-1][:-3]}]({p.rsplit('/', 1)[-1]})"
                 + (f" - {concepts[p]['description']}" if concepts[p].get("description") else "")
                 for p in sorted(missing, key=lambda x: str(concepts[x].get("title") or x).lower())]
        if existing:
            out[idx] = existing.rstrip() + "\n\n# Unlisted concepts\n\n" + "\n".join(lines) + "\n"
            log.append(f"{idx}: listed {len(lines)} missing concept(s)")
        elif d:
            out[idx] = f"# {d.rsplit('/', 1)[-1].replace('-', ' ').title()}\n\n" + "\n".join(lines) + "\n"
            log.append(f"{idx}: created")

    # root index + okf_version
    root = out.get("index.md")
    subdirs = sorted({d.split("/")[0] for d in by_dir if d})
    if root is None:
        body = "# Bundle\n\n" + "\n".join(f"* [{s.replace('-', ' ').title()}]({s}/) - {s} concepts" for s in subdirs)
        out["index.md"] = compose({"okf_version": "0.2"}, body)
        log.append("index.md: created root index with okf_version 0.2")
    else:
        doc = parse(root)
        if not doc.has_frontmatter or "okf_version" not in (doc.frontmatter or {}):
            out["index.md"] = compose({"okf_version": "0.2"}, doc.body)
            log.append('index.md: declared okf_version "0.2"')

    # log ordering
    for path, text in list(out.items()):
        if path.rsplit("/", 1)[-1] != "log.md":
            continue
        parts = re.split(r"(?m)^(?=##\s+\d{4}-\d{2}-\d{2}\s*$)", text)
        head, secs = parts[0], parts[1:]
        if secs:
            srt = sorted(secs, key=lambda s: s.split("\n", 1)[0], reverse=True)
            if srt != secs:
                out[path] = head.rstrip() + "\n\n" + "\n\n".join(s.strip() for s in srt) + "\n"
                log.append(f"{path}: sorted entries newest first")
    return out, log
