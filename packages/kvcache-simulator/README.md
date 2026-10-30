# KV Cache Simulator

`kvcache-simulator` runs the KVCache.AI hit-rate simulator locally on JSONL traces. It uses the same model accounting formulas as the KV Cache Size Calculator and the same prefix-aware hit-rate semantics as the KV Cache Hit Rate Simulator.

## Installation

```bash
pip install kvcache-simulator
```

The default backend compiles the bundled C++ simulator on first use.
You need a local C++ compiler such as `c++` or `clang++`.
The Python backend supports sequential replay for the five existing policies.
It does not support recorded-time replay or sequential Sampled LRU.

## Quick Start

```bash
 kvcache-simulator run --scheduler sequential --trace ~/ai_traces/mooncake_trace.jsonl  --model glm-5.2  --kv-precision fp8_int8  --indexer-precision fp4_int4 --block-size 512

 kvcache-simulator run --scheduler recorded --trace ~/ai_traces/semianalysis_converted_concurrency_10.jsonl  --model glm-5.2  --kv-precision fp8_int8  --indexer-precision fp4_int4 --block-size 512 --hyper-clock-max-score 3

```

The default output is a readable table. Use `--format json` when another script needs to consume the result.

```bash
kvcache-simulator run \
  --scheduler sequential \
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

Fields required only for recorded-time replay:

- `timestamp`: arrival time in seconds for recorded-time replay. Requests must follow chronological arrival order.
- `completion_timestamp` / `api_time`: absolute completion time / duration in seconds for recorded-time replay.

Other optional fields:

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

Recorded-time replay uses the converted timestamps without another conversion.
These times describe the recorded workload.
They do not predict execution times for another model or server.
The simulator still loads the converted trace into memory.

## Request schedulers

Every `run` or `sweep` command requires `--scheduler recorded` or `--scheduler sequential`.
There is no default scheduler.
The selected scheduler controls every requested eviction policy and the hit-rate ceiling.
Use the same scheduler when you compare policy results.

| Behavior | `sequential` | `recorded` |
| --- | --- | --- |
| Request order | File order, with each completion immediately after its arrival. | Recorded arrival and completion times. |
| Requests in flight | One request at a time. | Requests can overlap. |
| Cache reads | At each arrival. | At each arrival. |
| Cache writes | Immediately after that request's read. | At the recorded completion time. |
| Timing fields | Not required. The scheduler ignores them. | Required for every request. |
| Virtual clock | Always zero. | Jumps between event times without real-time waits. |
| Backend | C++ supports all six policies. Python supports the five existing policies. | C++ supports all six policies. |

Use sequential replay to compare eviction behavior with immediate cache writes.
It preserves the original replay order for FIFO, LRU, LRU-FLAT, HYPER-CLOCK, and OPTIMAL.
For example:

```bash
kvcache-simulator run \
  --scheduler sequential \
  --trace ~/ai_traces/mooncake_trace.jsonl \
  --model glm-5.2 \
  --kv-precision fp8_int8 \
  --indexer-precision fp4_int4 \
  --block-size 512
```

Use recorded-time replay to include concurrent requests and delayed cache writes.
A request cannot read blocks that another request publishes after its arrival.
For example:

```bash
kvcache-simulator run \
  --scheduler recorded \
  --trace ~/ai_traces/semianalysis_converted_concurrency_10.jsonl \
  --model glm-5.2 \
  --kv-precision fp8_int8 \
  --indexer-precision fp4_int4 \
  --hyper-clock-max-score 3
