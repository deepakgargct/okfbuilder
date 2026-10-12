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
from markdownify import markdownify as md

APP_VERSION = "0.2.0"
MAX_PAGES_DEFAULT = 20
MAX_CHARS_FOR_LLM = 12000
USER_AGENT = "CT-OKF-Builder/0.2 (+internal knowledge-bundle generation)"

st.set_page_config(page_title="CT OKF Builder", page_icon="🧠", layout="wide")
st.title("CT OKF Builder")
st.caption(f"Prototype v{APP_VERSION} · Generate, review, validate and export Open Knowledge Format (v0.2) bundles")
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
    model = st.text_input("OpenAI model", value="gpt-4o-mini")
    st.caption("Without an API key, the app creates standard OKF draft bundles using clean source Markdown.")

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

def clean_html_to_markdown(html):
    soup = BeautifulSoup(html, "html.parser")
    # Decompose non-content UI and script noise
    for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()
        
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    main = soup.find("main") or soup.find("article") or soup.body or soup
    
    # Extract plain text for LLM metadata parsing
    raw_text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))
    raw_text = re.sub(r"[ \t]+\n", "\n", raw_text)
    
    # Convert HTML to rich Markdown for full content preservation
    markdown_body = md(str(main), heading_style="ATX", strip=['img'])
    markdown_body = re.sub(r"\n{3,}", "\n\n", markdown_body).strip()
    
    return title, raw_text.strip(), markdown_body

