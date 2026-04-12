/**
 * Dashboard HTML template - single-page app with no external dependencies.
 * Port of Python sdk/patter/dashboard/ui.py.
 */

export const DASHBOARD_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Patter Dashboard</title>
<style>
  :root {
    --bg: #0a0a0f;
    --surface: #12121a;
    --surface2: #1a1a26;
    --border: #2a2a3a;
    --text: #e4e4ef;
    --text2: #8888a0;
    --accent: #6366f1;
    --accent2: #818cf8;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #eab308;
    --blue: #3b82f6;
    --radius: 8px;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: var(--bg); color: var(--text);
    min-height: 100vh;
  }
  header {
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
    display: flex; align-items: center; gap: 16px;
  }
  header h1 { font-size: 18px; font-weight: 600; }
  header h1 span { color: var(--accent); }
  header .status {
    margin-left: auto; font-size: 13px; color: var(--text2);
    display: flex; align-items: center; gap: 6px;
  }
  header .dot {
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--green); display: inline-block;
  }
  .container { max-width: 1200px; margin: 0 auto; padding: 24px; }
  .cards {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px; margin-bottom: 24px;
  }
  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 16px;
  }
  .card .label { font-size: 12px; color: var(--text2); text-transform: uppercase; letter-spacing: 0.5px; }
  .card .value { font-size: 28px; font-weight: 700; margin-top: 4px; }
  .card .sub { font-size: 12px; color: var(--text2); margin-top: 2px; }
  .section { margin-bottom: 24px; }
  .section h2 { font-size: 15px; font-weight: 600; margin-bottom: 12px; color: var(--text2); }
  table {
    width: 100%; border-collapse: collapse;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); overflow: hidden;
  }
  th { text-align: left; font-size: 11px; text-transform: uppercase;
    color: var(--text2); padding: 10px 14px; border-bottom: 1px solid var(--border);
    letter-spacing: 0.5px;
  }
  td { padding: 10px 14px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr.clickable { cursor: pointer; }
  tr.clickable:hover { background: var(--surface2); }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; font-weight: 600;
  }
  .badge-active { background: rgba(34,197,94,0.15); color: var(--green); }
  .badge-ended { background: rgba(136,136,160,0.15); color: var(--text2); }
  .badge-pipeline { background: rgba(99,102,241,0.15); color: var(--accent2); }
  .badge-realtime { background: rgba(59,130,246,0.15); color: var(--blue); }
  .cost { color: var(--green); }
  .latency { color: var(--yellow); }
  .empty { text-align: center; padding: 40px; color: var(--text2); font-size: 14px; }
  .modal-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,0.7); z-index: 100;
    justify-content: center; align-items: flex-start;
    padding: 60px 20px; overflow-y: auto;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; max-width: 800px; width: 100%;
    padding: 24px;
  }
  .modal-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 20px;
  }
  .modal-header h2 { font-size: 16px; }
  .modal-close {
    background: none; border: none; color: var(--text2);
    font-size: 24px; cursor: pointer;
  }
  .modal-close:hover { color: var(--text); }
  .detail-grid {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 16px; margin-bottom: 20px;
  }
  .detail-card {
    background: var(--surface2); border-radius: var(--radius); padding: 14px;
  }
  .detail-card h3 { font-size: 12px; color: var(--text2); text-transform: uppercase; margin-bottom: 8px; }
  .detail-row { display: flex; justify-content: space-between; font-size: 13px; padding: 3px 0; }
  .detail-row .k { color: var(--text2); }
  .transcript-box {
    background: var(--surface2); border-radius: var(--radius);
    padding: 14px; max-height: 300px; overflow-y: auto;
  }
  .transcript-box .msg { padding: 4px 0; font-size: 13px; }
  .transcript-box .msg.user .role { color: var(--blue); }
  .transcript-box .msg.assistant .role { color: var(--accent2); }
  .transcript-box .role { font-weight: 600; margin-right: 8px; }
  .turns-table { margin-top: 16px; }
  .bar-container { display: flex; height: 14px; border-radius: 3px; overflow: hidden; min-width: 120px; }
  .bar-stt { background: var(--blue); }
  .bar-llm { background: var(--accent); }
  .bar-tts { background: var(--yellow); }
  .nav-tabs {
    display: flex; gap: 4px; margin-bottom: 16px;
    border-bottom: 1px solid var(--border); padding-bottom: 0;
  }
  .nav-tab {
    padding: 8px 16px; font-size: 13px; color: var(--text2);
    cursor: pointer; border: none; background: none;
    border-bottom: 2px solid transparent; margin-bottom: -1px;
  }
  .nav-tab:hover { color: var(--text); }
  .nav-tab.active { color: var(--accent2); border-bottom-color: var(--accent); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
</style>
</head>
<body>
<header>
  <h1><span>Patter</span> Dashboard</h1>
  <div class="status"><span class="dot"></span> <span id="status-text">Listening</span></div>
</header>

<div class="container">
  <div class="cards">
    <div class="card">
      <div class="label">Total Calls</div>
      <div class="value" id="stat-total">0</div>
      <div class="sub"><span id="stat-active">0</span> active</div>
    </div>
    <div class="card">
      <div class="label">Total Cost</div>
      <div class="value cost" id="stat-cost">$0.00</div>
      <div class="sub" id="stat-cost-breakdown">-</div>
    </div>
    <div class="card">
      <div class="label">Avg Duration</div>
      <div class="value" id="stat-duration">0s</div>
    </div>
    <div class="card">
      <div class="label">Avg Latency</div>
      <div class="value latency" id="stat-latency">0ms</div>
      <div class="sub">end-to-end response</div>
    </div>
  </div>

  <div class="nav-tabs">
    <button class="nav-tab active" data-tab="calls">Calls</button>
    <button class="nav-tab" data-tab="active">Active</button>
  </div>

  <div class="tab-content active" id="tab-calls">
    <div class="section">
      <table id="calls-table">
        <thead>
          <tr>
            <th>Call ID</th><th>Direction</th><th>From / To</th>
            <th>Duration</th><th>Mode</th><th>Cost</th><th>Avg Latency</th><th>Turns</th>
          </tr>
        </thead>
        <tbody id="calls-body">
          <tr><td colspan="8" class="empty">No calls yet. Waiting for incoming calls...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="tab-content" id="tab-active">
    <div class="section">
      <table>
        <thead>
          <tr><th>Call ID</th><th>Caller</th><th>Callee</th><th>Direction</th><th>Duration</th><th>Turns</th></tr>
        </thead>
        <tbody id="active-body">
          <tr><td colspan="6" class="empty">No active calls</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-header">
      <h2 id="modal-title">Call Detail</h2>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div id="modal-body"></div>
  </div>
</div>

<script>
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

$$('.nav-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    $$('.nav-tab').forEach(t => t.classList.remove('active'));
    $$('.tab-content').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    document.querySelector('#tab-'+tab.dataset.tab).classList.add('active');
  });
});

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function fmt$(v) { return v >= 0.01 ? '$'+v.toFixed(4) : v > 0 ? '$'+v.toFixed(6) : '$0.00'; }
function fmtMs(v) { return v > 0 ? Math.round(v)+'ms' : '-'; }
function fmtDur(s) {
  if (s == null || s < 0) return '-';
  if (s < 60) return Math.round(s)+'s';
  return Math.floor(s/60)+'m '+Math.round(s%60)+'s';
}
function shortId(id) { return id ? esc(id.length > 16 ? id.slice(0,8)+'...'+id.slice(-4) : id) : '-'; }

