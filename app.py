import csv
import io
import json
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
import xml.etree.ElementTree as ET

import requests
import streamlit as st
import yaml
from bs4 import BeautifulSoup

APP_VERSION = "0.1.0"
MAX_PAGES_DEFAULT = 20
MAX_CHARS_PER_SOURCE = 14000
USER_AGENT = "CT-OKF-Builder/0.1 (+internal knowledge-bundle generation)"

st.set_page_config(page_title="CT OKF Builder", page_icon="🧠", layout="wide")
st.title("CT OKF Builder")
st.caption(f"Prototype v{APP_VERSION} · Generate, review, validate and export Open Knowledge Format bundles")
st.warning(
    "Review all generated concepts before client use. Do not upload confidential, personal, regulated, "
    "or client-restricted data unless your organization's approved AI/data policy permits it."
)

with st.sidebar:
    st.header("Generation settings")
    business_type = st.selectbox(
        "Client type",
        ["Lead generation / services", "Ecommerce", "Mixed / other"]
    )
    extraction_depth = st.selectbox(
        "Extraction depth",
        ["Quick: core facts", "Standard: concepts + relationships", "Detailed: concepts + evidence"]
    )
    max_pages = st.slider("Maximum website pages", 1, 100, MAX_PAGES_DEFAULT)
    delay = st.slider("Delay between page requests (seconds)", 0.0, 2.0, 0.25, 0.05)
    api_key = st.text_input(
        "OpenAI API key (optional)", type="password",
        help="Used only when you click Generate concepts. You can also configure OPENAI_API_KEY in Streamlit secrets."
    )
    model = st.text_input("OpenAI model", value="gpt-4.1-mini")
    st.caption("Without an API key, the app can still create a basic draft bundle from source documents.")

if "sources" not in st.session_state:
    st.session_state.sources = []
if "concepts_json" not in st.session_state:
    st.session_state.concepts_json = "[]"
if "client_name" not in st.session_state:
    st.session_state.client_name = "client"
if "bundle" not in st.session_state:
    st.session_state.bundle = {}

def dedupe_sources(items):
    seen = set()
    result = []
    for item in items:
        key = item.get("url", "") + "|" + item.get("title", "")
        if key not in seen:
            result.append(item)
            seen.add(key)
    return result

def clean_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    text = re.sub(r"[ \t]+\n", "\n", text)
    return title, text.strip()

def fetch_url(url, timeout=15):
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").lower()
    if "text/html" not in ctype and not url.lower().endswith((".html", ".htm")):
        return None
    title, text = clean_text(r.text)
    return {"title": title or url, "url": url, "text": text[:MAX_CHARS_PER_SOURCE]}

def same_domain(a, b):
    try:
        return urlparse(a).netloc.lower().removeprefix("www.") == urlparse(b).netloc.lower().removeprefix("www.")
    except Exception:
        return False

def sitemap_urls(sitemap_url, limit):
    found = []
    seen_maps = set()
    queue = [sitemap_url]
    while queue and len(found) < limit:
        sm = queue.pop(0)
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        try:
            resp = requests.get(sm, headers={"User-Agent": USER_AGENT}, timeout=15)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            for node in root.iter():
                if node.tag.endswith("}loc") or node.tag == "loc":
                    loc = (node.text or "").strip()
                    if not loc:
                        continue
                    if loc.lower().endswith(".xml") and len(found) == 0:
                        queue.append(loc)
                    elif loc not in found:
                        found.append(loc)
                        if len(found) >= limit:
                            break
        except Exception:
            continue
    return found[:limit]

