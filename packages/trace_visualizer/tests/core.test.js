import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeTrace, buildForest, serializeForest, snapshot, footprintSeries, indexLines, buildReplay, timelineTotals } from '../src/core.js';
import { exampleTrace } from '../src/demo.js';

const request = (hash_ids, t = 0, model = 'A', input = hash_ids.length * 64) => ({ hash_ids, t, model, in: input, out: 10, api_time: 2, type: 'n' });
const trace = requests => ({ id: 'test', block_size: 64, hash_id_scope: 'local', requests });
function prepare(raw) { const normalized = normalizeTrace(raw); const forest = buildForest(normalized); return { normalized, forest, data: serializeForest(forest, normalized) }; }

test('radix insertion preserves early endpoints, divergence and duplicate endpoints', () => {
  const { data, forest } = prepare(trace([request([1, 2, 3, 4], 0), request([1, 2], 1), request([1, 2, 3, 5], 2), request([1, 2, 3, 4], 3)]));
  assert.equal(forest.nodes.reduce((sum, n) => sum + n.hashes.length, 0), 5);
  const shared = forest.nodes.find(n => n.hashes.join() === '1,2');
  assert.deepEqual(shared.visits, [0, 1, 2, 3]); assert.deepEqual(shared.ends, [1]);
  const end = forest.nodes.find(n => n.hashes.join() === '4');
  assert.deepEqual(end.visits, [0, 3]); assert.deepEqual(end.ends, [0, 3]);
  assert.equal(snapshot(data, { time: 0 }).blocks, 4);
  assert.equal(snapshot(data, { time: 1 }).blocks, 4);
  assert.equal(snapshot(data, { time: 2 }).blocks, 5);
  assert.equal(snapshot(data, { time: 3 }).processed, 4);
});

test('same hash after a different prefix is a separate block', () => {
  const { data } = prepare(trace([request([1, 9]), request([2, 9], 1)]));
  assert.equal(snapshot(data, { time: 1 }).blocks, 4);
});

test('models have independent roots and footprints', () => {
  const { data } = prepare(trace([request([1, 2], 0, 'A'), request([1, 2], 1, 'B')]));
  assert.equal(data.roots.length, 2);
  assert.equal(snapshot(data, { time: 1 }).blocks, 4);
  assert.equal(snapshot(data, { time: 1, model: 'B' }).blocks, 2);
});

test('nested agent timing is absolute, stable at ties, and filterable', () => {
  const { data } = prepare(trace([
    request([1], 2),
    { type: 'subagent', agent_id: 'child', t: 0, requests: [request([1, 2], 0), request([1, 3], 2)] }
  ]));
  assert.deepEqual(data.requests.map(r => [r.t, r.agent]), [[0, 'child'], [2, 'main'], [2, 'child']]);
  assert.equal(snapshot(data, { time: 2 }).processed, 3);
  assert.equal(snapshot(data, { time: 2, agent: 'main' }).blocks, 1);
  assert.equal(snapshot(data, { time: 2, agent: 'child' }).blocks, 3);
  const window = snapshot(data, { time: 2, mode: 'window', window: 1 });
  assert.equal(window.blocks, 2); assert.equal(window.processed, 2);
  assert.equal(snapshot(data, { time: 9, mode: 'window', window: 1 }).blocks, 0);
  assert.deepEqual(footprintSeries(data, '*', 'main'), [[0, 0], [2, 64], [2, 64]]);
});

test('partial final blocks allocate capacity; output tokens are not invented as blocks', () => {
  const { data } = prepare(trace([request([1, 2], 0, 'A', 65)]));
  const result = snapshot(data, { time: 0 });
  assert.equal(result.input, 65); assert.equal(result.blocks * data.blockSize, 128);
  assert.equal(footprintSeries(data).at(-1)[1], 128);
});

test('empty requests and invalid schemas are handled explicitly', () => {
  assert.equal(snapshot(prepare(trace([])).data, { time: 0 }).blocks, 0);
  assert.equal(snapshot(prepare(trace([request([], 0, 'A', 0)])).data, { time: 0 }).processed, 1);
  assert.throws(() => normalizeTrace(trace([request([1], 0, 'A', 65)])), /block count/);
  assert.throws(() => normalizeTrace({ ...trace([]), hash_id_scope: 'global' }), /local/);
  assert.throws(() => normalizeTrace(trace([request([NaN])])), /Invalid/);
});

test('byte index handles UTF-8, CRLF, blanks, BOM and final lines without newline', async () => {
  const text = '\uFEFF{"id":"α"}\r\n \t\n{"id":"b"}\n\n{"id":"終"}';
  const file = new Blob([text]);
  for (const size of [1, 2, 7, 1000]) {
    const ranges = await indexLines(file, size);
    assert.deepEqual(ranges.map(r => r.line), [1, 3, 5]);
    const rows = [];
    for (const r of ranges) rows.push(JSON.parse((await file.slice(r.start, r.end).text()).replace(/^\uFEFF/, '')).id);
    assert.deepEqual(rows, ['α', 'b', '終']);
  }
  assert.equal((await indexLines(new Blob([' \n\n']))).length, 0);
});

