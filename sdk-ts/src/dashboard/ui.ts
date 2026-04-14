/**
 * Dashboard HTML template — single-page app matching the Patter website style.
 * Port of Python sdk/patter/dashboard/ui.py.
 */

/* eslint-disable no-useless-escape */

export const DASHBOARD_HTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Patter | Dashboard</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1188 1773' fill='none'%3E%3Cpath d='M25 561L245 694M25 561V818M245 694V951M25 961V1218M25 1357V1614M245 1489V1747M245 1093V1351M942 823V1080M1161 955V1213M1162 555V812M942 422V679M669 585V843L787 913M942 25V282M1162 158V415M25 818L245 951M244 1094L464 962M25 961L143 890M244 1352L464 1219M942 823L1162 956M942 679L1162 812M721 811L942 679M669 842L724 809M669 586L724 553M1041 883L1162 812M245 1747L1161 1213M244 1490L942 1080M25 1357L142 1289M518 1071L942 823M721 555L942 422M942 422L1162 556M942 282L1162 415M942 25L1162 158M942 1080L1161 1213M25 1218L245 1351M25 961L245 1094M464 962L519 929M464 1219L519 1186V928L403 859M25 1357L245 1490M25 1614L245 1747M25 561L942 25M244 694L941 282M1043 484L1162 415M245 951L668 704' stroke='%2309090b' stroke-width='50' stroke-linecap='round'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #fdfcfc;
    --fg: #09090b;
    --card: #ffffff;
    --primary: #18181b;
    --primary-fg: #fafafa;
    --secondary: #f4f4f5;
    --muted: #71717b;
    --border: #e4e4e7;
    --border-d: #d4d4d8;
    --green: #22c55e;
    --red: #ef4444;
    --blue: #3b82f6;
    --purple: #a78bfa;
    --orange: #fb923c;
    --yellow: #eab308;
    --radius: 12px;
    --font: 'Instrument Sans', ui-sans-serif, system-ui, sans-serif;
    --mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  html { -webkit-font-smoothing: antialiased; }
  body {
    font-family: var(--font);
    font-size: 15px;
    line-height: 1.6;
    color: var(--fg);
    background: var(--bg);
    min-height: 100vh;
  }

  /* Header */
  header {
    position: sticky; top: 0; z-index: 100;
    background: #fff;
    border-bottom: 1px solid var(--border);
    padding: 0 24px;
    height: 56px;
    display: flex; align-items: center; gap: 14px;
  }
  .logo {
    display: flex; align-items: center; gap: 10px;
    font-weight: 700; font-size: 18px; letter-spacing: -0.02em;
    text-decoration: none; color: var(--fg);
  }
  .logo svg { width: 22px; height: 22px; }
  .header-sep {
    width: 1px; height: 20px; background: var(--border-d); margin: 0 2px;
  }
  .header-title {
    font-size: 14px; font-weight: 500; color: var(--muted);
  }
  .badge-beta {
    font-size: 10px; font-weight: 600; letter-spacing: 0.5px;
    color: #e67e22; background: rgba(230,126,34,0.1);
    border: 1px solid rgba(230,126,34,0.25);
    padding: 2px 8px; border-radius: 100px; text-transform: uppercase;
  }
  .status {
    margin-left: auto; font-size: 13px; color: var(--muted);
    display: flex; align-items: center; gap: 6px;
  }
  .dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--green); display: inline-block;
  }

  /* Layout */
  .container { max-width: 1200px; margin: 0 auto; padding: 24px; }

  /* Stat cards */
  .cards {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 14px; margin-bottom: 28px;
  }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px 20px;
  }
  .card .label {
    font-size: 12px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.5px; font-weight: 500;
  }
  .card .value {
    font-size: 28px; font-weight: 700; margin-top: 4px;
    font-family: var(--mono); letter-spacing: -0.02em;
  }
  .card .sub { font-size: 12px; color: var(--muted); margin-top: 2px; }

  /* Tabs */
  .nav-tabs {
    display: flex; gap: 0; margin-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }
  .nav-tab {
    padding: 10px 20px; font-size: 13px; font-weight: 500;
    color: var(--muted); cursor: pointer;
    border: none; background: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px; font-family: var(--font);
    transition: color .15s;
  }
  .nav-tab:hover { color: var(--fg); }
  .nav-tab.active { color: var(--fg); border-bottom-color: var(--primary); }

  .tab-content { display: none; }
  .tab-content.active { display: block; }

  /* Tables */
  table {
    width: 100%; border-collapse: collapse;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
  }
  th {
    text-align: left; font-size: 11px; text-transform: uppercase;
    color: var(--muted); padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    letter-spacing: 0.5px; font-weight: 600;
    background: var(--secondary);
  }
  td {
    padding: 12px 16px; border-bottom: 1px solid var(--border);
    font-size: 13px;
  }
  tr:last-child td { border-bottom: none; }
  tr.clickable { cursor: pointer; transition: background .1s; }
  tr.clickable:hover { background: var(--secondary); }

  code {
    font-family: var(--mono); font-size: 12px;
    background: var(--secondary); padding: 2px 6px;
    border-radius: 4px;
  }

  /* Badges */
  .badge {
    display: inline-block; padding: 3px 10px; border-radius: 100px;
    font-size: 11px; font-weight: 600;
  }
  .badge-active { background: rgba(34,197,94,0.1); color: #16a34a; }
  .badge-ended { background: var(--secondary); color: var(--muted); }
  .badge-pipeline { background: rgba(167,139,250,0.1); color: #7c3aed; }
  .badge-realtime { background: rgba(59,130,246,0.1); color: #2563eb; }

  .cost { color: #16a34a; font-family: var(--mono); font-size: 13px; }
  .latency { color: #ca8a04; font-family: var(--mono); font-size: 13px; }
  .empty {
    text-align: center; padding: 48px; color: var(--muted);
    font-size: 14px;
  }

  /* Modal */
  .modal-overlay {
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,0.3); backdrop-filter: blur(4px);
    z-index: 200;
    justify-content: center; align-items: flex-start;
    padding: 60px 20px; overflow-y: auto;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    max-width: 800px; width: 100%;
    padding: 28px;
    box-shadow: 0 24px 48px rgba(0,0,0,0.1);
  }
  .modal-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 24px;
  }
  .modal-header h2 { font-size: 16px; font-weight: 600; }
  .modal-close {
    background: none; border: 1px solid var(--border);
    color: var(--muted); width: 32px; height: 32px;
    border-radius: 8px; font-size: 18px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: all .15s;
  }
  .modal-close:hover { background: var(--secondary); color: var(--fg); }

  .detail-grid {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 16px; margin-bottom: 20px;
  }
  .detail-card {
    background: var(--secondary);
    border-radius: var(--radius); padding: 16px;
  }
  .detail-card h3 {
    font-size: 11px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.5px;
    margin-bottom: 10px; font-weight: 600;
  }
  .detail-row {
    display: flex; justify-content: space-between;
    font-size: 13px; padding: 4px 0;
  }
  .detail-row .k { color: var(--muted); }

  .transcript-box {
    background: var(--secondary);
    border-radius: var(--radius);
    padding: 16px; max-height: 300px; overflow-y: auto;
  }
  .transcript-box .msg { padding: 4px 0; font-size: 13px; }
  .transcript-box .msg.user .role { color: var(--blue); }
  .transcript-box .msg.assistant .role { color: #7c3aed; }
  .transcript-box .role { font-weight: 600; margin-right: 8px; }

  /* Turn bars */
  .turns-table { margin-top: 16px; }
  .bar-container { display: flex; height: 14px; border-radius: 4px; overflow: hidden; min-width: 120px; }
  .bar-stt { background: var(--blue); }
  .bar-llm { background: var(--purple); }
  .bar-tts { background: var(--orange); }
</style>
</head>
<body>
<header>
  <a href="/" class="logo">
    <svg viewBox="0 0 1188 1773" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M25 561L245 694M25 561V818M245 694V951M25 961V1218M25 1357V1614M245 1489V1747M245 1093V1351M942 823V1080M1161 955V1213M1162 555V812M942 422V679M669 585V843L787 913M942 25V282M1162 158V415M25 818L245 951M244 1094L464 962M25 961L143 890M244 1352L464 1219M942 823L1162 956M942 679L1162 812M721 811L942 679M669 842L724 809M669 586L724 553M1041 883L1162 812M245 1747L1161 1213M244 1490L942 1080M25 1357L142 1289M518 1071L942 823M721 555L942 422M942 422L1162 556M942 282L1162 415M942 25L1162 158M942 1080L1161 1213M25 1218L245 1351M25 961L245 1094M464 962L519 929M464 1219L519 1186V928L403 859M25 1357L245 1490M25 1614L245 1747M25 561L942 25M244 694L941 282M1043 484L1162 415M245 951L668 704" stroke="currentColor" stroke-width="50" stroke-linecap="round"/>
    </svg>
    Patter
  </a>
  <div class="header-sep"></div>
  <span class="header-title">Dashboard</span>
  <span class="badge-beta">Beta</span>
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
var _$ = function(s) { return document.querySelector(s); };
var _$$ = function(s) { return document.querySelectorAll(s); };

_$$('.nav-tab').forEach(function(tab) {
  tab.addEventListener('click', function() {
    _$$('.nav-tab').forEach(function(t) { t.classList.remove('active'); });
    _$$('.tab-content').forEach(function(t) { t.classList.remove('active'); });
    tab.classList.add('active');
    document.querySelector('#tab-'+tab.dataset.tab).classList.add('active');
  });
});

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function fmtCost(v) { return v >= 0.01 ? '$'+v.toFixed(4) : v > 0 ? '$'+v.toFixed(6) : '$0.00'; }
function fmtMs(v) { return v != null && v >= 0 ? Math.round(v)+'ms' : '-'; }
function fmtDur(s) {
  if (s == null || s < 0) return '-';
  if (s < 60) return Math.round(s)+'s';
  return Math.floor(s/60)+'m '+Math.round(s%60)+'s';
}
function shortId(id) { return id ? esc(id.length > 16 ? id.slice(0,8)+'...'+id.slice(-4) : id) : '-'; }

function fetchJSON(url) {
  return fetch(url).then(function(r) { return r.json(); });
}

function refreshAggregates() {
  return fetchJSON('/api/dashboard/aggregates').then(function(d) {
    _$('#stat-total').textContent = d.total_calls;
    _$('#stat-active').textContent = d.active_calls;
    _$('#stat-cost').textContent = fmtCost(d.total_cost);
    var cb = d.cost_breakdown;
    _$('#stat-cost-breakdown').textContent =
      'STT '+fmtCost(cb.stt)+' | LLM '+fmtCost(cb.llm)+' | TTS '+fmtCost(cb.tts)+' | Tel '+fmtCost(cb.telephony);
    _$('#stat-duration').textContent = fmtDur(d.avg_duration);
    _$('#stat-latency').textContent = fmtMs(d.avg_latency_ms);
  });
}

function refreshCalls() {
  return fetchJSON('/api/dashboard/calls?limit=50').then(function(calls) {
    var body = _$('#calls-body');
    if (!calls.length) {
      body.innerHTML = '<tr><td colspan="8" class="empty">No calls yet. Waiting for incoming calls...</td></tr>';
      return;
    }
    body.innerHTML = calls.map(function(c) {
      var m = c.metrics || {};
      var cost = m.cost || {};
      var lat = m.latency_avg || {};
      var mode = m.provider_mode || '-';
      var turns = m.turns ? m.turns.length : 0;
      var modeClass = mode === 'pipeline' ? 'badge-pipeline' : 'badge-realtime';
      return '<tr class="clickable" onclick="showCall(\\''+esc(c.call_id)+'\\')">'+
        '<td><code>'+shortId(c.call_id)+'</code></td>'+
        '<td>'+(esc(c.direction) || '-')+'</td>'+
        '<td>'+(esc(c.caller) || '-')+' &rarr; '+(esc(c.callee) || '-')+'</td>'+
        '<td>'+fmtDur(m.duration_seconds)+'</td>'+
        '<td><span class="badge '+modeClass+'">'+esc(mode)+'</span></td>'+
        '<td class="cost">'+fmtCost(cost.total || 0)+'</td>'+
        '<td class="latency">'+fmtMs(lat.total_ms || 0)+'</td>'+
        '<td>'+turns+'</td></tr>';
    }).join('');
  });
}

function refreshActive() {
  return fetchJSON('/api/dashboard/active').then(function(active) {
    var body = _$('#active-body');
    if (!active.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">No active calls</td></tr>';
      return;
    }
    var now = Date.now() / 1000;
    body.innerHTML = active.map(function(c) {
      var dur = c.started_at ? Math.round(now - c.started_at) : 0;
      var turns = c.turns ? c.turns.length : 0;
      return '<tr>'+
        '<td><code>'+shortId(c.call_id)+'</code></td>'+
        '<td>'+(esc(c.caller) || '-')+'</td>'+
        '<td>'+(esc(c.callee) || '-')+'</td>'+
        '<td>'+(esc(c.direction) || '-')+'</td>'+
        '<td>'+fmtDur(dur)+'</td>'+
        '<td>'+turns+'</td></tr>';
    }).join('');
  });
}

function showCall(callId) {
  fetchJSON('/api/dashboard/calls/'+encodeURIComponent(callId)).then(function(c) {
    if (c.error) return;
    var m = c.metrics || {};
    var cost = m.cost || {};
    var latAvg = m.latency_avg || {};
    var latP95 = m.latency_p95 || {};
    var turns = m.turns || [];

    _$('#modal-title').textContent = 'Call '+shortId(c.call_id);

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
        '<div class="detail-row"><span class="k">STT</span><span class="cost">'+fmtCost(cost.stt || 0)+'</span></div>'+
        '<div class="detail-row"><span class="k">LLM</span><span class="cost">'+fmtCost(cost.llm || 0)+'</span></div>'+
        '<div class="detail-row"><span class="k">TTS</span><span class="cost">'+fmtCost(cost.tts || 0)+'</span></div>'+
        '<div class="detail-row"><span class="k">Telephony</span><span class="cost">'+fmtCost(cost.telephony || 0)+'</span></div>'+
        '<div class="detail-row" style="border-top:1px solid var(--border);padding-top:8px;margin-top:6px">'+
          '<span class="k" style="font-weight:600">Total</span><span class="cost" style="font-weight:700">'+fmtCost(cost.total || 0)+'</span>'+
        '</div>'+
        '<h3 style="margin-top:16px">Latency (avg / p95)</h3>'+
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
        '<div style="margin-top:10px;font-size:11px;color:var(--muted)">'+
          '<span style="color:var(--blue)">&#9632;</span> STT &nbsp;'+
          '<span style="color:var(--purple)">&#9632;</span> LLM &nbsp;'+
          '<span style="color:var(--orange)">&#9632;</span> TTS'+
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

    _$('#modal-body').innerHTML = html;
    _$('#modal').classList.add('open');
  });
}

function closeModal() { _$('#modal').classList.remove('open'); }
_$('#modal').addEventListener('click', function(e) { if (e.target === _$('#modal')) closeModal(); });
document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeModal(); });

function refresh() {
  return Promise.all([refreshAggregates(), refreshCalls(), refreshActive()]).then(function() {
    _$('#status-text').textContent = 'Listening';
  }).catch(function() {
    _$('#status-text').textContent = 'Connection error';
  });
}

refresh();

if (typeof EventSource !== 'undefined') {
  var sseUrl = '/api/dashboard/events';
  var sseBackoff = 1000;
  var sseFailures = 0;
  var SSE_MAX_BACKOFF = 30000;
  var SSE_MAX_FAILURES = 5;

  function connectSSE() {
    var es = new EventSource(sseUrl);
    function onEvent() { sseBackoff = 1000; sseFailures = 0; }
    es.addEventListener('call_start', function() { onEvent(); refresh(); });
    es.addEventListener('turn_complete', function() { onEvent(); refreshAggregates(); });
    es.addEventListener('call_end', function() { onEvent(); refresh(); });
    es.onerror = function() {
      es.close();
      sseFailures++;
      if (sseFailures >= SSE_MAX_FAILURES) {
        _$('#status-text').textContent = 'Polling';
        setInterval(refresh, 5000);
        return;
      }
      _$('#status-text').textContent = 'Reconnecting...';
      setTimeout(connectSSE, sseBackoff);
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
