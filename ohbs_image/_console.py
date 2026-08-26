CONSOLE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ohbs-image Control Plane</title>
  <link rel="stylesheet" href="/console.css">
</head>
<body>
  <a class="skip" href="#main">Skip to main content</a>
  <header class="topbar">
    <div class="brand"><span class="mark" aria-hidden="true">O</span><div><b>ohbs-image</b><small>CONTROL PLANE</small></div></div>
    <form id="session" class="session" autocomplete="off">
      <label for="token">Bearer Token</label>
      <input id="token" name="token" type="password" required placeholder="Kept in page memory only">
      <button type="submit">Connect</button>
      <button id="disconnect" class="quiet" type="button">Disconnect</button>
    </form>
  </header>
  <main id="main">
    <section class="intro">
      <div><p class="eyebrow">GOLDEN IMAGE OPERATIONS</p><h1>Trusted images,<br>at a glance.</h1><p>A read-first workspace for teams to inspect artifacts, lifecycle state, and control-plane health.</p></div>
      <div id="connection" class="connection" role="status"><span></span>Disconnected</div>
    </section>
    <section class="metrics" aria-label="Artifact summary">
      <article><span>VISIBLE ARTIFACTS</span><strong id="total">—</strong><small>Within identity scope</small></article>
      <article><span>ACTIVE</span><strong id="active">—</strong><small>Available to consume</small></article>
      <article><span>QUARANTINED</span><strong id="quarantined">—</strong><small>Held for review</small></article>
      <article><span>REVOKED</span><strong id="revoked">—</strong><small>Permanently blocked</small></article>
      <article><span>RUNS</span><strong id="runs">—</strong><small>Within identity scope</small></article>
      <article><span>REBUILD QUEUE</span><strong id="rebuilds">—</strong><small>Queued requests</small></article>
    </section>
    <section class="panel">
      <div class="panel-head"><div><p class="eyebrow">REGISTRY</p><h2>Artifact inventory</h2></div><div class="tools"><label for="bucket">Bucket</label><input id="bucket" placeholder="All authorized buckets"><button id="refresh" type="button">Refresh</button></div></div>
      <div id="notice" class="notice">Enter a token to connect. Tokens are never written to browser storage.</div>
      <div class="table-wrap"><table><thead><tr><th>Artifact</th><th>Bucket / Version</th><th>Platform</th><th>Status</th><th>Created</th></tr></thead><tbody id="artifacts"><tr><td colspan="5" class="empty">Waiting for connection</td></tr></tbody></table></div>
    </section>
    <section class="split">
      <article class="panel"><div class="panel-head"><div><p class="eyebrow">OBSERVABILITY</p><h2>Control-plane metrics</h2></div></div><pre id="metrics">Prometheus metrics load after connection</pre></article>
      <article class="panel guide"><p class="eyebrow">TRUST BOUNDARY</p><h2>Security model</h2><ol><li><b>Read first</b><span>Governance writes are not exposed in this console.</span></li><li><b>Identity scoped</b><span>The server filters results by Bucket authorization.</span></li><li><b>Memory-only session</b><span>Refreshing the page clears the token.</span></li></ol></article>
    </section>
  </main>
  <footer>ohbs-image · evidence-first golden image governance</footer>
  <script src="/console.js" defer></script>