def fetch_url(url, timeout=15):
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").lower()
    if "text/html" not in ctype and not url.lower().endswith((".html", ".htm")):
        return None
    title, text, markdown_body = clean_html_to_markdown(r.text)
    return {
        "title": title or url,
        "url": url,
        "text": text[:MAX_CHARS_FOR_LLM],
        "markdown_body": markdown_body
    }

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
        title, text, md_body = clean_html_to_markdown(raw.decode("utf-8", errors="replace"))
        return [{
            "title": title or name,
            "url": f"upload://{name}",
            "text": text[:MAX_CHARS_FOR_LLM],
            "markdown_body": md_body
        }]
    if suffix in (".txt", ".md"):
        txt = raw.decode("utf-8", errors="replace")
        return [{
            "title": name,
            "url": f"upload://{name}",
            "text": txt[:MAX_CHARS_FOR_LLM],
            "markdown_body": txt
        }]
    if suffix == ".csv":
        decoded = raw.decode("utf-8-sig", errors="replace")
        rows = list(csv.DictReader(io.StringIO(decoded)))
        chunks = []
        for idx in range(0, len(rows), 20):
            chunk_data = json.dumps(rows[idx:idx+20], ensure_ascii=False, indent=2)
            chunks.append({
                "title": f"{name} rows {idx+1}-{min(idx+20, len(rows))}",
                "url": f"upload://{name}",
                "text": chunk_data[:MAX_CHARS_FOR_LLM],
                "markdown_body": f"```json\n{chunk_data}\n```"
            })
        return chunks
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            return [{
                "title": name,
                "url": f"upload://{name}",
                "text": text[:MAX_CHARS_FOR_LLM],
                "markdown_body": text
            }]
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
            "type": "Article",
            "title": title,
            "description": f"Extracted knowledge page for {title}.",
            "tags": ["extracted-page"],
            "resource": src.get("url", ""),
            "full_body": src.get("markdown_body", text),
            "facts": [],
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
    
    source_payload = [
        {"title": s.get("title", ""), "url": s.get("url", ""), "content": s.get("text", "")[:8500]}
        for s in sources[:30]
    ]
    depth_note = {
        "Quick: core facts": "Return key concepts and basic relationships; merge near-duplicate titles.",
        "Standard: concepts + relationships": "Return distinct concepts and clear relationships across pages.",
        "Detailed: concepts + evidence": "Return detailed concepts with facts, explicit relationships, and detailed frontmatter tags."
    }[depth]
    
    prompt = f"""
You are preparing a draft Open Knowledge Format (OKF v0.2) metadata extraction for a web asset.
Client type: {business}
Extraction mode: {depth_note}

Return ONLY valid JSON matching this shape:
{{
 "concepts": [
  {{
   "type": "Descriptive concept type (e.g. Article, Service, Product, Guide, Organization)",
   "title": "Exact title matching the source document",
   "description": "Comprehensive one-to-two sentence summary of the page content",
   "tags": ["tag1", "tag2"],
   "resource": "exact URL from source material",
   "facts": [{{"statement":"Key factual claim from text","source_url":"exact source URL"}}],
   "relationships": [{{"label":"related to","target_title":"exact title of another source document"}}],
   "verification_status": "unverified"
  }}
 ]
}}

Rules:
- Generate 1 concept per distinct webpage or uploaded item provided in source material.
- Retain exact URLs in the resource field.
- Do not make up information or facts.
- Return JSON only, with no markdown code blocks.

Source material:
{json.dumps(source_payload, ensure_ascii=False)}
"""
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": "Return precise structured JSON."},
            {"role": "user", "content": prompt}
        ],
        response_format={"type": "json_object"},
        temperature=0.1
    )
    data = json.loads(response.choices[0].message.content)
    extracted_concepts = data.get("concepts", [])
    
    # Map extracted concepts back to the complete markdown body
    url_to_source = {s.get("url"): s for s in sources}
    title_to_source = {s.get("title"): s for s in sources}
    
    for c in extracted_concepts:
        res = c.get("resource", "")
        title = c.get("title", "")
        matched_src = url_to_source.get(res) or title_to_source.get(title)
        
        if matched_src:
            c["full_body"] = matched_src.get("markdown_body", matched_src.get("text", ""))
        else:
            c["full_body"] = ""
            
        c.setdefault("type", "Article")
        c.setdefault("title", "Untitled concept")
        c.setdefault("description", "")
        c.setdefault("tags", [])
        c.setdefault("resource", "")
        c.setdefault("facts", [])
        c.setdefault("relationships", [])
        c["verification_status"] = "unverified"
        
    return extracted_concepts

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
    if resource and resource not in seen:
        sources.append({"resource": resource})
        
    fm = {
        "type": str(concept.get("type") or "Article"),
        "title": str(concept.get("title") or slug),
        "description": str(concept.get("description") or ""),
        "tags": [str(x) for x in concept.get("tags", []) if str(x).strip()],
        "generated": {
            "by": f"ct_okf_builder/{APP_VERSION}",
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        },
        "verified": [],
        "status": "stable"
    }
    if sources:
        fm["sources"] = sources
    if resource.startswith(("http://", "https://")):
        fm["resource"] = resource
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, default_flow_style=False).strip() + "\n---\n"