def crawl_site(start_url, limit, wait):
    parsed = urlparse(start_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Enter a complete URL beginning with http:// or https://")
    root = f"{parsed.scheme}://{parsed.netloc}"
    candidate_maps = [urljoin(root, "/sitemap.xml"), urljoin(root, "/sitemap_index.xml")]
    urls = []
    for sm in candidate_maps:
        urls = sitemap_urls(sm, limit)
        if urls:
            break
    if not urls:
        urls = [start_url]
    # Keep the requested page first when it belongs to the site.
    urls = [start_url] + [u for u in urls if u.rstrip("/") != start_url.rstrip("/")]
    urls = [u for u in urls if same_domain(root, u)][:limit]
    results, errors = [], []
    for i, url in enumerate(urls):
        try:
            item = fetch_url(url)
            if item and item["text"]:
                results.append(item)
        except Exception as e:
            errors.append(f"{url}: {str(e)[:180]}")
        if wait and i < len(urls) - 1:
            time.sleep(wait)
    return results, errors

def decode_upload(upload):
    name = upload.name
    raw = upload.getvalue()
    suffix = Path(name).suffix.lower()
    if suffix in (".html", ".htm"):
        title, text = clean_text(raw.decode("utf-8", errors="replace"))
        return [{"title": title or name, "url": f"upload://{name}", "text": text[:MAX_CHARS_PER_SOURCE]}]
    if suffix in (".txt", ".md"):
        return [{"title": name, "url": f"upload://{name}", "text": raw.decode("utf-8", errors="replace")[:MAX_CHARS_PER_SOURCE]}]
    if suffix == ".csv":
        decoded = raw.decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(decoded)))
        chunks = []
        for idx in range(0, len(rows), 20):
            chunks.append({"title": f"{name} rows {idx+1}-{min(idx+20, len(rows))}",
                           "url": f"upload://{name}", "text": json.dumps(rows[idx:idx+20], ensure_ascii=False)[:MAX_CHARS_PER_SOURCE]})
        return chunks
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            return [{"title": name, "url": f"upload://{name}", "text": text[:MAX_CHARS_PER_SOURCE]}]
        except Exception as e:
            raise ValueError(f"Could not read PDF {name}: {e}")
    raise ValueError(f"Unsupported file type: {name}")

def fallback_concepts(sources):
    concepts = []
    for i, src in enumerate(sources, start=1):
        text = src.get("text", "").strip()
        if not text:
            continue
        title = src.get("title") or f"Source {i}"
        concepts.append({
            "type": "Source Document",
            "title": title,
            "description": f"Source-derived draft from {title}. Requires review and concept-level refinement.",
            "tags": ["draft", "source-document"],
            "resource": src.get("url", ""),
            "facts": [{"statement": text[:1800], "source_url": src.get("url", "")}],
            "relationships": [],
            "verification_status": "unverified"
        })
    return concepts

def llm_extract(sources, key, model_name, depth, business):
    try:
        from openai import OpenAI
    except ImportError:
        raise RuntimeError("The OpenAI package is missing. Install dependencies from requirements.txt.")
    client = OpenAI(api_key=key)
    # Keep request bounded; summarize the source set in batches if needed.
    source_payload = [
        {"title": s.get("title", ""), "url": s.get("url", ""), "content": s.get("text", "")[:8500]}
        for s in sources[:30]
    ]
    depth_note = {
        "Quick: core facts": "Return 1-2 high-value concepts per source; prioritize business identity, products/services and facts.",
        "Standard: concepts + relationships": "Return distinct concepts and meaningful relationships; avoid duplicating the same concept across pages.",
        "Detailed: concepts + evidence": "Return distinct concepts, detailed factual statements, evidence/source URLs, relationships, limitations and explicit unknowns."
    }[depth]
    prompt = f"""
You are preparing a draft Open Knowledge Format (OKF v0.2) knowledge bundle for a client.
Client type: {business}
Extraction mode: {depth_note}

Return ONLY valid JSON matching this shape:
{{
 "concepts": [
  {{
   "type": "Descriptive concept type",
   "title": "Human-readable title",
   "description": "One-sentence summary",
   "tags": ["tag"],
   "resource": "canonical URL or empty string",
   "facts": [{{"statement":"A concise fact supported by provided content","source_url":"exact source URL"}}],
   "relationships": [{{"label":"relationship description","target_title":"title of another concept in this response"}}],
   "verification_status": "unverified"
  }}
 ]
}}

Rules:
- Extract concepts, not just one file per URL. Merge duplicates.
- Do not infer facts that are not in the source content. If a fact is uncertain, omit it.
- Every factual statement must be traceable to a supplied source URL.
- Do not invent customer results, certifications, product specifications, prices, locations, or claims.
- Keep relationships grounded in the supplied text. target_title must match another concept title where possible.
- Keep facts concise. Use no more than 12 concepts in this response.
- All concepts are drafts and must remain unverified.
- Return JSON only, no markdown fences.

Source material:
{json.dumps(source_payload, ensure_ascii=False)}
"""
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": "Return precise structured JSON. Never invent unsupported facts."},
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"},
        temperature=0.1
    )
    data = json.loads(response.choices[0].message.content)
    concepts = data.get("concepts", [])
    # Ensure each concept has required fields and keep provenance attached.
    for c in concepts:
        c.setdefault("type", "Concept")
        c.setdefault("title", "Untitled concept")
        c.setdefault("description", "")
        c.setdefault("tags", [])
        c.setdefault("resource", "")
        c.setdefault("facts", [])
        c.setdefault("relationships", [])
        c["verification_status"] = "unverified"
    return concepts

