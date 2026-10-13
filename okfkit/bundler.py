"""Assemble an OKF v0.2 bundle from page analyses.

Layout (directory = pluralised OKF type, entities/concepts grouped):

    bundle/
      index.md            (frontmatter: okf_version "0.2" only)
      log.md
      <type-dir>/index.md, <page>.md ...
      entities/index.md, <entity>.md ...
      concepts/ playbooks/ systems/ ...

Trust policy (follows the spec's intent and Suganthan's argument): every
machine-written concept gets `generated` and `sources`, status `draft`, and
NO `verified` entry unless a named human ticked "reviewed" for that page.
"""
from __future__ import annotations

import datetime as _dt
import io
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .classifier import okf_type_for
from .extractor import PageAnalysis
from .fetcher import normalize_url
from .frontmatter import compose, now_iso

TOOL_VERSION = "1.0"
_IRREGULAR = {"person": "people", "entity": "entities", "policy": "policies", "faq": "faqs",
              "glossary-term": "glossary-terms", "product-category": "product-categories",
              "reference": "references", "software": "software", "case-study": "case-studies"}


@dataclass
class BuildOptions:
    title: str = "Knowledge Bundle"
    description: str = "Concepts extracted from website pages."
    vocabulary: str = "page"             # page | brain
    status: str = "draft"
    stale_days: int = 180
    actor: str = f"okf-builder/{TOOL_VERSION}"
    reviewer: str = ""                   # e.g. "human:jane" — used only for reviewed pages
    content_mode: str = "full"           # full | excerpt | none
    include_entities: bool = True
    include_concepts: bool = True
    min_entity_pages: int = 1
    min_entity_salience: float = 0.0


@dataclass
class _Agg:
    name_counts: Counter = field(default_factory=Counter)
    types: Counter = field(default_factory=Counter)
    descriptions: list[str] = field(default_factory=list)
    key_points: list[str] = field(default_factory=list)
    same_as: set = field(default_factory=set)
    mentions: list[str] = field(default_factory=list)      # page concept paths
    related: set = field(default_factory=set)
    salience: float = 0.0
    strong: bool = False                                   # llm / schema.org origin

    @property
    def name(self) -> str:
        return self.name_counts.most_common(1)[0][0]


def slugify(text: str, limit: int = 70) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return (text[:limit].rstrip("-")) or "untitled"


def type_dir(okf_type: str) -> str:
    s = slugify(okf_type)
    if s in _IRREGULAR:
        return _IRREGULAR[s]
    if s.endswith("y") and not s.endswith(("ay", "ey", "oy")):
        return s[:-1] + "ies"
    return s if s.endswith("s") else s + "s"


