"""Knowledge-graph view of a bundle, styled after the OKF 'brain' graph.

Every concept is a node coloured by `type`; every concept-to-concept markdown
link is a directed, untyped edge (§6.1). index.md / log.md are optional
"system" nodes.
"""
from __future__ import annotations

import html
import json

from .validator import Report

# Brain vocabulary colours (Concept / Entity / System / Playbook / Reference)
BASE_COLORS = {
    "Concept": "#E0194E", "Entity": "#1FCF2A", "System": "#7A4E3A",
    "Playbook": "#E8732E", "Reference": "#4F5F2A",
}
EXTRA = ["#2B6CB0", "#8E44AD", "#16A085", "#C0392B", "#D4A017", "#2C3E50", "#E84393",
         "#00838F", "#6D4C41", "#5E35B1", "#9E9D24", "#AD1457", "#00897B", "#F4511E"]
SYSTEM_COLOR = "#9AA0A6"


def type_colors(types: list[str]) -> dict[str, str]:
    out, i = {}, 0
    for t in sorted(types):
        if t in BASE_COLORS:
            out[t] = BASE_COLORS[t]
        else:
            out[t] = EXTRA[i % len(EXTRA)]
            i += 1
    return out


def graph_json(report: Report) -> dict:
    nodes = [{"id": c.path, "label": c.title, "type": c.type, "description": c.description,
              "trust": c.trust, "status": c.status, "stale": c.stale} for c in report.concepts.values()]
    edges = [{"source": a, "target": b} for a, b in report.edges]
    return {"okf_version": report.okf_version, "nodes": nodes, "edges": edges}


def render_html(report: Report, files: dict[str, str], visible_types: set[str] | None,
                show_system: bool = False, height: int = 720, physics: bool = True) -> str:
    types = sorted({c.type or "(none)" for c in report.concepts.values()})
    colors = type_colors(types)
    vis_nodes, keep = [], set()
    deg: dict[str, int] = {}
    for a, b in report.edges:
        deg[a] = deg.get(a, 0) + 1
        deg[b] = deg.get(b, 0) + 1
    for c in report.concepts.values():
        t = c.type or "(none)"
        if visible_types is not None and t not in visible_types:
            continue
        keep.add(c.path)
        tip = (f"{c.title}\n{t} · {c.trust}{' · stale' if c.stale else ''}{' · ' + c.status if c.status != 'stable' else ''}"
               f"\n{c.description[:180]}\n{c.path}")
        vis_nodes.append({
            "id": c.path, "label": c.title[:48], "title": tip,
            "color": {"background": colors[t], "border": "#ffffff",
                      "highlight": {"background": colors[t], "border": "#111111"}},
            "size": 10 + min(18, deg.get(c.path, 0) * 1.5),
            "borderWidth": 3 if c.trust == "human-reviewed" else 1,
            "shapeProperties": {"borderDashes": [4, 3] if c.stale else False},
            "opacity": 0.45 if c.status == "deprecated" else 1,
        })
    vis_edges = [{"from": a, "to": b} for a, b in report.edges if a in keep and b in keep]
    if show_system:
        for p in files:
            name = p.rsplit("/", 1)[-1]
            if name in ("index.md", "log.md"):
                vis_nodes.append({"id": p, "label": p, "shape": "box", "title": "reserved file",
                                  "color": {"background": SYSTEM_COLOR, "border": SYSTEM_COLOR},
                                  "font": {"color": "#ffffff", "size": 11}})
                if name == "index.md":
                    d = p.rsplit("/", 1)[0] if "/" in p else ""
                    for c in keep:
                        cd = c.rsplit("/", 1)[0] if "/" in c else ""
                        if cd == d:
                            vis_edges.append({"from": p, "to": c, "dashes": True, "color": {"color": "#d0d4d9"}})
                    if d:
                        vis_edges.append({"from": "index.md", "to": p, "dashes": True, "color": {"color": "#d0d4d9"}})
    legend = "".join(
        f'<div class="lg"><span style="background:{colors[t]}"></span>{html.escape(t)}</div>'
        for t in types if visible_types is None or t in visible_types)
    # Titles/descriptions come from crawled pages: neutralise any </script> or <!-- break-out.
    data = (json.dumps({"nodes": vis_nodes, "edges": vis_edges})
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<script src="https://unpkg.com/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
 html,body{{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#fafafa}}
 #g{{width:100%;height:{height}px;border:1px solid #e6e6e6;border-radius:10px;background:#fbfbfc}}
 #legend{{position:absolute;left:14px;bottom:14px;background:#fff;border:1px solid #e3e3e3;border-radius:8px;
   padding:10px 12px;font-size:13px;box-shadow:0 1px 3px rgba(0,0,0,.06);max-height:45%;overflow:auto}}
 #legend b{{display:block;margin-bottom:6px;font-size:12px}}
 .lg{{display:flex;align-items:center;gap:8px;margin:3px 0;color:#333}}
 .lg span{{width:11px;height:11px;border-radius:50%;display:inline-block}}
 #hint{{position:absolute;right:14px;bottom:14px;font-size:12px;color:#666;background:#fff;
   border:1px solid #e3e3e3;border-radius:8px;padding:6px 10px}}
</style></head><body><div style="position:relative">
<div id="g"></div>
<div id="legend"><b>Legend</b>{legend}</div>
<div id="hint">Thick border = human-reviewed · dashed = stale · faded = deprecated</div>
</div>
<script>
const d={data};
const net=new vis.Network(document.getElementById('g'),
 {{nodes:new vis.DataSet(d.nodes),edges:new vis.DataSet(d.edges)}},
 {{nodes:{{shape:'dot',font:{{size:12,color:'#4a4f57',strokeWidth:3,strokeColor:'#fbfbfc'}}}},
  edges:{{color:{{color:'#c9ccd1',highlight:'#555'}},width:0.8,arrows:{{to:{{enabled:true,scaleFactor:0.4}}}},smooth:false}},
  physics:{{enabled:{str(physics).lower()},solver:'forceAtlas2Based',
   forceAtlas2Based:{{gravitationalConstant:-45,springLength:110,avoidOverlap:0.4}},
   stabilization:{{iterations:250}}}},
  interaction:{{hover:true,tooltipDelay:120,navigationButtons:true,keyboard:true}}}});
net.once('stabilizationIterationsDone',()=>net.setOptions({{physics:false}}));
</script></body></html>"""
