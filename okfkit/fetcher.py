"""Read pages: single URLs, sitemaps, same-site crawl, or uploaded files.

Every page is normalised into a `Page` with clean markdown, metadata,
JSON-LD nodes and the internal links it contains.
"""
from __future__ import annotations

import gzip
import json
import re
import time
import urllib.robotparser
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md

from .frontmatter import to_iso

UA = "OKF-Builder/1.0 (+https://github.com/GoogleCloudPlatform/open-knowledge-format)"
TIMEOUT = 20
SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|pdf|zip|gz|mp4|mp3|webm|css|js|xml|json|woff2?|ttf)$", re.I)
NOISE_TAGS = ["script", "style", "noscript", "iframe", "svg", "form", "nav", "header",
              "footer", "aside", "button", "template"]
NOISE_HINT = re.compile(r"(cookie|consent|newsletter|subscribe|sidebar|share|social|related|"
                        r"breadcrumb|comment|advert|promo|modal|popup|menu|navbar)", re.I)


@dataclass
class Page:
    url: str
    title: str = ""
    description: str = ""
    markdown: str = ""
    text: str = ""
    html: str = ""
    lang: str = ""
    canonical: str = ""
    og_type: str = ""
    meta_keywords: list[str] = field(default_factory=list)
    jsonld: list[dict] = field(default_factory=list)
    headings: list[tuple[int, str]] = field(default_factory=list)
    internal_links: list[str] = field(default_factory=list)
    last_modified: str | None = None
    published: str | None = None
    author: str = ""
    status_code: int = 200
    error: str = ""
    source_kind: str = "url"      # url | upload

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.markdown.strip())


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    return s


_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def allowed_by_robots(url: str, session: requests.Session) -> bool:
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    if root not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = session.get(root + "/robots.txt", timeout=10)
            if r.status_code >= 400:
                _robots_cache[root] = None
            else:
                rp.parse(r.text.splitlines())
                _robots_cache[root] = rp
        except requests.RequestException:
            _robots_cache[root] = None
    rp = _robots_cache[root]
    return True if rp is None else rp.can_fetch(UA, url)


def normalize_url(u: str) -> str:
    u, _ = urldefrag(u.strip())
    p = urlparse(u)
    path = p.path or "/"
    return f"{p.scheme}://{p.netloc.lower()}{path}" + (f"?{p.query}" if p.query else "")