```

Recorded-time replay requires `timestamp` and at least one of `completion_timestamp` or `api_time`.
All fields use seconds. Completion time is `timestamp + api_time` unless an absolute `completion_timestamp` supplies it.
If both completion fields exist, their values must agree within one microsecond.
Missing or invalid timing data stops the run, even when Sampled LRU is not selected.
Sequential replay ignores missing or invalid timing data.

At equal timestamps, previously admitted requests complete before new arrivals.
The arrival batch keeps file order.
New zero-duration requests complete after the entire arrival batch.
Completion ties use conversation identifiers and request indices.
See [timing and tie rules](docs/SAMPLED_LRU.md#clock-and-cache-access) for details.

Sequential replay keeps every Sampled LRU timestamp at zero.
A positive age threshold therefore reaches the threshold-halving fallback during eviction.
A threshold of zero makes all sampled resident blocks eligible immediately.
Use recorded-time replay when you need a threshold measured in seconds.

The C++ backend constructs only the selected scheduler, once per invocation.
All policies access that instance through `RequestScheduler::get()`.
Each simulation resets its cursor before the next policy or cache capacity starts.
The recorded sequence stays the same across cache capacities.
HyperClock retains separate write offsets for requests in flight.
OPTIMAL updates future-use priorities as arrivals occur.

JSON reports the selection in `metadata.scheduler`, `metadata.replayModes`, and `metadata.ceilingReplayMode`.
Python API calls to `run_sweep()` and `run_cpp_sweep()` also require `scheduler="recorded"` or `scheduler="sequential"`.

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
| `--policies fifo,lru,lru-flat,hyper-clock,optimal,sampled-lru` | C++ defaults to all six policies. Python defaults to the five existing policies. |
| `--scheduler recorded\|sequential` | Required. Selects one scheduler for every policy and the ceiling. |
| `--hyper-clock-max-score N` | Hyper Clock score limit; default `3`. Must be an integer in `1..2^64-1`. |
| `--sampled-lru-max-evictions k` | Evict at most `k` oldest qualifying sampled blocks; default evicts all qualifying sampled blocks. |
| `--backend cpp\|python` | Default is `cpp`. Python supports sequential replay for the five existing policies. |
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

### Hyper Clock

`hyper-clock` keeps blocks in stable array slots and chooses victims by circular scanning. Each new block starts with score `1`. A lookup hit or a write to an existing block increases its score by one, saturating at `--hyper-clock-max-score` (default `3`). Hits leave the block's position and clock hand unchanged. Scores do not age with timestamps, misses, or insertion into free space.

When full, the cache repeatedly inspects the slot at the hand and advances the hand to the next slot, wrapping around as needed. Empty slots are skipped. An occupied slot with a positive score loses one point and survives that visit, including a change from `1` to `0`. An occupied slot already at zero is evicted. The hand stays at the next slot for the next victim search. No entries are pinned, and ancestors and descendants are equally eligible for eviction.

With `max_score=1`, this is one-bit second-chance CLOCK. An entry with score `S` and no further accesses survives `S` decrementing visits and is evicted on the following visit. Lookup and score refresh are O(1) on average; selecting a victim can require several complete sweeps. With `N` occupied slots in a dense array and no concurrent accesses, a victim is found within `max_score * N + 1` inspections.

Deleted or evicted slots become holes without moving other entries. Clearing a slot does not reposition the hand; victim selection advances it as described above. Free indices go onto a stack: deleting slots `1` then `3` produces `[1,3]`; the next insertion pops and fills slot `3`. This allocation does not scan or age entries. Storage grows on demand up to the block capacity, and a full-cache insertion reuses its victim's slot.

With recorded-time replay, HyperClock restores read hits that the cache evicts before completion.
It then writes the suffix identified during that request's read.
Surviving read hits receive no additional score update at completion.

Run only Hyper Clock with either backend:

```bash
kvcache-simulator run \
  --scheduler sequential \
  --trace trace.jsonl.gz \
  --model qwen3-32b \
  --policies hyper-clock \
  --hyper-clock-max-score 3
