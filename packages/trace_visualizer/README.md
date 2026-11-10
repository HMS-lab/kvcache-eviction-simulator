# Prefix Trace Explorer

A local interactive viewer for SemiAnalysis JSONL inference traces. Prefixes grow **top to bottom**. The browser processes the file locally; no trace content is uploaded.

## Start

With Node.js 20 or newer installed:

```bash
npm run dev
```

Open **http://localhost:5173**. There are no dependencies to install and no build step. Alternatively, serve this directory with `python3 -m http.server 5173 --bind 127.0.0.1`.

Click **Open trace**, or drop a JSONL file onto the page. For the dataset used during development, select:

```text
/home/chen_baihua/ai_traces/semianalysis-infrencex-052726-256k.jsonl
```

**Try example** opens a small, explicitly synthetic multi-model workload to explore the controls. No real traces are bundled.

## Explore

- **First session:** choose where replay starts in file order. All sessions from that point onward are replayed without wrapping. Hash IDs never merge across sessions.
- **Concurrent conversations:** set how many sessions run together. A free slot immediately takes the next session after its predecessor’s final request completes, including subagent requests. Each session’s first request is placed at its new start time; subsequent relative timestamps are preserved. Changing the first session or concurrency rebuilds replay and resets the playhead to zero. Near the end, concurrency drops as the remaining sessions finish.
- **Request timeline:** the top density bar summarizes all matching request durations. Session rows are collapsed by default; click a session label to reveal its main agent and subagents. Click an agent label to highlight its blocks. Live counters show in-flight requests and active conversations across the replay, independently of model/agent filters. A request is active from its start until its API duration ends; a conversation stays active through gaps until its final completion.
- **Model:** select one model, or show independent model trees together with a shared timeline. Footprint is summed across model trees.
- **Agent:** filter the main agent or a specific sub-agent. All agents contribute to the same model tree when they share prefixes.
- **Cumulative:** show requests starting at or before the playhead.
- **Window:** show requests starting within the trailing window, including both endpoints.
- **Playback:** play at 1×–1000×, step by request, drag the playhead, or click a request on the timeline. Long gaps are preserved.
- **Timeline row totals:** every session, agent, and **All requests** row shows cumulative input tokens and input blocks accessed through the playhead. Repeated inputs count on every request; partial input blocks count as full blocks. Totals follow model/agent filters and remain cumulative in window mode. Hover over a row label for one second (or focus it with the keyboard) to see its full session ID or agent name, exact comma-separated input/output token counts, block accesses, and started requests.
- **Agent highlighting:** click an agent label in the timeline to highlight all its prefix segments in the current time range and dim other segments. Counts and filters remain unchanged. Click the label again or **Clear agent highlight** to reset. The tree prioritizes that agent's branches; collapsed descendants remain available through **Child prefixes**.
- **Tree:** drag to pan and scroll to zoom. Use **Fit**, the zoom buttons, or keyboard arrows, `+`, `-`, and `0` while the canvas is focused. The initial view prioritizes frequently used branches within a viewport-sized detail budget. Select a segment and use **Child prefixes** to explore any omitted branch. Increase **Levels** for deeper paths; **All** disables the detail budget and arranges the complete tree. Branch layout stays fixed within a session while scrubbing. Session roots enter or leave the view as the selected time range changes; unseen branches within each session reserve space but remain hidden. Metrics always include all matching branches.
- **Token depth:** vertical position reflects prefix depth with minimum spacing to keep consecutive labels readable. **Compact** uses uniform segment spacing.
- **Selection:** click a segment to see its request count, agent breakdown, accesses over time, and latest matching requests. White endpoint markers indicate requests ending there. Use the expandable **Browse visible segments** list as an alternative to canvas selection.
- **Focus subtree:** show a selected prefix and its descendants. **Full tree** restores the overview.
- **Inspect blocks:** page through the selected segment's hash IDs and token positions, 100 blocks at a time. Positions describe full block capacity; the final block can be partial. Original prompt text is not available.

## Metrics and time

An **access** is one request whose input contains that prefix block. Every input traversal is attributed to its request's start timestamp; within-request block timings are not recorded. A compressed segment has the same visiting requests throughout its length. Request endpoints and branch points are preserved.

**Unique prefix blocks** counts each distinct block path once within each session and model. **Prefix footprint** is that count multiplied by `block_size`, in token slots. A partial final block occupies a full block. **Total input volume** includes repeated inputs. In window mode, the cards describe only the selected requests; the inspector's footprint chart remains explicitly cumulative for the selected model and agent.

This is an input workload footprint, **not measured cache occupancy**. There is no cache simulation, eviction policy, or inferred hit rate. Output hashes are absent and are not included in the footprint. The trace does not contain enough KV layout information to convert token slots to bytes without external assumptions.

The source dataset uses proxy-tokenizer input lengths and Anthropic-reported output lengths. Its timestamps have been reshifted after request filtering. Sub-agent timestamps are absolute within the session, not offsets from the containing group.

## Large files

A module Web Worker first scans file bytes for JSONL record boundaries, then reads and validates each trace. Progress is **Trace X/Y** (initially **Trace 0/…** while finding the total). Cancel terminates the worker; reopen the file to restart. Malformed JSON or invalid request shapes report their source line number.

Only record offsets and session summaries survive corpus loading. Replay preparation rereads sessions from the selected first session onward, one record at a time, and builds separate compressed radix trees. All replay topology and compact block hashes remain in memory; memory use grows with the replay workload. Raw request hash lists are released after each session is built. Raw hashes remain in the worker for block inspection; the UI receives compressed topology, request metadata, and traversal indexes. Canvas draws visible segments, with a level limit for large trees. The file is read from the selected local `File`, never a server upload.

Implementation uses native JavaScript modules, Canvas, and Web Workers; there are no third-party runtime dependencies. A modern desktop browser is required. Very large replays can still exceed browser memory. Selecting a later first session reduces the replay workload; lowering concurrency changes timing but does not reduce the number of sessions processed.

## Checks

```bash
npm test
npm run check
node scripts/verify-trace.mjs /path/to/traces.jsonl
```

Tests cover radix splits, endpoints, model separation, prefix identity, time and agent filters, partial blocks, UTF-8 file indexing, concurrent scheduling, session isolation, exhaustion without wrapping, and randomized comparison with an independent prefix enumerator. The optional real-file check validates the corpus and analyzes its first and largest sessions.
