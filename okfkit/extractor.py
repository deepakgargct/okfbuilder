"""Extract entities, concepts and tags from a page.

Two engines:
* heuristic — schema.org JSON-LD + spaCy NER (if installed) or a
  capitalised-phrase fallback, plus glossary-style definitions. No API key.
* llm — asks a model for a typed JSON extraction (recommended; this is the
  Karpathy "LLM wiki" ingest step that Marie Haynes' brain uses).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .classifier import PAGE_TYPES, Classification, classify
from .fetcher import Page
from .llm import complete_json

ENTITY_TYPES = ["Organization", "Person", "Product", "Brand", "Place", "Event",
                "Software", "Creative Work", "Standard", "Other"]
JSONLD_ENTITY = {
    "Organization": "Organization", "Corporation": "Organization", "NGO": "Organization",
    "EducationalOrganization": "Organization", "LocalBusiness": "Organization",
    "Person": "Person", "Product": "Product", "Brand": "Brand", "Place": "Place",
    "City": "Place", "Country": "Place", "Event": "Event",
    "SoftwareApplication": "Software", "WebApplication": "Software",
    "Book": "Creative Work", "Movie": "Creative Work", "CreativeWork": "Creative Work",
}
SPACY_MAP = {"ORG": "Organization", "PERSON": "Person", "PRODUCT": "Product", "GPE": "Place",
             "LOC": "Place", "FAC": "Place", "EVENT": "Event", "WORK_OF_ART": "Creative Work",
             "LAW": "Standard"}
STOP = set("""a an the and or but if of to in on at for from by with about as into like through after
over between out against during without before under around among is are was were be been being it its
this that these those you your we our they their he she his her i me my not no yes can will just
than then so such very also more most other some any each all both few many much new how what when where
which who why get got use using used one two three may might must should would could""".split())
CAP_PHRASE = re.compile(r"\b(?:[A-Z][a-zA-Z0-9&'’\-]+(?:\s+(?:of|for|the|de)\s+|\s+)){0,4}[A-Z][a-zA-Z0-9&'’\-]+\b")
ACRONYM = re.compile(r"\b[A-Z]{2,6}s?\b")


@dataclass
class EntityRef:
    name: str
    entity_type: str = "Other"
    description: str = ""
    same_as: list[str] = field(default_factory=list)
    salience: float = 0.5
    related: list[str] = field(default_factory=list)
    origin: str = "heuristic"


@dataclass
class ConceptRef:
    name: str
    kind: str = "concept"            # concept | playbook | system
    definition: str = ""
    key_points: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    origin: str = "heuristic"


@dataclass
class PageAnalysis:
    page: Page
    classification: Classification
    page_type: str
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    entities: list[EntityRef] = field(default_factory=list)
    concepts: list[ConceptRef] = field(default_factory=list)
    method: str = "heuristic"
    reviewed: bool = False
    include: bool = True
    error: str = ""


# --------------------------------------------------------------------------- #
# Heuristic engine
# --------------------------------------------------------------------------- #
_nlp = None


def _spacy():
    global _nlp
    if _nlp is None:
        try:
            import spacy  # type: ignore
            _nlp = spacy.load("en_core_web_sm")
        except Exception:  # noqa: BLE001 - optional dependency
            _nlp = False
    return _nlp or None


def spacy_available() -> bool:
    return _spacy() is not None


def _first_sentence(text: str, limit: int = 220) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"(.{30,}?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s[:limit].rstrip() + ("…" if len(s) > limit else "")


def lead_sentence(page: Page) -> str:
    """First real prose sentence of the content (skips headings, dates, lists, code)."""
    for block in re.split(r"\n\s*\n", page.markdown):
        b = block.strip()
        if (not b or b.startswith(("#", "*", "-", "|", ">", "`", "!", "[")) or re.match(r"\d+\.", b)
                or len(b) < 60):
            continue
        b = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", b)
        return _first_sentence(b)
    return _first_sentence(page.text or page.markdown)


def _jsonld_entities(page: Page) -> list[EntityRef]:
    out = []
    for n in page.jsonld:
        types = n.get("@type", [])
        types = [types] if isinstance(types, str) else types
        et = next((JSONLD_ENTITY[t] for t in types if t in JSONLD_ENTITY), None)
        name = n.get("name")
        if not et or not isinstance(name, str) or not name.strip():
            continue
        same = n.get("sameAs", [])
        same = [same] if isinstance(same, str) else [s for s in same if isinstance(s, str)]
        desc = n.get("description") if isinstance(n.get("description"), str) else ""
        out.append(EntityRef(name.strip(), et, _first_sentence(desc) if desc else "", same, 0.9, origin="schema.org"))
    return out


def _text_entities(text: str, min_mentions: int, max_n: int) -> list[EntityRef]:
    nlp = _spacy()
    if nlp is not None:
        doc = nlp(text[:100_000])
        c = Counter((e.text.strip(), SPACY_MAP[e.label_]) for e in doc.ents if e.label_ in SPACY_MAP)
        return [EntityRef(n, t, salience=min(1.0, k / 10), origin="spacy")
                for (n, t), k in c.most_common(max_n) if k >= min_mentions and len(n) > 1]
    c: Counter = Counter()
    for m in CAP_PHRASE.finditer(text):
        phrase = m.group(0).strip()
        words = phrase.split()
        if len(words) == 1 and (phrase.lower() in STOP or len(phrase) < 4):
            continue
        # skip sentence-initial single words
        start = m.start()
        if len(words) == 1 and (start == 0 or text[max(0, start - 2):start].strip() in (".", "!", "?", ":")):
            continue
        c[phrase] += 1
    acr = Counter(m.group(0) for m in ACRONYM.finditer(text) if m.group(0).lower() not in STOP)
    for a, k in acr.items():
        if len(a) > 2 or k >= 3:
            c[a] = max(c[a], k)
    return [EntityRef(n, "Other", salience=min(1.0, k / 10)) for n, k in c.most_common(max_n * 2)
            if k >= min_mentions][:max_n]


def _glossary_concepts(page: Page) -> list[ConceptRef]:
    out: list[ConceptRef] = []
    for _, h in page.headings:
        m = re.match(r"(?:what\s+(?:is|are)\s+(?:an?\s+|the\s+)?)(.+?)\??$", h.strip(), re.I)
        if m and 2 < len(m.group(1)) < 60:
            out.append(ConceptRef(m.group(1).strip().rstrip("?"), "concept"))
    if page.html:
        soup = BeautifulSoup(page.html, "lxml")
        for dt in soup.find_all("dt")[:30]:
            dd = dt.find_next_sibling("dd")
            term = dt.get_text(" ", strip=True)
            if 1 < len(term) < 60:
                out.append(ConceptRef(term, "concept", _first_sentence(dd.get_text(" ", strip=True)) if dd else ""))
        for d in soup.find_all("dfn")[:20]:
            term = d.get_text(" ", strip=True)
            if 1 < len(term) < 60:
                out.append(ConceptRef(term, "concept"))
    seen, uniq = set(), []
    for c in out:
        k = c.name.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


def _tags(page: Page, n: int = 6) -> list[str]:
    tags = [t.lower() for t in page.meta_keywords]
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{2,}", page.text or page.markdown)]
    bigrams = Counter(f"{a} {b}" for a, b in zip(words, words[1:]) if a not in STOP and b not in STOP)
    tags += [b for b, k in bigrams.most_common(n) if k >= 3]
    return list(dict.fromkeys(t.strip() for t in tags if t.strip()))[:n]


def analyze_heuristic(page: Page, min_mentions: int = 2, max_entities: int = 15) -> PageAnalysis:
    cls = classify(page)
    ents = _jsonld_entities(page)
    names = {e.name.lower() for e in ents}
    for e in _text_entities(page.text or page.markdown, min_mentions, max_entities):
        if e.name.lower() not in names:
            ents.append(e)
            names.add(e.name.lower())
    summary = page.description or lead_sentence(page)
    return PageAnalysis(page, cls, cls.label, summary, _tags(page), ents[:max_entities],
                        _glossary_concepts(page))


# --------------------------------------------------------------------------- #
# LLM engine
# --------------------------------------------------------------------------- #
SYSTEM = """You turn a web page into Open Knowledge Format (OKF v0.2) knowledge.
Extract CONCEPTS (reusable ideas, not page summaries), ENTITIES (named things),
and PLAYBOOKS (step-by-step procedures) the page teaches. One page can yield
several concepts. Only extract what the page actually supports; never invent.
Return ONLY a JSON object, no prose, no code fences."""

USER_TMPL = """Page URL: {url}
Page title: {title}
Heuristic page-type guess: {guess} (signals: {signals})

