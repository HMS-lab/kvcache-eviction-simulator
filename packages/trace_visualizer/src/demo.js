/** Small synthetic session for discovering the controls without opening a file. */
export function exampleTrace() {
  const prefix = Array.from({ length: 96 }, (_, i) => i);
  let nextHash = 1000;
  const make = (t, hashes, model = 'claude-opus-4-7') => ({ t, type: 's', model, in: hashes.length * 64 - 13, out: 160 + Math.round(t % 200), hash_ids: hashes.slice(), api_time: 5 + t % 6, ttft: .8, think_time: 3 });
  const extension = length => Array.from({ length }, () => nextHash++);
  const main = prefix.slice(), requests = [];
  for (let i = 0; i < 28; i++) {
    requests.push(make(i * 24, main));
    main.push(...extension(5 + i % 4));
    if ([4, 9, 16, 21].includes(i)) {
      const context = [...prefix, ...extension(24)], inner = [];
      for (let j = 0; j < 8; j++) { inner.push(make(i * 24 + 8 + j * 9, context)); context.push(...extension(4)); }
      requests.push({ t: inner[0].t, type: 'subagent', agent_id: `subagent_${i}`, requests: inner, duration_ms: 75000, models: ['claude-opus-4-7'] });
    }
    if (i % 7 === 0) requests.push(make(i * 24 + 2, [...prefix.slice(0, 24), ...extension(12)], 'claude-haiku-4-5'));
  }
  return { id: 'example-session-illustrative-data', models: ['claude-opus-4-7', 'claude-haiku-4-5'], block_size: 64, hash_id_scope: 'local', requests };
}
