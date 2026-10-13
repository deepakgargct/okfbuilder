"""Explainable page-type classification.

Signals are weighted and summed: schema.org JSON-LD (strongest), URL path,
Open Graph type, and on-page DOM cues. Every decision returns the signals
that produced it so a human can audit or override it.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .fetcher import Page

PAGE_TYPES = [
    "Homepage", "Article", "Product", "Product Category", "Service", "Landing Page",
    "About Page", "Contact Page", "FAQ", "Playbook", "Reference", "Case Study",
    "Event", "Recipe", "Job Posting", "Policy", "Pricing", "Person", "Location",
    "Glossary Term", "Software", "Course", "Archive", "Webpage",
]

# How page types map onto the "brain" vocabulary used in Marie Haynes' build
# (Concept / Entity / System / Playbook / Reference).
BRAIN_MAP = {
    "Homepage": "Entity", "About Page": "Entity", "Product": "Entity", "Service": "Entity",
    "Person": "Entity", "Location": "Entity", "Software": "Entity", "Event": "Entity",
    "Playbook": "Playbook", "Recipe": "Playbook", "Course": "Playbook",
    "Glossary Term": "Concept",
}

JSONLD_MAP = {
    "Article": "Article", "BlogPosting": "Article", "NewsArticle": "Article", "Report": "Article",
    "TechArticle": "Reference", "APIReference": "Reference",
    "Product": "Product", "ProductGroup": "Product", "Offer": "Product",
    "CollectionPage": "Product Category", "ItemList": "Product Category", "OfferCatalog": "Product Category",
    "Service": "Service", "FAQPage": "FAQ", "HowTo": "Playbook", "Recipe": "Recipe",
    "Event": "Event", "JobPosting": "Job Posting", "AboutPage": "About Page", "ContactPage": "Contact Page",
    "ProfilePage": "Person", "Person": "Person", "LocalBusiness": "Location", "Place": "Location",
    "SoftwareApplication": "Software", "WebApplication": "Software", "Course": "Course",
    "DefinedTerm": "Glossary Term", "DefinedTermSet": "Glossary Term",
}

URL_RULES: list[tuple[str, str]] = [
    (r"/(blog|news|articles?|posts?|insights|stories|journal)/.+", "Article"),
    (r"/(products?|p|item|shop|store)/[^/]+/?$", "Product"),
    (r"/(category|categories|collections?|c|catalog|shop)/?", "Product Category"),
    (r"/(services?|solutions?|what-we-do)(/|$)", "Service"),
    (r"/(lp|landing|campaign|offer)s?/", "Landing Page"),
    (r"/(about|about-us|company|our-story|who-we-are)/?$", "About Page"),
    (r"/(contact|contact-us|get-in-touch|support/contact)/?$", "Contact Page"),
    (r"/(faqs?|help|questions)(/|$)", "FAQ"),
    (r"/(how-to|guides?|tutorials?|playbooks?|checklists?)(/|-)", "Playbook"),
    (r"/(docs?|documentation|reference|api|manual|kb|knowledge-base)(/|$)", "Reference"),
    (r"/(case-stud(y|ies)|customers?|success-stories|clients)/.+", "Case Study"),
    (r"/(events?|webinars?|conferences?)/.+", "Event"),
    (r"/recipes?/.+", "Recipe"),
    (r"/(careers?|jobs?)/.+", "Job Posting"),
    (r"/(privacy|terms|legal|cookies?|gdpr|disclaimer|accessibility)", "Policy"),
    (r"/(pricing|plans|price)(/|$)", "Pricing"),
    (r"/(author|authors|team|people|staff|profile)/.+", "Person"),
    (r"/(locations?|stores?|branches|offices?)(/|$)", "Location"),
    (r"/(glossary|definitions?|what-is|terms)[/-]", "Glossary Term"),
    (r"/(courses?|academy|learn)/.+", "Course"),
    (r"/(tag|tags|topics?|archive|page/\d+)(/|$)", "Archive"),
]

PRICE_RE = re.compile(r"(\$|€|£|₹|USD|EUR|INR|Rs\.?)\s?\d[\d,]*(\.\d{2})?")
CART_RE = re.compile(r"add to (cart|bag|basket)|buy now|in stock|out of stock", re.I)
STEP_RE = re.compile(r"\bstep\s*\d+\b", re.I)


@dataclass
class Classification:
    label: str
    confidence: float
    signals: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)


def _types(node: dict) -> list[str]:
    t = node.get("@type", [])
    return [t] if isinstance(t, str) else [x for x in t if isinstance(x, str)]


def classify(page: Page) -> Classification:
    scores: dict[str, float] = defaultdict(float)
    signals: list[str] = []

    def add(label: str, w: float, why: str):
        scores[label] += w
        signals.append(f"{why} → {label} (+{w:g})")

    # 1. JSON-LD (strongest). Only the page's primary entities, not nested authors etc.
    for node in page.jsonld:
        for t in _types(node):
            if t in JSONLD_MAP:
                add(JSONLD_MAP[t], 3.0, f"schema.org @type {t}")

    # 2. URL path
    path = urlparse(page.url).path.lower() if page.source_kind == "url" else ""
    if page.source_kind == "url" and path in ("", "/", "/index.html", "/home"):
        add("Homepage", 4.0, "root URL")
    for pat, label in URL_RULES:
        if path and re.search(pat, path):
            add(label, 2.0, f"URL matches {pat}")
            break

    # 3. Open Graph
    og = page.og_type
    if og.startswith("article"):
        add("Article", 1.0, "og:type article")
    elif og.startswith("product"):
        add("Product", 1.5, "og:type product")
    elif og in ("profile",):
        add("Person", 1.0, "og:type profile")

    # 4. DOM cues (use raw html so forms/navs still count)
    soup = BeautifulSoup(page.html, "lxml") if page.html else None
    text = page.text or page.markdown
    if soup is not None:
        if len(PRICE_RE.findall(text)) >= 1 and CART_RE.search(text):
            add("Product", 2.0, "price + cart wording")
        elif len(PRICE_RE.findall(text)) >= 6:
            add("Product Category", 1.0, "many prices on page")
        forms = soup.find_all("form")
        if any(f.find("textarea") for f in forms) and re.search(r"contact|get in touch|message", text, re.I):
            add("Contact Page", 1.5, "message form")
        if soup.find("time") and (page.author or soup.find(attrs={"rel": "author"})):
            add("Article", 1.0, "byline + date")
        if len(soup.find_all("pre")) >= 3:
            add("Reference", 1.0, "multiple code blocks")
        if len(soup.find_all("details")) >= 3:
            add("FAQ", 1.0, "multiple <details> blocks")
        if len(soup.find_all("dt")) >= 5:
            add("Glossary Term", 1.0, "definition list")
    q_heads = [h for _, h in page.headings if h.strip().endswith("?")]
    if len(q_heads) >= 4:
        add("FAQ", 1.5, f"{len(q_heads)} question headings")
    if len(STEP_RE.findall(text)) >= 3:
        add("Playbook", 1.5, "numbered steps")
    if page.published and not scores:
        add("Article", 0.5, "has publish date")

    if not scores:
        return Classification("Webpage", 0.2, ["no strong signals"], {})
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, top_score = ranked[0]
    total = sum(scores.values())
    conf = round(min(0.99, 0.35 + 0.65 * (top_score / total) * min(1.0, top_score / 4)), 2)
    return Classification(top, conf, signals, dict(scores))


def okf_type_for(page_type: str, vocabulary: str) -> str:
    """Map a page type onto the chosen OKF `type` vocabulary."""
    if vocabulary == "brain":
        return BRAIN_MAP.get(page_type, "Reference")
    return page_type