def fetch_page(url: str, session: requests.Session | None = None, respect_robots: bool = True) -> Page:
    session = session or _session()
    url = normalize_url(url)
    if respect_robots and not allowed_by_robots(url, session):
        return Page(url=url, error="Blocked by robots.txt")
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        return Page(url=url, error=f"Request failed: {e.__class__.__name__}")
    ctype = r.headers.get("Content-Type", "")
    if r.status_code >= 400:
        return Page(url=url, status_code=r.status_code, error=f"HTTP {r.status_code}")
    if "html" not in ctype and "xml" not in ctype and ctype:
        return Page(url=url, status_code=r.status_code, error=f"Not HTML ({ctype.split(';')[0]})")
    r.encoding = r.encoding or r.apparent_encoding
    page = parse_html(r.text, normalize_url(r.url))
    page.status_code = r.status_code
    page.last_modified = page.last_modified or to_iso(r.headers.get("Last-Modified"))
    return page


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _jsonld_nodes(soup: BeautifulSoup) -> list[dict]:
    nodes: list[dict] = []

    def walk(x):
        if isinstance(x, dict):
            if "@graph" in x:
                walk(x["@graph"])
            if "@type" in x:
                nodes.append(x)
            for k, v in x.items():
                if k != "@graph" and isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)

    for tag in soup.find_all("script", type=lambda t: t and "ld+json" in t):
        raw = tag.string or tag.get_text() or ""
        try:
            walk(json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            continue
    return nodes


def _meta(soup, *names) -> str:
    for n in names:
        t = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if t and t.get("content"):
            return t["content"].strip()
    return ""


def _main_node(soup: BeautifulSoup):
    for sel in ["main", "article", "[role=main]", "#content", ".content", ".post", ".entry-content"]:
        n = soup.select_one(sel)
        if n and len(n.get_text(" ", strip=True)) > 200:
            return n
    return soup.body or soup


def parse_html(html: str, url: str) -> Page:
    soup = BeautifulSoup(html, "lxml")
    page = Page(url=url, html=html)
    page.jsonld = _jsonld_nodes(soup)
    page.title = (_meta(soup, "og:title") or (soup.title.get_text(strip=True) if soup.title else "")
                  or (soup.h1.get_text(strip=True) if soup.h1 else ""))
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        page.title = h1.get_text(" ", strip=True)
    page.description = _meta(soup, "description", "og:description", "twitter:description")
    page.og_type = _meta(soup, "og:type").lower()
    page.lang = (soup.html.get("lang", "") if soup.html else "")
    can = soup.find("link", rel=lambda v: v and "canonical" in v)
    page.canonical = urljoin(url, can["href"]) if can and can.get("href") else ""
    kw = _meta(soup, "keywords")
    page.meta_keywords = [k.strip() for k in kw.split(",") if k.strip()][:15]
    page.meta_keywords += [t["content"] for t in soup.find_all("meta", attrs={"property": "article:tag"}) if t.get("content")]
    page.author = _meta(soup, "author", "article:author")
    page.published = to_iso(_meta(soup, "article:published_time"))
    page.last_modified = to_iso(_meta(soup, "article:modified_time", "og:updated_time"))
    for n in page.jsonld:
        if not page.last_modified and n.get("dateModified"):
            page.last_modified = to_iso(n.get("dateModified"))
        if not page.published and n.get("datePublished"):
            page.published = to_iso(n.get("datePublished"))
        if not page.author and isinstance(n.get("author"), dict):
            page.author = n["author"].get("name", "")

    # Links: collect before stripping chrome so site structure is preserved.
    host = urlparse(url).netloc.lower()
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        p = urlparse(href)
        if p.scheme in ("http", "https") and p.netloc.lower() == host and not SKIP_EXT.search(p.path):
            links.append(normalize_url(href))
    page.internal_links = list(dict.fromkeys(links))

    main = _main_node(soup)
    for t in main.find_all(NOISE_TAGS):
        t.decompose()
    for t in main.find_all(True):
        if t.attrs is None:
            continue
        ident = " ".join(t.get("class", []) or []) + " " + (t.get("id") or "")
        if ident.strip() and NOISE_HINT.search(ident) and t.name not in ("main", "article", "body"):
            t.decompose()
    page.headings = [(int(h.name[1]), h.get_text(" ", strip=True)) for h in main.find_all(re.compile(r"^h[1-6]$"))]
    markdown = md(str(main), heading_style="ATX", bullets="*", strip=["img"])
    # Absolutise relative links in markdown.
    markdown = re.sub(r"\]\((?!https?:|mailto:|#)([^)\s]+)\)", lambda m: f"]({urljoin(url, m.group(1))})", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    page.markdown = markdown
    page.text = main.get_text(" ", strip=True)
    if not page.markdown:
        page.error = "No readable content found"
    return page


def parse_upload(name: str, data: bytes) -> Page:
    text = data.decode("utf-8", errors="replace")
    pseudo = f"file:///{name}"
    if name.lower().endswith((".html", ".htm")):
        p = parse_html(text, pseudo)
    else:  # .md / .txt
        title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.strip()), name)
        p = Page(url=pseudo, title=title, markdown=text.strip(), text=text)
        p.headings = [(len(m.group(1)), m.group(2).strip()) for m in re.finditer(r"^(#{1,6})\s+(.+)$", text, re.M)]
    p.source_kind = "upload"
    return p


# --------------------------------------------------------------------------- #
# Discovery: sitemap + crawl
# --------------------------------------------------------------------------- #
def sitemap_urls(sitemap_url: str, limit: int = 200, session: requests.Session | None = None) -> list[str]:
    session = session or _session()
    out: list[str] = []
    queue = deque([sitemap_url])
    seen = set()
    while queue and len(out) < limit:
        sm = queue.popleft()
        if sm in seen:
            continue
        seen.add(sm)
        try:
            r = session.get(sm, timeout=TIMEOUT)
            content = r.content
            if sm.endswith(".gz"):
                content = gzip.decompress(content)
        except (requests.RequestException, OSError):
            continue
        soup = BeautifulSoup(content, "xml")
        if soup.find("sitemapindex"):
            queue.extend(loc.get_text(strip=True) for loc in soup.find_all("loc"))
        else:
            for loc in soup.find_all("loc"):
                u = loc.get_text(strip=True)
                if u and not SKIP_EXT.search(urlparse(u).path):
                    out.append(normalize_url(u))
    return list(dict.fromkeys(out))[:limit]


def crawl(start_url: str, limit: int = 30, delay: float = 0.5, respect_robots: bool = True,
          progress=None) -> list[Page]:
    session = _session()
    start = normalize_url(start_url)
    queue, seen, pages = deque([start]), {start}, []
    while queue and len(pages) < limit:
        url = queue.popleft()
        page = fetch_page(url, session, respect_robots)
        pages.append(page)
        if progress:
            progress(len(pages), limit, url)
        for link in page.internal_links:
            if link not in seen and "?" not in link:
                seen.add(link)
                queue.append(link)
        time.sleep(delay)
    return pages


def fetch_many(urls: list[str], delay: float = 0.5, respect_robots: bool = True, progress=None) -> list[Page]:
    session = _session()
    pages = []
    for i, u in enumerate(urls, 1):
        pages.append(fetch_page(u, session, respect_robots))
        if progress:
            progress(i, len(urls), u)
        time.sleep(delay)
    return pages