def norm_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def one_sentence(text: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def _unique(path: str, taken: set) -> str:
    base, n = path[:-3], 2
    while path.lower() in taken:
        path = f"{base}-{n}.md"
        n += 1
    taken.add(path.lower())
    return path


def _page_slug(a: PageAnalysis) -> str:
    p = a.page
    if p.source_kind == "upload":
        return slugify(re.sub(r"\.\w+$", "", p.url.rsplit("/", 1)[-1]))
    path = urlparse(p.url).path.strip("/")
    if not path:
        return "home"
    last = path.split("/")[-1]
    last = re.sub(r"\.(html?|php|aspx?)$", "", last)
    return slugify(last if len(last) > 2 else (p.title or path))


def _demote(markdown: str) -> str:
    out, fence = [], False
    for line in markdown.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and re.match(r"#{1,5}\s", line):
            line = "#" + line
        out.append(line)
    return "\n".join(out)


def _rewrite_links(markdown: str, url_map: dict[str, str]) -> str:
    def sub(m):
        text, url = m.group(1), m.group(2)
        key = normalize_url(url) if url.startswith("http") else None
        return f"[{text}]({url_map[key]})" if key in url_map else m.group(0)
    return re.sub(r"(?<!!)\[([^\]]*)\]\((https?://[^)\s]+)\)", sub, markdown)


def build_bundle(analyses: list[PageAnalysis], opt: BuildOptions) -> dict[str, str]:
    now = now_iso()
    stale = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=opt.stale_days)) \
        .replace(hour=0, minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")
    analyses = [a for a in analyses if a.include and a.page.ok]
    methods = Counter(a.method for a in analyses).most_common(1)
    node_actor = (methods[0][0].replace("llm:", "okf-builder/") if methods and methods[0][0].startswith("llm:")
                  else opt.actor)
    generated = {"by": node_actor, "at": now}
    taken: set[str] = set()
    files: dict[str, str] = {}

    # ---- 1. Assign a concept path to every page ---------------------------
    page_path: dict[int, str] = {}
    url_map: dict[str, str] = {}
    for i, a in enumerate(analyses):
        okf_type = okf_type_for(a.page_type, opt.vocabulary)
        path = _unique(f"{type_dir(okf_type)}/{_page_slug(a)}.md", taken)
        page_path[i] = path
        if a.page.source_kind == "url":
            url_map[normalize_url(a.page.url)] = "/" + path
            if a.page.canonical:
                url_map[normalize_url(a.page.canonical)] = "/" + path

    # ---- 2. Aggregate entities & concepts across pages --------------------
    ents: dict[str, _Agg] = defaultdict(_Agg)
    cons: dict[str, _Agg] = defaultdict(_Agg)
    for i, a in enumerate(analyses):
        if opt.include_entities:
            for e in a.entities:
                g = ents[norm_key(e.name)]
                g.name_counts[e.name] += 1
                g.types[e.entity_type] += 1
                if e.description:
                    g.descriptions.append(e.description)
                g.same_as.update(e.same_as)
                g.related.update(e.related)
                g.salience = max(g.salience, e.salience)
                g.strong |= e.origin in ("llm", "schema.org", "spacy")
                if page_path[i] not in g.mentions:
                    g.mentions.append(page_path[i])
        if opt.include_concepts:
            for c in a.concepts:
                g = cons[norm_key(c.name)]
                g.name_counts[c.name] += 1
                g.types[c.kind] += 1
                if c.definition:
                    g.descriptions.append(c.definition)
                g.key_points.extend(k for k in c.key_points if k not in g.key_points)
                g.related.update(c.related)
                g.strong = True
                if page_path[i] not in g.mentions:
                    g.mentions.append(page_path[i])

    keep_ents = {k: g for k, g in ents.items()
                 if k and (len(g.mentions) >= opt.min_entity_pages or g.strong)
                 and g.salience >= opt.min_entity_salience}
    entity_path = {k: _unique(f"entities/{slugify(g.name)}.md", taken) for k, g in keep_ents.items()}
    kind_type = {"concept": "Concept", "playbook": "Playbook", "system": "System"}
    concept_path = {k: _unique(f"{type_dir(kind_type[g.types.most_common(1)[0][0]])}/{slugify(g.name)}.md", taken)
                    for k, g in cons.items() if k}
    name_to_path = {**{k: "/" + p for k, p in entity_path.items()},
                    **{k: "/" + p for k, p in concept_path.items()}}
    path_title: dict[str, str] = {}
    path_desc: dict[str, str] = {}

    # ---- 3. Page concepts ---------------------------------------------------
    for i, a in enumerate(analyses):
        p, path = a.page, page_path[i]
        okf_type = okf_type_for(a.page_type, opt.vocabulary)
        sid = "page"
        src = {"id": sid, "resource": p.url if p.source_kind == "url" else f"uploaded file {p.url.rsplit('/', 1)[-1]}",
               "title": p.title or path}
        if p.author:
            au = p.author.strip()
            src["author"] = au if re.match(r"^[a-z]+:\S+$|^\S+/\S+$", au) else f"human:{slugify(au, 40)}"
        if p.last_modified:
            src["last_modified"] = p.last_modified
        desc = one_sentence(a.summary or p.description or p.title)
        fm = {"type": okf_type, "title": p.title or _page_slug(a).replace("-", " ").title(),
              "description": desc}
        if p.source_kind == "url":
            fm["resource"] = p.url
        fm.update({"tags": a.tags, "status": opt.status, "stale_after": stale,
                   "generated": {"by": a.method.replace("llm:", "okf-builder/") if a.method.startswith("llm:") else opt.actor,
                                 "at": now}})
        if a.reviewed and opt.reviewer.startswith("human:"):
            fm["verified"] = [{"by": opt.reviewer, "at": now}]
        fm["sources"] = [src]
        fm["page_type"] = a.page_type
        fm["page_type_confidence"] = a.classification.confidence
        if p.lang:
            fm["language"] = p.lang

        body = [f"# Summary\n\n{a.summary or desc}[^{sid}]"]
        ent_lines = [f"* [{keep_ents[norm_key(e.name)].name}]({name_to_path[norm_key(e.name)]}) - {e.entity_type}"
                     for e in a.entities if norm_key(e.name) in keep_ents]
        if ent_lines:
            body.append("# Entities\n\n" + "\n".join(dict.fromkeys(ent_lines)))
        con_lines = [f"* [{cons[norm_key(c.name)].name}]({name_to_path[norm_key(c.name)]}) - {c.kind}"
                     for c in a.concepts if norm_key(c.name) in concept_path]
        if con_lines:
            body.append("# Concepts\n\n" + "\n".join(dict.fromkeys(con_lines)))
        if opt.content_mode != "none":
            content = _rewrite_links(p.markdown, url_map)
            if opt.content_mode == "excerpt" and len(content) > 1500:
                content = content[:1500].rsplit("\n", 1)[0] + "\n\n…"
            body.append("# Content\n\n" + _demote(content))
        body.append(f"[^{sid}]: {src['title']}")
        files[path] = compose(fm, "\n\n".join(body))
        path_title[path], path_desc[path] = fm["title"], desc

    # ---- 4. Entity + concept documents --------------------------------------
    def page_sources(paths: list[str]) -> tuple[list[dict], list[str]]:
        srcs, ids = [], []
        for pp in paths[:10]:
            i = list(page_path.values()).index(pp)
            pg = analyses[i].page
            sid = slugify(pp[:-3].split("/")[-1], 40)
            if sid in ids:
                continue
            ids.append(sid)
            srcs.append({"id": sid, "resource": "/" + pp, "title": pg.title or pp})
        return srcs, ids

    def related_lines(g: _Agg, self_key: str) -> list[str]:
        out = []
        for r in sorted(g.related):
            k = norm_key(r)
            if k != self_key and k in name_to_path:
                out.append(f"* [{r}]({name_to_path[k]})")
        return list(dict.fromkeys(out))

    def write_node(kind_label: str, key: str, g: _Agg, path: str, okf_type: str, extra: dict):
        srcs, ids = page_sources(g.mentions)
        desc_full = max(g.descriptions, key=len) if g.descriptions else \
            f"{g.name} is mentioned in {len(g.mentions)} page(s) of this bundle."
        fm = {"type": okf_type, "title": g.name, "description": one_sentence(desc_full),
              "tags": [slugify(t, 30) for t in list(g.types)[:1]], "status": opt.status,
              "stale_after": stale, "generated": generated, **extra, "sources": srcs}
        foot = f"[^{ids[0]}]" if ids else ""
        body = [f"# Definition\n\n{desc_full}{foot}"]
        if g.key_points:
            body.append("# Key points\n\n" + "\n".join(f"* {k}" for k in g.key_points[:8]))
        body.append("# Mentioned in\n\n" + "\n".join(f"* [{path_title[m]}](/{m}) - {path_desc[m]}" for m in g.mentions))
        rel = related_lines(g, key)
        if rel:
            body.append("# Related\n\n" + "\n".join(rel))
        if g.same_as:
            body.append("# Same as\n\n" + "\n".join(f"* <{u}>" for u in sorted(g.same_as)))
        body.append("\n".join(f"[^{s['id']}]: {s['title']}" for s in srcs))
        files[path] = compose(fm, "\n\n".join(body))
        path_title[path], path_desc[path] = g.name, fm["description"]

    for k, g in keep_ents.items():
        et = g.types.most_common(1)[0][0]
        okf_type = "Entity" if opt.vocabulary == "brain" else (et if et != "Other" else "Entity")
        write_node("entity", k, g, entity_path[k], okf_type, {"entity_type": et})
    for k, path in concept_path.items():
        g = cons[k]
        write_node("concept", k, g, path, kind_type[g.types.most_common(1)[0][0]], {})

    # ---- 5. index.md per directory + root, log.md ---------------------------
    by_dir: dict[str, list[str]] = defaultdict(list)
    for path in files:
        by_dir[path.rsplit("/", 1)[0] if "/" in path else ""].append(path)
    root_lines = []
    for d in sorted(k for k in by_dir if k):
        items = sorted(by_dir[d], key=lambda x: path_title[x].lower())
        label = d.replace("-", " ").title()
        lines = [f"* [{path_title[x]}]({x.rsplit('/', 1)[-1]}) - {path_desc[x]}" for x in items]
        files[f"{d}/index.md"] = f"# {label}\n\n" + "\n".join(lines) + "\n"
        root_lines.append(f"* [{label}]({d}/) - {len(items)} concept(s)")
    files["index.md"] = (compose({"okf_version": "0.2"}, "") .rstrip("\n") + "\n\n"
                         f"# {opt.title}\n\n{opt.description}\n\n# Sections\n\n" + "\n".join(root_lines) + "\n")
    today = now[:10]
    n_pages, n_e, n_c = len(analyses), len(keep_ents), len(concept_path)
    files["log.md"] = (f"# Bundle Update Log\n\n## {today}\n"
                       f"* **Creation**: Generated {n_pages + n_e + n_c} concepts with okf-builder/{TOOL_VERSION}: "
                       f"{n_pages} pages, {n_e} entities, {n_c} concepts/playbooks/systems.\n"
                       f"* **Initialization**: Created directory structure ({', '.join(sorted(k for k in by_dir if k))}).\n")
    return dict(sorted(files.items()))


def zip_bundle(files: dict[str, str], root: str = "okf-bundle") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(f"{root}/{path}", content.encode("utf-8"))
    return buf.getvalue()


def read_zip(data: bytes) -> dict[str, str]:
    """Load a bundle zip into {relative_path: text}; strips a single top folder."""
    out: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n]
        md_names = [n for n in names if n.lower().endswith(".md")]
        tops = {n.split("/", 1)[0] for n in md_names if "/" in n}
        strip = (tops.pop() + "/") if len(tops) == 1 and all("/" in n for n in md_names) else ""
        for n in names:
            raw = z.read(n)
            rel = n[len(strip):] if strip and n.startswith(strip) else n
            try:
                out[rel] = raw.decode("utf-8")
            except UnicodeDecodeError:
                out[rel] = "\x00NON-UTF8\x00"
    return out