def slugify(s):
    s = re.sub(r"[^\w\s-]", "", str(s), flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:70] or "concept"

def yaml_frontmatter(concept, slug):
    sources = []
    seen = set()
    for fact in concept.get("facts", []):
        u = str(fact.get("source_url", "")).strip()
        if u and u not in seen:
            sources.append({"resource": u})
            seen.add(u)
    resource = str(concept.get("resource", "")).strip()
    fm = {
        "type": str(concept.get("type") or "Concept"),
        "title": str(concept.get("title") or slug),
        "description": str(concept.get("description") or ""),
        "tags": [str(x) for x in concept.get("tags", []) if str(x).strip()],
        "generated": {"by": f"ct_okf_builder/{APP_VERSION}", "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")},
        "verified": [],
        "sources": sources
    }
    if resource.startswith(("http://", "https://")):
        fm["resource"] = resource
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False).strip() + "\n---\n"

def render_concept(concept, slug, title_to_slug):
    body = [f"# {concept.get('title') or slug}", ""]
    if concept.get("description"):
        body += [str(concept["description"]), ""]
    body += ["## Facts", ""]
    facts = concept.get("facts", [])
    if facts:
        for fact in facts:
            statement = str(fact.get("statement", "")).strip()
            source = str(fact.get("source_url", "")).strip()
            if statement:
                line = f"- {statement}"
                if source:
                    line += f" ([source]({source}))"
                body.append(line)
    else:
        body.append("- No facts extracted. Add verified facts during review.")
    body += ["", "## Relationships", ""]
    rels = concept.get("relationships", [])
    if rels:
        for rel in rels:
            label = str(rel.get("label", "related to")).strip()
            target = str(rel.get("target_title", "")).strip()
            target_slug = title_to_slug.get(target)
            if target_slug and target_slug != slug:
                body.append(f"- {label}: [{target}]({target_slug}.md)")
            elif target:
                body.append(f"- {label}: {target} (target concept not resolved)")
    else:
        body.append("- No relationships extracted; review whether related concepts should be linked.")
    body += ["", "## Review status", "", f"- Status: {concept.get('verification_status', 'unverified')}", "- Confirm all claims and links before using this bundle."]
    return yaml_frontmatter(concept, slug) + "\n" + "\n".join(body).strip() + "\n"

def make_bundle(concepts, client_slug):
    files = {}
    used = set()
    title_to_slug = {}
    for c in concepts:
        base = slugify(c.get("title") or "concept")
        slug = base
        counter = 2
        while slug in used or slug in ("index", "log"):
            slug = f"{base}-{counter}"
            counter += 1
        used.add(slug)
        title_to_slug[str(c.get("title") or slug)] = slug
        c["_slug"] = slug
    grouped = {}
    for c in concepts:
        slug = c["_slug"]
        path = f"concepts/{slug}.md"
        files[path] = render_concept(c, slug, title_to_slug)
        grouped.setdefault(str(c.get("type") or "Concept"), []).append((c, path))
    index = ["# OKF Knowledge Bundle", "", f"- Client: {client_slug}", "- Format target: OKF v0.2", f"- Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}", "", "## Concepts", ""]
    for typ, items in sorted(grouped.items()):
        index += [f"### {typ}", ""]
        for c, path in items:
            desc = str(c.get("description", "")).strip()
            index.append(f"* [{c.get('title', path)}]({path})" + (f" - {desc}" if desc else ""))
        index.append("")
    files["index.md"] = "\n".join(index).rstrip() + "\n"
    files["log.md"] = "# Update Log\n\n" + f"## {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n* **Creation**: Generated initial draft bundle with CT OKF Builder {APP_VERSION}.\n"
    return files

