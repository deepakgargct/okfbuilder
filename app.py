"""OKF Builder — read pages, classify them, extract entities, bundle and lint.

Run:  streamlit run app.py
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from okfkit.bundler import BuildOptions, build_bundle, norm_key, read_zip, zip_bundle
from okfkit.classifier import PAGE_TYPES, okf_type_for
from okfkit.extractor import ENTITY_TYPES, analyze_heuristic, analyze_llm, spacy_available
from okfkit.fetcher import crawl, fetch_many, parse_upload, sitemap_urls
from okfkit.fixer import apply_fixes
from okfkit.graph import graph_json, render_html
from okfkit.llm import DEFAULT_MODELS, LLMError
from okfkit.validator import validate

st.set_page_config(page_title="OKF Builder", page_icon="🧠", layout="wide")

ss = st.session_state
for k, v in {"pages": [], "analyses": [], "bundle": {}, "report": None, "report_files": {},
             "report_src": ""}.items():
    ss.setdefault(k, v)

# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("🧠 OKF Builder")
    st.caption("Targets **OKF v0.2** (GoogleCloudPlatform/open-knowledge-format, rev. Aug 21 2026).")

    st.subheader("Extraction")
    engine = st.radio("Engine", ["Heuristic (no API key)", "LLM (recommended)"], index=0,
                      help="LLM mode extracts real concepts and playbooks, not just named things.")
    provider = model = api_key = None
    if engine.startswith("LLM"):
        provider = st.selectbox("Provider", list(DEFAULT_MODELS))
        model = st.text_input("Model ID", DEFAULT_MODELS[provider],
                              help="Editable: providers rename and retire model IDs.")
        env = {"Anthropic": "ANTHROPIC_API_KEY", "Google Gemini": "GEMINI_API_KEY", "OpenAI": "OPENAI_API_KEY"}[provider]
        default_key = os.environ.get(env, "")
        try:
            default_key = default_key or st.secrets.get(env, "")
        except Exception:  # noqa: BLE001 - no secrets file
            pass
        api_key = st.text_input("API key", default_key, type="password", help=f"Or set {env}")
    elif not spacy_available():
        st.caption("Tip: `pip install spacy && python -m spacy download en_core_web_sm` "
                   "improves heuristic entity extraction.")
    max_entities = st.slider("Max entities per page", 3, 40, 15)
    max_concepts = st.slider("Max concepts per page (LLM)", 1, 20, 8)
    min_mentions = st.slider("Heuristic: min mentions", 1, 6, 2)

    st.subheader("Bundle")
    b_title = st.text_input("Bundle title", "Website Knowledge Bundle")
    b_desc = st.text_input("Bundle description", "Concepts, entities and pages extracted from the website.")
    vocab = st.radio("Type vocabulary", ["page", "brain"],
                     format_func=lambda v: {"page": "Page types (Article, Product, FAQ…)",
                                            "brain": "Brain types (Concept, Entity, System, Playbook, Reference)"}[v])
    status = st.selectbox("status", ["draft", "stable"], index=0,
                          help="§5.4 — machine-written, unreviewed content is honestly `draft`.")
    stale_days = st.number_input("stale_after (days from today)", 7, 3650, 180)
    content_mode = st.selectbox("Page body", ["full", "excerpt", "none"],
                                help="How much of each page's cleaned content to embed under # Content.")
    min_pages = st.number_input("Heuristic entities: min pages", 1, 20, 1)
    reviewer = st.text_input("Reviewer actor (for pages you tick 'reviewed')", "",
                             placeholder="human:yourname",
                             help="Only pages you actually checked get a `verified` entry. Must start with human:")

    st.subheader("Fetching")
    delay = st.slider("Delay between requests (s)", 0.0, 3.0, 0.5, 0.25)
    robots = st.checkbox("Respect robots.txt", True)


def run_analysis(pages):
    out, bar = [], st.progress(0.0, "Analysing…")
    for i, p in enumerate(pages, 1):
        if not p.ok:
            a = analyze_heuristic(p) if p.markdown else None
            if a is None:
                from okfkit.classifier import Classification
                from okfkit.extractor import PageAnalysis
                a = PageAnalysis(p, Classification("Webpage", 0.0, [p.error]), "Webpage", include=False, error=p.error)
            out.append(a)
        elif engine.startswith("LLM"):
            try:
                out.append(analyze_llm(p, provider, model, api_key, max_entities, max_concepts))
            except (LLMError, ValueError) as e:
                a = analyze_heuristic(p, min_mentions, max_entities)
                a.error = f"LLM failed, used heuristics: {e}"
                out.append(a)
        else:
            out.append(analyze_heuristic(p, min_mentions, max_entities))
        bar.progress(i / len(pages), f"Analysed {i}/{len(pages)}: {p.url[:70]}")
    bar.empty()
    return out


def options() -> BuildOptions:
    return BuildOptions(title=b_title, description=b_desc, vocabulary=vocab, status=status,
                        stale_days=int(stale_days), content_mode=content_mode,
                        min_entity_pages=int(min_pages), reviewer=reviewer.strip())


t_ingest, t_classify, t_extract, t_build, t_validate, t_graph = st.tabs(
    ["📥 Read pages", "🏷️ Classify & review", "🧩 Entities & concepts", "📦 Build bundle", "✅ Validate", "🕸️ Graph"])

# --------------------------------------------------------------------------- #
# 1. Ingest
# --------------------------------------------------------------------------- #
with t_ingest:
    st.subheader("Read pages")
    mode = st.radio("Source", ["URL list", "Sitemap", "Crawl a site", "Upload files"], horizontal=True)
    pages = None
    if mode == "URL list":
        urls = st.text_area("One URL per line", height=140, placeholder="https://example.com/\nhttps://example.com/blog/post")
        if st.button("Read pages", type="primary", disabled=not urls.strip()):
            lst = [u.strip() for u in urls.splitlines() if u.strip().startswith("http")]
            bar = st.progress(0.0)
            pages = fetch_many(lst, delay, robots, lambda i, n, u: bar.progress(i / n, f"{i}/{n} {u[:70]}"))
            bar.empty()
    elif mode == "Sitemap":
        sm = st.text_input("Sitemap URL", placeholder="https://example.com/sitemap.xml")
        limit = st.number_input("Max pages", 1, 500, 50)
        filt = st.text_input("Only URLs containing (optional)", "")
        if st.button("Read pages", type="primary", disabled=not sm.strip()):
            with st.spinner("Reading sitemap…"):
                lst = [u for u in sitemap_urls(sm.strip(), int(limit) * 3) if filt in u][: int(limit)]
            st.caption(f"{len(lst)} URLs found in sitemap")
            bar = st.progress(0.0)
            pages = fetch_many(lst, delay, robots, lambda i, n, u: bar.progress(i / n, f"{i}/{n} {u[:70]}"))
            bar.empty()
    elif mode == "Crawl a site":
        start = st.text_input("Start URL", placeholder="https://example.com/")
        limit = st.number_input("Max pages", 1, 300, 25)
        if st.button("Crawl", type="primary", disabled=not start.strip()):
            bar = st.progress(0.0)
            pages = crawl(start.strip(), int(limit), delay, robots, lambda i, n, u: bar.progress(i / n, f"{i}/{n} {u[:70]}"))
            bar.empty()
    else:
        ups = st.file_uploader("HTML, Markdown or text files", type=["html", "htm", "md", "txt"], accept_multiple_files=True)
        if st.button("Read files", type="primary", disabled=not ups):
            pages = [parse_upload(u.name, u.getvalue()) for u in ups]

    if pages is not None:
        ss.pages = pages
        ss.analyses = run_analysis(pages)
        ss.bundle, ss.report = {}, None
        ok = sum(p.ok for p in pages)
        st.success(f"Read {ok} of {len(pages)} pages and classified them. Review them in the next tab.")

    if ss.pages:
        df = pd.DataFrame([{"URL": p.url, "Title": p.title, "Words": len((p.text or p.markdown).split()),
                            "JSON-LD types": ", ".join(sorted({str(n.get('@type')) for n in p.jsonld}))[:60],
                            "Internal links": len(p.internal_links), "Status": p.error or "OK"} for p in ss.pages])
        st.dataframe(df, width="stretch", hide_index=True)
        bad = [a for a in ss.analyses if a.error]
        if bad:
            with st.expander(f"{len(bad)} page(s) had problems"):
                for a in bad:
                    st.write(f"**{a.page.url}** — {a.error}")

# --------------------------------------------------------------------------- #
# 2. Classify & review
# --------------------------------------------------------------------------- #
with t_classify:
    st.subheader("Page types")
    if not ss.analyses:
        st.info("Read some pages first.")
    else:
        rows = [{"include": a.include, "reviewed": a.reviewed, "title": a.page.title or a.page.url,
                 "page_type": a.page_type, "confidence": a.classification.confidence,
                 "okf type": okf_type_for(a.page_type, vocab), "entities": len(a.entities),
                 "concepts": len(a.concepts), "url": a.page.url, "method": a.method}
                for a in ss.analyses]
        edited = st.data_editor(
            pd.DataFrame(rows), width="stretch", hide_index=True, key="cls_editor",
            column_config={
                "include": st.column_config.CheckboxColumn("Include"),
                "reviewed": st.column_config.CheckboxColumn("Reviewed", help="Adds verified: human:… on build (needs reviewer actor)"),
                "page_type": st.column_config.SelectboxColumn("Page type", options=PAGE_TYPES, required=True),
                "confidence": st.column_config.ProgressColumn("Confidence", min_value=0, max_value=1, format="%.2f"),
                "url": st.column_config.LinkColumn("URL"),
            },
            disabled=["title", "confidence", "okf type", "entities", "concepts", "url", "method"])
        for a, (_, r) in zip(ss.analyses, edited.iterrows()):
            a.include, a.reviewed, a.page_type = bool(r["include"]), bool(r["reviewed"]), r["page_type"]
        if any(a.reviewed for a in ss.analyses) and not reviewer.startswith("human:"):
            st.warning("You ticked 'Reviewed' but the reviewer actor in the sidebar doesn't start with `human:` — no `verified` entry will be written.")

        c1, c2 = st.columns([1, 2])
        with c1:
            dist = Counter(a.page_type for a in ss.analyses if a.include)
            st.bar_chart(pd.Series(dist, name="pages"))
        with c2:
            labels = [f"{i + 1}. {(x.page.title or x.page.url)[:80]}" for i, x in enumerate(ss.analyses)]
            pick = st.selectbox("Why was this page classified this way?", labels)
            a = ss.analyses[labels.index(pick)]
            st.markdown(f"**{a.page_type}** · confidence {a.classification.confidence:.2f} · via `{a.method}`")
            for s in a.classification.signals[:12]:
                st.markdown(f"- {s}")
            st.markdown(f"**Summary:** {a.summary}")
            st.markdown("**Tags:** " + (", ".join(a.tags) or "—"))

# --------------------------------------------------------------------------- #
# 3. Entities & concepts
# --------------------------------------------------------------------------- #
with t_extract:
    st.subheader("Entities and concepts across all pages")
    if not ss.analyses:
        st.info("Read some pages first.")
    else:
        agg = defaultdict(lambda: {"name": "", "type": Counter(), "pages": 0, "desc": "", "origin": set()})
        cagg = defaultdict(lambda: {"name": "", "kind": Counter(), "pages": 0, "def": ""})
        for a in ss.analyses:
            if not a.include:
                continue
            for e in a.entities:
                g = agg[norm_key(e.name)]
                g["name"] = g["name"] or e.name
                g["type"][e.entity_type] += 1
                g["pages"] += 1
                g["desc"] = max(g["desc"], e.description, key=len)
                g["origin"].add(e.origin)
            for c in a.concepts:
                g = cagg[norm_key(c.name)]
                g["name"] = g["name"] or c.name
                g["kind"][c.kind] += 1
                g["pages"] += 1
                g["def"] = max(g["def"], c.definition, key=len)
        st.markdown(f"**{len(agg)} entities** · **{len(cagg)} concepts / playbooks / systems**")
        st.caption("Untick to drop, edit name or type to merge/rename. Changes apply to every page that mentions it.")
        edf = pd.DataFrame([{"keep": True, "key": k, "name": g["name"], "entity_type": g["type"].most_common(1)[0][0],
                             "pages": g["pages"], "origin": ", ".join(sorted(g["origin"])), "description": g["desc"]}
                            for k, g in sorted(agg.items(), key=lambda kv: -kv[1]["pages"])])
        cdf = pd.DataFrame([{"keep": True, "key": k, "name": g["name"], "kind": g["kind"].most_common(1)[0][0],
                             "pages": g["pages"], "definition": g["def"]}
                            for k, g in sorted(cagg.items(), key=lambda kv: -kv[1]["pages"])])
        ce, cc = st.columns(2)
        with ce:
            e_ed = st.data_editor(edf, hide_index=True, width="stretch", key="ent_ed",
                                  column_config={"key": None,
                                                 "entity_type": st.column_config.SelectboxColumn("Type", options=ENTITY_TYPES)},
                                  disabled=["pages", "origin"]) if len(edf) else edf
        with cc:
            c_ed = st.data_editor(cdf, hide_index=True, width="stretch", key="con_ed",
                                  column_config={"key": None,
                                                 "kind": st.column_config.SelectboxColumn("Kind", options=["concept", "playbook", "system"])},
                                  disabled=["pages"]) if len(cdf) else cdf
            if not len(cdf):
                st.caption("No concepts yet. LLM mode extracts concepts and playbooks; heuristics only find glossary-style definitions.")
        if st.button("Apply edits", type="primary"):
            emap = {r.key: r for r in e_ed.itertuples()} if len(edf) else {}
            cmap = {r.key: r for r in c_ed.itertuples()} if len(cdf) else {}
            for a in ss.analyses:
                ne = []
                for e in a.entities:
                    r = emap.get(norm_key(e.name))
                    if r is None or r.keep:
                        if r is not None:
                            e.name, e.entity_type = r.name, r.entity_type
                            e.description = r.description or e.description
                        ne.append(e)
                a.entities = ne
                nc = []
                for c in a.concepts:
                    r = cmap.get(norm_key(c.name))
                    if r is None or r.keep:
                        if r is not None:
                            c.name, c.kind = r.name, r.kind
                            c.definition = r.definition or c.definition
                        nc.append(c)
                a.concepts = nc
            st.success("Edits applied.")
            st.rerun()

# --------------------------------------------------------------------------- #
# 4. Build
# --------------------------------------------------------------------------- #
with t_build:
    st.subheader("Build the OKF bundle")
    if not ss.analyses:
        st.info("Read some pages first.")
    else:
        n_inc = sum(a.include and a.page.ok for a in ss.analyses)
        st.markdown(f"{n_inc} pages will become concepts, plus their entities and concepts. "
                    f"Vocabulary: **{vocab}** · status **{status}** · stale after **{stale_days}** days.")
        if st.button("Build bundle", type="primary", disabled=n_inc == 0):
            ss.bundle = build_bundle(ss.analyses, options())
            ss.report = validate(ss.bundle)
            ss.report_files, ss.report_src = ss.bundle, "built"
        if ss.bundle:
            rep = validate(ss.bundle)
            c = rep.counts()
            m = st.columns(5)
            m[0].metric("Files", len(ss.bundle))
            m[1].metric("Concepts", rep.stats["concepts"])
            m[2].metric("Links", rep.stats["edges"])
            m[3].metric("Conformant", "Yes" if rep.conformant else "No")
            m[4].metric("Warnings", c.get("warning", 0))
            d1, d2 = st.columns(2)
            d1.download_button("Download bundle (.zip)", zip_bundle(ss.bundle, "okf-bundle"),
                               "okf-bundle.zip", "application/zip", type="primary", width="stretch")
            d2.download_button("Download graph.json", json.dumps(graph_json(rep), indent=2),
                               "graph.json", "application/json", width="stretch")
            left, right = st.columns([1, 2])
            with left:
                tree = defaultdict(list)
                for p in ss.bundle:
                    tree[p.rsplit("/", 1)[0] if "/" in p else "(root)"].append(p)
                for d in sorted(tree):
                    st.markdown(f"**{d}/** — {len(tree[d])}")
                sel = st.selectbox("Preview file", list(ss.bundle))
            with right:
                st.code(ss.bundle[sel], language="markdown")

# --------------------------------------------------------------------------- #
# 5. Validate
# --------------------------------------------------------------------------- #
with t_validate:
    st.subheader("Validate an OKF bundle")
    st.caption("Errors break §11 conformance. Warnings break a SHOULD inside the v0.2 trust/provenance families or use "
               "superseded v0.1 fields. Info items are health signals the spec says consumers must tolerate.")
    src = st.radio("Bundle", ["Built in this app", "Upload .zip", "Upload .md files"], horizontal=True)
    files = None
    if src == "Built in this app":
        files = ss.bundle or None
        if not files:
            st.info("Build a bundle first, or upload one.")
    elif src == "Upload .zip":
        z = st.file_uploader("Bundle zip", type=["zip"])
        if z:
            files = read_zip(z.getvalue())
    else:
        mds = st.file_uploader("Markdown files (paths are flattened to the bundle root)", type=["md"], accept_multiple_files=True)
        if mds:
            files = {m.name: m.getvalue().decode("utf-8", errors="replace") for m in mds}
    if files:
        fix = st.checkbox("Preview safe auto-fixes (indexes, okf_version, UTC offsets, log order)")
        fixlog: list[str] = []
        if fix:
            files, fixlog = apply_fixes(files)
        rep = validate(files)
        ss.report, ss.report_files, ss.report_src = rep, files, src
        c = rep.counts()
        m = st.columns(6)
        m[0].metric("Conformant (§11)", "Yes ✅" if rep.conformant else "No ❌")
        m[1].metric("Errors", c.get("error", 0))
        m[2].metric("Warnings", c.get("warning", 0))
        m[3].metric("Info", c.get("info", 0))
        m[4].metric("Concepts", rep.stats["concepts"])
        m[5].metric("okf_version", rep.okf_version or "—")
        s = rep.stats
        st.markdown(
            f"**Trust tiers:** {', '.join(f'{k} {v}' for k, v in s['trust_tiers'].items()) or '—'} · "
            f"**Stale:** {s['stale']} · **With description:** {s['pct_description']}% · "
            f"**With sources:** {s['pct_sources']}% · **Orphans:** {s['orphans']} · **Broken links:** {s['broken_links']}")
        if fixlog:
            with st.expander(f"{len(fixlog)} fix(es) applied"):
                st.write("\n".join(f"- {x}" for x in fixlog))
            st.download_button("Download fixed bundle (.zip)", zip_bundle(files, "okf-bundle"), "okf-bundle-fixed.zip")
        if rep.findings:
            fdf = pd.DataFrame([f.__dict__ for f in rep.findings])
            sev = st.multiselect("Severity", ["error", "warning", "info"], default=["error", "warning", "info"])
            rules = st.multiselect("Rule", sorted(fdf.rule.unique()))
            view = fdf[fdf.severity.isin(sev)]
            if rules:
                view = view[view.rule.isin(rules)]
            st.dataframe(view, width="stretch", hide_index=True,
                         column_config={"severity": st.column_config.TextColumn("Severity", width="small"),
                                        "spec": st.column_config.TextColumn("Spec", width="small")})
            with st.expander("Rule summary"):
                st.dataframe(fdf.groupby(["severity", "rule"]).size().rename("count").reset_index(),
                             hide_index=True, width="stretch")
        else:
            st.success("No findings. Clean bundle.")
        r1, r2 = st.columns(2)
        r1.download_button("Report (.json)", json.dumps(rep.to_dict(), indent=2, default=str), "okf-report.json",
                           width="stretch")
        r2.download_button("Report (.md)", rep.to_markdown(), "okf-report.md", width="stretch")

# --------------------------------------------------------------------------- #
# 6. Graph
# --------------------------------------------------------------------------- #
with t_graph:
    rep, files = ss.report, ss.report_files
    if rep is None or not files:
        st.info("Build or validate a bundle to see its graph.")
    else:
        types = sorted({c.type or "(none)" for c in rep.concepts.values()})
        top = st.columns([1.2, 4, 1])
        show_sys = top[0].toggle("Show system nodes", value=False, help="index.md and log.md")
        if hasattr(st, "pills"):
            chosen = top[1].pills("Filter types", types, selection_mode="multi", default=types)
        else:
            chosen = top[1].multiselect("Filter types", types, default=types)
        physics = top[2].toggle("Physics", value=True)
        page_html = render_html(rep, files, set(chosen or []), show_sys, 700, physics)
        if hasattr(st, "iframe"):          # Streamlit >= 1.52
            st.iframe(page_html, height=720)
        else:
            components.html(page_html, height=720, scrolling=False)
        inbound = Counter(b for _, b in rep.edges)
        g1, g2 = st.columns(2)
        with g1:
            st.markdown("**Most-linked concepts (hubs)**")
            st.dataframe(pd.DataFrame([{"concept": rep.concepts[p].title, "type": rep.concepts[p].type, "inbound": n}
                                       for p, n in inbound.most_common(12)]), hide_index=True, width="stretch")
        with g2:
            orph = [c for c in rep.concepts.values() if not c.links_out and not inbound.get(c.path)]
            st.markdown(f"**Islands (no links in or out): {len(orph)}**")
            st.dataframe(pd.DataFrame([{"concept": c.title, "type": c.type, "path": c.path} for c in orph]),
                         hide_index=True, width="stretch")
