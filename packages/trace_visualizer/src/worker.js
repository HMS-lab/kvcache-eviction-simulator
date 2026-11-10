import { indexLines, normalizeTrace, summarize, buildReplay, serializeForest } from './core.js';

let file, ranges = [], forest, summaries = [];
let queue = Promise.resolve();
self.onmessage = ({ data }) => {
  queue = queue.then(() => handle(data)).catch(error => self.postMessage({ type: 'error', message: error.message, token: data.token }));
};

async function readTrace(range) {
  try { return normalizeTrace(JSON.parse((await file.slice(range.start, range.end).text()).replace(/^\uFEFF/, ''))); }
  catch (error) { throw new Error(`Line ${range.line}: ${error.message}`); }
}

async function handle(message) {
  if (message.type === 'load') {
    file = message.file; forest = undefined;
    self.postMessage({ type: 'progress', current: 0, total: null });
    ranges = await indexLines(file);
    if (!ranges.length) throw new Error('The file contains no traces. Choose a JSONL file with one trace per line.');
    self.postMessage({ type: 'progress', current: 0, total: ranges.length });
    summaries = [];
    for (let i = 0; i < ranges.length; i++) {
      const trace = await readTrace(ranges[i]);
      summaries.push(summarize(trace));
      self.postMessage({ type: 'progress', current: i + 1, total: ranges.length });
    }
    self.postMessage({ type: 'loaded', summaries, name: file.name });
  }
  if (message.type === 'select') {
    if (!ranges[message.index]) throw new Error('Unknown trace');
    forest = undefined;
    const replay = await buildReplay(summaries, message.index, message.concurrency, index => readTrace(ranges[index]),
      (current, total) => self.postMessage({ type: 'progress', current, total }));
    forest = replay.forest;
    const trace = replay.trace;
    self.postMessage({ type: 'trace', data: serializeForest(forest, trace), token: message.token });
  }
  if (message.type === 'inspect') {
    const node = forest?.nodes[message.id];
    if (!node) return;
    const offset = Math.max(0, message.offset || 0);
    self.postMessage({ type: 'blocks', id: node.id, offset, hashes: node.hashes.slice(offset, offset + 100), token: message.token });
  }
}
