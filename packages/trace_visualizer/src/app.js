import { snapshot, footprintSeries, lowerBound, timelineTotals } from './core.js';
import { TreeView } from './tree.js';
import { exampleTrace } from './demo.js';

const $ = id => document.getElementById(id);
const fmt = value => Math.round(value).toLocaleString();
const compact = value => Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(value);
const clock = seconds => {
  const s = Math.max(0, Math.floor(seconds));
  return (s >= 3600 ? `${Math.floor(s / 3600)}:` : '') + `${String(Math.floor(s / 60) % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
};
let worker, summaries = [], data = null, snap = null, time = 0, mode = 'cumulative';
let selected = null, focused = null, currentRequest = null, playing = false, busy = false;
let token = 0, blockOffset = 0, series = [], timelineHits = [], framePending = false, lastTick = 0;
let sourceName = '';
let highlightedAgent = null;
let rootSignature = '';
const expandedSessions = new Set();
const timelineTooltip = document.createElement('div');
timelineTooltip.id = 'timeline-tooltip'; timelineTooltip.role = 'tooltip'; timelineTooltip.hidden = true;
const tooltipContent = document.createElement('span');
const tooltipClose = document.createElement('button');
tooltipClose.textContent = '×'; tooltipClose.setAttribute('aria-label', 'Close pinned popup'); tooltipClose.hidden = true;
timelineTooltip.append(tooltipContent, tooltipClose);
document.body.append(timelineTooltip);
let tooltipTimer, tooltipHideTimer, tooltipSwitchTimer, tooltipTarget = null, tooltipPinned = false;
function hideTimelineTooltip(force = false) {
  if (tooltipPinned && force !== true) return;
  if (force === true) clearTimeout(tooltipSwitchTimer);
  clearTimeout(tooltipHideTimer);
  tooltipPinned = false; tooltipClose.hidden = true; timelineTooltip.classList.remove('pinned');
  timelineTooltip.role = 'tooltip';
  clearTimeout(tooltipTimer);
  tooltipTarget?.removeAttribute('aria-describedby');
  tooltipTarget = null; timelineTooltip.hidden = true;
}
function queueTimelineTooltip(button, delay = 1000) {
  clearTimeout(tooltipSwitchTimer);
  if (tooltipPinned) return;
  if (!timelineTooltip.hidden) {
    if (tooltipTarget === button) { clearTimeout(tooltipHideTimer); return; }
    // Crossing another row must not replace the popup during the travel grace period.
    tooltipSwitchTimer = setTimeout(() => {
      if (button.matches(':hover') || document.activeElement === button) {
        hideTimelineTooltip(); queueTimelineTooltip(button, 0);
      }
    }, 1000);
    return;
  }
  hideTimelineTooltip(); tooltipTarget = button;
  tooltipTimer = setTimeout(() => {
    if (!button.isConnected) return hideTimelineTooltip();
    tooltipContent.textContent = button.dataset.tooltip;
    timelineTooltip.hidden = false; button.setAttribute('aria-describedby', timelineTooltip.id);
    const rect = button.getBoundingClientRect(), popup = timelineTooltip.getBoundingClientRect();
    timelineTooltip.style.left = `${Math.max(8, Math.min(rect.left, innerWidth - popup.width - 8))}px`;
    timelineTooltip.style.top = `${Math.max(8, rect.top - popup.height - 8 >= 8 ? rect.top - popup.height - 8 : Math.min(rect.bottom + 8, innerHeight - popup.height - 8))}px`;
  }, delay);
}
function leaveTimelineTooltip() {
  clearTimeout(tooltipSwitchTimer);
  clearTimeout(tooltipHideTimer);
  tooltipHideTimer = setTimeout(() => {
    if (!timelineTooltip.matches(':hover') && !tooltipTarget?.matches(':hover')) hideTimelineTooltip();
  }, 300);
}
timelineTooltip.onpointerenter = () => { clearTimeout(tooltipHideTimer); clearTimeout(tooltipSwitchTimer); };
timelineTooltip.onpointerleave = leaveTimelineTooltip;
timelineTooltip.onclick = () => {
  tooltipPinned = true; clearTimeout(tooltipHideTimer); clearTimeout(tooltipSwitchTimer);
  tooltipClose.hidden = false; timelineTooltip.classList.add('pinned'); timelineTooltip.role = 'dialog';
  timelineTooltip.setAttribute('aria-label', 'Pinned timeline details');
};
tooltipClose.onclick = event => { event.stopPropagation(); hideTimelineTooltip(true); };
document.addEventListener('click', event => {
  if (tooltipPinned && !timelineTooltip.contains(event.target)) hideTimelineTooltip(true);
});
document.addEventListener('scroll', () => {
  if (tooltipPinned) return;
  hideTimelineTooltip();
  // Scrolling a row into view can finish after pointerenter or keyboard focus.
  const target = document.querySelector('#timeline-agents button:hover') || document.activeElement?.closest('#timeline-agents button');
  if (target) queueTimelineTooltip(target);
}, true);
document.addEventListener('keydown', event => { if (event.key === 'Escape' && !tooltipPinned) hideTimelineTooltip(); });
const agentLabel = key => {
  const session = data?.sessions?.find(s => s.agents.some(a => a.key === key));
  const name = session?.agents.find(a => a.key === key)?.name ?? key;
  return `${session ? `S${session.index + 1} · ` : ""}${name === "main" ? "Main agent" : name}`;
};
const tree = new TreeView($('tree'), selectNode);

function createWorker() {
  worker?.terminate();
  worker = new Worker(new URL('./worker.js', import.meta.url), { type: 'module' });
  worker.onmessage = ({ data: message }) => {
    if (message.type === 'progress') $('progress').textContent = `Trace ${message.current}/${message.total ?? '…'}`;
    if (message.type === 'loaded') {
      summaries = message.summaries; sourceName = message.name;
      $('filename').textContent = message.name; $('filename').title = message.name;
      options($('session'), summaries.map((s, index) => [index, `${s.id} · ${fmt(s.count)} requests · ${s.models.length} model${s.models.length === 1 ? '' : 's'}`]));
      selectTrace(0);
    }
    if (message.type === 'trace' && message.token === token) {
      data = message.data; selected = null; focused = null; currentRequest = null; time = 0;
      $('filename').textContent = sourceName; $('filename').title = `${sourceName} · ${message.data.id}`;
      options($('model'), [['*', 'All models'], ...data.models.map(m => [m, m])]);
      options($('agent'), [['*', 'All agents'], ...data.agents.map(a => [a, agentLabel(a)])]);
      $('scrub').max = String(data.duration || 1); $('scrub').value = String(time);
      $('empty').hidden = data.requests.length > 0;
      if (!data.requests.length) { $('empty').querySelector('h2').textContent = 'This trace has no requests'; }
      setBusy(false); recomputeSeries(); layout(); render();
    }
    if (message.type === 'blocks' && message.token === token && message.id === selected) showBlocks(message);
    if (message.type === 'error' && (message.token === undefined || message.token === token)) fail(message.message);
  };
  worker.onerror = event => fail(event.message || 'The background worker stopped. Try reopening the file.');
}

function options(select, values) {
  select.replaceChildren(...values.map(([value, label]) => { const o = document.createElement('option'); o.value = value; o.textContent = label; return o; }));
}

function notice(message = '') { $('notice').hidden = !message; $('notice').textContent = message; }
function setBusy(value, detail = '') {
  busy = value; $('loading').hidden = !value; $('loading-detail').textContent = detail;
  for (const id of ['session', 'concurrency', 'model', 'agent', 'play', 'previous', 'next', 'scrub']) $(id).disabled = value || !data;
  $('open').disabled = value; $('demo').disabled = value;
}
function pause() { playing = false; $('play').textContent = '▶ Play'; $('play').setAttribute('aria-label', 'Play timeline'); }
function clearView() {
  hideTimelineTooltip(true);
  data = null; snap = null; selected = null; focused = null; currentRequest = null; series = []; highlightedAgent = null; expandedSessions.clear();
  $('timeline-agents').replaceChildren(); $('clear-agent-highlight').hidden = true;
  delete $('timeline-agents').dataset.agents;
  tree.data = null; tree.draw(); $('selection').hidden = true; $('empty').hidden = false; $('unfocus').hidden = true;
  $('selection-subtitle').textContent = 'Click a segment to explore its accesses';
  for (const id of ['stat-requests', 'stat-blocks', 'stat-footprint', 'stat-input']) $(id).textContent = '—';
  for (const id of ['segments', 'model-breakdown']) $(id).replaceChildren();
  $('filename').textContent = 'Your data stays in this browser';
  $('clock').textContent = '00:00 / 00:00'; $('request-detail').textContent = ''; $('tree-info').textContent = 'Root at top · prefixes grow downward';
  $('scrub').value = '0'; $('active-requests').textContent = '0 active requests'; $('active-sessions').textContent = '0 active conversations';
  for (const id of ['timeline', 'footprint-chart', 'access-chart']) { const canvas = $(id); canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height); }
}
function fail(message) { pause(); clearView(); setBusy(false); notice(message); }

function loadFile(file) {
  if (!file) return;
  pause(); token++; notice(); clearView();
  $('empty').querySelector('h2').textContent = 'Follow the shape of your workload';
  $('progress').textContent = 'Trace 0/…';
  createWorker(); setBusy(true);
  worker.postMessage({ type: 'load', file });
}
function selectTrace(index) {
  pause(); token++; clearView(); setBusy(true, 'Building prefix trees…');
  $('progress').textContent = `Trace ${index + 1}/${summaries.length}`;
  const concurrency = Math.max(1, Math.min(summaries.length, Math.floor(Number($('concurrency').value)) || 1));
  $('concurrency').value = concurrency;
  worker.postMessage({ type: 'select', index, concurrency, token });
}
function layout() {
  if (!data) return;
  const rootCounts = snapshot(data, filters()).counts;
  rootSignature = data.roots.filter(id => rootCounts[id]).join(',');
  const priorityCounts = highlightedAgent === null ? null : snapshot(data, { ...filters(), time: data.duration, mode: 'cumulative', agent: highlightedAgent }).counts;
  tree.layout(data, { model: $('model').value, focus: focused, levels: Number($('levels').value), layout: $('layout').value, priorityCounts, rootCounts });
  $('unfocus').hidden = focused === null;
  scheduleRender();
}
function filters() { return { time, mode, window: Math.max(.001, Number($('window-size').value) || 60), model: $('model').value, agent: $('agent').value }; }
function matches(request) { return ($('model').value === '*' || request.model === $('model').value) && ($('agent').value === '*' || request.agent === $('agent').value); }
function inRange(id) { return id >= snap.lo && id < snap.hi && matches(data.requests[id]); }
function activeVisits(node) { return [...node.visits.subarray(lowerBound(node.visits, snap.lo), lowerBound(node.visits, snap.hi))].filter(i => matches(data.requests[i])); }
function recomputeSeries() { if (data) series = footprintSeries(data, $('model').value, $('agent').value); }
function scheduleRender() {
  if (framePending) return;
  framePending = true;
  requestAnimationFrame(() => { framePending = false; render(); });
}
function render() {
  if (!data || busy) return;
  snap = snapshot(data, filters());
  if (data.roots.filter(id => snap.counts[id]).join(',') !== rootSignature) layout();
  $('active-requests').textContent = `${fmt(data.requests.filter(r => r.t <= time && time < r.t + r.api_time).length)} active requests`;
  $('active-sessions').textContent = `${data.sessions.filter(s => s.start <= time && time < s.end).length} / ${$('concurrency').value} active conversations`;
  $('stat-requests').textContent = `${fmt(snap.processed)} / ${fmt(snap.total)}`;
  $('stat-blocks').textContent = fmt(snap.blocks);
  $('stat-footprint').textContent = fmt(snap.blocks * data.blockSize);
  $('stat-input').textContent = `${compact(snap.input)} tokens`;
  $('stat-scope').textContent = `${mode === 'window' ? 'Selected window' : 'Cumulative'} · ${$('model').value === '*' ? 'all models' : 'one model'}`;
  $('block-size').textContent = `${data.blockSize} token slots per block`;
  $('clock').textContent = `${clock(time)} / ${clock(data.duration)}`;
  $('scrub').value = String(time);
  $('chart-end').textContent = clock(data.duration);
  let max = 0;
  for (const id of data.roots) max = Math.max(max, snap.counts[id]);
  $('heat-max').textContent = fmt(max);
  $('tree-info').textContent = snap.processed ? `${fmt(tree.positions.filter(p => snap.counts[p.id]).length)} visible segments · scroll to zoom · drag to pan` : 'No requests in this time range';
  const agentCounts = highlightedAgent === null ? null : snapshot(data, { ...filters(), agent: highlightedAgent }).counts;
  if (agentCounts) $('tree-info').textContent += ` · highlighted: ${agentLabel(highlightedAgent)}`;
  $('clear-agent-highlight').hidden = highlightedAgent === null;
  tree.update({ counts: snap.counts, endpoints: snap.endpoints, selected, request: currentRequest !== null && inRange(currentRequest) ? currentRequest : null, logarithmic: $('log').checked, agentCounts });
  renderInspector(); renderTimeline(); drawFootprint(); renderSegmentList();
  const request = currentRequest === null ? null : data.requests[currentRequest];
  $('request-detail').textContent = request ? `#${currentRequest + 1} · ${agentLabel(request.agent)} · ${fmt(request.in)} in / ${fmt(request.out)} out` : '';
  $('request-detail').title = request ? `${request.model} · start ${request.t}s · API ${request.api_time}s${request.ttft === undefined ? '' : ` · TTFT ${request.ttft}s`}` : '';
}

