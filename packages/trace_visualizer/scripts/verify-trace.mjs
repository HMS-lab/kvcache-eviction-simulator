// Optional real-file verification: node scripts/verify-trace.mjs /path/to/traces.jsonl
import { openAsBlob } from 'node:fs';
import { performance } from 'node:perf_hooks';
import { indexLines, normalizeTrace, buildForest, serializeForest, snapshot } from '../src/core.js';

if (!process.argv[2]) throw new Error('Usage: node scripts/verify-trace.mjs /path/to/traces.jsonl');
const started = performance.now(), file = await openAsBlob(process.argv[2]), ranges = await indexLines(file);
let requests = 0, largest = { count: -1 }, first;
for (let i = 0; i < ranges.length; i++) {
  const range = ranges[i];
  const raw = JSON.parse(await file.slice(range.start, range.end).text());
  const trace = normalizeTrace(raw); requests += trace.requests.length;
  if (!first) first = trace;
  if (trace.requests.length > largest.count) largest = { count: trace.requests.length, range };
}
for (const trace of [first, normalizeTrace(JSON.parse(await file.slice(largest.range.start, largest.range.end).text()))]) {
  const start = performance.now();
  const forest = buildForest(trace), data = serializeForest(forest, trace);
  const result = snapshot(data, { time: data.duration });
  if (result.processed !== trace.requests.length) throw new Error('Request count mismatch');
  const expectedBlocks = forest.nodes.reduce((sum, node) => sum + node.hashes.length, 0);
  if (result.blocks !== expectedBlocks) throw new Error('Prefix footprint mismatch');
  console.log(JSON.stringify({ id: trace.id, requests: result.processed, prefixBlocks: result.blocks, radixSegments: data.nodes.length - data.roots.length, analysisMs: Math.round(performance.now() - start) }));
}
console.log(JSON.stringify({ traces: ranges.length, requests, totalSeconds: +((performance.now() - started) / 1000).toFixed(2), heapMB: Math.round(process.memoryUsage().heapUsed / 1024 ** 2) }));