Allowed page_type values: {types}
Allowed entity_type values: {etypes}

Return JSON with exactly these keys:
{{
  "page_type": one allowed page_type,
  "page_type_confidence": number 0-1,
  "page_type_reason": short string,
  "summary": ONE sentence describing the page,
  "tags": up to 6 short lowercase tags,
  "entities": [{{"name": str, "entity_type": allowed entity_type, "description": one sentence, "salience": 0-1, "related": [names of other extracted entities or concepts]}}],
  "concepts": [{{"name": str, "kind": "concept" | "playbook" | "system", "definition": one or two sentences grounded in the page, "key_points": up to 5 short strings, "related": [names]}}]
}}
Limit: {max_e} entities, {max_c} concepts. Use canonical names (e.g. "Google Search Console", not "GSC").

PAGE CONTENT (markdown, may be truncated):
<<<
{content}
>>>"""


def analyze_llm(page: Page, provider: str, model: str, api_key: str,
                max_entities: int = 15, max_concepts: int = 8, char_limit: int = 14000) -> PageAnalysis:
    base = analyze_heuristic(page)
    prompt = USER_TMPL.format(
        url=page.url, title=page.title, guess=base.classification.label,
        signals="; ".join(base.classification.signals[:5]) or "none",
        types=", ".join(PAGE_TYPES), etypes=", ".join(ENTITY_TYPES),
        max_e=max_entities, max_c=max_concepts, content=page.markdown[:char_limit])
    data = complete_json(provider, model, api_key, SYSTEM, prompt)
    pt = data.get("page_type") if data.get("page_type") in PAGE_TYPES else base.page_type
    cls = base.classification
    if pt != cls.label:
        cls = Classification(pt, float(data.get("page_type_confidence") or 0.7),
                             [f"LLM: {data.get('page_type_reason', '')}"] + cls.signals, cls.scores)
    else:
        cls.signals.insert(0, f"LLM agrees: {data.get('page_type_reason', '')}")
        cls.confidence = max(cls.confidence, float(data.get("page_type_confidence") or 0))
    ents = []
    for e in data.get("entities", [])[:max_entities]:
        if isinstance(e, dict) and e.get("name"):
            et = e.get("entity_type") if e.get("entity_type") in ENTITY_TYPES else "Other"
            ents.append(EntityRef(str(e["name"]).strip(), et, str(e.get("description", "")),
                                  salience=float(e.get("salience") or 0.5),
                                  related=[str(r) for r in e.get("related", []) if r], origin="llm"))
    # keep schema.org sameAs links the model cannot see
    for je in _jsonld_entities(page):
        match = next((e for e in ents if e.name.lower() == je.name.lower()), None)
        if match:
            match.same_as = je.same_as
        else:
            ents.append(je)
    concepts = []
    for c in data.get("concepts", [])[:max_concepts]:
        if isinstance(c, dict) and c.get("name"):
            kind = c.get("kind") if c.get("kind") in ("concept", "playbook", "system") else "concept"
            concepts.append(ConceptRef(str(c["name"]).strip(), kind, str(c.get("definition", "")),
                                       [str(k) for k in c.get("key_points", [])][:5],
                                       [str(r) for r in c.get("related", []) if r], origin="llm"))
    tags = [str(t).lower() for t in data.get("tags", []) if t][:6] or base.tags
    return PageAnalysis(page, cls, cls.label, str(data.get("summary") or base.summary), tags,
                        ents, concepts, method=f"llm:{model}")
