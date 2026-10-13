"""Run with: pytest -q"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from okfkit.bundler import BuildOptions, build_bundle, read_zip, zip_bundle  # noqa: E402
from okfkit.extractor import analyze_heuristic  # noqa: E402
from okfkit.fetcher import parse_html  # noqa: E402
from okfkit.fixer import apply_fixes  # noqa: E402
from okfkit.validator import validate  # noqa: E402

NOW = dt.datetime(2026, 10, 13, tzinfo=dt.timezone.utc)
HOME = """<html><head><title>Acme</title><script type="application/ld+json">
{"@type":"Organization","name":"Acme SEO","description":"An SEO agency."}</script></head>
<body><main><h1>Acme SEO</h1><p>Acme SEO works with Google Search Console and Google Analytics every day.
Google Search Console and Google Analytics are core tools. Read the <a href="/blog/guide">guide</a>.</p></main></body></html>"""
BLOG = """<html><head><meta property="og:type" content="article"><script type="application/ld+json">
{"@type":"BlogPosting","headline":"Guide"}</script></head><body><main><article><h1>Guide</h1>
<p>Core updates reassess helpfulness across the whole site, and Google Search Console shows the impact clearly.</p>
<h2>What is E-E-A-T?</h2><p>Experience, Expertise, Authoritativeness and Trust.</p></article></main></body></html>"""


def _bundle(vocab="page"):
    pages = [parse_html(HOME, "https://acme.test/"), parse_html(BLOG, "https://acme.test/blog/guide")]
    return build_bundle([analyze_heuristic(p) for p in pages], BuildOptions(vocabulary=vocab))


def test_classification():
    a = analyze_heuristic(parse_html(HOME, "https://acme.test/"))
    assert a.page_type == "Homepage"
    b = analyze_heuristic(parse_html(BLOG, "https://acme.test/blog/guide"))
    assert b.page_type == "Article"
    assert any(c.name == "E-E-A-T" for c in b.concepts)


def test_built_bundle_is_conformant_and_linked():
    for vocab in ("page", "brain"):
        files = _bundle(vocab)
        rep = validate(files, NOW)
        assert rep.conformant
        assert rep.okf_version == "0.2"
        assert not [f for f in rep.findings if f.severity == "warning"]
        assert rep.stats["edges"] > 0


def test_internal_links_rewritten_to_bundle_paths():
    files = _bundle()
    home = next(v for k, v in files.items() if k.startswith("homepages/") and not k.endswith("index.md"))
    assert "](/articles/guide.md)" in home


def test_no_verified_without_human_review():
    assert all("verified:" not in v for v in _bundle().values())


def test_zip_roundtrip():
    files = _bundle()
    assert read_zip(zip_bundle(files)) == files


def test_conformance_errors():
    files = {"a.md": "# no fm", "b.md": "---\ntitle: x\n---\n", "c.md": "---\ntype: [x\n---\n",
             "d/index.md": "---\ntitle: x\n---\n# D\n", "log.md": "# Log\n\n## June 1\n"}
    rules = {f.rule for f in validate(files, NOW).findings if f.severity == "error"}
    assert rules == {"okf/no-frontmatter", "okf/missing-type", "okf/frontmatter-parse",
                     "okf/index-frontmatter", "okf/log-date-heading"}


def test_v02_trust_rules():
    doc = ("---\ntype: Note\ntimestamp: 2026-01-01T00:00:00Z\ngenerated: {by: some agent, at: 2026-06-01}\n"
           "verified: {by: human:jane, at: 2026-06-02T00:00:00Z}\nstale_after: 2026-07-01T00:00:00Z\n"
           "sources:\n  - id: s1\n    resource: https://x.test\n---\nClaim.[^s1] Other.[^nope]\n\n[^s1]: x\n# Citations\n")
    rep = validate({"n.md": doc}, NOW)
    rules = {f.rule for f in rep.findings}
    assert {"okf/legacy-timestamp", "okf/legacy-citations", "okf/actor-format",
            "okf/datetime-no-offset", "okf/stale", "okf/footnote-unknown-source"} <= rules
    assert rep.concepts["n.md"].trust == "human-reviewed"   # bare mapping read as list


def test_spec_attested_computation_example_is_clean():
    doc = """---
type: Attested Computation
title: Revenue
description: Recognized revenue for a fiscal year.
runtime: bigquery
parameters:
  - { name: year, type: integer, required: true }
executor:
  resource: references/skills/run-on-bq.md
  receipt: [job_id, executed_sql, result]
attester:
  resource: references/attesters/sql-equality.py
generated: { by: reference_agent/gemini-2.5-pro, at: 2026-06-28T14:00:00Z }
verified: { by: human:ahormati, at: 2026-06-25T09:00:00Z }
stale_after: 2026-12-31T00:00:00Z
sources:
  - id: rev-policy
    resource: https://wiki.acme/finance/revenue-recognition
---

# Computation

    SELECT SUM(amount) FROM t WHERE fiscal_year = @year

Per policy.[^rev-policy]

[^rev-policy]: Revenue recognition policy
"""
    rep = validate({"computations/revenue.md": doc}, NOW)
    assert rep.conformant
    assert not [f for f in rep.findings if f.severity != "info"]


def test_fixer_declares_version_and_indexes():
    files = {"t/a.md": "---\ntype: X\ntitle: A\nstale_after: 2027-01-01\n---\nhi\n"}
    fixed, log = apply_fixes(files)
    rep = validate(fixed, NOW)
    assert rep.okf_version == "0.2"
    assert "t/index.md" in fixed
    assert not [f for f in rep.findings if f.rule == "okf/datetime-no-offset"]