</body>
</html>
""".encode()


CONSOLE_CSS = b""":root{--ink:#12231c;--muted:#65736d;--line:#d5dcd7;--paper:#eef2ef;--card:#fff;--green:#0d6644;--lime:#b9dc68;--amber:#bd6e14;--red:#a73b31;--radius:12px}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}.skip{position:absolute;left:-999px}.skip:focus{left:12px;top:12px;background:#fff;padding:8px;z-index:3}.topbar{height:72px;padding:0 max(24px,calc((100vw - 1280px)/2));display:flex;align-items:center;justify-content:space-between;background:var(--ink);color:#fff}.brand{display:flex;align-items:center;gap:10px}.brand .mark{display:grid;place-items:center;width:34px;height:34px;border:2px solid var(--lime);border-radius:50%;font-weight:900;color:var(--lime)}.brand b,.brand small{display:block}.brand small{font:9px/1.2 ui-monospace,monospace;letter-spacing:.17em;color:#9eb0a8}.session{display:flex;align-items:center;gap:8px}.session label{font-size:11px;color:#bdc9c4}.session input,.tools input{border:1px solid #52645c;background:#20372e;color:#fff;padding:9px 11px;border-radius:7px;min-width:230px}.session button,.tools button{border:0;border-radius:7px;padding:9px 14px;background:var(--lime);color:var(--ink);font-weight:800;cursor:pointer}.session .quiet{background:transparent;color:#d4ddd9;border:1px solid #52645c}button:focus-visible,input:focus-visible{outline:3px solid var(--lime);outline-offset:2px}main{max-width:1280px;margin:auto;padding:42px 24px 70px}.intro{display:flex;align-items:end;justify-content:space-between;gap:30px;margin-bottom:28px}.eyebrow{margin:0 0 7px;font:800 10px/1.2 ui-monospace,monospace;letter-spacing:.16em;color:var(--green)}h1{font-size:clamp(36px,5vw,66px);letter-spacing:-.06em;line-height:1;margin:0 0 16px}h2{font-size:20px;margin:0;letter-spacing:-.025em}.intro>div>p:last-child{max-width:680px;color:var(--muted);font-size:16px}.connection{background:#e1e6e3;color:var(--muted);border-radius:99px;padding:8px 13px;font-weight:700}.connection span{display:inline-block;width:8px;height:8px;margin-right:7px;background:#8c9993;border-radius:50%}.connection.online{background:#dcebdc;color:var(--green)}.connection.online span{background:var(--green)}.metrics{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--line);background:var(--card);margin-bottom:20px}.metrics article{padding:20px;border-right:1px solid var(--line)}.metrics article:last-child{border:0}.metrics span,.metrics small{display:block;color:var(--muted);font-size:10px;letter-spacing:.08em}.metrics strong{display:block;font:800 34px/1.2 ui-monospace,monospace;margin:7px 0}.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:22px;margin-bottom:20px}.panel-head{display:flex;align-items:end;justify-content:space-between;gap:20px;margin-bottom:18px}.tools{display:flex;align-items:center;gap:8px}.tools label{font-size:11px;color:var(--muted)}.tools input{background:#fff;color:var(--ink);border-color:var(--line);min-width:190px}.tools button{background:var(--ink);color:#fff}.notice{padding:10px 12px;background:#eef4e7;border-left:3px solid var(--lime);margin-bottom:14px;color:#435249}.notice.error{background:#f8e9e6;border-color:var(--red);color:var(--red)}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;min-width:760px}th,td{text-align:left;border-bottom:1px solid var(--line);padding:12px 10px}th{font:800 10px ui-monospace,monospace;letter-spacing:.08em;color:var(--muted)}td code{font-size:12px}.pill{display:inline-block;padding:3px 8px;border-radius:99px;background:#dcebdd;color:var(--green);font-size:11px;font-weight:800}.pill.quarantined{background:#faead3;color:var(--amber)}.pill.revoked{background:#f5ddda;color:var(--red)}.empty{text-align:center;color:var(--muted);padding:35px}.split{display:grid;grid-template-columns:1.7fr 1fr;gap:20px}.split .panel{margin:0}pre{white-space:pre-wrap;max-height:310px;overflow:auto;background:#10251c;color:#cbe0d6;padding:16px;border-radius:8px;font:11px/1.6 ui-monospace,monospace}.guide ol{padding:0;list-style:none}.guide li{border-top:1px solid var(--line);padding:14px 0}.guide li b,.guide li span{display:block}.guide li span{color:var(--muted);font-size:12px;margin-top:3px}footer{border-top:1px solid var(--line);padding:25px;text-align:center;color:var(--muted);font-size:11px}@media(max-width:850px){.topbar{height:auto;padding:16px 20px;align-items:flex-start;gap:16px}.session{flex-wrap:wrap;justify-content:flex-end}.session label{display:none}.session input{min-width:170px}.metrics{grid-template-columns:1fr 1fr}.metrics article:nth-child(2){border-right:0}.split{grid-template-columns:1fr}.intro{align-items:flex-start;flex-direction:column}.panel-head{align-items:flex-start;flex-direction:column}.tools{width:100%;flex-wrap:wrap}.tools input{flex:1}}@media(max-width:520px){.topbar{display:block}.session{margin-top:14px;justify-content:flex-start}.metrics{grid-template-columns:1fr}.metrics article{border-right:0;border-bottom:1px solid var(--line)}main{padding:28px 12px}.panel{padding:16px}}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}"""


CONSOLE_JS = """(() => {
  'use strict';
  let token = '';
  const byId = (id) => document.getElementById(id);
  const session = byId('session');
  const notice = byId('notice');
  const connection = byId('connection');
  const body = byId('artifacts');
  function setNotice(message, error) { notice.textContent = message; notice.classList.toggle('error', Boolean(error)); }
  function setConnected(value) { connection.classList.toggle('online', value); connection.lastChild.textContent = value ? 'Connected' : 'Disconnected'; }
  function cell(row, value, code) { const td = document.createElement('td'); const el = code ? document.createElement('code') : document.createElement('span'); el.textContent = value || '—'; td.appendChild(el); row.appendChild(td); }
  function render(rows) {
    body.textContent = '';
    const counts = {active: 0, quarantined: 0, revoked: 0};
    rows.forEach((item) => {
      const tr = document.createElement('tr');
      cell(tr, item.artifact_id || item.image_id, true);
      cell(tr, [item.bucket, item.version].filter(Boolean).join(' / '));
      cell(tr, item.platform || item.profile || item.os);
      const td = document.createElement('td'); const pill = document.createElement('span'); const status = item.status || 'unknown'; pill.className = 'pill ' + status; pill.textContent = status; td.appendChild(pill); tr.appendChild(td);
      cell(tr, item.created_at || item.registered_at);
      body.appendChild(tr); if (Object.hasOwn(counts, status)) counts[status] += 1;
    });
    if (!rows.length) { const tr = document.createElement('tr'); const td = document.createElement('td'); td.colSpan = 5; td.className = 'empty'; td.textContent = 'No artifacts are visible in the current scope'; tr.appendChild(td); body.appendChild(tr); }
    byId('total').textContent = String(rows.length); Object.keys(counts).forEach((key) => { byId(key).textContent = String(counts[key]); });
  }
  async function request(path, text) { const response = await fetch(path, {headers: {'Authorization': 'Bearer ' + token}}); if (!response.ok) { let message = 'HTTP ' + response.status; try { const problem = (await response.json()).error; message = typeof problem === 'string' ? problem : problem.message || message; } catch (_) {} throw new Error(message); } return text ? response.text() : response.json(); }
  async function load() {
    if (!token) { setNotice('Enter a Bearer token first.', true); return; }
    const bucket = byId('bucket').value.trim(); const query = bucket ? '?bucket=' + encodeURIComponent(bucket) : '';
    setNotice('Reading the control plane…', false);
    try { const [registry, runs, rebuilds, metrics] = await Promise.all([request('/api/v1/artifacts' + query, false), request('/api/v1/runs?limit=1', false), request('/api/v1/rebuild-requests?status=queued&limit=1', false), request('/api/v1/metrics', true)]); render(registry.artifacts || []); byId('runs').textContent = String(runs.count || 0); byId('rebuilds').textContent = String(rebuilds.count || 0); byId('metrics').textContent = metrics; setConnected(true); setNotice('Data is scoped to the current identity and its authorized Buckets.', false); }
    catch (error) { setConnected(false); setNotice(error.message, true); }
  }
  session.addEventListener('submit', (event) => { event.preventDefault(); token = byId('token').value; byId('token').value = ''; load(); });
  byId('refresh').addEventListener('click', load);
  byId('disconnect').addEventListener('click', () => { token = ''; setConnected(false); render([]); byId('runs').textContent = '—'; byId('rebuilds').textContent = '—'; byId('metrics').textContent = 'Prometheus metrics load after connection'; setNotice('Session cleared.', false); });
})();
""".encode()