```

The table includes `HYPER-CLOCK hit`, `HYPER-CLOCK speedup`, and the configured maximum score. JSON results use `hyper-clock`, with the configuration in `metadata.policyParameters["hyper-clock"].maxScore`.

#### Clock examples

Slot and hand indices below are zero-based. `null` means an empty slot. In the first two examples, perform one victim search; in the third, perform only the listed accesses. The test suite reads these examples directly and checks both implementations.

<!-- hyper-clock-victim-examples -->
```json
[
  {"name":"second chances", "max_score":3, "scores":[1,1,0,1], "hand":0, "accesses":[], "victim_slot":2, "scores_after":[0,0,null,1], "hand_after":3},
  {"name":"wraparound", "max_score":3, "scores":[1,1], "hand":0, "accesses":[], "victim_slot":0, "scores_after":[null,0], "hand_after":1},
  {"name":"saturating hits", "max_score":3, "scores":[1], "hand":0, "accesses":[0,0,0,0], "scores_after":[3], "hand_after":0}
]
```

For `[1,1,0,1]`, slots `0` and `1` lose their scores, slot `2` is evicted, and slot `3` is untouched. For `[1,1]`, the first sweep clears both scores; the search wraps and evicts slot `0`. Repeated hits in the third example never raise the score above `3`.

#### Prefix holes and suffix writes

Hyper Clock reads only the continuous cached prefix and stops at the first miss. It then writes the suffix starting at that miss, in prefix order. A complete hit has no write pass, so each read hit gains only one point. A cached block beyond a hole gets no read refresh or hit credit; it is refreshed only when reached by the suffix writer.

For request `[A,B,C]`, suppose only `A` and `C` are cached, both with score `1`, with one free slot. Each block represents 64 tokens. Reading hits `A`, stops at missing `B`, and returns `(64,1)`: 64 hit tokens and write start at request position `1`. It leaves `C` unchanged. Writing inserts `B` with score `1` and refreshes `C` to `2`. Scores by request position are now `[2,1,2]`. The next read hits all 192 tokens, leaving scores `[3,2,3]`; it returns write start `3`, the exclusive end of the request, so nothing is rewritten.

<!-- hyper-clock-prefix-example -->
```json
{"capacity":3, "max_score":3, "block_size":64, "request":["A","B","C"], "cached_positions":[0,2], "hit_tokens":64, "write_start":1, "scores_after_read":[2,null,1], "scores_after_write":[2,1,2], "next_hit_tokens":192, "next_scores":[3,2,3]}
```

Evicting an ancestor leaves its descendants resident, consuming capacity. They can be evicted only by ordinary victim selection, or become readable again when their preceding prefix is present. Suffix writing checks current membership at each step: earlier writes may have evicted a later suffix block, which must then be inserted with score `1`. Writes may also evict blocks read earlier in the same request. Already counted hits remain valid, and the writer makes one pass without restarting repairs. A request can exceed cache capacity because its blocks are inserted individually. Token weights affect hit accounting; every block still occupies one slot.

For an end-to-end example, use capacity `3`, maximum score `3`, and these four one-token-block requests. The first two requests are warmup. Inserting `X` evicts `A` but leaves `B` and `C`. The third request misses immediately, reconstructs its suffix, and receives zero hits; the fourth hits all three tokens. The measured result is `3 / 6 = 50%` and ideal speedup is `2.0x`.

<!-- hyper-clock-replay-example -->
```json
{"capacity":3, "max_score":3, "warmup_fraction":0.5, "requests":[["A","B","C"],["X"],["A","B","C"],["A","B","C"]], "hit_tokens":3, "total_tokens":6, "hit_rate":0.5}
```

### Measurement for all policies

- Hit rate is measured over the last 50% of requests.
- Budget points that do not fill the cache before that measurement window are omitted, because they are not under memory pressure yet.
- Hit tokens count only the longest continuous cached prefix of each request. If a middle block misses, later blocks in that same request do not count as prefill hits even if their ids are already cached.
- `speedup` is an ideal prefill-only upper bound: `1 / (1 - hit_rate)`. `1.0x` means no-cache prefill throughput where every prefill input token is computed. It does not include decode, KV lookup, network, batching, scheduling, or memory bandwidth overhead.

## Performance Notes

The C++ backend loads the trace and builds the prefix trie once.
It constructs the selected scheduler once and reuses it across all policies and cache capacities.

After writing the binary trace files, the CLI releases Python's block ID, token weight, and request boundary lists before starting C++. It retains scalar metadata for output. Parsing still holds the full trace in memory before this handoff. Temporary files follow `TMPDIR` (normally `/tmp`); if that directory uses `tmpfs`, the files themselves consume memory.

Python API calls to `run_sweep()` and `run_cpp_sweep()` preserve the input trace by default. With the C++ backend, pass `release_trace_data=True` to release those lists after writing; the consumed trace cannot be reused for another simulation. The Python backend retains its lists for replay.

`--jobs` applies only to the Python backend. It parallelizes independent `(policy, cache budget)` simulation tasks. More jobs are not always faster: the default budget sweep has only a small number of budget points, tasks have uneven runtimes, and large traces can become memory-bandwidth limited.

## Limits

The browser version caps uploads to protect the UI. This local package does not use that browser cap, but the C++ backend currently stores trace events and prefix node indexes in 32-bit arrays, so it supports at most `2^32 - 1` block events. In practice, memory and runtime are usually the real limits before that.

## Development

The package bundles two files under `src/kvcache_sim/resources/`: `models.yaml`, a copy of `data/kv_cache_calculator/models.yaml`, and `kv-cache-lab-native-sim.cc`, the batch C++ core the `cpp` backend compiles on first use (it takes `--policy batch --capacities ... --policies ...`; the single-budget `scripts/kv-cache-lab-native-sim.cc` serves the site's precompute pipeline). After changing the catalog run `scripts/sync-simulator-resources.sh`; the test suite checks that the bundled copy matches.

```bash
pip install -e ./packages/kvcache-simulator
pytest packages/kvcache-simulator
```

### Sampled LRU and recorded-time replay

See [Sampled LRU documentation](docs/SAMPLED_LRU.md) for configuration, timing, and eviction behavior.
