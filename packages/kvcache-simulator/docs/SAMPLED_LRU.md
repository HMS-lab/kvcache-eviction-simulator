# Sampled LRU and recorded-time replay

`sampled-lru` runs by default alongside the five existing policies when the trace
has usable timing. **The existing policies retain their original algorithms and
sequential replay; they ignore timestamps.** A default run skips Sampled LRU with
a notice if timing is missing or invalid. Explicitly requesting it instead reports
an error. JSON lists effective policies and `metadata.skippedPolicies`; CLI notices
are written to stderr.

Your converted SemiAnalysis JSONL can be used directly, without another conversion:

```bash
kvcache-simulator run --trace converted.jsonl --model glm-5.2 \
  --policies sampled-lru --sampled-lru-threshold-seconds 10 \
  --sampled-lru-sample-size 16 --sampled-lru-seed 0
```

All three policy parameters are optional; their defaults are **10 seconds**, 16
sampled blocks, and seed 0. Threshold zero makes every resident block eligible.
Sampling uses Python `random.sample` and C++ `std::sample` without replacement.
Each capacity starts with a fresh seeded generator. Repeated runs in the same
backend/runtime are reproducible; Python and C++ need not produce identical
samples, evictions, or hit rates. C++ standard sampling scans the population.

Timing fields are numeric seconds, normalized to integer microseconds. Each request
needs `timestamp` and either `completion_timestamp` or `api_time` (or both, agreeing
within one microsecond). Completion is `timestamp + api_time`; an absolute
`completion_timestamp` already includes the start. Times must be finite,
nonnegative, ordered by arrival, and finish no earlier than arrival. Equal arrivals
retain file order. Python parsers accept `timing_mode="optional"` (default),
`"required"`, or `"ignore"`; sweep/policy APIs accept `sampled_lru_threshold_seconds`,
`sampled_lru_sample_size`, and `sampled_lru_seed`.

The virtual clock jumps between arrivals and completions without real-time waiting.
Previously admitted completions run before arrivals at the same time. Completion
ties sort by `(conversation_id, conversation_index, request_index, input_ordinal)`;
conversation IDs compare lexicographically, indices numerically. Missing identifiers
use an empty conversation ID, conversation index zero, and the input ordinal as
request index. Zero-duration requests complete after the entire arrival batch.

At arrival, reads count and refresh only the continuous cached prefix, stopping at
the first hole. At completion, the entire input is written in prefix order,
refreshing existing exact nodes or inserting missing ones. Membership is checked
on each write. Entries evicted while in flight can be restored; writes can evict
earlier entries of the same request without restarting repairs. Identical raw
blocks reached through different prefixes occupy different entries. Evicting an
ancestor leaves descendants resident, but they earn no read hits beyond a hole.

On a full-cache insertion, sample up to the configured number of resident entries
and evict **all** sampled entries strictly older than the threshold. Extra free
slots serve later writes without further sampling. If none qualify, scan slots
from index zero and stop when one eligible victim is found. If a complete scan
finds none, halve a local copy of the threshold and restart. Integer halving
reaches zero, where even entries accessed at the current time are eligible. Every
new eviction operation starts with the configured threshold again. Slots remain
stable and free slots are reused last-in-first-out; oversized requests write one
block at a time.

Warmup and measurement still use request counts. Sampled LRU must fill the cache
before the first measured arrival; unavailable results appear as `N/A` in mixed
sweeps and do not suppress existing-policy budget points. New-policy-only sweeps
omit underfilled budgets. Replay modes and policy parameters are included in
metadata. The existing ceiling uses sequential replay. `idealPrefillSpeedup` remains
a hit-rate-derived bound, not measured latency or throughput.

Recorded service durations never change with simulated cache hits. Active request
working state is modeled separately from reusable cache capacity, with no pinning,
in-flight computation sharing, active GPU memory accounting, or publication of
generated-output block identities. Timing-aware latency prediction and dependent
turn rescheduling are outside this replay model.

Timing adds two uint64 values and one uint32 rank per request in the native input,
plus in-flight heap and per-cache recency metadata. It does not add timestamps per
trace block. The CLI continues releasing Python event and timing arrays before C++
starts. API calls preserve their input by default; `release_trace_data=True` also
consumes timing arrays. This is not a hard 4 GB memory guarantee.

## Worked examples

All times below are **seconds**; tests convert them to integer microseconds.
`A@4` means a prefix-specific cache entry named A last accessed at time 4.
`--` is a free slot. Slot indices are zero-based. The threshold is 10 seconds
in every example. Samples are explicitly chosen where shown, so the examples do
not depend on Python and C++ generating the same random sequence.

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
DocumentedExamplesTests.test_batch_eviction_and_free_slot_reuse](../tests/test_sampled_lru_examples.py#L33).

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

This scan succeeds at the configured threshold. There is no threshold halving
and no second sample. The fallback stops once this insertion has space; the
sampling phase is the phase that evicts every eligible member of its sample.

Test: [test_sampled_lru_examples.py —
DocumentedExamplesTests.test_sample_miss_falls_back_to_first_eligible_slot](../tests/test_sampled_lru_examples.py#L53).

### 3. Halving is local, and zero permits eviction of same-time entries

Capacity and sample size are both 2. A and B were last accessed at time 8;
C, D, and E are written one after another at time 10.

```text
initial slots:       [A@8] [B@8]       both ages = 2

write C@10:
  sample both at threshold 10 -> no victims
  scan with threshold 10     -> none
  scan with threshold  5     -> none
  scan with threshold  2.5   -> none
  scan with threshold  1.25  -> evict A from slot 0
  slots:             [C@10] [B@8]

write D@10:
  START AGAIN at threshold 10; sample both -> no victims
  scans: 10 -> 5 -> 2.5 -> 1.25
  slot 0: C's age 0 fails; slot 1: B's age 2 qualifies
  slots:             [C@10] [D@10]

write E@10:
  START AGAIN at threshold 10; both ages = 0
  scans: 10 -> 5 -> 2.5 -> 1.25 -> ... -> 0.000001 -> 0
  at ZERO: every resident entry is eligible, including age 0
  evict C from slot 0
  slots:             [E@10] [D@10]

configured threshold after all three writes: still 10 seconds
```

Halving operates on integer microseconds, rounding down. Zero is a special rule:
it permits eviction even when `last_access_time == now`. Only the local scan
threshold changes; the next eviction starts at the configured value.

Test: [test_sampled_lru_examples.py —
DocumentedExamplesTests.test_threshold_halving_resets_and_reaches_zero](../tests/test_sampled_lru_examples.py#L67).

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
DocumentedExamplesTests.test_prefix_hole_and_completion_refresh](../tests/test_sampled_lru_examples.py#L92).

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
**50%**. The test checks the exact read/write events and final timestamp, and
also checks the result with the C++ backend when a compiler is available.

Test: [test_sampled_lru_examples.py —
DocumentedExamplesTests.test_clock_overlap_and_completion_before_arrival](../tests/test_sampled_lru_examples.py#L135).