async function fetchJSON(url) {
  const r = await fetch(url);
  return r.json();
}

async function refreshAggregates() {
  const d = await fetchJSON('/api/dashboard/aggregates');
  $('#stat-total').textContent = d.total_calls;
  $('#stat-active').textContent = d.active_calls;
  $('#stat-cost').textContent = fmt$(d.total_cost);
  const cb = d.cost_breakdown;
  $('#stat-cost-breakdown').textContent =
    'STT '+fmt$(cb.stt)+' | LLM '+fmt$(cb.llm)+' | TTS '+fmt$(cb.tts)+' | Tel '+fmt$(cb.telephony);
  $('#stat-duration').textContent = fmtDur(d.avg_duration);
  $('#stat-latency').textContent = fmtMs(d.avg_latency_ms);
}

async function refreshCalls() {
  const calls = await fetchJSON('/api/dashboard/calls?limit=50');
  const body = $('#calls-body');
  if (!calls.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">No calls yet. Waiting for incoming calls...</td></tr>';
    return;
  }
  body.innerHTML = calls.map(c => {
    const m = c.metrics || {};
    const cost = m.cost || {};
    const lat = m.latency_avg || {};
    const mode = m.provider_mode || '-';
    const turns = m.turns ? m.turns.length : 0;
    const modeClass = mode === 'pipeline' ? 'badge-pipeline' : 'badge-realtime';
    return '<tr class="clickable" onclick="showCall(\\''+esc(c.call_id)+'\\')">'+
      '<td><code>'+shortId(c.call_id)+'</code></td>'+
      '<td>'+(esc(c.direction) || '-')+'</td>'+
      '<td>'+(esc(c.caller) || '-')+' &rarr; '+(esc(c.callee) || '-')+'</td>'+
      '<td>'+fmtDur(m.duration_seconds)+'</td>'+
      '<td><span class="badge '+modeClass+'">'+esc(mode)+'</span></td>'+
      '<td class="cost">'+fmt$(cost.total || 0)+'</td>'+
      '<td class="latency">'+fmtMs(lat.total_ms || 0)+'</td>'+
      '<td>'+turns+'</td></tr>';
  }).join('');
}

