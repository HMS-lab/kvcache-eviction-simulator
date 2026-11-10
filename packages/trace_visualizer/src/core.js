/** Pure trace analysis. Hash IDs are scoped by session, model and prefix path. */
export function normalizeTrace(trace) {
  if (!trace || typeof trace.id !== 'string' || !Array.isArray(trace.requests)) throw new Error('Expected a trace with id and requests');
  if (!Number.isSafeInteger(trace.block_size) || trace.block_size <= 0) throw new Error('Invalid block_size');
  if (trace.hash_id_scope !== 'local') throw new Error('Expected hash_id_scope: local');
  const requests = [];
  const agents = new Set();
  function visit(entries, agent) {
    for (const entry of entries) {
      if (entry?.type === 'subagent') {
        if (typeof entry.agent_id !== 'string' || !Array.isArray(entry.requests)) throw new Error('Invalid sub-agent group');
        agents.add(entry.agent_id);
        visit(entry.requests, entry.agent_id);
        continue;
      }
      if (!entry || !Number.isFinite(entry.t) || entry.t < 0 || typeof entry.model !== 'string' ||
          !Number.isSafeInteger(entry.in) || entry.in < 0 || !Number.isSafeInteger(entry.out) || entry.out < 0 ||
          !Number.isFinite(entry.api_time) || entry.api_time < 0 || !Array.isArray(entry.hash_ids) ||
          entry.hash_ids.length !== Math.ceil(entry.in / trace.block_size) ||
          entry.hash_ids.some(h => !Number.isSafeInteger(h) || h < 0)) throw new Error('Invalid request or input block count');
      requests.push({ ...entry, agent, originalIndex: requests.length });
    }
  }
  visit(trace.requests, 'main');
  requests.sort((a, b) => a.t - b.t || a.originalIndex - b.originalIndex);
  const models = [...new Set(requests.map(r => r.model))].sort();
  return { id: trace.id, blockSize: trace.block_size, requests, models, agents: ['main', ...agents], duration: requests.reduce((max, r) => Math.max(max, r.t + r.api_time), 0) };
}

export function summarize(trace) {
  return { id: trace.id, count: trace.requests.length, models: trace.models, agents: trace.agents.length, duration: trace.duration, firstTime: trace.requests[0]?.t || 0, blockSize: trace.blockSize };
}

/** Fill conversation slots in file order, replacing a session at its final completion. */
export async function buildReplay(summaries, start, concurrency, read, progress = () => {}) {
  const slots = Array(Math.max(1, Math.min(summaries.length - start, Math.floor(concurrency) || 1))).fill(0);
  const nodes = [], roots = [], requests = [], sessions = [], agents = [], models = new Set();
  let blockSize;
  for (let index = start; index < summaries.length; index++) {
    const trace = await read(index);
    if (blockSize !== undefined && blockSize !== trace.blockSize) throw new Error('Replay sessions must use the same block size.');
    blockSize = trace.blockSize;
    const slot = slots.indexOf(Math.min(...slots)), t0 = slots[slot];
    const origin = trace.requests[0]?.t || 0, duration = trace.requests.length ? trace.duration - origin : 0;
    const session = { index, id: trace.id, start: t0, end: t0 + duration, slot, agents: [] };
    sessions.push(session); slots[slot] = session.end;
    for (const agent of trace.agents) { const key = `${index}:${agent}`; agents.push(key); session.agents.push({ key, name: agent }); }
    const forest = buildForest(trace), offset = nodes.length, requestOffset = requests.length;
    for (const node of forest.nodes) {
      node.id += offset; node.parent = node.parent < 0 ? -1 : node.parent + offset;
      node.session = index;
      node.hashes = Float64Array.from(node.hashes);
      node.visits = node.visits.map(id => id + requestOffset); node.ends = node.ends.map(id => id + requestOffset);
      nodes.push(node);
    }
    roots.push(...forest.roots.map(id => id + offset));
    for (const r of trace.requests) {
      const { hash_ids, ...metadata } = r;
      requests.push({ ...metadata, t: t0 + r.t - origin, localTime: r.t - origin, session: index, agentName: r.agent, agent: `${index}:${r.agent}`, replayIndex: requests.length });
      models.add(r.model);
    }
    progress(index - start + 1, summaries.length - start);
  }
  requests.sort((a, b) => a.t - b.t || a.replayIndex - b.replayIndex);
  const remap = new Uint32Array(requests.length);
  requests.forEach((r, id) => { remap[r.replayIndex] = id; delete r.replayIndex; });
  for (const node of nodes) {
    node.visits = Uint32Array.from(node.visits, id => remap[id]).sort();
    node.ends = Uint32Array.from(node.ends, id => remap[id]).sort();
  }
  const trace = { id: summaries[start].id, blockSize, requests, models: [...models].sort(), agents, duration: Math.max(0, ...slots), sessions };
  return { forest: { nodes, roots }, trace };
}

