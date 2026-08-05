APP_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SEO Link Graph Analyzer</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; background: #f7f8fb; color: #172033; font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    header { display: flex; justify-content: space-between; align-items: center; gap: 16px; padding: 16px 24px; background: #fff; border-bottom: 1px solid #d8dee8; }
    h1 { margin: 0; font-size: 18px; letter-spacing: 0; }
    main { width: 100%; max-width: 1220px; margin: 0 auto; padding: 24px; }
    form { display: grid; grid-template-columns: minmax(280px, 1fr) repeat(3, 120px) minmax(160px, 220px) 130px; gap: 10px; align-items: end; padding: 16px; background: #fff; border: 1px solid #d8dee8; border-radius: 8px; }
    label { min-width: 0; display: grid; gap: 5px; color: #637083; font-size: 12px; font-weight: 650; }
    input { width: 100%; min-width: 0; min-height: 38px; border: 1px solid #ccd5e1; border-radius: 6px; padding: 8px 10px; font: inherit; }
    button, .button { min-height: 38px; border: 1px solid #0f766e; border-radius: 6px; padding: 8px 12px; background: #0f766e; color: #fff; font: inherit; font-weight: 650; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; justify-content: center; }
    button.secondary { background: #fff; color: #0f766e; }
    button:disabled { opacity: .55; cursor: wait; }
    .actions { display: flex; gap: 8px; margin-top: 12px; }
    [hidden] { display: none !important; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 16px; }
    .metric, section { min-width: 0; background: #fff; border: 1px solid #d8dee8; border-radius: 8px; }
    .metric { padding: 14px; }
    .metric b { display: block; font-size: 26px; }
    section { margin-top: 16px; overflow: auto; }
    h2 { margin: 0; padding: 12px 14px; border-bottom: 1px solid #d8dee8; font-size: 14px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 9px 10px; border-bottom: 1px solid #edf0f5; text-align: left; vertical-align: top; }
    th { color: #637083; font-size: 12px; }
    .url { overflow-wrap: anywhere; }
    .empty { padding: 16px; color: #637083; }
    @media (max-width: 850px) { form, .grid { grid-template-columns: 1fr 1fr; } label:first-child, form button { grid-column: 1 / -1; } }
    @media (max-width: 560px) { header { padding: 14px; } main { padding: 14px; } form, .grid { grid-template-columns: minmax(0, 1fr); } .actions { flex-wrap: wrap; } }
  </style>
</head>
<body>
  <header><h1>SEO Link Graph Analyzer</h1><span id="state">Idle</span></header>
  <main>
    <form id="form">
      <label>URL <input id="url" type="url" required placeholder="https://example.com"></label>
      <label>Max pages <input id="max_pages" type="number" min="1" max="1000" value="50"></label>
      <label>Max depth <input id="max_depth" type="number" min="0" max="25" value="4"></label>
      <label>Concurrency <input id="concurrency" type="number" min="1" max="16" value="4"></label>
      <label>API key <input id="api_key" type="password" autocomplete="off" placeholder="optional"></label>
      <button id="start" type="submit">Start Scan</button>
    </form>
    <div class="actions">
      <button id="cancel" class="secondary" type="button" hidden>Cancel</button>
      <button id="rerun" class="secondary" type="button" hidden>Rerun</button>
      <button id="dashboard" type="button" hidden>Open graph dashboard</button>
    </div>
    <div class="grid">
      <div class="metric"><b id="pages">0</b>Pages</div>
      <div class="metric"><b id="links">0</b>Internal links</div>
      <div class="metric"><b id="score">0</b>Avg SEO</div>
      <div class="metric"><b id="broken">0</b>Broken pages</div>
    </div>
    <section><h2>Dashboard</h2><div id="dash" class="empty">Run a scan to open the graph.</div></section>
    <section><h2>SEO Issues</h2><table><thead><tr><th>Severity</th><th>Code</th><th>URL</th><th>Message</th></tr></thead><tbody id="issues"></tbody></table></section>
  </main>
  <script>
    let scanId = null, timer = null;
    document.getElementById('form').addEventListener('submit', async event => {
      event.preventDefault();
      clearInterval(timer);
      try {
        const start = document.getElementById('start');
        start.disabled = true;
        state('Creating project');
        const url = document.getElementById('url').value.trim();
        const project = await post('/api/projects', {url});
        const scan = await post(`/api/projects/${project.id}/scans`, {
          max_pages: Number(document.getElementById('max_pages').value),
          max_depth: Number(document.getElementById('max_depth').value),
          concurrency: Number(document.getElementById('concurrency').value)
        });
        activate(scan.id);
      } catch (error) { fail(error); }
    });
    document.getElementById('cancel').addEventListener('click', async () => {
      try { await post(`/api/scans/${scanId}/cancel`, {}); await poll(); } catch (error) { fail(error); }
    });
    document.getElementById('rerun').addEventListener('click', async () => {
      try { activate((await post(`/api/scans/${scanId}/rerun`, {})).id); } catch (error) { fail(error); }
    });
    document.getElementById('dashboard').addEventListener('click', openDashboard);
    function activate(id) {
      scanId = id;
      document.getElementById('start').disabled = true;
      document.getElementById('cancel').hidden = false;
      document.getElementById('rerun').hidden = true;
      document.getElementById('dashboard').hidden = true;
      clearInterval(timer);
      timer = setInterval(poll, 1200);
      poll().catch(fail);
    }
    async function poll() {
      const status = await get(`/api/scans/${scanId}/status`);
      state(status.status + (status.current_url ? ` · ${status.current_url}` : ''));
      if (['completed', 'failed', 'cancelled'].includes(status.status)) {
        clearInterval(timer);
        document.getElementById('start').disabled = false;
        document.getElementById('cancel').hidden = true;
        document.getElementById('rerun').hidden = false;
      }
      if (status.status === 'completed') {
        document.getElementById('dashboard').hidden = false;
        await loadResults();
      } else if (status.status === 'failed') {
        state(`failed · ${status.error || 'Unknown scan error'}`);
      }
    }
    async function loadResults() {
      const [stats, issues] = await Promise.all([
        get(`/api/scans/${scanId}/stats`),
        get(`/api/scans/${scanId}/seo/issues`)
      ]);
      document.getElementById('pages').textContent = stats.total_pages || 0;
      document.getElementById('links').textContent = stats.total_internal_links || 0;
      document.getElementById('score').textContent = stats.average_seo_score || 0;
      document.getElementById('broken').textContent = (stats.broken_pages || []).length;
      document.getElementById('dash').textContent = 'Graph and page SEO data are ready.';
      document.getElementById('issues').innerHTML = issues.slice(0, 100).map(i => `<tr><td>${esc(i.severity)}</td><td>${esc(i.code)}</td><td class="url">${esc(i.url)}</td><td>${esc(i.message || '')}</td></tr>`).join('');
    }
    async function post(url, payload) {
      const r = await fetch(url, {method: 'POST', headers: headers(), body: JSON.stringify(payload)});
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    }
    async function get(url) {
      const r = await fetch(url, {headers: headers(false)});
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    }
    async function openDashboard() {
      const popup = window.open('about:blank', '_blank');
      if (popup) popup.opener = null;
      try {
        const r = await fetch(`/api/scans/${scanId}/dashboard`, {headers: headers(false)});
        if (!r.ok) throw new Error(await r.text());
        const markup = await r.text();
        if (!popup) throw new Error('The browser blocked the dashboard window');
        popup.document.open(); popup.document.write(markup); popup.document.close();
      } catch (error) { if (popup) popup.close(); fail(error); }
    }
    function headers(withJson = true) {
      const h = withJson ? {'Content-Type': 'application/json'} : {};
      const key = document.getElementById('api_key').value.trim();
      if (key) h['X-API-Key'] = key;
      return h;
    }
    function state(value) { document.getElementById('state').textContent = value; }
    function fail(error) {
      clearInterval(timer);
      document.getElementById('start').disabled = false;
      state(`Error · ${error instanceof Error ? error.message : String(error)}`);
    }
    function esc(value) { return String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;', "'": '&#39;'}[c])); }
  </script>
</body>
</html>"""