async function refreshActive() {
  const active = await fetchJSON('/api/dashboard/active');
  const body = $('#active-body');
  if (!active.length) {
    body.innerHTML = '<tr><td colspan="6" class="empty">No active calls</td></tr>';
    return;
  }
  const now = Date.now() / 1000;
  body.innerHTML = active.map(c => {
    const dur = c.started_at ? Math.round(now - c.started_at) : 0;
    const turns = c.turns ? c.turns.length : 0;
    return '<tr>'+
      '<td><code>'+shortId(c.call_id)+'</code></td>'+
      '<td>'+(esc(c.caller) || '-')+'</td>'+
      '<td>'+(esc(c.callee) || '-')+'</td>'+
      '<td>'+(esc(c.direction) || '-')+'</td>'+
      '<td>'+fmtDur(dur)+'</td>'+
      '<td>'+turns+'</td></tr>';
  }).join('');
}

async function showCall(callId) {
  const c = await fetchJSON('/api/dashboard/calls/'+encodeURIComponent(callId));
  if (c.error) return;
  const m = c.metrics || {};
  const cost = m.cost || {};
  const latAvg = m.latency_avg || {};
  const latP95 = m.latency_p95 || {};
  const turns = m.turns || [];

  $('#modal-title').textContent = 'Call '+shortId(c.call_id);

  var html = '<div class="detail-grid">'+
    '<div class="detail-card">'+
      '<h3>Overview</h3>'+
      '<div class="detail-row"><span class="k">Call ID</span><span>'+esc(c.call_id)+'</span></div>'+
      '<div class="detail-row"><span class="k">Direction</span><span>'+(esc(c.direction) || '-')+'</span></div>'+
      '<div class="detail-row"><span class="k">From</span><span>'+(esc(c.caller) || '-')+'</span></div>'+
      '<div class="detail-row"><span class="k">To</span><span>'+(esc(c.callee) || '-')+'</span></div>'+
      '<div class="detail-row"><span class="k">Duration</span><span>'+fmtDur(m.duration_seconds)+'</span></div>'+
      '<div class="detail-row"><span class="k">Mode</span><span>'+(esc(m.provider_mode) || '-')+'</span></div>'+
      '<div class="detail-row"><span class="k">STT</span><span>'+(esc(m.stt_provider) || '-')+'</span></div>'+
      '<div class="detail-row"><span class="k">TTS</span><span>'+(esc(m.tts_provider) || '-')+'</span></div>'+
      '<div class="detail-row"><span class="k">LLM</span><span>'+(esc(m.llm_provider) || '-')+'</span></div>'+
      '<div class="detail-row"><span class="k">Telephony</span><span>'+(esc(m.telephony_provider) || '-')+'</span></div>'+
    '</div>'+
    '<div class="detail-card">'+
      '<h3>Cost Breakdown</h3>'+
      '<div class="detail-row"><span class="k">STT</span><span class="cost">'+fmt$(cost.stt || 0)+'</span></div>'+
      '<div class="detail-row"><span class="k">LLM</span><span class="cost">'+fmt$(cost.llm || 0)+'</span></div>'+
      '<div class="detail-row"><span class="k">TTS</span><span class="cost">'+fmt$(cost.tts || 0)+'</span></div>'+
      '<div class="detail-row"><span class="k">Telephony</span><span class="cost">'+fmt$(cost.telephony || 0)+'</span></div>'+
      '<div class="detail-row" style="border-top:1px solid var(--border);padding-top:6px;margin-top:4px">'+
        '<span class="k" style="font-weight:600">Total</span><span class="cost" style="font-weight:700">'+fmt$(cost.total || 0)+'</span>'+
      '</div>'+
      '<h3 style="margin-top:14px">Latency (avg / p95)</h3>'+
      '<div class="detail-row"><span class="k">STT</span><span class="latency">'+fmtMs(latAvg.stt_ms)+' / '+fmtMs(latP95.stt_ms)+'</span></div>'+
      '<div class="detail-row"><span class="k">LLM</span><span class="latency">'+fmtMs(latAvg.llm_ms)+' / '+fmtMs(latP95.llm_ms)+'</span></div>'+
      '<div class="detail-row"><span class="k">TTS</span><span class="latency">'+fmtMs(latAvg.tts_ms)+' / '+fmtMs(latP95.tts_ms)+'</span></div>'+
      '<div class="detail-row"><span class="k">Total</span><span class="latency" style="font-weight:700">'+fmtMs(latAvg.total_ms)+' / '+fmtMs(latP95.total_ms)+'</span></div>'+
    '</div></div>';

  if (turns.length) {
    var maxMs = Math.max.apply(null, turns.map(function(t) {
      var l = t.latency || {};
      return (l.stt_ms||0) + (l.llm_ms||0) + (l.tts_ms||0) + (l.total_ms||0);
    }).concat([1]));
    html += '<div class="detail-card turns-table"><h3>Turns ('+turns.length+')</h3>'+
      '<table><thead><tr><th>#</th><th>User</th><th>Agent</th><th>Latency</th><th>Breakdown</th></tr></thead><tbody>';
    turns.forEach(function(t, i) {
      var l = t.latency || {};
      var total = l.total_ms || ((l.stt_ms||0) + (l.llm_ms||0) + (l.tts_ms||0));
      var scale = total > 0 ? 120 / maxMs : 0;
      var sttW = (l.stt_ms||0) * scale;
      var llmW = (l.llm_ms||0) * scale;
      var ttsW = (l.tts_ms||0) * scale;
      var totalW = total > 0 && sttW === 0 && llmW === 0 && ttsW === 0 ? total * scale : 0;
      html += '<tr>'+
        '<td>'+(t.turn_index !== undefined ? t.turn_index : i)+'</td>'+
        '<td title="'+esc(t.user_text||'')+'">'+esc((t.user_text||'').slice(0,40))+((t.user_text||'').length>40?'...':'')+'</td>'+
        '<td title="'+esc(t.agent_text||'')+'">'+esc((t.agent_text||'').slice(0,40))+((t.agent_text||'').length>40?'...':'')+'</td>'+
        '<td class="latency">'+fmtMs(total)+'</td>'+
        '<td><div class="bar-container">'+
          (sttW > 0 ? '<div class="bar-stt" style="width:'+sttW+'px" title="STT '+fmtMs(l.stt_ms)+'"></div>' : '')+
          (llmW > 0 ? '<div class="bar-llm" style="width:'+llmW+'px" title="LLM '+fmtMs(l.llm_ms)+'"></div>' : '')+
          (ttsW > 0 ? '<div class="bar-tts" style="width:'+ttsW+'px" title="TTS '+fmtMs(l.tts_ms)+'"></div>' : '')+
          (totalW > 0 ? '<div class="bar-llm" style="width:'+totalW+'px" title="Total '+fmtMs(total)+'"></div>' : '')+
        '</div></td></tr>';
    });
    html += '</tbody></table>'+
      '<div style="margin-top:8px;font-size:11px;color:var(--text2)">'+
        '<span style="color:var(--blue)">&#9632;</span> STT &nbsp;'+
        '<span style="color:var(--accent)">&#9632;</span> LLM &nbsp;'+
        '<span style="color:var(--yellow)">&#9632;</span> TTS'+
      '</div></div>';
  }

  var transcript = c.transcript || [];
  if (transcript.length) {
    html += '<div class="detail-card" style="margin-top:16px"><h3>Transcript</h3><div class="transcript-box">';
    transcript.forEach(function(msg) {
      var role = esc(msg.role || 'unknown');
      html += '<div class="msg '+role+'"><span class="role">'+role+'</span>'+esc(msg.text || '')+'</div>';
    });
    html += '</div></div>';
  }

  $('#modal-body').innerHTML = html;
  $('#modal').classList.add('open');
}

