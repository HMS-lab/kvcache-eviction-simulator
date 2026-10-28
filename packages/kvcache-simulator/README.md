# KV Cache Simulator

`kvcache-simulator` runs the KVCache.AI hit-rate simulator locally on JSONL traces. It uses the same model accounting formulas as the KV Cache Size Calculator and the same prefix-aware hit-rate semantics as the KV Cache Hit Rate Simulator.

## Installation

```bash
pip install kvcache-simulator
```

The default simulation backend compiles and runs a bundled C++ replay core on first use. You need a local C++ compiler such as `c++` or `clang++`. If a compiler is not available, use `--backend python`.

## Quick Start

```bash
kvcache-simulator run \
  --trace trace.jsonl.gz \
  --model glm-5.2 \
  --kv-precision fp8_int8 \
  --indexer-precision fp4_int4
```

The default output is a readable table. Use `--format json` when another script needs to consume the result.

```bash
kvcache-simulator run \
  --trace trace.jsonl.gz \
  --model deepseek-v4-pro \
  --kv-precision fp8_int8 \
  --indexer-precision fp4_int4 \
  --format json \
  --output result.json
```

List supported model ids:

```bash
kvcache-simulator list-models
```

`python -m kvcache_sim ...` also works, but the installed CLI command is preferred.

`run` is the main command. It evaluates the selected trace across a set of KV cache memory budgets. `sweep` is kept as an alias for users who prefer benchmark terminology.

## Input Trace Format

Input is JSONL or JSONL.GZ, one request per line. The minimal accepted format is:

```json
{"block_size":64,"hash_ids":[2001,2002],"input_length":128}
```

Required fields:

- `hash_ids`: cache block identities in request-prefix order.
- `input_length`: prefill input token count for this request.
- `block_size`: source-native block size. It can be omitted only when `--block-size` is provided.

Optional fields:

- `timestamp`: ignored by the simulator. Requests are replayed in file order, so sort production traces by timestamp before running the command.
- `output_length`: ignored by the hit-rate denominator. Generated output matters only if it appears later in another request's `hash_ids`.
- `block_tokens`: advanced field for exact per-block token weights. If present, it must be a positive integer list with the same length as `hash_ids`.

`--block-size` is only a fallback for records that omit `block_size`. If any record declares `block_size`, the trace-declared value is used and overrides the CLI fallback for the whole trace.

## Converting SemiAnalysis Conversation Traces

The standalone converter in this repository accepts the nested SemiAnalysis format (one conversation per JSONL line, including subagent groups). It uses only Python's standard library:

```bash
python scripts/convert_semianalysis_trace.py \
  --input ~/ai_traces/tmp.jsonl \
  --output ~/ai_traces/converted_file.jsonl \
  --concurrency 2
```

`--concurrency` defaults to `1`. The first N conversations start at time zero. A conversation occupies its slot during execution and gaps between turns; its subagents share that slot. When all its requests have completed, the next input conversation starts in the freed slot. Output requests are sorted by global submission time, with ties resolved by input conversation order and original request order. Index fields are zero-based. Conversion creates a replay file immediately; it does not wait for timestamps in real time.

Every request has `conversation_id` copied unchanged from its original top-level conversation's `id`, including requests inside nested subagent groups. `agent_id` records the enclosing subagent separately and is null for main-agent requests. `conversation_index` distinguishes input occurrences even if source conversation IDs repeat; `request_index` records the original depth-first request order.

The converter renames `in`/`out` to `input_length`/`output_length` and preserves the conversation's native `block_size`. It remaps hash IDs to compact integers, keeping reuse within each conversation and model, including between its main agent and subagents, while preventing false reuse across conversations or models. No reblocking is performed. For this dataset the output declares `block_size: 64`, which overrides a simulator CLI fallback such as `--block-size 512`.

`timestamp` is the conversation start time plus the source request's `t`; `completion_timestamp` adds its recorded `api_time`. `relative_timestamp` preserves `t`, and `conversation_start_timestamp` records the offset. Subagent timestamps already use the conversation's time origin. The source `model`, request `type`, and available `api_time`, `think_time`, and `ttft` fields are preserved. Recorded timestamps are authoritative; the converter does not recalculate them from think times or subagent group durations.

Input and output support `.jsonl`, `.jsonl.gz`, and `-` for stdin/stdout. Summaries go to stderr. `--temp-dir PATH` selects temporary storage; otherwise the system temporary directory is used. Active conversations are spooled to disk, leaving only one parsed source conversation and one serialized pending request per active conversation in memory. Memory therefore depends on the largest conversation rather than the entire input, while temporary disk usage depends on active conversations. An exceptionally large single JSONL line can still require substantial memory.

Invalid records fail conversion with line/request context. File output is replaced atomically only on success, and temporary files are cleaned up on failure or interruption; stdout may contain partial output on failure. The converter accepts per-conversation `hash_id_scope: "local"` and requires consistent positive block sizes, nonempty conversation IDs/models/hash lists, positive input lengths, nonnegative output lengths, and finite nonnegative submission times and API durations.