def validate_bundle(files):
    issues = []
    concept_count = 0
    for path, content in files.items():
        if not path.endswith(".md") or path.split("/")[-1] in ("index.md", "log.md"):
            continue
        concept_count += 1
        if not content.startswith("---\n"):
            issues.append(("ERROR", path, "Missing YAML frontmatter opening delimiter."))
            continue
        parts = content.split("---", 2)
        if len(parts) < 3:
            issues.append(("ERROR", path, "Missing YAML frontmatter closing delimiter."))
            continue
        try:
            fm = yaml.safe_load(parts[1])
            if not isinstance(fm, dict) or not str(fm.get("type", "")).strip():
                issues.append(("ERROR", path, "Frontmatter must contain a non-empty 'type'."))
            elif not isinstance(fm.get("type"), str):
                issues.append(("ERROR", path, "'type' must be a string."))
        except Exception as e:
            issues.append(("ERROR", path, f"YAML parsing failed: {e}"))
    if concept_count == 0:
        issues.append(("ERROR", "-", "No concept documents found."))
    if "index.md" not in files:
        issues.append(("WARNING", "-", "Bundle has no root index.md."))
    if "log.md" not in files:
        issues.append(("WARNING", "-", "Bundle has no root log.md."))
    return issues, concept_count

tab_input, tab_review, tab_export = st.tabs(["1 · Sources", "2 · Review concepts", "3 · Validate & export"])

with tab_input:
    st.subheader("Add source material")
    site_url = st.text_input("Website URL", placeholder="https://clientwebsite.com")
    col1, col2 = st.columns(2)
    with col1:
        crawl_clicked = st.button("Crawl website", type="primary", use_container_width=True)
    with col2:
        clear_clicked = st.button("Clear collected sources", use_container_width=True)
    uploads = st.file_uploader(
        "Or upload files", type=["html", "htm", "txt", "md", "csv", "pdf"],
        accept_multiple_files=True, help="PDF extraction requires pypdf. CSV rows are grouped into manageable chunks."
    )
    if clear_clicked:
        st.session_state.sources = []
        st.session_state.concepts_json = "[]"
        st.session_state.bundle = {}
        st.success("Cleared current session.")
    if crawl_clicked:
        if not site_url.strip():
            st.error("Enter a website URL first.")
        else:
            with st.spinner("Checking sitemap and crawling same-domain pages..."):
                try:
                    crawled, errors = crawl_site(site_url.strip(), max_pages, delay)
                    st.session_state.sources.extend(crawled)
                    st.session_state.sources = dedupe_sources(st.session_state.sources)
                    if crawled:
                        st.success(f"Collected {len(crawled)} page(s).")
                    else:
                        st.warning("No readable HTML pages were collected.")
                    if errors:
                        with st.expander(f"Fetch warnings ({len(errors)})"):
                            st.write("\n".join(errors))
                except Exception as e:
                    st.error(str(e))
    if uploads:
        if st.button("Import uploaded files"):
            added, errors = [], []
            for up in uploads:
                try:
                    added.extend(decode_upload(up))
                except Exception as e:
                    errors.append(str(e))
            st.session_state.sources.extend(added)
            st.session_state.sources = dedupe_sources(st.session_state.sources)
            st.success(f"Imported {len(added)} source item(s).")
            for e in errors:
                st.warning(e)
    st.divider()
    st.subheader("Collected sources")
    if st.session_state.sources:
        st.dataframe(
            [{"Title": s["title"], "Source": s["url"], "Characters": len(s.get("text", ""))} for s in st.session_state.sources],
            use_container_width=True, hide_index=True
        )
        if st.button("Generate concepts from sources", type="primary"):
            key = api_key.strip()
            if not key:
                try:
                    key = st.secrets.get("OPENAI_API_KEY", "")
                except Exception:
                    key = ""
            with st.spinner("Generating draft concepts..."):
                try:
                    if key:
                        concepts = llm_extract(st.session_state.sources, key, model, extraction_depth, business_type)
                    else:
                        concepts = fallback_concepts(st.session_state.sources)
                        st.info("No API key configured: created basic source-document concepts. Add an API key for concept extraction.")
                    st.session_state.concepts_json = json.dumps(concepts, ensure_ascii=False, indent=2)
                    st.success(f"Prepared {len(concepts)} draft concept(s). Review them in the next tab.")
                except Exception as e:
                    st.error(f"Generation failed: {e}")
    else:
        st.info("No sources yet. Crawl a website or upload supported files to begin.")