function selectNode(id) {
  selected = id; blockOffset = 0; $('block-panel').hidden = true;
  scheduleRender();
}
function selectRequest(id) {
  pause();
  if (highlightedAgent !== null) { highlightedAgent = null; layout(); }
  currentRequest = id; time = data.requests[id].t; scheduleRender();
}
function highlightAgent(agent) {
  if (!data || busy) return;
  pause(); highlightedAgent = highlightedAgent === agent ? null : agent;
  currentRequest = null; selected = null; focused = null;
  layout();
}
function seek(value) {
  if (!data) return;
  time = Math.max(0, Math.min(data.duration, value));
  currentRequest = null;
  for (let i = data.requests.length - 1; i >= 0; i--) if (data.requests[i].t <= time && matches(data.requests[i])) { currentRequest = i; break; }
  scheduleRender();
}

function renderInspector() {
  $('selection').hidden = selected === null;
  if (selected !== null) {
    const node = data.nodes[selected], visits = activeVisits(node);
    $('selection-subtitle').textContent = `${node.model} · ${node.blocks ? `blocks ${fmt(node.depth - node.blocks + 1)}–${fmt(node.depth)}` : 'model root'}`;
    $('sel-blocks').textContent = fmt(node.blocks); $('sel-slots').textContent = fmt(node.blocks * data.blockSize);
    $('sel-count').textContent = fmt(visits.length); $('sel-ends').textContent = fmt(snap.endpoints[selected]);
    $('inspect').disabled = !node.blocks;
    const byAgent = new Map();
    for (const id of visits) { const a = data.requests[id].agent; byAgent.set(a, (byAgent.get(a) || 0) + 1); }
    $('agent-bars').replaceChildren(...[...byAgent].sort((a, b) => b[1] - a[1]).map(([agent, count]) => {
      const row = document.createElement('div'); row.className = 'agent-bar';
      const name = document.createElement('span'); name.textContent = agentLabel(agent); name.title = agent;
      const bar = document.createElement('i'); bar.style.width = `${100 * count / Math.max(1, visits.length)}%`;
      const number = document.createElement('b'); number.textContent = fmt(count);
      row.append(name, bar, number); return row;
    }));
    if (!visits.length) $('agent-bars').textContent = 'No accesses in this time range.';
    drawAccess(visits);
    $('child-prefixes').replaceChildren(...node.children.map(id => {
      const child = data.nodes[id], button = document.createElement('button');
      button.textContent = `${fmt(child.blocks)} blocks · ${fmt(snap.counts[id])} requests →`;
      button.title = `Prefix depth ${child.depth} blocks`;
      button.onclick = () => { selectNode(id); focused = id; layout(); };
      return button;
    }));
    if (!node.children.length) $('child-prefixes').textContent = 'This prefix has no child branches.';
    const chosen = visits.slice(-50).reverse();
    $('request-list').replaceChildren(...chosen.map(id => {
      const r = data.requests[id], button = document.createElement('button');
      button.className = currentRequest === id ? 'selected' : '';
      button.textContent = `#${id + 1} · ${clock(r.t)} · ${agentLabel(r.agent)}`;
      button.title = `${r.t}s · ${r.model} · ${fmt(r.in)} input tokens`;
      button.onclick = () => selectRequest(id); return button;
    }));
    if (visits.length > 50) { const hint = document.createElement('small'); hint.textContent = `Latest 50 of ${fmt(visits.length)} accesses`; $('request-list').append(hint); }
  } else $('selection-subtitle').textContent = 'Click a segment to explore its accesses';
  const modelBlocks = new Map(data.models.map(model => [model, 0]));
  for (const n of data.nodes) if (snap.counts[n.id]) modelBlocks.set(n.model, modelBlocks.get(n.model) + n.blocks);
  $('model-breakdown').replaceChildren(...data.models.filter(model => $('model').value === '*' || model === $('model').value).map(model => {
    const blocks = modelBlocks.get(model);
    const row = document.createElement('div'); row.className = 'model-row';
    const name = document.createElement('span'); name.textContent = model; name.title = model;
    const count = document.createElement('strong'); count.textContent = `${compact(blocks * data.blockSize)} slots`;
    row.append(name, count); return row;
  }));
}