/** Insert directly into a radix tree instead of allocating an object per block. */
export function buildForest(trace) {
  const nodes = [];
  const roots = new Map();
  function create(parent, model, hashes, visits = []) {
    const node = { id: nodes.length, parent, model, hashes, children: new Map(), visits, ends: [] };
    nodes.push(node); return node;
  }
  for (const model of trace.models) roots.set(model, create(-1, model, []));
  for (let rid = 0; rid < trace.requests.length; rid++) {
    const request = trace.requests[rid];
    const hashes = request.hash_ids;
    let node = roots.get(request.model), pos = 0;
    node.visits.push(rid);
    while (pos < hashes.length) {
      let child = node.children.get(hashes[pos]);
      if (!child) {
        child = create(node.id, request.model, hashes.slice(pos), [rid]);
        node.children.set(hashes[pos], child); node = child; pos = hashes.length; break;
      }
      let common = 0;
      while (common < child.hashes.length && pos + common < hashes.length && child.hashes[common] === hashes[pos + common]) common++;
      if (common < child.hashes.length) {
        const middle = create(node.id, request.model, child.hashes.slice(0, common), child.visits.slice());
        node.children.set(hashes[pos], middle);
        child.hashes = child.hashes.slice(common);
        child.parent = middle.id;
        middle.children.set(child.hashes[0], child);
        child = middle;
      }
      child.visits.push(rid);
      pos += common;
      node = child;
    }
    node.ends.push(rid);
  }
  const stack = [...roots.values()].map(n => [n, 0]);
  while (stack.length) {
    const [node, depth] = stack.pop();
    node.depth = depth + node.hashes.length;
    for (const child of node.children.values()) stack.push([child, node.depth]);
  }
  return { nodes, roots: [...roots.values()].map(n => n.id) };
}

export function serializeForest(forest, trace) {
  return {
    ...summarize(trace), agents: trace.agents, sessions: trace.sessions, roots: forest.roots,
    requests: trace.requests.map(({ hash_ids, ...request }) => request),
    nodes: forest.nodes.map(n => ({ id: n.id, parent: n.parent, model: n.model, session: n.session, blocks: n.hashes.length, depth: n.depth,
      children: [...n.children.values()].map(c => c.id), visits: Uint32Array.from(n.visits), ends: Uint32Array.from(n.ends) }))
  };
}

export function lowerBound(array, value) {
  let lo = 0, hi = array.length;
  while (lo < hi) { const mid = (lo + hi) >>> 1; if (array[mid] < value) lo = mid + 1; else hi = mid; }
  return lo;
}
export function upperBound(array, value) {
  let lo = 0, hi = array.length;
  while (lo < hi) { const mid = (lo + hi) >>> 1; if (array[mid] <= value) lo = mid + 1; else hi = mid; }
  return lo;
}

/** Cumulative input volume; repeated input blocks count on every request. */
export function timelineTotals(data, { time, model = '*', agent = '*' }) {
  const empty = () => ({ tokens: 0, output: 0, blocks: 0, requests: 0 });
  const all = empty(), sessions = new Map(), agents = new Map();
  for (const request of data.requests) {
    if (request.t > time) break;
    if ((model !== '*' && request.model !== model) || (agent !== '*' && request.agent !== agent)) continue;
    if (!sessions.has(request.session)) sessions.set(request.session, empty());
    if (!agents.has(request.agent)) agents.set(request.agent, empty());
    for (const total of [all, sessions.get(request.session), agents.get(request.agent)]) {
      total.tokens += request.in; total.output += request.out;
      total.blocks += Math.ceil(request.in / data.blockSize); total.requests++;
    }
  }
  return { all, sessions, agents };
}

/** Time range includes both endpoints; requests contribute at their start time. */
export function snapshot(data, { time, mode = 'cumulative', window = 60, model = '*', agent = '*' }) {
  const times = data.requests.map(r => r.t);
  const start = mode === 'window' ? Math.max(0, time - window) : -Infinity;
  const lo = lowerBound(times, start), hi = upperBound(times, time);
  const counts = new Uint32Array(data.nodes.length), endpoints = new Uint32Array(data.nodes.length);
  let blocks = 0, processed = 0, input = 0, output = 0, total = 0;
  const matches = r => (model === '*' || r.model === model) && (agent === '*' || r.agent === agent);
  for (let i = 0; i < data.requests.length; i++) {
    const r = data.requests[i];
    if (!matches(r)) continue;
    total++;
    if (i >= lo && i < hi) { processed++; input += r.in; output += r.out; }
  }
  for (const node of data.nodes) {
    if (model !== '*' && node.model !== model) continue;
    const count = list => {
      const a = lowerBound(list, lo), b = lowerBound(list, hi);
      if (agent === '*') return b - a;
      let result = 0;
      for (let j = a; j < b; j++) if (data.requests[list[j]].agent === agent) result++;
      return result;
    };
    counts[node.id] = count(node.visits);
    endpoints[node.id] = count(node.ends);
    if (counts[node.id]) blocks += node.blocks;
  }
  return { counts, endpoints, blocks, processed, total, input, output, lo, hi, start };
}

export function footprintSeries(data, model = '*', agent = '*') {
  const additions = new Float64Array(data.requests.length);
  for (const node of data.nodes) {
    if (!node.blocks || (model !== '*' && node.model !== model)) continue;
    const first = agent === '*' ? node.visits[0] : node.visits.find(i => data.requests[i].agent === agent);
    if (first !== undefined) additions[first] += node.blocks * data.blockSize;
  }
  const result = []; let total = 0;
  for (let i = 0; i < additions.length; i++) {
    total += additions[i]; result.push([data.requests[i].t, total]);
  }
  return result;
}

/** Bounded byte scanning: retain file offsets, never the complete JSONL string. */
export async function indexLines(file, chunkSize = 4 * 1024 * 1024) {
  const lines = []; let start = 0, line = 1, hasContent = false;
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    const bytes = new Uint8Array(await file.slice(offset, offset + chunkSize).arrayBuffer());
    let cursor = 0;
    while (cursor < bytes.length) {
      const newline = bytes.indexOf(10, cursor);
      const end = newline < 0 ? bytes.length : newline;
      if (!hasContent) hasContent = bytes.subarray(cursor, end).some(b => b > 32);
      if (newline < 0) break;
      if (hasContent) lines.push({ start, end: offset + newline, line });
      start = offset + newline + 1; line++; hasContent = false; cursor = newline + 1;
    }
  }
  if (hasContent) lines.push({ start, end: file.size, line });
  return lines;
}