with tab_review:
    st.subheader("Review and edit concepts")
    st.caption("Edit the JSON directly. Every concept remains unverified until a human checks it.")
    client_name = st.text_input("Client name for bundle", value=st.session_state.client_name)
    st.session_state.client_name = client_name
    edited_json = st.text_area("Concept JSON", value=st.session_state.concepts_json, height=430)
    if st.button("Save concept edits"):
        try:
            parsed = json.loads(edited_json)
            if not isinstance(parsed, list):
                raise ValueError("Top-level JSON must be a list of concepts.")
            for idx, concept in enumerate(parsed):
                if not isinstance(concept, dict):
                    raise ValueError(f"Concept {idx+1} must be an object.")
                if not str(concept.get("title", "")).strip():
                    raise ValueError(f"Concept {idx+1} is missing title.")
                if not str(concept.get("type", "")).strip():
                    raise ValueError(f"Concept {idx+1} is missing type.")
            st.session_state.concepts_json = json.dumps(parsed, ensure_ascii=False, indent=2)
            st.success(f"Saved {len(parsed)} concept(s).")
        except Exception as e:
            st.error(f"Could not save edits: {e}")
    try:
        parsed_concepts = json.loads(st.session_state.concepts_json)
        st.metric("Current concepts", len(parsed_concepts))
        if parsed_concepts:
            st.dataframe([{
                "Title": c.get("title", ""),
                "Type": c.get("type", ""),
                "Facts": len(c.get("facts", [])),
                "Relationships": len(c.get("relationships", [])),
                "Review": c.get("verification_status", "unverified")
            } for c in parsed_concepts], use_container_width=True, hide_index=True)
    except Exception:
        st.info("Save valid JSON edits to refresh the concept summary.")

with tab_export:
    st.subheader("Validate and export")
    try:
        concepts = json.loads(st.session_state.concepts_json)
        if not isinstance(concepts, list):
            raise ValueError("Concept JSON must be a list.")
    except Exception as e:
        concepts = []
        st.error(f"Fix the concept JSON before export: {e}")
    client_slug = slugify(st.session_state.client_name or "client")
    if st.button("Build and validate bundle", type="primary", disabled=not concepts):
        try:
            bundle = make_bundle(concepts, client_slug)
            issues, count = validate_bundle(bundle)
            st.session_state.bundle = bundle
            st.session_state.validation = issues
            st.session_state.concept_count = count
            st.success(f"Bundle built with {count} concept document(s).")
        except Exception as e:
            st.error(f"Bundle generation failed: {e}")
    if st.session_state.bundle:
        issues = st.session_state.get("validation", [])
        count = st.session_state.get("concept_count", 0)
        errors = sum(1 for i in issues if i[0] == "ERROR")
        warnings = sum(1 for i in issues if i[0] == "WARNING")
        c1, c2, c3 = st.columns(3)
        c1.metric("Concept files", count)
        c2.metric("Errors", errors)
        c3.metric("Warnings", warnings)
        if issues:
            st.dataframe([{"Severity": x[0], "File": x[1], "Issue": x[2]} for x in issues], use_container_width=True, hide_index=True)
        else:
            st.success("Basic OKF v0.2 structure checks passed. This is not a substitute for a full specification audit.")
        with st.expander("Preview generated files"):
            for path, content in st.session_state.bundle.items():
                with st.expander(path):
                    st.code(content, language="markdown")
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, content in st.session_state.bundle.items():
                zf.writestr(f"{client_slug}-okf/{path}", content)
            report = {
                "app_version": APP_VERSION,
                "target_specification": "OKF v0.2",
                "client": st.session_state.client_name,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "concept_count": count,
                "validation_issues": [{"severity": x[0], "file": x[1], "issue": x[2]} for x in issues],
                "note": "Draft bundle. Human review required before use."
            }
            zf.writestr(f"{client_slug}-okf/validation-report.json", json.dumps(report, indent=2))
        st.download_button(
            "Download OKF bundle (.zip)", data=memory.getvalue(),
            file_name=f"{client_slug}-okf-bundle.zip", mime="application/zip",
            type="primary", use_container_width=True
        )
        st.download_button(
            "Download validation report (.json)",
            data=json.dumps({
                "client": st.session_state.client_name,
                "concept_count": count,
                "issues": [{"severity": x[0], "file": x[1], "issue": x[2]} for x in issues]
            }, indent=2),
            file_name=f"{client_slug}-okf-validation.json", mime="application/json",
            use_container_width=True
        )
    else:
        st.info("Generate concepts, review them, then build the bundle here.")

st.divider()
st.caption("CT OKF Builder is an internal prototype. Website crawling is limited to same-domain URLs discovered from common sitemap locations. Respect robots.txt, website terms, client permissions, rate limits and applicable data policies.")
