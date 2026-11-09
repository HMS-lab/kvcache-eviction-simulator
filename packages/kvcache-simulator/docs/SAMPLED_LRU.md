# Sampling LRU variants and recorded-time replay

The C++ backend runs both Sampling LRU variants with the six other policies by default.
Every policy uses the scheduler selected with the required `--scheduler` option.
The ceiling uses the same scheduler.

Choose `recorded` to use trace timing and concurrent requests.
Missing or invalid timing data stops recorded-time replay.
Choose `sequential` for immediate reads and writes in file order.
Sequential replay ignores timing fields and keeps the virtual clock at zero.
The five existing policies preserve their original replay behavior with this scheduler.
See the [scheduler comparison](../README.md#request-schedulers) for examples and tradeoffs.

Use your converted SemiAnalysis JSONL directly. No further conversion is necessary:

```bash
kvcache-simulator run --scheduler recorded --trace converted.jsonl --model glm-5.2 \
  --policies sampling-lru-touch-on-put --sampled-lru-threshold-seconds 10 \
  --sampled-lru-sample-size 16 --sampled-lru-seed 0
```

## Recency variants

Select `sampling-lru-touch-on-put` to update recency on insertion, successful reads, and puts to existing entries.
Select `sampling-lru-touch-on-get` to update recency on insertion and successful reads only.
For touch-on-get, a put to an existing entry changes neither its timestamp nor its recency sequence.
An insertion after eviction sets a new timestamp and sequence in both variants.

Both variants use the same `--sampled-lru-*` options and eviction rules.
The existing touch-on-put policy keeps its behavior.
The worked examples below describe touch-on-put.

C++ tests check timestamps, recency sequences, eviction, and reinsertion:
[`NativeTests.test_native_batch_eviction_and_fallback`](../tests/test_sampled_lru.py).
A delayed completion causes different eviction victims and hit counts:
[`NativeTests.test_touch_on_get_preserves_recency_at_delayed_completion`](../tests/test_sampled_lru.py).

## Defaults and sampling

All four policy parameters are optional. The defaults are:

- A threshold of **10 seconds**.
- A sample size of 16 blocks.
- A random seed of 0.
- No eviction limit: evict all qualifying sampled blocks.

A threshold of zero makes every resident block eligible for eviction.
A resident block is a block that the cache currently stores.

Python uses `random.sample`. C++ draws random slot indices and rejects duplicates until it selects enough distinct slots.
The sample contains at most the number of slots.
Each variant and cache capacity starts with a new generator that uses the configured seed.

Repeated runs in the same backend and runtime produce the same results.
Python and C++ need not produce identical samples, evictions, or hit rates.

## Compare sample sizes

```bash
kvcache-simulator run --scheduler recorded --trace converted.jsonl --model glm-5.2 \
  --policies sampling-lru-touch-on-put,sampling-lru-touch-on-get \
  --sampled-lru-sample-size 16,32,128 --sampled-lru-max-evictions 2
```

This command runs six simulations for each cache capacity.
Each run has its own cache and random generator. Each generator starts with the configured seed.
Changing the order of sample sizes does not change a run's results.
The simulator constructs the scheduler once and resets it between runs.
Additional sample sizes increase the number of jobs. Each active job has a separate simulation cache.

Sample sizes must be positive 32-bit integers. Empty list elements are invalid.
Duplicate sizes appear once, in the order first specified.
The maximum evictions option accepts one integer and applies to every sampling run.

A header such as `Sampling LRU (touch on get) hit (2 out 128)` shows the configured limit and requested sample size.
With no limit, the header shows `all out 128`.
The effective sample size is the smaller of the requested size and the cache capacity.
Distinct requested sizes keep separate columns, even when their effective sizes match.

JSON uses an ordered `runs` array to describe each configuration.
With multiple sizes, result keys include the size, such as `sampling-lru-touch-on-get:sample-size=128`.
Single-size runs keep their existing keys.
Each sampling result contains `sampleSize` and `effectiveSampleSize`.
Shared parameters contain `sampleSizes`. For one size, they also retain the scalar `sampleSize` field.

C++ tests check standalone results against combined runs and verify capacity capping:
[`SampleSizeTests.test_native_isolation_and_cap`](../tests/test_sample_sizes.py).
Report and CLI checks use
[`SampleSizeTests.test_report_and_cli`](../tests/test_sample_sizes.py).

For a cache of five entries, insert a sixth entry with threshold zero and no eviction limit.
Sample sizes 1, 4, 5, and 16 leave 5, 2, 1, and 1 entries, respectively.
With an eviction limit of 2, they leave 5, 4, 4, and 4 entries.
These values are checked by
[`SampleSizeTests.test_native_eviction_counts`](../tests/test_sample_sizes.py).

## Progress display

Each Sampling LRU run reports progress during execution. The label identifies the policy, sample size, and cache capacity.
The label ends with `(events completed/total (X%))`, the percentage of scheduler events completed within that run.
Each request has two events: arrival and completion. An event counts as complete after all its cache operations finish.

Updates occur about once per second during active processing, including long requests and fallback scans.
The simulator checks wall time every 4096 operations to limit logging overhead.
The simulator also attempts to report the start and end of each run.
Short runs can finish before the first periodic update.

The event percentage belongs to the current run.
The header shows total, finished, and running jobs across the whole simulation.
An underfilled cache reports its percentage at the stopping point. It does not count skipped events as processed.
A zero-capacity run reports that it skips cache simulation.

The CLI displays a job-count header and one fixed row per worker on interactive stderr.
The CLI starts up to `--jobs` C++ worker threads. The default is `16`.
Use `--jobs 1` for sequential execution.
Each message identifies the worker and job. A new job replaces the previous job in that worker's row.
Worker rows show the policy, sample size where applicable, cache capacity in blocks, and event progress when available.
Worker rows omit job IDs and position counters. Policies without event progress show only their policy and capacity.
Python reads both subprocess pipes in background threads. The calling thread invokes progress callbacks and renders the rows.
The display refreshes at most five times per second, except for new rows and final updates.

C++ uses one mutex to keep each progress message intact.
Job updates use `try_lock()` and skip the message if the mutex is busy.
The shared completed-job count uses an atomic integer. The final overall update follows all completed jobs.
Progress is best effort. Missing updates do not delay a simulation.
Use `--no-progress` to disable the display.
The progress display does not change JSON results or cache behavior.

C++ tests: [test_sampled_lru_progress.py](../tests/test_sampled_lru_progress.py),
`NativeSampledProgressTests.test_event_counts_run_identity_and_unchanged_results`,
`test_early_exit_reports_actual_events`, and `test_throttle_counters_and_updates_inside_long_fallback`.

Progress synchronization tests: [test_request_scheduler.py](../tests/test_request_scheduler.py),
`NativeRequestSchedulerTests.test_concurrent_job_assignment_and_best_effort_reporting`.
Display tests: [test_progress_workers.py](../tests/test_progress_workers.py),
`WorkerProgressTests.test_worker_rows_keep_identity_throttle_and_clear_on_finish`.

## Trace timing

Timing fields contain numeric seconds. The simulator converts these values to
integer microseconds. Each request needs `timestamp` and at least one of
`completion_timestamp` or `api_time`. If both completion fields exist, `completion_timestamp` must match
`timestamp + api_time` within one microsecond.

Completion time is `timestamp + api_time`. An absolute `completion_timestamp`
already includes the start time. Timing values must meet these conditions:

- Each value is finite and nonnegative.
- Arrival times follow chronological order.
- Each request finishes at or after its arrival time.

Requests with equal arrival times keep their file order.

Python parsers accept three values for `timing_mode`:

- `"optional"` is the default.
- `"required"` requires valid timing data.
- `"ignore"` ignores timing data.

The sweep and policy APIs accept these parameters:

- `sampled_lru_threshold_seconds`.
- `sampled_lru_sample_size`.
- `sampled_lru_seed`.
- `sampled_lru_max_evictions` (default `None`, meaning no limit).

## Clock and cache access

With the recorded-time scheduler, the virtual clock jumps between arrivals and completions without waiting in
real time. Previously admitted requests complete before new requests arrive at
the same time.

Requests with equal completion times use this sort key:
`(conversation_id, conversation_index, request_index, input_ordinal)`.
Conversation IDs use lexicographical order. Indices use numeric order.
Missing identifiers use an empty conversation ID, conversation index zero, and
the input ordinal as the request index.

Zero-duration requests complete after the entire batch of arrivals at that time.

At arrival, the simulator reads only the continuous cached prefix.
A prefix hole is the first missing block in that prefix.
Reads count hits and refresh entries only before the first hole.

At completion, the simulator writes the entire input in prefix order.
Both variants insert missing nodes.
Touch-on-put also refreshes existing exact nodes. Touch-on-get preserves their recency.
It checks cache membership on each write.
Writes can restore entries that the cache evicted while the request was in flight.

A write can evict an earlier entry from the same request.
The writer does not restart repairs. Identical raw blocks reached through
different prefixes occupy different entries.
Evicting an ancestor leaves its descendants in the cache.
Those descendants earn no read hits beyond a prefix hole.

## Reusable C++ scheduler

The C++ backend uses the abstract `RequestScheduler` interface.
Its singleton accessor returns the active scheduler.
Future scheduling strategies can implement this interface.
The Python backend keeps its existing scheduler.

`SimulationRunner` constructs only the selected scheduler, once per invocation.
All policies and cache capacities reuse that instance.
Policies do not select their own scheduler.
Each simulation uses a separate cache and random generator where applicable.

`SequentialRequestScheduler` generates an arrival followed immediately by a completion for each request in file order.
It stores only the event count and cursor.
Its events use timestamp zero.
A positive Sampling LRU threshold cannot expire entries while the clock stays at zero.
Eviction therefore uses the threshold-halving fallback.
A threshold of zero makes every sampled resident block eligible immediately.

`RecordedTimeScheduler` constructs one sequence from the recorded times.
Each request contributes an arrival event and a completion event.
The sequence preserves the ordering rules above.
HyperClock stores a separate write offset for each request in flight.
It restores read hits that the cache evicts before completion.
OPTIMAL excludes past arrivals when it selects future-use priorities at completion.

The shared scheduler provides these methods:

- `RequestScheduler::get()` returns a read-only reference to the shared instance.
  It asserts that the instance exists.
- `size()` returns the number of events.
- `event_at(index)` returns the event at an index without changing shared state.
  Each event contains a request index, an event kind, and a timestamp.

Each simulation constructs a separate `SchedulerCursor`.
The cursor references the shared scheduler and stores its own position.
A thread-local pointer identifies the active cursor.
C++ worker threads share the read-only scheduler. Each active simulation has a separate cursor.

The cursor provides these methods:

- `SchedulerCursor::get()` returns the active cursor for the current thread.
  It asserts that the cursor exists and its position is zero.
  Policies call this method instead of receiving a cursor argument.
- `next_event()` returns the next event and advances only this cursor.
  At the end, the method returns no event.
- `exhausted()` reports whether the cursor reached the end.

The cursor constructor asserts that no other cursor is active on the current thread.
The cursor destructor removes its registration, including after an early return or exception.
The next simulation creates a fresh cursor at position zero.
No reset or discard operation is necessary.
The shared scheduler contains no cursor or cache blocks.

The recorded-time scheduler stores two 64-bit event codes per request, or 16 bytes per request.
It reads timestamps from the existing timing arrays. The owner must keep these arrays alive until it destroys the scheduler.
The selected scheduler registers itself during construction and removes its registration during destruction.
The scheduler stores no cache blocks.

C++ tests cover ordering, reuse, cancellation, and the abstract interface.
`test_legacy_policies_match_results_before_extraction` checks recorded results for all five existing policies and the ceiling.
`test_selected_scheduler_shared_after_all_return_paths` checks scheduler reuse after normal, underfilled, and zero-capacity results.
Both tests use the C++ backend:

[NativeRequestSchedulerTests](../tests/test_request_scheduler.py).
[SchedulerSelectionTests](../tests/test_scheduler_selection.py) checks CLI selection and concurrent replay for all C++ policies.

## Eviction

When an insertion finds the cache full, the policy samples up to the configured
number of resident entries. It evicts **all** sampled entries strictly older than
the threshold by default. Set `--sampled-lru-max-evictions k` (or
`sampled_lru_max_evictions=k` in the sweep/policy APIs) to evict at most `k`
qualifying sampled blocks, starting with the oldest. The limit must be a positive
32-bit integer. A limit of 1 evicts only the oldest qualifying sampled block;
a limit larger than the sample size can evict the entire qualifying sample.
Ties in access time use access sequence, oldest first. Blocks whose age equals
the positive threshold do not qualify. Later writes use the extra free slots
without further sampling.

If no sampled entry qualifies, the policy uses this fallback:

1. Scan all slots once. Collect resident slot indices.
2. Build a min-heap from these indices. The oldest entry is at the top.
3. Check the oldest entry against the configured threshold.
4. If it does not qualify, halve a local copy of the threshold. Repeat the check without another scan.
5. Evict eligible entries from the heap, oldest first, up to `--sampled-lru-max-evictions`.

Without an eviction limit, the fallback evicts all entries eligible at the selected threshold.
The fallback stops before an entry that does not qualify. Equal timestamps use access sequence, oldest first.
The successful-sample path keeps its existing behavior.

For `N` resident entries and `K` victims, the fallback uses `O(N + K log N)` work and `O(N)` temporary storage.
Threshold halving adds at most 64 checks of the oldest entry for a 64-bit threshold.
It does not repeat the full scan.

C++ tests: [test_sampled_lru_fallback.py](../tests/test_sampled_lru_fallback.py),
`NativeHeapFallbackTests.test_oldest_first_limit_and_local_halving`,
`test_configured_threshold_and_strict_age_boundary`, and `test_zero_age_ties_and_no_repeated_full_scans`.

For example, use a threshold of 10 microseconds, an eviction limit of 2, and the following full cache:

```text
slot:                  0      1      2      3      4
before:              [A@9]  [B@8]  [C@3]  [D@2]  [E@1]
age at now=10:          1      2      7      8      9
sample:                ^
                       A does not qualify

one scan -> min-heap: E, D, C, B, A (oldest first)
threshold 10: E's age 9 does not qualify
threshold  5: E qualifies; evict E, then D; stop at limit 2
insert F@10:         [A@9]  [B@8]  [C@3] [F@10]   [--]
```

The final insertion reuses slot 3, the last freed slot.
Without a limit, the fallback also evicts C. B and A remain because their ages do not exceed 5.
The configured threshold remains 10 for the next eviction.
Test: `NativeHeapFallbackTests.test_oldest_first_limit_and_local_halving` in
[test_sampled_lru_fallback.py](../tests/test_sampled_lru_fallback.py).

Integer halving reaches zero. At zero, entries accessed at the current time
also qualify. Each new eviction operation starts with the configured threshold.
Slots keep their positions. The policy reuses free slots in last-in-first-out
order. Requests larger than the cache write one block at a time.

## Measurement

Warmup and measurement use request counts. Each Sampling LRU variant must fill the cache
before the first measured arrival. Mixed sweeps show unavailable results as
`N/A`. These results do not suppress budget points for the existing policies.
Sweeps with exactly one Sampling LRU run omit underfilled budgets.
With multiple runs, an underfilled sampling result shows `N/A` without removing other runs.

Metadata includes the selected scheduler, replay modes, and policy parameters.
The ceiling uses the selected scheduler.
`idealPrefillSpeedup` is a bound derived from the hit rate.
It does not measure latency or throughput.

## Model limits and memory

Recorded service durations do not change with simulated cache hits.
The model keeps active request working state separate from reusable cache capacity.
It excludes these mechanisms:

- Pinning entries.
- Sharing computation between requests in flight.
- Accounting for active GPU memory.
- Publishing block identities for generated output.

The model does not predict latency from timing data.
It does not reschedule dependent turns.

Timing adds two uint64 values and one uint32 rank per request to the native input.
It also adds an in-flight heap and recency metadata for each cache.
It does not add timestamps per trace block.

The CLI releases Python event and timing arrays before C++ starts.
API calls keep their input by default. With `release_trace_data=True`, API calls
also consume timing arrays. This behavior does not guarantee a 4 GB memory limit.

## Worked examples

All times below are **seconds**; the C++ test fixtures convert them to integer microseconds.
`A@4` means a prefix-specific cache entry named A last accessed at time 4.
`--` is a free slot. Slot indices are zero-based. The threshold is 10 seconds
in every example. Samples are explicitly chosen where shown, so the examples do
not depend on Python and C++ generating the same random sequence. All five
examples execute the actual **C++ backend** in a temporary native test harness;
Python only compiles and launches the harness. Test-only hooks control samples
and record cache events without changing production source.

### 1. Evict every eligible sampled entry, then reuse the free slots

Capacity is 4 blocks, sample size is 3, and E must be inserted at time 20.
The sample selects slots `[0, 1, 2]`:

```text
slot:                 0       1       2       3
before:             [A@0]   [B@4]  [C@10]  [D@18]
age at now=20:        20      16      10       2
sampled?             yes     yes     yes      no
age > 10?            yes     yes      no      no
                      |       |
                      +---+---+    evict BOTH A and B
                          v
free slots:           [0, 1]        oldest victim freed first
insert E@20:          pop 1         free slots now [0]

slot:                 0       1       2       3
after E:             [--]  [E@20]  [C@10]  [D@18]
insert F@21:        [F@21] [E@20]  [C@10]  [D@18]
                    ^
                    reuse slot 0; no new sample
```

C survives because its age is **exactly** 10, rather than strictly greater than
10. D was not sampled. The insertion frees two slots even though E needs only
one; F consumes the spare slot at time 21 without sampling again.

Test: [test_sampled_lru_examples.py —
NativeDocumentedExamplesTests.test_batch_eviction_and_free_slot_reuse](../tests/test_sampled_lru_examples.py#L170).

### 2. An unhelpful sample falls back to a linear scan

Capacity is 3, sample size is 1, and D must be inserted at time 20.
Only slot 2 is sampled:

```text
slot:                 0        1        2
before:             [A@0]    [B@15]   [C@19]
age at now=20:        20        5        1
sample:                                ^
                                       C is too recent; evict nothing

linear scan:         ^
                     A's age 20 > threshold 10
                     evict A; stop after freeing one slot

after D:            [D@20]   [B@15]   [C@19]
```

The fallback scans all three slots and builds the heap. A is the only eligible entry at the configured threshold.
The fallback therefore evicts A. It does not halve the threshold or take another sample.
The diagram marks the victim. It does not show every slot that the scan inspects.

Test: [test_sampled_lru_examples.py —
NativeDocumentedExamplesTests.test_sample_miss_falls_back_to_first_eligible_slot](../tests/test_sampled_lru_examples.py#L173).

### 3. Halving is local, and zero permits eviction of same-time entries

Capacity and sample size are both 2. The eviction limit is 1. A and B were last accessed at time 8;
C, D, and E are written one after another at time 10.

```text
initial slots:       [A@8] [B@8]       both ages = 2

write C@10:
  sample both at threshold 10 -> no victims
  heap check at threshold 10     -> none
  heap check at threshold  5     -> none
  heap check at threshold  2.5   -> none
  heap check at threshold  1.25  -> evict A from slot 0
  slots:             [C@10] [B@8]

write D@10:
  START AGAIN at threshold 10; sample both -> no victims
  heap checks: 10 -> 5 -> 2.5 -> 1.25
  slot 0: C's age 0 fails; slot 1: B's age 2 qualifies
  slots:             [C@10] [D@10]

write E@10:
  START AGAIN at threshold 10; both ages = 0
  heap checks: 10 -> 5 -> 2.5 -> 1.25 -> ... -> 0.000001 -> 0
  at ZERO: every resident entry is eligible, including age 0
  evict C from slot 0
  slots:             [E@10] [D@10]

configured threshold after all three writes: still 10 seconds
```

Halving operates on integer microseconds, rounding down. Zero is a special rule:
it permits eviction even when `last_access_time == now`. Only the local eviction
threshold changes; the next eviction starts at the configured value.

Test: [test_sampled_lru_examples.py —
NativeDocumentedExamplesTests.test_threshold_halving_resets_and_reaches_zero](../tests/test_sampled_lru_examples.py#L176).

### 4. A prefix hole stops reads; completion writes refresh the entire input

Capacity is 3, and each block accounts for 64 tokens. Initially A and C from the
same `[A, B, C]` prefix path are cached with timestamp 0; B is missing and there
is one free slot. The first request is warmup.

```text
request:          [A] ----> [B] ----> [C]
resident at t=10:  yes       no       yes
                   |        X         |
read:             HIT      MISS      not read
refresh to:        10       --         0
hit tokens:        64        0         0     total = 64
                            |
                            +--> recompute suffix B, C

completion t=12:  write A -> write B -> write C
                  refresh   insert     refresh
logical path:    [A@12] --> [B@12] --> [C@12]
physical slots:  [A@12]     [C@12]     [B@12]
                  slot 0     slot 1     slot 2

next arrival t=13: A HIT + B HIT + C HIT = 192 tokens
                  timestamps become 13
completion t=14:   write all three again; timestamps become 14
```

C gets no read refresh beyond the hole. Completion also refreshes A, although A
was already consumed as a hit. Slot allocation preserves C's existing slot and
appends B; logical prefix order does not require physical slot order.

The measured second request has `192 / 192 = 100%` hits; the first request's
64-token hit belongs to warmup. A matching raw ID on another path still has a
separate cache identity:

```text
root -- A -- B -- C     B and C here belong to prefix A
     \
      -- X -- B -- C   separate B and C entries under prefix X
```

Test: [test_sampled_lru_examples.py —
NativeDocumentedExamplesTests.test_prefix_hole_and_completion_refresh](../tests/test_sampled_lru_examples.py#L179).

### 5. In-flight computation is unavailable until completion

Capacity and sample size are both 1; each request has one 64-token block.
The first two requests are warmup. Cache hit status is fixed at arrival.

```text
request   block   arrival   completion   arrival-time result
R0        W          0          1        miss (warmup)
R1        A          0          5        miss (warmup)
R2        A          2          6        MISS (measured)
R3        A          5          7        HIT  (measured)

clock jumps:  0 -------- 1 -------- 2 -------- 5 -------- 6 -------- 7
              |         |          |          |          |          |
           R0,R1      R0 ends   R2 arrives  R1 ends    R2 ends    R3 ends
           arrive     write W   A still    write A    refresh A  refresh A
           no writes            in flight     |
                                              +-> THEN R3 arrives: A hits

cache:      empty     [W@1]       [W@1]     [A@5]      [A@6]      [A@7]
```

R2 misses at time 2 even though R1 is computing the same block. R1's later
completion cannot retroactively turn R2's miss into a hit. At time 5, completion
runs before arrival, so R3 sees A. Both R2 and the complete-hit R3 write A when
they finish. After the last arrival at time 5, the clock still visits completions
at times 6 and 7.

Measured hit tokens are **64**, total tokens are **128**, and hit rate is
**50%**. The test checks the exact read/write events and final timestamp, directly in the C++ backend. A C++ compiler is required to run these examples.

Test: [test_sampled_lru_examples.py —
NativeDocumentedExamplesTests.test_clock_overlap_and_completion_before_arrival](../tests/test_sampled_lru_examples.py#L182).
