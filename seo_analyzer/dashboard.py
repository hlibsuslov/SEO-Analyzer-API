import html
import json
from typing import Any


def render_dashboard(result: dict[str, Any]) -> str:
    payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    root = html.escape(result.get("stats", {}).get("root_domain", "site"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Link Graph · {root}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: #172033;
      background: #f6f8fb;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      background: #fff;
      border-bottom: 1px solid #d9e0ea;
    }}
    h1 {{ margin: 0; font-size: 18px; letter-spacing: 0; }}
    main {{ display: grid; grid-template-columns: minmax(0, 1fr) 380px; min-height: calc(100vh - 58px); }}
    #graph-wrap {{ position: relative; min-height: 620px; overflow: hidden; background: #fff; }}
    svg {{ width: 100%; height: 100%; min-height: 620px; display: block; }}
    aside {{ border-left: 1px solid #d9e0ea; background: #fbfcfe; overflow: auto; }}
    .panel {{ padding: 14px; border-bottom: 1px solid #d9e0ea; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }}
    .metric {{ padding: 10px; border: 1px solid #d9e0ea; border-radius: 8px; background: #fff; }}
    .metric b {{ display: block; font-size: 20px; }}
    label {{ display: grid; gap: 5px; margin-top: 10px; color: #5f6d7e; font-size: 12px; font-weight: 650; }}
    input, select {{ min-height: 34px; border: 1px solid #ccd5e1; border-radius: 6px; padding: 7px 9px; font: inherit; }}
    .node {{ cursor: pointer; stroke: #fff; stroke-width: 1.5; }}
    .edge {{ stroke: #9aa8bb; stroke-opacity: .7; stroke-width: 1; marker-end: url(#arrow); }}
    .label {{ fill: #2b3648; font-size: 11px; pointer-events: none; }}
    .url {{ overflow-wrap: anywhere; color: #0f766e; }}
    .issue {{ margin: 8px 0; padding: 8px; border-left: 4px solid #cbd5e1; background: #fff; border-radius: 6px; }}
    .critical, .high {{ border-color: #b42318; }}
    .medium {{ border-color: #b54708; }}
    .low, .info {{ border-color: #667085; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 8px; border-bottom: 1px solid #e7ecf3; text-align: left; vertical-align: top; }}
    th {{ font-size: 12px; color: #5f6d7e; }}
    @media (max-width: 900px) {{ main {{ grid-template-columns: 1fr; }} aside {{ border-left: 0; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Link Graph · {root}</h1>
    <a href="/docs">API docs</a>
  </header>
  <main>
    <section id="graph-wrap"><svg id="graph"></svg></section>
    <aside>
      <div class="panel metrics">
        <div class="metric"><b id="m-pages">0</b>Pages</div>
        <div class="metric"><b id="m-links">0</b>Internal</div>
        <div class="metric"><b id="m-score">0</b>Avg SEO</div>
        <div class="metric"><b id="m-broken">0</b>Broken</div>
      </div>
      <div class="panel">
        <label>Status <select id="status"><option value="">Any</option><option>200</option><option>404</option><option>500</option></select></label>
        <label>Max depth <input id="depth" type="number" min="0" placeholder="Any"></label>
        <label>Min SEO score <input id="score" type="number" min="0" max="100" placeholder="Any"></label>
        <label>Issue code <input id="issue" placeholder="title_duplicate"></label>
      </div>
      <div id="detail" class="panel">Select a node.</div>
      <div class="panel"><table><thead><tr><th>Status</th><th>Score</th><th>URL</th></tr></thead><tbody id="pages"></tbody></table></div>
    </aside>
  </main>
  <script>
    const RESULT = {payload};
    const stats = RESULT.stats || {{}};
    const graph = RESULT.graph || {{nodes: [], edges: []}};
    document.getElementById('m-pages').textContent = stats.total_pages || 0;
    document.getElementById('m-links').textContent = stats.total_internal_links || 0;
    document.getElementById('m-score').textContent = stats.average_seo_score || 0;
    document.getElementById('m-broken').textContent = (stats.broken_pages || []).length;
    const pagesByUrl = RESULT.crawl.pages || {{}};
    const svg = document.getElementById('graph');
    const controls = ['status', 'depth', 'score', 'issue'].map(id => document.getElementById(id));
    controls.forEach(el => el.addEventListener('input', render));

    function filteredNodes() {{
      const st = document.getElementById('status').value;
      const maxDepth = document.getElementById('depth').value;
      const minScore = document.getElementById('score').value;
      const issue = document.getElementById('issue').value.trim();
      return graph.nodes.filter(n => {{
        const p = pagesByUrl[n.url] || {{}};
        const issues = ((p.seo || {{}}).issues || []);
        if (st && String(n.status) !== st) return false;
        if (maxDepth !== '' && Number(n.depth || 0) > Number(maxDepth)) return false;
        if (minScore !== '' && Number(n.seo_score || 0) < Number(minScore)) return false;
        if (issue && !issues.some(i => i.code === issue)) return false;
        return true;
      }});
    }}

    function render() {{
      const nodes = filteredNodes();
      const allowed = new Set(nodes.map(n => n.id));
      const edges = graph.edges.filter(e => allowed.has(e.source) && allowed.has(e.target));
      const w = svg.clientWidth || 900, h = svg.clientHeight || 620;
      const cx = w / 2, cy = h / 2, r = Math.max(120, Math.min(w, h) / 2 - 70);
      nodes.forEach((n, i) => {{
        const a = (Math.PI * 2 * i) / Math.max(nodes.length, 1);
        n.x = cx + Math.cos(a) * r;
        n.y = cy + Math.sin(a) * r;
      }});
      const byId = new Map(nodes.map(n => [n.id, n]));
      svg.innerHTML = '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#9aa8bb"/></marker></defs>';
      for (const e of edges) {{
        const s = byId.get(e.source), t = byId.get(e.target);
        if (!s || !t) continue;
        svg.insertAdjacentHTML('beforeend', `<line class="edge" x1="${{s.x}}" y1="${{s.y}}" x2="${{t.x}}" y2="${{t.y}}"/>`);
      }}
      for (const n of nodes) {{
        const color = n.status >= 400 || n.status === 0 ? '#d92d20' : (n.seo_score < 75 ? '#f79009' : '#0f766e');
        const size = Math.max(7, Math.min(18, Number(n.seo_score || 50) / 6));
        svg.insertAdjacentHTML('beforeend', `<circle class="node" data-id="${{esc(n.id)}}" cx="${{n.x}}" cy="${{n.y}}" r="${{size}}" fill="${{color}}"><title>${{esc(n.url)}} · SEO ${{n.seo_score}}</title></circle>`);
        svg.insertAdjacentHTML('beforeend', `<text class="label" x="${{n.x + size + 4}}" y="${{n.y + 4}}">${{esc(short(n.url))}}</text>`);
      }}
      svg.querySelectorAll('.node').forEach(node => node.addEventListener('click', () => showDetail(byId.get(node.dataset.id))));
      document.getElementById('pages').innerHTML = nodes.map(n => `<tr><td>${{n.status || ''}}</td><td>${{n.seo_score ?? ''}}</td><td class="url">${{esc(n.url)}}</td></tr>`).join('');
    }}

    function showDetail(n) {{
      if (!n) return;
      const p = pagesByUrl[n.url] || {{}};
      const seo = p.seo || {{}};
      const issues = seo.issues || [];
      document.getElementById('detail').innerHTML = `
        <h2>${{esc(p.title || '(no title)')}}</h2>
        <p class="url">${{esc(n.url)}}</p>
        <p>Status <b>${{n.status}}</b> · SEO <b>${{n.seo_score}}</b> · Depth <b>${{n.depth}}</b></p>
        <p>Inbound <b>${{n.inbound}}</b> · Internal out <b>${{n.internal_out}}</b> · External out <b>${{n.external_out}}</b></p>
        <h3>SEO issues</h3>
        ${{issues.length ? issues.map(i => `<div class="issue ${{esc(i.severity)}}"><b>${{esc(i.code)}}</b><br>${{esc(i.message || '')}}</div>`).join('') : '<p>No issues.</p>'}}
      `;
    }}
    function esc(v) {{ return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'": '&#39;'}}[c])); }}
    function short(url) {{ try {{ const u = new URL(url); return (u.pathname || '/') + u.search; }} catch {{ return url; }} }}
    render();
  </script>
</body>
</html>"""
