"""OKF v0.2 validator.

Severity model mirrors the spec's own permissiveness (§11):
  ERROR    – breaks one of the three conformance rules. Bundle is not conformant.
  WARNING  – violates a SHOULD/MUST inside an optional family (§5–§10) or a
             v0.1 construct that v0.2 supersedes (§13.1).
  INFO     – quality and graph-health signals. The spec says consumers MUST NOT
             reject for these (broken links, missing index, unknown types...).
Every finding has a stable `okf/<code>` rule ID so reports are diffable.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from posixpath import dirname, join, normpath

from .frontmatter import has_explicit_offset, parse, parse_dt

RESERVED = {"index.md", "log.md"}
ACTOR_RE = re.compile(r"^(human:[^\s]+|process:[^\s]+|[^\s/:]+/[^\s]+)$")
LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\)")
FOOT_REF = re.compile(r"\[\^([^\]\s]+)\](?!:)")
FOOT_DEF = re.compile(r"^\[\^([^\]\s]+)\]:", re.M)
FENCE_RE = re.compile(r"(^|\n)(```|~~~).*?(\n\2[^\n]*)", re.S)
DATE_HEAD = re.compile(r"^##\s+(.*?)\s*$", re.M)
STATUS_VOCAB = {"draft", "stable", "deprecated"}
TIMESTAMP_KEYS_TOP = ("stale_after",)

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass
class Finding:
    severity: str
    rule: str
    path: str
    message: str
    spec: str = ""


@dataclass
class ConceptInfo:
    path: str
    id: str
    type: str = ""
    title: str = ""
    description: str = ""
    status: str = "stable"
    trust: str = "unverified"
    stale: bool = False
    links_out: list[str] = field(default_factory=list)
    broken_out: list[str] = field(default_factory=list)


@dataclass
class Report:
    findings: list[Finding]
    concepts: dict[str, ConceptInfo]
    edges: list[tuple[str, str]]
    stats: dict
    okf_version: str | None = None

    @property
    def conformant(self) -> bool:
        return not any(f.severity == ERROR for f in self.findings)

    def counts(self) -> Counter:
        return Counter(f.severity for f in self.findings)

    def to_dict(self) -> dict:
        return {"conformant": self.conformant, "okf_version": self.okf_version,
                "counts": dict(self.counts()), "stats": self.stats,
                "findings": [asdict(f) for f in self.findings]}

    def to_markdown(self) -> str:
        c = self.counts()
        lines = [f"# OKF validation report\n",
                 f"* Conformant (§11): **{'yes' if self.conformant else 'no'}**",
                 f"* Declared okf_version: {self.okf_version or 'not declared'}",
                 f"* Errors: {c.get(ERROR, 0)}, warnings: {c.get(WARNING, 0)}, info: {c.get(INFO, 0)}",
                 f"* Concepts: {self.stats.get('concepts', 0)}, links: {self.stats.get('edges', 0)}\n"]
        for sev in (ERROR, WARNING, INFO):
            group = [f for f in self.findings if f.severity == sev]
            if group:
                lines.append(f"## {sev.title()}s\n")
                lines += [f"* `{f.rule}` {f.path}: {f.message}" + (f" ({f.spec})" if f.spec else "") for f in group]
                lines.append("")
        return "\n".join(lines)


def _strip_code(body: str) -> str:
    body = FENCE_RE.sub("\n", body)
    body = re.sub(r"(?m)^(?: {4}|\t).*$", "", body)        # indented code blocks
    return re.sub(r"`[^`\n]*`", "", body)


def resolve_link(src_path: str, target: str) -> tuple[str, str | None]:
    """Return (kind, resolved_path). kind: external|anchor|internal."""
    t = target.split("#", 1)[0].split("?", 1)[0]
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", target):
        return "external", None
    if not t:
        return "anchor", None
    base = "" if t.startswith("/") else dirname(src_path)
    p = normpath(join(base, t.lstrip("/")))
    if p.startswith(".."):
        return "internal", p
    if t.endswith("/") or not p.endswith(".md"):
        p = (p.rstrip("/") + "/index.md") if not p.endswith(".md") else p
    return "internal", p


def _actor_ok(v) -> bool:
    return isinstance(v, str) and bool(ACTOR_RE.match(v.strip()))


def _show(v) -> str:
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _check_ts(findings, path, label, value):
    if value is None:
        return
    if parse_dt(value) is None:
        findings.append(Finding(WARNING, "okf/invalid-datetime", path, f"`{label}` is not an ISO 8601 datetime: {_show(value)}", "§5"))
    elif not has_explicit_offset(value):
        findings.append(Finding(WARNING, "okf/datetime-no-offset", path,
                                f"`{label}` lacks an explicit UTC offset (e.g. 2026-06-30T14:00:00Z): {_show(value)}", "§5"))


def validate(files: dict[str, str], now: _dt.datetime | None = None) -> Report:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    F: list[Finding] = []
    md_files = {p: t for p, t in files.items() if p.lower().endswith(".md")}
    concepts: dict[str, ConceptInfo] = {}
    sources_by_concept: dict[str, list] = {}
    has_sources: dict[str, bool] = {}
    okf_version = None

    if not md_files:
        F.append(Finding(ERROR, "okf/empty-bundle", "/", "No markdown files found in the bundle", "§3"))

    # ---- per-file checks ---------------------------------------------------
    for path, text in sorted(md_files.items()):
        name = path.rsplit("/", 1)[-1]
        if text == "\x00NON-UTF8\x00":
            F.append(Finding(ERROR, "okf/not-utf8", path, "File is not valid UTF-8", "§4"))
            continue
        doc = parse(text)
        if doc.bom:
            F.append(Finding(INFO, "okf/bom", path, "File starts with a UTF-8 byte-order mark; some parsers will miss the frontmatter"))

        if name == "index.md":
            is_root = "/" not in path
            if doc.has_frontmatter:
                keys = set((doc.frontmatter or {}).keys())
                if not is_root:
                    F.append(Finding(ERROR, "okf/index-frontmatter", path, "Index files contain no frontmatter (only the bundle-root index may carry `okf_version`)", "§8"))
                elif keys - {"okf_version"}:
                    F.append(Finding(ERROR, "okf/index-frontmatter", path, f"Root index frontmatter may only carry `okf_version`; found {sorted(keys - {'okf_version'})}", "§8, §12"))
                if is_root and doc.frontmatter and "okf_version" in doc.frontmatter:
                    okf_version = str(doc.frontmatter["okf_version"])
                    if not isinstance(doc.frontmatter["okf_version"], str):
                        F.append(Finding(INFO, "okf/version-not-string", path, "Quote okf_version (\"0.2\") so YAML keeps it a string", "§12"))
                    if okf_version not in ("0.1", "0.2"):
                        F.append(Finding(INFO, "okf/version-unknown", path, f"okf_version {okf_version} is not known to this validator; consumers should still try best-effort", "§12"))
            entries = re.findall(r"^\s*[*\-+]\s+(.*)$", doc.body, re.M)
            for e in entries:
                if not LINK_RE.search(e):
                    F.append(Finding(WARNING, "okf/index-entry-no-link", path, f"Index entry has no link: {e[:60]!r}", "§8"))
                elif not re.search(r"\)\s+[-–—:]\s+\S", e):
                    F.append(Finding(INFO, "okf/index-entry-no-description", path, f"Index entry lacks a description: {e[:60]!r}", "§8"))
            if not re.search(r"^#\s", doc.body, re.M) and entries:
                F.append(Finding(INFO, "okf/index-no-heading", path, "Index entries should be grouped under section headings", "§8"))
            for _, tgt in LINK_RE.findall(_strip_code(doc.body)):
                kind, rp = resolve_link(path, tgt)
                if kind == "internal" and rp not in md_files:
                    F.append(Finding(WARNING, "okf/index-dead-entry", path, f"Index lists `{tgt}`, which is not in the bundle", "§8"))
            continue

        if name == "log.md":
            if doc.has_frontmatter:
                F.append(Finding(ERROR, "okf/log-frontmatter", path, "log.md is a reserved file, not a concept; it should not carry frontmatter", "§3.1, §9"))
            dates = []
            for h in DATE_HEAD.findall(doc.body):
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", h):
                    F.append(Finding(ERROR, "okf/log-date-heading", path, f"Log date heading must be ISO 8601 YYYY-MM-DD, found {h!r}", "§9"))
                else:
                    dates.append(h)
            if dates and dates != sorted(dates, reverse=True):
                F.append(Finding(WARNING, "okf/log-order", path, "Log entries should be newest first", "§9"))
            if not dates:
                F.append(Finding(INFO, "okf/log-empty", path, "log.md has no dated entries"))
            continue

        # ---- concept documents ----
        cid = path[:-3]
        ci = ConceptInfo(path=path, id=cid)
        concepts[path] = ci
        if not doc.has_frontmatter:
            F.append(Finding(ERROR, "okf/no-frontmatter", path, "Concept has no YAML frontmatter block delimited by `---`", "§11 rule 1"))
            continue
        if doc.error:
            F.append(Finding(ERROR, "okf/frontmatter-parse", path, f"Frontmatter is not parseable YAML: {doc.error}", "§11 rule 1"))
            continue
        if doc.not_mapping:
            F.append(Finding(ERROR, "okf/frontmatter-not-mapping", path, "Frontmatter must be a key/value mapping", "§4.1"))
            continue
        fm = doc.frontmatter or {}
        t = fm.get("type")
        if t is None or (isinstance(t, str) and not t.strip()):
            F.append(Finding(ERROR, "okf/missing-type", path, "Frontmatter has no non-empty `type`", "§11 rule 2"))
        elif not isinstance(t, str):
            F.append(Finding(WARNING, "okf/type-not-string", path, f"`type` should be a short string, got {type(t).__name__}", "§4.1"))
        ci.type = str(t or "")
        ci.title = str(fm.get("title") or cid.rsplit("/", 1)[-1].replace("-", " "))
        ci.description = str(fm.get("description") or "")

        # recommended fields
        if not fm.get("title"):
            F.append(Finding(INFO, "okf/missing-title", path, "No `title`; consumers will derive one from the filename", "§4.1"))
        if not fm.get("description"):
            F.append(Finding(INFO, "okf/missing-description", path, "No `description`; index generators and previews rely on it", "§4.1"))
        elif len(re.findall(r"[.!?](\s|$)", ci.description.strip())) > 1 or len(ci.description) > 300:
            F.append(Finding(INFO, "okf/description-not-one-sentence", path, "`description` should be a single sentence", "§4.1"))
        if "tags" in fm and not (isinstance(fm["tags"], list) and all(isinstance(x, str) for x in fm["tags"])):
            F.append(Finding(WARNING, "okf/tags-not-list", path, "`tags` should be a YAML list of short strings", "§4.1"))

        # v0.1 legacy (§13.1)
        if "timestamp" in fm:
            F.append(Finding(WARNING, "okf/legacy-timestamp", path, "`timestamp` is superseded by `generated.at` in v0.2", "§13.1"))
            if "generated" not in fm:
                _check_ts(F, path, "timestamp", fm["timestamp"])
        if re.search(r"^#\s+Citations\s*$", doc.body, re.M):
            F.append(Finding(WARNING, "okf/legacy-citations", path, "Body `# Citations` list is superseded by the `sources` frontmatter + keyed footnotes", "§13.1"))

        # trust: generated (§5.2)
        g = fm.get("generated")
        if g is not None:
            if not isinstance(g, dict):
                F.append(Finding(WARNING, "okf/generated-shape", path, "`generated` should be a mapping `{ by, at }`", "§5.2"))
            else:
                if not g.get("by"):
                    F.append(Finding(WARNING, "okf/generated-missing-by", path, "`generated.by` is REQUIRED within `generated`", "§5.2"))
                elif not _actor_ok(g["by"]):
                    F.append(Finding(WARNING, "okf/actor-format", path, f"`generated.by` {g['by']!r} should be `<producer>/<version>`, `human:<id>` or `process:<id>`", "§7"))
                _check_ts(F, path, "generated.at", g.get("at"))

        # trust: verified (§5.2/5.3)
        v = fm.get("verified")
        if v is not None:
            entries = [v] if isinstance(v, dict) else v if isinstance(v, list) else None
            if entries is None:
                F.append(Finding(WARNING, "okf/verified-shape", path, "`verified` should be a list of `{ by, at }` entries", "§5.2"))
                entries = []
            humans = machines = 0
            for e in entries:
                if not isinstance(e, dict) or not e.get("by"):
                    F.append(Finding(WARNING, "okf/verified-entry", path, "Each `verified` entry needs `by` (actor) and `at`", "§5.2"))
                    continue
                if not _actor_ok(e["by"]):
                    F.append(Finding(WARNING, "okf/actor-format", path, f"`verified.by` {e['by']!r} does not follow the actor convention", "§7"))
                if not e.get("at"):
                    F.append(Finding(WARNING, "okf/verified-entry", path, "`verified` entry is missing `at`", "§5.2"))
                _check_ts(F, path, "verified.at", e.get("at"))
                if str(e["by"]).startswith("human:"):
                    humans += 1
                else:
                    machines += 1
            ci.trust = "human-reviewed" if humans else ("machine-confirmed" if machines else "unverified")
            gen_by = g.get("by") if isinstance(g, dict) else None
            if isinstance(gen_by, str) and not gen_by.startswith("human:") and humans == 0 and machines and \
                    all(isinstance(e, dict) and e.get("by") == gen_by for e in entries):
                F.append(Finding(INFO, "okf/self-verified", path, "Only verifier is the same agent that generated the content", "§5.2"))

        # lifecycle (§5.4/5.5)
        st = fm.get("status")
        if st is not None:
            ci.status = str(st)
            if str(st) not in STATUS_VOCAB:
                F.append(Finding(INFO, "okf/status-nonstandard", path, f"status {st!r} is outside draft|stable|deprecated", "§5.4"))
            if str(st) == "deprecated":
                F.append(Finding(INFO, "okf/deprecated", path, "Concept is deprecated (kept for links/history)", "§5.4"))
        sa = fm.get("stale_after")
        if sa is not None:
            _check_ts(F, path, "stale_after", sa)
            d = parse_dt(sa)
            if d is not None:
                d = d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
                if now >= d:
                    ci.stale = True
                    F.append(Finding(INFO, "okf/stale", path, f"Stale since {_show(sa)} — trust signal, not an error", "§5.5"))

        # provenance (§5.1)
        srcs = fm.get("sources")
        ids: list[str] = []
        if srcs is not None:
            if not isinstance(srcs, list):
                F.append(Finding(WARNING, "okf/sources-shape", path, "`sources` should be a list of entries", "§5.1"))
                srcs = []
            for s in srcs:
                if not isinstance(s, dict) or not s.get("resource"):
                    F.append(Finding(WARNING, "okf/source-missing-resource", path, "Each `sources` entry REQUIRES `resource`", "§5.1"))
                    continue
                if s.get("id"):
                    ids.append(str(s["id"]))
                if "usage_count" in s:
                    if not isinstance(s["usage_count"], int) or s["usage_count"] < 0:
                        F.append(Finding(WARNING, "okf/usage-count", path, "`usage_count` should be a non-negative integer", "§5.1"))
                    if "usage_window" not in fm and "usage_window" not in s:
                        F.append(Finding(WARNING, "okf/usage-count-no-window", path, "`usage_count` needs a `usage_window { from, to }`", "§5.1"))
                if "last_modified" in s:
                    _check_ts(F, path, "sources.last_modified", s["last_modified"])
                if "author" in s and not re.match(r"^[a-z]+:\S+$|^\S+/\S+$", str(s["author"])):
                    F.append(Finding(INFO, "okf/source-author-format", path, f"Source author {s['author']!r} is not in actor form (e.g. human:<id>, team:<id>)", "§5.1, §7"))
            dup = [k for k, n in Counter(ids).items() if n > 1]
            if dup:
                F.append(Finding(WARNING, "okf/source-duplicate-id", path, f"Duplicate source ids: {dup}", "§5.1"))
        uw = fm.get("usage_window")
        if uw is not None:
            if not isinstance(uw, dict) or "from" not in uw or "to" not in uw:
                F.append(Finding(WARNING, "okf/usage-window-shape", path, "`usage_window` should be `{ from, to }`", "§5.1"))
            else:
                _check_ts(F, path, "usage_window.from", uw["from"])
                _check_ts(F, path, "usage_window.to", uw["to"])
        sources_by_concept[path] = ids
        has_sources[path] = bool(srcs)

        # per-claim attribution (§5.1)
        body_nc = _strip_code(doc.body)
        refs = set(FOOT_REF.findall(body_nc))
        defs = set(FOOT_DEF.findall(body_nc))
        for r in sorted(refs - set(ids)):
            if ids or srcs:
                F.append(Finding(WARNING, "okf/footnote-unknown-source", path, f"Footnote [^{r}] does not match any `sources[].id`", "§5.1"))
        for r in sorted(refs - defs):
            F.append(Finding(INFO, "okf/footnote-undefined", path, f"Footnote [^{r}] has no definition line"))
        if srcs and not ids and refs:
            F.append(Finding(INFO, "okf/source-no-id", path, "Sources cited in the body SHOULD carry an `id`", "§5.1"))

        # attested computation (§10)
        if ci.type == "Attested Computation":
            if not fm.get("runtime"):
                F.append(Finding(WARNING, "okf/ac-missing-runtime", path, "`runtime` is REQUIRED for Attested Computation", "§10.2"))
            params = fm.get("parameters", [])
            if params and not (isinstance(params, list) and all(isinstance(p, dict) and {"name", "type"} <= set(p) for p in params)):
                F.append(Finding(WARNING, "okf/ac-parameters", path, "Each parameter should be `{ name, type, required }`", "§10.2"))
            has_fence = bool(re.search(r"^#\s+Computation\s*$", doc.body, re.M)) and \
                bool(re.search(r"```|~~~|\n(?: {4}|\t)\S", doc.body.split("# Computation", 1)[-1]))
            if not fm.get("computation") and not has_fence:
                F.append(Finding(WARNING, "okf/ac-no-computation", path, "Provide the computation inline under `# Computation` or via `computation:` path", "§10.3"))
            if fm.get("computation") and has_fence:
                F.append(Finding(INFO, "okf/ac-double-computation", path, "Both `computation:` and an inline fence present; the file path wins by convention", "§10.3"))
            ex = fm.get("executor")
            if ex is not None and not (isinstance(ex, dict) and ex.get("resource")):
                F.append(Finding(WARNING, "okf/ac-executor", path, "`executor` should name a `resource` and a `receipt` list", "§10.2"))
            if isinstance(ex, dict) and "receipt" in ex and not isinstance(ex["receipt"], list):
                F.append(Finding(WARNING, "okf/ac-executor", path, "`executor.receipt` should be a list of field names", "§10.2"))
            at = fm.get("attester")
            if at is not None and not (isinstance(at, dict) and at.get("resource")):
                F.append(Finding(WARNING, "okf/ac-attester", path, "`attester` should name a deterministic `resource`", "§10.2"))

        # links (§6) — broken links are tolerated, reported as INFO
        for _, tgt in LINK_RE.findall(body_nc):
            kind, rp = resolve_link(path, tgt)
            if kind != "internal":
                continue
            if rp in md_files and rp.rsplit("/", 1)[-1] not in RESERVED:
                ci.links_out.append(rp)
            elif rp not in md_files:
                ci.broken_out.append(tgt)

    # ---- bundle-level checks ----------------------------------------------
    edges = [(c.path, t) for c in concepts.values() for t in dict.fromkeys(c.links_out) if t != c.path]
    inbound = Counter(t for _, t in edges)
    for c in concepts.values():
        if c.broken_out:
            F.append(Finding(INFO, "okf/broken-link", c.path, f"{len(c.broken_out)} link(s) to concepts not in the bundle (tolerated): {', '.join(c.broken_out[:3])}", "§6.1"))
        if not c.links_out and not inbound.get(c.path):
            F.append(Finding(INFO, "okf/orphan", c.path, "No links in or out — an island in the graph"))
    lower = Counter(p.lower() for p in md_files)
    for p, n in lower.items():
        if n > 1:
            F.append(Finding(WARNING, "okf/case-collision", p, "Concept IDs differ only by case; breaks on case-insensitive filesystems", "§2"))
    dirs = {p.rsplit("/", 1)[0] if "/" in p else "" for p in concepts}
    for d in sorted(dirs):
        idx = f"{d}/index.md" if d else "index.md"
        if idx not in md_files:
            F.append(Finding(INFO, "okf/missing-index", idx, "Directory has no index.md (consumers may synthesize one)", "§8"))
        else:
            listed = {resolve_link(idx, t)[1] for _, t in LINK_RE.findall(md_files[idx])}
            missing = [p for p in concepts if (p.rsplit("/", 1)[0] if "/" in p else "") == d and p not in listed]
            if missing:
                F.append(Finding(INFO, "okf/index-incomplete", idx, f"{len(missing)} concept(s) in this directory are not listed: {', '.join(m.rsplit('/', 1)[-1] for m in missing[:4])}", "§8"))
    titles = Counter(c.title.lower() for c in concepts.values() if c.title)
    for t, n in titles.items():
        if n > 1:
            F.append(Finding(INFO, "okf/duplicate-title", "/", f"{n} concepts share the title {t!r} — possible duplicates to merge"))
    if okf_version is None and "index.md" in md_files:
        F.append(Finding(INFO, "okf/version-undeclared", "index.md", 'Declare `okf_version: "0.2"` in the root index frontmatter', "§12"))
    if "index.md" not in md_files and md_files:
        F.append(Finding(INFO, "okf/missing-root-index", "index.md", "No bundle-root index.md for progressive disclosure", "§8"))

    n = max(1, len(concepts))
    tiers = Counter(c.trust for c in concepts.values())
    stats = {
        "concepts": len(concepts), "edges": len(edges), "files": len(files),
        "types": dict(Counter(c.type or "(none)" for c in concepts.values()).most_common()),
        "trust_tiers": dict(tiers), "status": dict(Counter(c.status for c in concepts.values())),
        "stale": sum(c.stale for c in concepts.values()),
        "pct_description": round(100 * sum(bool(c.description) for c in concepts.values()) / n),
        "pct_sources": round(100 * sum(has_sources.values()) / n),
        "orphans": sum(1 for c in concepts.values() if not c.links_out and not inbound.get(c.path)),
        "broken_links": sum(len(c.broken_out) for c in concepts.values()),
    }
    order = {ERROR: 0, WARNING: 1, INFO: 2}
    F.sort(key=lambda f: (order[f.severity], f.rule, f.path))
    return Report(F, concepts, edges, stats, okf_version)