def render_concept(concept, slug, title_to_slug):
    body = []
    
    # Optional metadata summary block
    rels = concept.get("relationships", [])
    facts = concept.get("facts", [])
    
    if rels or facts:
        body.append("## AI-Extracted Context\n")
        if facts:
            body.append("### Key Facts")
            for fact in facts:
                statement = str(fact.get("statement", "")).strip()
                source = str(fact.get("source_url", "")).strip()
                if statement:
                    line = f"- {statement}"
                    if source:
                        line += f" ([source]({source}))"
                    body.append(line)
            body.append("")
            
        if rels:
            body.append("### Linked Concepts")
            for rel in rels:
                label = str(rel.get("label", "related to")).strip()
                target = str(rel.get("target_title", "")).strip()
                target_slug = title_to_slug.get(target)
                if target_slug and target_slug != slug:
                    body.append(f"- {label}: [{target}](concepts/{target_slug}.md)")
                elif target:
                    body.append(f"- {label}: {target}")
            body.append("")
        body.append("---\n")

    # Append complete original document text
    full_text = concept.get("full_body", "").strip()
    if full_text:
        body.append(full_text)
    else:
        body.append(f"# {concept.get('title') or slug}\n\n{concept.get('description', '')}")

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
        grouped.setdefault(str(c.get("type") or "Article"), []).append((c, path))
        
    index = [
        "# OKF Knowledge Bundle",
        "",
        f"- Client: {client_slug}",
        "- Target Spec: OKF v0.2",
        f"- Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "## Knowledge Graph Concepts",
        ""
    ]
    
    for typ, items in sorted(grouped.items()):
        index += [f"### {typ}", ""]
        for c, path in items:
            desc = str(c.get("description", "")).strip()
            index.append(f"* [{c.get('title', path)}]({path})" + (f" - {desc}" if desc else ""))
        index.append("")
        
    files["index.md"] = "\n".join(index).rstrip() + "\n"
    files["log.md"] = (
        "# Update Log\n\n"
        f"## {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
        f"* **Creation**: Initial OKF v0.2 bundle compiled with CT OKF Builder {APP_VERSION}.\n"
    )
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
        accept_multiple_files=True, help="Extracts native formatting to Markdown."
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
            with st.spinner("Crawling website and extracting clean Markdown..."):
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
            [{"Title": s["title"], "Source": s["url"], "Content Length (Chars)": len(s.get("markdown_body", s.get("text", "")))} for s in st.session_state.sources],
            use_container_width=True, hide_index=True
        )
        if st.button("Generate concepts from sources", type="primary"):
            key = api_key.strip()
            if not key:
                try:
                    key = st.secrets.get("OPENAI_API_KEY", "")
                except Exception:
                    key = ""
            with st.spinner("Extracting metadata & building concept structure..."):
                try:
                    if key:
                        concepts = llm_extract(st.session_state.sources, key, model, extraction_depth, business_type)
                    else:
                        concepts = fallback_concepts(st.session_state.sources)
                        st.info("No API key configured: created basic OKF concepts preserving full document text. Add an API key for automatic relation discovery.")
                    
                    st.session_state.concepts_json = json.dumps(concepts, ensure_ascii=False, indent=2)
                    st.success(f"Prepared {len(concepts)} draft concept(s). Review them in Tab 2.")
                except Exception as e:
                    st.error(f"Generation failed: {e}")
    else:
        st.info("No sources yet. Crawl a website or upload supported files to begin.")

with tab_review:
    st.subheader("Review and edit concept metadata")
    st.caption("Edit concept JSON attributes below. Full document text will be preserved in the generated output files.")
    client_name = st.text_input("Client name for bundle", value=st.session_state.client_name)
    st.session_state.client_name = client_name
    
    edited_json = st.text_area("Concept Metadata JSON", value=st.session_state.concepts_json, height=430)
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
                "Body Length": len(c.get("full_body", "")),
                "Facts": len(c.get("facts", [])),
                "Relationships": len(c.get("relationships", []))
            } for c in parsed_concepts], use_container_width=True, hide_index=True)
    except Exception:
        st.info("Save valid JSON edits to refresh summary.")

with tab_export:
    st.subheader("Validate and export OKF bundle")
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
            st.success(f"OKF Bundle built with {count} concept document(s).")
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
            st.success("OKF v0.2 structural verification passed.")
            
        with st.expander("Preview generated bundle files"):
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
                "validation_issues": [{"severity": x[0], "file": x[1], "issue": x[2]} for x in issues]
            }
            zf.writestr(f"{client_slug}-okf/validation-report.json", json.dumps(report, indent=2))
            
        st.download_button(
            "Download OKF bundle (.zip)", data=memory.getvalue(),
            file_name=f"{client_slug}-okf-bundle.zip", mime="application/zip",
            type="primary", use_container_width=True
        )
    else:
        st.info("Generate concepts, review them, then build the bundle here.")

st.divider()
st.caption("CT OKF Builder is an internal prototype. Crawling is restricted to same-domain URLs discovered from common sitemap locations.")