function renderSegmentList() {
  if (!$('segments').parentElement.open) return;
  const positions = tree.positions.filter(p => snap.counts[p.id]);
  $('segments').replaceChildren(...positions.slice(0, 200).map(p => {
    const node = data.nodes[p.id], button = document.createElement('button');
    button.textContent = `${node.blocks ? `${node.blocks} blocks at depth ${node.depth}` : node.model} · ${snap.counts[p.id]} requests`;
    button.onclick = () => selectNode(p.id); return button;
  }));
  if (positions.length > 200) { const text = document.createElement('small'); text.textContent = 'Showing 200 segments. Focus a subtree for more detail.'; $('segments').append(text); }
}

function prepareCanvas(canvas, height = 88) {
  const width = canvas.parentElement.clientWidth - (canvas.id === 'timeline' ? 0 : 2);
  const dpr = devicePixelRatio || 1;
  canvas.style.height = `${height}px`;
  canvas.width = Math.max(1, Math.round(width * dpr)); canvas.height = Math.round(height * dpr);
  const c = canvas.getContext('2d'); c.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { c, width, height };
}
function drawFootprint() {
  const { c, width, height } = prepareCanvas($('footprint-chart'));
  const max = Math.max(1, series.at(-1)?.[1] || 0);
  const x = t => 2 + t / (data.duration || 1) * (width - 4), y = v => height - 8 - v / max * (height - 24);
  c.strokeStyle = '#e3eae6'; c.lineWidth = 1;
  for (let i = 0; i < 3; i++) { const yy = 16 + i * 29; c.beginPath(); c.moveTo(0, yy); c.lineTo(width, yy); c.stroke(); }
  const path = new Path2D(); path.moveTo(2, height - 8);
  let previous = 0;
  for (const [t, value] of series) { path.lineTo(x(t), y(previous)); path.lineTo(x(t), y(value)); previous = value; }
  path.lineTo(width, y(previous)); c.strokeStyle = '#c3d5cd'; c.lineWidth = 1.6; c.stroke(path);
  c.save(); c.beginPath(); c.rect(0, 0, x(time), height); c.clip(); c.strokeStyle = '#087f76'; c.stroke(path); c.restore();
  c.fillStyle = '#647b70'; c.font = '9px system-ui'; c.fillText(compact(max), 3, 10);
  c.strokeStyle = '#11a796'; c.beginPath(); c.moveTo(x(time), 12); c.lineTo(x(time), height); c.stroke();
}
function drawAccess(visits) {
  const canvas = $('access-chart');
  const { c, width, height } = prepareCanvas(canvas, 112);
  const bins = new Uint32Array(32);
  for (const id of visits) bins[Math.min(31, Math.floor(data.requests[id].t / (data.duration || 1) * 32))]++;
  const max = Math.max(1, ...bins);
  // Whole-number ticks with a little headroom, including for sparse selections.
  const target = Math.max(1, max / 3), magnitude = 10 ** Math.floor(Math.log10(target));
  const step = [1, 2, 5, 10].find(value => value * magnitude >= target) * magnitude;
  const ceiling = Math.ceil(max / step) * step;
  c.font = '9px system-ui';
  const left = Math.ceil(c.measureText(fmt(ceiling)).width) + 10;
  const top = 18, bottom = height - 18, plotWidth = Math.max(1, width - left - 3);
  const y = count => bottom - count / ceiling * (bottom - top);
  c.fillStyle = '#6b7d79'; c.textAlign = 'left'; c.fillText('Count', 0, 9);
  for (let tick = 0; tick <= ceiling; tick += step) {
    const yy = y(tick);
    c.strokeStyle = '#e3eae6'; c.beginPath(); c.moveTo(left, yy); c.lineTo(width - 3, yy); c.stroke();
    c.fillStyle = '#6b7d79'; c.textAlign = 'right'; c.fillText(fmt(tick), left - 6, yy + 3);
  }
  c.strokeStyle = '#bdcfc5'; c.beginPath(); c.moveTo(left, top); c.lineTo(left, bottom); c.lineTo(width - 3, bottom); c.stroke();
  c.fillStyle = '#6eb6a7';
  for (let i = 0; i < bins.length; i++) c.fillRect(left + i * plotWidth / 32 + 1, y(bins[i]), Math.max(1, plotWidth / 32 - 2), bottom - y(bins[i]));
  c.fillStyle = '#6b7d79'; c.textAlign = 'left'; c.fillText('0s', left, height - 2); c.textAlign = 'right'; c.fillText(clock(data.duration), width - 3, height - 2);
  canvas.setAttribute('aria-label', `Selected prefix accesses over time. Y axis: access count, 0 to ${ceiling}. ${fmt(visits.length)} accesses in 32 time bins.`);
}
function renderTimeline() {
  const totals = timelineTotals(data, filters());
  const requests = data.requests.map((r, i) => [r, i]).filter(([r]) => matches(r));
  const bySession = new Map(data.sessions.map(s => [s.index, []]));
  for (const entry of requests) bySession.get(entry[0].session).push(entry);
  const rows = [{ key: 'all', label: 'All requests', entries: requests }];
  for (const session of data.sessions) {
    const entries = bySession.get(session.index);
    rows.push({ key: `session:${session.index}`, session, label: `${expandedSessions.has(session.index) ? '▾' : '▸'} S${session.index + 1} · ${session.id}`, entries });
    if (expandedSessions.has(session.index)) {
      for (const agent of session.agents) rows.push({ key: agent.key, agent: agent.key, label: `　${agent.name === 'main' ? 'Main agent' : agent.name}`, entries: entries.filter(([r]) => r.agent === agent.key) });
    }
  }
  const { c, width } = prepareCanvas($('timeline'), Math.max(100, 32 + rows.length * 29));
  const left = Math.min(240, width * .28), span = Math.max(1, width - left - 20), duration = data.duration || 1;
  const x = t => left + t / duration * span;
  timelineHits = [];
  c.font = '10px system-ui'; c.fillStyle = '#6b7d79';
  for (let i = 0; i <= 6; i++) { const t = duration * i / 6; c.fillText(clock(t), x(t) - 12, 18); }
  const labels = $('timeline-agents'), signature = JSON.stringify(rows.map(r => r.key));
  if (labels.dataset.agents !== signature) {
    hideTimelineTooltip();
    labels.dataset.agents = signature;
    labels.replaceChildren(...rows.map((row, i) => {
      const button = document.createElement('button');
      const name = document.createElement('span'); name.className = 'timeline-name'; name.textContent = row.label;
      const metrics = document.createElement('span'); metrics.className = 'timeline-totals';
      button.append(name, metrics); button.style.top = `${28 + i * 29}px`;
      button.onpointerenter = () => queueTimelineTooltip(button);
      button.onpointerleave = leaveTimelineTooltip;
      button.onfocus = () => queueTimelineTooltip(button);
      button.onblur = leaveTimelineTooltip;
      button.onpointerdown = () => hideTimelineTooltip();
      if (row.agent) {
        button.dataset.agent = row.agent;
        button.onclick = () => highlightAgent(row.agent);
      } else if (row.session) {
        button.dataset.session = row.session.index;
        button.setAttribute('aria-expanded', String(expandedSessions.has(row.session.index)));
        button.onclick = () => {
          const id = row.session.index;
          if (expandedSessions.has(id)) expandedSessions.delete(id); else expandedSessions.add(id);
          scheduleRender();
        };
      } else { button.dataset.summary = 'true'; button.setAttribute('aria-disabled', 'true'); }
      return button;
    }));
  }
  labels.style.width = `${left}px`;
  for (const [index, button] of [...labels.children].entries()) {
    const row = rows[index];
    const total = (row.agent ? totals.agents.get(row.agent) : row.session ? totals.sessions.get(row.session.index) : totals.all) || { tokens: 0, blocks: 0, output: 0, requests: 0 };
    const shortTokens = compact(total.tokens).replace(/([KMBT])$/, ' $1');
    button.querySelector('.timeline-totals').textContent = `${shortTokens} tok. | ${fmt(total.blocks)} blk`;
    const session = row.session || (row.agent ? data.sessions.find(s => s.agents.some(a => a.key === row.agent)) : null);
    const exact = value => value.toLocaleString('en-US');
    const details = [row.agent ? agentLabel(row.agent) : row.session ? `Session S${row.session.index + 1}` : 'All requests'];
    if (session) details.push(`Session ID: ${session.id}`);
    details.push(`At replay time: ${time.toLocaleString('en-US', { maximumFractionDigits: 3 })} s`,
      `Input tokens: ${exact(total.tokens)}`, `Input blocks accessed: ${exact(total.blocks)}`,
      `Output tokens: ${exact(total.output)}`, `Started requests: ${exact(total.requests)}`,
      'Cumulative to playhead · repeated inputs counted');
    button.dataset.tooltip = details.join('\n');
    button.setAttribute('aria-label', details.join('. '));
    if (tooltipTarget === button && !timelineTooltip.hidden) tooltipContent.textContent = button.dataset.tooltip;
    const active = button.dataset.agent === highlightedAgent;
    button.classList.toggle('active', active);
    if (button.dataset.agent) button.setAttribute('aria-pressed', String(active));
  }
  const node = selected === null ? null : data.nodes[selected];
  rows.forEach((row, i) => {
    const y = 32 + i * 29;
    c.fillStyle = row.agent === highlightedAgent ? '#e1f2ec' : i % 2 ? '#fafcfb' : '#f1f6f3'; c.fillRect(0, y - 4, width, 28);
    if (row.key === 'all') {
      // Duration-density overview of every matching request, independent of expansion.
      const bins = new Int32Array(401);
      for (const [r] of requests) {
        const a = Math.min(399, Math.floor(r.t / duration * 400));
        const b = Math.min(400, Math.max(a + 1, Math.ceil((r.t + r.api_time) / duration * 400)));
        bins[a]++; bins[b]--;
      }
      let count = 0, max = 1; const values = [];
      for (let j = 0; j < 400; j++) { count += bins[j]; values.push(count); max = Math.max(max, count); }
      values.forEach((v, j) => { c.fillStyle = `rgba(8,127,118,${v ? .15 + .85 * v / max : .03})`; c.fillRect(left + j / 400 * span, y, span / 400 + .5, 16); });
      return;
    }
    for (const [r, id] of row.entries) {
      const active = inRange(id), related = (!node || node.visits[lowerBound(node.visits, id)] === id) && (highlightedAgent === null || r.agent === highlightedAgent);
      const xx = x(r.t), w = Math.max(3, r.api_time / duration * span);
      c.globalAlpha = related ? 1 : .22;
      c.fillStyle = !active ? '#dfe5e1' : currentRequest === id ? '#e7ae3c' : '#62ac9d';
      c.fillRect(xx, y, w, 14);
      if (currentRequest === id) { c.strokeStyle = '#69491b'; c.strokeRect(xx - 1, y - 1, w + 2, 16); }
      timelineHits.push({ x: xx, y, w, h: 16, id });
    }
    c.globalAlpha = 1;
  });
  if (mode === 'window') {
    c.fillStyle = '#13a69612'; c.fillRect(x(Math.max(0, time - filters().window)), 25, x(time) - x(Math.max(0, time - filters().window)), rows.length * 29 + 5);
  }
  c.strokeStyle = '#0a9789'; c.beginPath(); c.moveTo(x(time), 25); c.lineTo(x(time), 35 + rows.length * 29); c.stroke();
  c.fillStyle = '#087f76'; c.beginPath(); c.arc(x(time), 27, 4, 0, 2 * Math.PI); c.fill();
  $('timeline')._geometry = { left, span, duration };
}