function closeModal() { $('#modal').classList.remove('open'); }
$('#modal').addEventListener('click', function(e) { if (e.target === $('#modal')) closeModal(); });
document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeModal(); });

async function refresh() {
  try {
    await Promise.all([refreshAggregates(), refreshCalls(), refreshActive()]);
    $('#status-text').textContent = 'Listening';
  } catch (e) {
    $('#status-text').textContent = 'Connection error';
  }
}

refresh();

if (typeof EventSource !== 'undefined') {
  var tokenParam = new URLSearchParams(window.location.search).get('token');
  var sseUrl = '/api/dashboard/events' + (tokenParam ? '?' + new URLSearchParams({ token: tokenParam }).toString() : '');
  var sseBackoff = 1000;
  var sseFailures = 0;
  var SSE_MAX_BACKOFF = 30000;
  var SSE_MAX_FAILURES = 5;
  var sseTimer = null;

  function connectSSE() {
    var es = new EventSource(sseUrl);
    function onEvent() {
      sseBackoff = 1000;
      sseFailures = 0;
    }
    es.addEventListener('call_start', function() { onEvent(); refresh(); });
    es.addEventListener('turn_complete', function() { onEvent(); refreshAggregates(); });
    es.addEventListener('call_end', function() { onEvent(); refresh(); });
    es.onerror = function() {
      es.close();
      sseFailures++;
      if (sseFailures >= SSE_MAX_FAILURES) {
        $('#status-text').textContent = 'Polling (SSE unavailable)';
        setInterval(refresh, 5000);
        return;
      }
      $('#status-text').textContent = 'Reconnecting...';
      sseTimer = setTimeout(function() {
        connectSSE();
      }, sseBackoff);
      sseBackoff = Math.min(sseBackoff * 2, SSE_MAX_BACKOFF);
    };
  }
  connectSSE();
} else {
  setInterval(refresh, 3000);
}
</script>
</body>
</html>`;