Scheduling uses recorded execution times to construct a workload; it does not predict timings for another model or server. The current simulator ignores timestamps and processes cache reads/writes in file order, without modeling in-flight execution or completion-dependent cache availability. The simulator also loads the converted trace into memory; the converter's memory behavior does not change that.

## Options

| Option | Meaning |
| --- | --- |
| `--trace PATH` | JSONL/JSONL.GZ trace path, or `-` for stdin. |
| `--model ID` | Model id from the bundled KV Cache Size Calculator model catalog. Use `kvcache-simulator list-models` to list ids. |
| `--kv-precision ID` | KV cache precision: usually `bf16_fp16`, `fp8_int8`, or `fp4_int4`. Defaults follow the web calculator. |
| `--indexer-precision ID` | Indexer cache precision for models with an indexer cache, such as DeepSeek V4 / GLM / MiniMax M3. |
| `--include-draft-kv-cache` | Include draft/MTP KV layers when the selected model defines them. Default is off. |
| `--block-size N` | Fallback block size when trace records omit `block_size`; trace-declared `block_size` overrides it. |
| `--estimate-tokens N` | Override the token count used for token-dependent bytes/token formulas. By default the trace average input length is used. |
| `--budgets-gib A,B,C` | Comma-separated KV cache memory budgets in GiB. Default matches the web budget sweep: `1,2,4,...,16384`. |
| `--policies fifo,lru,lru-flat,optimal` | Eviction policies to simulate. Defaults to all four. |
| `--backend cpp\|python` | Simulation backend. Default is `cpp`; use `python` for debugging or machines without a compiler. |
| `--jobs N` | Number of worker processes for the Python backend. The C++ backend runs one batch process and ignores this option. |
| `--no-progress` | Disable terminal progress output. Progress is written to stderr only when stderr is interactive, so JSON stdout stays valid. |
| `--format table\|json` | Output format. Default is `table`. |
| `--output PATH` | Write output to a file. Default `-` prints to stdout. |
| `--max-records N` | Debug/testing limit: stop after N valid requests. |
| `--max-events N` | Debug/testing limit: stop after N trace blocks. |

## Output Semantics

All policies use prefix-specific block identities: the same raw block ID under different preceding prefixes occupies separate cache entries. `lru` evicts eligible trie leaves and protects the current request's cached path. `lru-flat` evicts the least recently used entry regardless of its parent or children; eviction leaves descendants cached.

Flat LRU processes each request with separate read and write passes. Reading refreshes only the continuous cached prefix, stopping at the first miss. Writing visits the complete request in prefix order: an exact cached node is refreshed, and a missing node is inserted, evicting the least recent entry when full. A matching raw block ID under another prefix is a different entry. The writer checks current membership even for blocks read earlier. Tail insertion may evict the head of the same request; the writer makes one pass without restarting repairs. Recency follows access order, not timestamps.

Flat LRU appears as `LRU-FLAT hit` and `LRU-FLAT speedup` in the default table and as `lru-flat` in JSON results. To run only this policy, add `--policies lru-flat` with either backend.

- Hit rate is measured over the last 50% of requests.
- Budget points that do not fill the cache before that measurement window are omitted, because they are not under memory pressure yet.
- Hit tokens count only the longest continuous cached prefix of each request. If a middle block misses, later blocks in that same request do not count as prefill hits even if their ids are already cached.
- `speedup` is an ideal prefill-only upper bound: `1 / (1 - hit_rate)`. `1.0x` means no-cache prefill throughput where every prefill input token is computed. It does not include decode, KV lookup, network, batching, scheduling, or memory bandwidth overhead.

## Performance Notes

The default C++ backend runs all cache-budget points in one batch after loading the trace and building the prefix trie once. This is usually faster than the Python backend, especially on large traces.

`--jobs` applies only to the Python backend. It parallelizes independent `(policy, cache budget)` simulation tasks. More jobs are not always faster: the default budget sweep has only a small number of budget points, tasks have uneven runtimes, and large traces can become memory-bandwidth limited.

## Limits

The browser version caps uploads to protect the UI. This local package does not use that browser cap, but the C++ backend currently stores trace events and prefix node indexes in 32-bit arrays, so it supports at most `2^32 - 1` block events. In practice, memory and runtime are usually the real limits before that.

## Development

The package bundles two files under `src/kvcache_sim/resources/`: `models.yaml`, a copy of `data/kv_cache_calculator/models.yaml`, and `kv-cache-lab-native-sim.cc`, the batch C++ core the `cpp` backend compiles on first use (it takes `--policy batch --capacities ... --policies ...`; the single-budget `scripts/kv-cache-lab-native-sim.cc` serves the site's precompute pipeline). After changing the catalog run `scripts/sync-simulator-resources.sh`; the test suite checks that the bundled copy matches.

```bash
pip install -e ./packages/kvcache-simulator
pytest packages/kvcache-simulator
```