function requestBlocks() {
  if (selected === null || !data.nodes[selected].blocks) return;
  $('block-panel').hidden = false;
  worker.postMessage({ type: 'inspect', id: selected, offset: blockOffset, token });
}
function showBlocks(message) {
  const node = data.nodes[selected], table = document.createElement('table');
  const head = document.createElement('thead'), row = document.createElement('tr');
  for (const text of ['Block position', 'Hash ID', 'Token positions']) { const th = document.createElement('th'); th.textContent = text; row.append(th); }
  head.append(row); table.append(head);
  const body = document.createElement('tbody');
  message.hashes.forEach((hash, i) => {
    const position = node.depth - node.blocks + message.offset + i;
    const tr = document.createElement('tr');
    for (const text of [fmt(position + 1), String(hash), `${fmt(position * data.blockSize + 1)}–${fmt((position + 1) * data.blockSize)}`]) { const td = document.createElement('td'); td.textContent = text; tr.append(td); }
    body.append(tr);
  });
  table.append(body); $('block-table').replaceChildren(table);
  $('block-page').textContent = `${message.offset + 1}–${message.offset + message.hashes.length} / ${node.blocks}`;
  $('blocks-prev').disabled = message.offset === 0; $('blocks-next').disabled = message.offset + 100 >= node.blocks;
}