test('randomized snapshots agree with independent prefix enumeration', () => {
  let seed = 73;
  const random = n => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed % n; };
  const entries = [];
  for (let i = 0; i < 150; i++) {
    const hashes = Array.from({ length: 1 + random(8) }, () => random(5));
    const r = request(hashes, random(20), random(2) ? 'A' : 'B');
    entries.push(random(2) ? r : { type: 'subagent', agent_id: 'child', requests: [r] });
  }
  const { normalized, data } = prepare(trace(entries));
  for (const mode of ['cumulative', 'window']) for (const model of ['*', 'A', 'B']) for (const agent of ['*', 'main', 'child']) for (const time of [0, 5, 10, 20]) {
    const expected = new Set(); let processed = 0, input = 0;
    for (const r of normalized.requests) {
      if (r.t > time || (mode === 'window' && r.t < time - 4) || (model !== '*' && r.model !== model) || (agent !== '*' && r.agent !== agent)) continue;
      processed++; input += r.in;
      for (let i = 1; i <= r.hash_ids.length; i++) expected.add(`${r.model}:${r.hash_ids.slice(0, i).join(',')}`);
    }
    const actual = snapshot(data, { time, mode, window: 4, model, agent });
    assert.equal(actual.blocks, expected.size, JSON.stringify({ time, mode, model, agent }));
    assert.equal(actual.processed, processed); assert.equal(actual.input, input);
  }
});

test('example is a complete valid multi-model workload with shared agent prefixes', () => {
  const { data } = prepare(exampleTrace());
  assert.equal(data.models.length, 2); assert.equal(data.agents.length, 5);
  const end = snapshot(data, { time: data.duration });
  assert.equal(end.processed, 64);
  assert.equal(footprintSeries(data).at(-1)[1], end.blocks * 64);
});


test('concurrent replay replaces finished sessions, offsets time, and isolates local hashes and agents', async () => {
  const traces = [
    normalizeTrace({ ...trace([request([1, 2], 10), { type: 'subagent', agent_id: 'child', requests: [{ ...request([1, 3], 12), api_time: 8 }] }]), id: 'first' }),
    normalizeTrace({ ...trace([request([1, 2], 4)]), id: 'second' }),
    normalizeTrace({ ...trace([{ ...request([1, 2], 0), api_time: 3 }]), id: 'third' }),
    normalizeTrace({ ...trace([request([1, 2], 0)]), id: 'fourth' })
  ];
  const summaries = traces.map(t => ({ id: t.id }));
  const { forest, trace: replay } = await buildReplay(summaries, 0, 2, i => traces[i]);
  assert.deepEqual(replay.sessions.map(s => [s.start, s.end, s.slot]), [[0, 10, 0], [0, 2, 1], [2, 5, 1], [5, 7, 1]]);
  assert.deepEqual(replay.requests.map(r => [r.t, r.session]), [[0, 0], [0, 1], [2, 0], [2, 2], [5, 3]]);
  assert.equal(replay.duration, 10);
  const data = serializeForest(forest, replay);
  assert.equal(snapshot(data, { time: 10 }).blocks, 9);
  assert.equal(data.roots.length, 4);
  assert.equal(snapshot(data, { time: 10, agent: '0:child' }).blocks, 2);
  for (const node of data.nodes) {
    assert.deepEqual([...node.visits], [...node.visits].sort((a,b) => a-b));
    for (const id of node.visits) assert.equal(data.requests[id].session, node.session);
    for (const id of node.children) assert.equal(data.nodes[id].parent, node.id);
  }
  const activeSessions = t => replay.sessions.filter(s => s.start <= t && t < s.end).length;
  assert.deepEqual([0, 2, 5, 7, 10].map(activeSessions), [2, 2, 2, 1, 0]);
  const tail = await buildReplay(summaries, 2, 20, i => traces[i]);
  assert.deepEqual(tail.trace.sessions.map(s => s.index), [2, 3]);
  assert.deepEqual(tail.trace.sessions.map(s => s.start), [0, 0]);
  const serial = await buildReplay(summaries, 1, 1, i => traces[i]);
  assert.deepEqual(serial.trace.sessions.map(s => s.start), [0, 2, 5]);
});

test('empty and zero-duration sessions release slots immediately; mixed block sizes fail', async () => {
  const traces = [normalizeTrace(trace([])), normalizeTrace(trace([{ ...request([1], 0), api_time: 0 }])), normalizeTrace(trace([request([1], 0)]))];
  const result = await buildReplay(traces, 0, 1, i => traces[i]);
  assert.deepEqual(result.trace.sessions.map(s => s.start), [0, 0, 0]);
  const mismatch = normalizeTrace({ ...trace([request([1], 0, 'A', 32)]), block_size: 32 });
  await assert.rejects(buildReplay([traces[0], mismatch], 0, 2, i => [traces[0], mismatch][i]), /same block size/);
});


test('timeline row totals accumulate input traversals by session and agent through the playhead', () => {
  const data = { blockSize: 64, requests: [
    { t: 0, session: 0, agent: '0:main', model: 'A', in: 65, out: 7 },
    { t: 1, session: 1, agent: '1:main', model: 'A', in: 128, out: 9 },
    { t: 2, session: 0, agent: '0:child', model: 'B', in: 129, out: 10 },
    { t: 3, session: 0, agent: '0:main', model: 'A', in: 65, out: 7 }
  ] };
  const atTwo = timelineTotals(data, { time: 2, mode: 'window', window: .5 });
  assert.deepEqual(atTwo.all, { tokens: 322, output: 26, blocks: 7, requests: 3 });
  assert.deepEqual(atTwo.sessions.get(0), { tokens: 194, output: 17, blocks: 5, requests: 2 });
  assert.deepEqual(atTwo.agents.get('0:main'), { tokens: 65, output: 7, blocks: 2, requests: 1 });
  const end = timelineTotals(data, { time: 3 });
  assert.equal(end.agents.get('0:main').tokens, 130);
  assert.equal(end.agents.get('0:main').blocks, 4); // A repeated input is another access.
  assert.equal(timelineTotals(data, { time: 3, model: 'B' }).all.tokens, 129);
  assert.equal(timelineTotals(data, { time: 3, agent: '1:main' }).all.blocks, 2);
  assert.equal(timelineTotals(data, { time: -1 }).all.requests, 0);
});