$('open').onclick = $('empty-open').onclick = () => $('file').click();
$('file').onchange = event => { loadFile(event.target.files[0]); event.target.value = ''; };
$('demo').onclick = () => loadFile(new File([JSON.stringify(exampleTrace()) + '\n'], 'example-illustrative.jsonl', { type: 'application/x-ndjson' }));
$('cancel').onclick = () => { pause(); worker?.terminate(); token++; clearView(); summaries = []; options($('session'), [['', 'Open a trace file to begin']]); setBusy(false); notice('Loading cancelled. Open a trace file to start again.'); };
$('session').onchange = $('concurrency').onchange = () => selectTrace(Number($('session').value));
$('model').onchange = () => { pause(); highlightedAgent = null; selected = null; focused = null; currentRequest = null; $('block-panel').hidden = true; recomputeSeries(); layout(); };
$('agent').onchange = () => { pause(); highlightedAgent = null; currentRequest = null; recomputeSeries(); layout(); };
$('clear-agent-highlight').onclick = () => highlightAgent(highlightedAgent);
for (const value of ['cumulative', 'window']) $(value).onclick = () => {
  mode = value; $('window-label').hidden = mode !== 'window';
  for (const id of ['cumulative', 'window']) { $(id).classList.toggle('active', id === mode); $(id).setAttribute('aria-pressed', String(id === mode)); }
  scheduleRender();
};
$('window-size').onchange = scheduleRender;
$('levels').onchange = $('layout').onchange = layout;
$('log').onchange = scheduleRender;
$('fit').onclick = () => { tree.autoFit = true; tree.fit(); };
$('zoom-in').onclick = () => tree.zoom(1.3);
$('zoom-out').onclick = () => tree.zoom(1 / 1.3);
$('focus').onclick = () => { if (selected !== null) { focused = selected; layout(); } };
$('unfocus').onclick = () => { focused = null; layout(); };
$('inspect').onclick = () => { blockOffset = 0; requestBlocks(); };
$('blocks-prev').onclick = () => { blockOffset = Math.max(0, blockOffset - 100); requestBlocks(); };
$('blocks-next').onclick = () => { blockOffset += 100; requestBlocks(); };
$('scrub').oninput = () => { pause(); seek(Number($('scrub').value)); };
$('previous').onclick = () => step(-1);
$('next').onclick = () => step(1);
function step(direction) {
  if (!data) return;
  const candidates = data.requests.map((r, i) => i).filter(i => matches(data.requests[i]));
  if (!candidates.length) return;
  let index;
  if (currentRequest !== null) { const position = candidates.indexOf(currentRequest); index = Math.max(0, Math.min(candidates.length - 1, position + direction)); }
  else if (direction < 0) { index = candidates.findLastIndex(i => data.requests[i].t < time); if (index < 0) index = 0; }
  else { index = candidates.findIndex(i => data.requests[i].t > time); if (index < 0) index = candidates.length - 1; }
  selectRequest(candidates[index]);
}
$('play').onclick = () => {
  if (!data) return;
  if (playing) { pause(); return; }
  if (time >= data.duration) seek(0);
  playing = true; lastTick = performance.now(); $('play').textContent = 'Ⅱ Pause'; $('play').setAttribute('aria-label', 'Pause timeline');
  requestAnimationFrame(tick);
};
function tick(now) {
  if (!playing || !data) return;
  if (now - lastTick >= 50) {
    seek(time + (now - lastTick) / 1000 * Number($('speed').value)); lastTick = now;
    if (time >= data.duration) { pause(); return; }
  }
  requestAnimationFrame(tick);
}
$('timeline').onclick = event => {
  if (!data || busy) return;
  const rect = $('timeline').getBoundingClientRect(), x = event.clientX - rect.left, y = event.clientY - rect.top;
  if (x < $('timeline')._geometry.left) return;
  const hit = timelineHits.findLast(h => x >= h.x - 1 && x <= h.x + h.w + 1 && y >= h.y && y <= h.y + h.h);
  if (hit) selectRequest(hit.id);
  else { const { left, span, duration } = $('timeline')._geometry; pause(); seek((x - left) / span * duration); }
};
$('timeline').onpointermove = event => {
  const rect = $('timeline').getBoundingClientRect(), x = event.clientX - rect.left, y = event.clientY - rect.top;
  const hit = timelineHits.findLast(h => x >= h.x && x <= h.x + h.w && y >= h.y && y <= h.y + h.h);
  $('timeline').title = hit && data ? `#${hit.id + 1} · ${data.requests[hit.id].model} · ${data.requests[hit.id].t}s · ${fmt(data.requests[hit.id].in)} input tokens` : 'Click to seek';
};
$('segments').parentElement.addEventListener('toggle', () => { if (data && snap) renderSegmentList(); });
new ResizeObserver(() => scheduleRender()).observe($('timeline').parentElement);
new ResizeObserver(() => scheduleRender()).observe($('footprint-chart').parentElement);
document.addEventListener('visibilitychange', () => { if (document.hidden) pause(); });

// Files can also be dropped anywhere on the app; data never goes to the server.
document.addEventListener('dragover', event => { if (event.dataTransfer.types.includes('Files')) event.preventDefault(); });
document.addEventListener('drop', event => { event.preventDefault(); if (!busy && event.dataTransfer.files.length) loadFile(event.dataTransfer.files[0]); });
