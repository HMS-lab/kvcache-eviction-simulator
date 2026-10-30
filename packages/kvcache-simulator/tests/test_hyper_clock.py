from __future__ import annotations

from collections import deque
from dataclasses import replace
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from kvcache_sim._resources import package_resource_path
from kvcache_sim.cli import build_parser
from kvcache_sim.cpp_backend import _write_binary_trace, ensure_cpp_simulator, run_cpp_sweep
from kvcache_sim.plan import build_execution_plan
from kvcache_sim.policies import (
    _HyperClockCache, _read_hyper_clock_prefix, _write_hyper_clock_suffix, simulate_policy,
)
from kvcache_sim.simulator import _simulate_tasks, run_sweep
from kvcache_sim.trace import parse_trace_lines


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def documented_example(name):
    """The README's JSON examples are the fixtures, not copies in this file."""
    readme = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"<!-- hyper-clock-" + name + r" -->\s*```json\s*(.*?)\s*```", readme, re.S)
    if match is None:
        raise AssertionError(f"Missing documented Hyper Clock example: {name}")
    return json.loads(match[1])


def make_trace(paths, block_size=1):
    return parse_trace_lines([
        json.dumps({"block_size": block_size, "hash_ids": path, "input_length": len(path) * block_size})
        for path in paths
    ])


def seed_scores(scores, max_score=3, hand=0):
    cache = _HyperClockCache(len(scores), max_score)
    for index in range(len(scores)):
        cache.write(index + 1)
    for index, score in enumerate(scores):
        if score is None:
            cache.discard(index + 1)
        else:
            cache.slots[index].score = score
    cache.hand = hand
    return cache


def scores_for(cache, nodes):
    return [cache.slots[cache.entries[node]].score if node in cache else None for node in nodes]


def snapshot(cache, result=None):
    return {"result": result, "hand": cache.hand,
            "slots": [[slot.node or 0, slot.score] for slot in cache.slots]}


class HyperClockTests(unittest.TestCase):
    def test_documented_victim_examples(self):
        for example in documented_example("victim-examples"):
            with self.subTest(example=example["name"]):
                cache = seed_scores(example["scores"], example["max_score"], example["hand"])
                for index in example["accesses"]:
                    self.assertTrue(cache.lookup(index + 1))
                if "victim_slot" in example:
                    self.assertEqual(cache.evict(), example["victim_slot"] + 1)
                self.assertEqual(scores_for(cache, range(1, len(example["scores"]) + 1)), example["scores_after"])
                self.assertEqual(cache.hand, example["hand_after"])

    def test_documented_prefix_example(self):
        example = documented_example("prefix-example")
        plan = build_execution_plan(make_trace([example["request"]], example["block_size"]))
        cache = _HyperClockCache(example["capacity"], example["max_score"])
        for position in example["cached_positions"]:
            node = plan.node_for_event[position]
            cache.write(node)
        read = _read_hyper_clock_prefix(cache, plan, 0, len(plan.ids))
        self.assertEqual(read, (example["hit_tokens"], example["write_start"]))
        self.assertEqual(scores_for(cache, plan.node_for_event), example["scores_after_read"])
        _write_hyper_clock_suffix(cache, plan, read[1], len(plan.ids))
        self.assertEqual(scores_for(cache, plan.node_for_event), example["scores_after_write"])
        read = _read_hyper_clock_prefix(cache, plan, 0, len(plan.ids))
        self.assertEqual(read, (example["next_hit_tokens"], len(plan.ids)))
        _write_hyper_clock_suffix(cache, plan, read[1], len(plan.ids))
        self.assertEqual(scores_for(cache, plan.node_for_event), example["next_scores"])
        self.assertEqual(cache.hand, 0)

    def test_documented_replay_example(self):
        example = documented_example("replay-example")
        plan = build_execution_plan(make_trace(example["requests"]), example["warmup_fraction"])
        result = simulate_policy(plan, "hyper-clock", example["capacity"], hyper_clock_max_score=example["max_score"])
        self.assertEqual((result.hitTokens, result.totalTokens, result.hitRate),
                         (example["hit_tokens"], example["total_tokens"], example["hit_rate"]))

    def test_free_slots_reused_without_aging_or_moving_hand(self):
        cache = seed_scores([1, 2, 3, 1], hand=2)
        cache.discard(2)
        cache.discard(4)
        cache.discard(4)  # An absent deletion must not add the slot twice.
        self.assertEqual(cache.free_slots, [1, 3])
        cache.write(5)
        self.assertEqual(cache.entries[5], 3)
        self.assertEqual(cache.free_slots, [1])
        self.assertFalse(cache.lookup(999))
        self.assertEqual(cache.hand, 2)
        self.assertEqual(scores_for(cache, [1, 3, 5]), [1, 3, 1])
        self.assertEqual(len(cache.slots), 4)

    def test_sparse_wraparound_and_persistent_hand(self):
        cache = seed_scores([None, 1, None, 1], hand=2)
        self.assertEqual(cache.evict(), 4)
        self.assertEqual(cache.hand, 0)
        self.assertEqual(cache.evict(), 2)
        self.assertEqual(cache.hand, 2)
        with self.assertRaises(RuntimeError):
            cache.evict()
        cache.write(8)
        self.assertEqual(cache.entries[8], 1)
        self.assertEqual(cache.hand, 2)

    def test_duplicate_writes_saturation_and_wide_scores(self):
        for limit in (1, 3, 300, 2**64 - 1):
            cache = seed_scores([limit - 1], max_score=limit)
            cache.write(1)
            cache.lookup(1)
            self.assertEqual(scores_for(cache, [1]), [limit])
            self.assertEqual((len(cache), len(cache.slots), cache.hand), (1, 1, 0))

    def test_new_entries_start_at_one_in_fresh_and_reused_slots(self):
        for limit in (1, 3, 7, 300, 2**64 - 1):
            with self.subTest(max_score=limit):
                cache = _HyperClockCache(2, limit)
                cache.write(1)
                cache.write(2)
                self.assertEqual(scores_for(cache, [1, 2]), [1, 1])
                self.assertEqual(cache.hand, 0)
                cache.discard(1)
                cache.write(3)
                self.assertEqual(scores_for(cache, [3, 2]), [1, 1])
                cache.discard(3)
                cache.write(1)
                self.assertEqual(scores_for(cache, [1, 2]), [1, 1])
                # Eviction reuses its victim's slot with score one.
                cache.slots[cache.entries[1]].score = 0
                cache.write(4)
                self.assertNotIn(1, cache)
                self.assertEqual(scores_for(cache, [4, 2]), [1, 1])
                self.assertEqual(cache.hand, 1)

    def test_existing_write_increments_aged_score_without_resetting_it(self):
        cache = seed_scores([0], max_score=7)
        cache.write(1)
        self.assertEqual(scores_for(cache, [1]), [1])
        self.assertEqual(cache.hand, 0)

    def test_visit_count_and_dense_inspection_bound(self):
        class CountingSlots(list):
            inspections = 0

            def __getitem__(self, index):
                self.inspections += 1
                return super().__getitem__(index)

        for limit in (1, 3, 7):
            for count in (1, 2, 5):
                cache = seed_scores([limit] * count, max_score=limit)
                cache.slots = CountingSlots(cache.slots)
                self.assertEqual(cache.evict(), 1)
                self.assertEqual(cache.slots.inspections, limit * count + 1)
                self.assertEqual(scores_for(cache, range(2, count + 1)), [0] * (count - 1))

    def test_one_bit_matches_independent_second_chance_queue(self):
        rng = random.Random(1729)
        for capacity in (1, 2, 7):
            cache = _HyperClockCache(capacity, 1)
            queue = deque()
            bits = {}
            for _ in range(500):
                node = rng.randint(1, 15)
                if rng.random() < 0.4:
                    self.assertEqual(cache.lookup(node), node in bits)
                    if node in bits:
                        bits[node] = 1
                else:
                    if node in bits:
                        bits[node] = 1
                    else:
                        if len(bits) == capacity:
                            while bits[queue[0]]:
                                bits[queue[0]] = 0
                                queue.rotate(-1)
                            del bits[queue.popleft()]
                        queue.append(node)
                        bits[node] = 1
                    cache.write(node)
                self.assertEqual(set(cache.entries), set(bits))
                self.assertEqual({node: cache.slots[index].score for node, index in cache.entries.items()}, bits)

    def test_eviction_retains_descendants_and_suffix_uses_live_membership(self):
        plan = build_execution_plan(make_trace([["A", "B", "C"], ["X"]]))
        a, b, c, x = plan.node_for_event
        cache = _HyperClockCache(3)
        _write_hyper_clock_suffix(cache, plan, 0, 3)
        cache.write(x)
        self.assertNotIn(a, cache)
        self.assertIn(b, cache)
        self.assertIn(c, cache)
        self.assertEqual(_read_hyper_clock_prefix(cache, plan, 0, 3), (0, 0))
        self.assertEqual(scores_for(cache, [b, c]), [0, 0])
        # Writing A evicts B, writing B evicts C; both must be reinserted.
        _write_hyper_clock_suffix(cache, plan, 0, 3)
        self.assertEqual(set(cache.entries), {a, b, c})
        self.assertEqual(_read_hyper_clock_prefix(cache, plan, 0, 3), (3, 3))

    def test_suffix_can_evict_read_prefix_without_repair(self):
        plan = build_execution_plan(make_trace([["A", "B"], ["A", "B", "C", "D"]]))
        cache = _HyperClockCache(2)
        _write_hyper_clock_suffix(cache, plan, 0, 2)
        hits, start = _read_hyper_clock_prefix(cache, plan, 2, 6)
        self.assertEqual((hits, start), (2, 4))
        _write_hyper_clock_suffix(cache, plan, start, 6)
        self.assertEqual(set(cache.entries), set(plan.node_for_event[4:6]))
        result = simulate_policy(plan, "hyper-clock", 2)
        self.assertEqual((result.hitTokens, result.totalTokens), (2, 4))

    def test_prefix_identity_not_raw_id(self):
        plan = build_execution_plan(make_trace([["A", "C"], ["B", "C"]]))
        a, ac, b, bc = plan.node_for_event
        cache = _HyperClockCache(4)
        cache.write(a)
        cache.write(ac)
        cache.slots[cache.entries[ac]].score = 1
        cache.write(b)
        self.assertEqual(_read_hyper_clock_prefix(cache, plan, 2, 4), (1, 3))
        self.assertEqual(scores_for(cache, [ac]), [1])
        _write_hyper_clock_suffix(cache, plan, 3, 4)
        self.assertEqual(scores_for(cache, [ac, bc]), [1, 1])

    def test_boundaries_weighted_tokens_and_validation(self):
        trace = parse_trace_lines([
            json.dumps({"block_size": 8, "hash_ids": ["A", "B"], "input_length": 11, "block_tokens": [8, 3]}),
            json.dumps({"block_size": 8, "hash_ids": ["A", "C"], "input_length": 10, "block_tokens": [8, 2]}),
        ])
        plan = build_execution_plan(trace)
        result = simulate_policy(plan, "hyper-clock", 2)
        self.assertEqual((result.hitTokens, result.totalTokens, result.hitRate), (8, 10, 0.8))
        for capacity in (0, -1):
            self.assertEqual(simulate_policy(plan, "hyper-clock", capacity).hitTokens, 0)
        self.assertEqual(simulate_policy(plan, "hyper-clock", 3).measurementMode, "underfilled_at_window")
        empty = replace(trace, ids=[], tokens=[], request_starts=[0], request_count=0,
                        unique_raw_blocks=0, total_input_tokens=0, average_input_tokens=0)
        self.assertEqual(simulate_policy(build_execution_plan(empty), "hyper-clock", 3).totalTokens, 0)
        disabled = _HyperClockCache(0)
        disabled.write(1)
        self.assertEqual(len(disabled), 0)
        for value in (0, -1, True, False, 1.5, "3", None, 2**64):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    simulate_policy(plan, "hyper-clock", 0, hyper_clock_max_score=value)
                with self.assertRaises(ValueError):
                    run_sweep(trace, scheduler="sequential", model_id="qwen3-32b", hyper_clock_max_score=value)
                with self.assertRaises(ValueError):
                    run_cpp_sweep(trace, scheduler="sequential", capacities=[0], policies=["hyper-clock"], warmup_requests=1,
                                  hyper_clock_max_score=value)

    def test_worker_configuration_matches_serial_and_changes_outcome(self):
        rng = random.Random(21)
        trace = make_trace([[rng.choice("ABCD")] for _ in range(100)])
        plan = build_execution_plan(trace)
        tasks = [("hyper-clock", 2), ("hyper-clock", 3)]
        for limit in (1, 7):
            serial = _simulate_tasks(plan, tasks, 1, hyper_clock_max_score=limit)
            self.assertEqual(serial, _simulate_tasks(plan, tasks, 2, hyper_clock_max_score=limit))
            for policy, capacity in tasks:
                self.assertEqual(serial[(policy, capacity)], simulate_policy(plan, policy, capacity, hyper_clock_max_score=limit))
        self.assertNotEqual(_simulate_tasks(plan, tasks, 1, hyper_clock_max_score=1),
                            _simulate_tasks(plan, tasks, 1, hyper_clock_max_score=7))

    def test_cli_score_validation(self):
        import contextlib
        import io
        for command in ("run", "sweep"):
            for value in ("0", "-1", "1.5", "abc", str(2**64)):
                with self.subTest(command=command, value=value), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        build_parser().parse_args([command, "--scheduler", "sequential", "--trace", "unused", "--model", "qwen3-32b",
                                                   "--hyper-clock-max-score", value])
                    self.assertEqual(error.exception.code, 2)


@unittest.skipUnless(shutil.which("c++"), "C++ compiler is not available")
class NativeHyperClockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.harness = Path(cls.directory.name) / "clock-test"
        source = Path(cls.directory.name) / "clock-test.cc"
        # Exercise internal operations without a production seeding/debug CLI.
        source.write_text(r'''
#define main simulator_cli_main
#include SOURCE_PATH
#undef main
int main() {
  std::uint32_t capacity;
  std::uint64_t max_score;
  std::size_t count, hand, events;
  std::cin >> capacity >> max_score >> count >> hand >> events;
  HyperClockCache cache(capacity, max_score);
  for (std::size_t i = 0; i < count; ++i) {
    std::uint32_t node;
    std::uint64_t score;
    std::cin >> node >> score;
    cache.slots.push_back({node, score});
    if (node) cache.entries[node] = i;
    else cache.free_slots.push_back(i);
  }
  cache.hand = hand;
  SimulationInput input;
  for (std::size_t i = 0; i < events; ++i) {
    std::uint32_t node;
    std::uint16_t tokens;
    std::cin >> node >> tokens;
    input.prefix.node_for_event.push_back(node);
    input.tokens.push_back(tokens);
  }
  int operation;
  std::uint64_t a, b;
  while (std::cin >> operation) {
    std::cout << "{\"result\":";
    if (operation == 0) { std::cin >> a; cache.write(a); std::cout << "null"; }
    else if (operation == 1) { std::cin >> a; std::cout << cache.lookup(a); }
    else if (operation == 2) { std::cin >> a; cache.discard(a); std::cout << "null"; }
    else if (operation == 3) { std::cout << cache.evict(); }
    else if (operation == 4) {
      std::cin >> a >> b;
      auto read = read_hyper_clock_prefix(cache, input, a, b);
      std::cout << '[' << read.hit_tokens << ',' << read.write_start << ']';
    } else if (operation == 5) {
      std::cin >> a >> b; write_hyper_clock_suffix(cache, input, a, b); std::cout << "null";
    }
    std::cout << ",\"hand\":" << cache.hand << ",\"slots\":[";
    for (std::size_t i = 0; i < cache.slots.size(); ++i) {
      if (i) std::cout << ',';
      std::cout << '[' << cache.slots[i].node << ',' << cache.slots[i].score << ']';
    }
    std::cout << "]}\n";
  }
}
'''.replace("SOURCE_PATH", json.dumps(str(package_resource_path("kv-cache-lab-native-sim.cc")))), encoding="utf-8")
        subprocess.run([shutil.which("c++"), "-std=c++17", str(source), "-o", str(cls.harness)],
                       check=True, capture_output=True, text=True)

    def native_operations(self, cache, operations, plan=None):
        events = list(zip(plan.node_for_event, plan.tokens)) if plan else []
        lines = [f"{cache.capacity} {cache.max_score} {len(cache.slots)} {cache.hand} {len(events)}"]
        lines.extend(f"{slot.node or 0} {slot.score}" for slot in cache.slots)
        lines.extend(f"{node} {tokens}" for node, tokens in events)
        lines.extend(" ".join(map(str, operation)) for operation in operations)
        result = subprocess.run([str(self.harness)], input="\n".join(lines) + "\n", check=True,
                                capture_output=True, text=True, timeout=10)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_documented_victim_examples(self):
        for example in documented_example("victim-examples"):
            with self.subTest(example=example["name"]):
                cache = seed_scores(example["scores"], example["max_score"], example["hand"])
                operations = [(1, index + 1) for index in example["accesses"]]
                if "victim_slot" in example:
                    operations.append((3,))
                actual = self.native_operations(cache, operations)[-1]
                self.assertEqual([score if node else None for node, score in actual["slots"]], example["scores_after"])
                self.assertEqual(actual["hand"], example["hand_after"])
                if "victim_slot" in example:
                    self.assertEqual(actual["result"], example["victim_slot"] + 1)

    def test_documented_prefix_example(self):
        example = documented_example("prefix-example")
        plan = build_execution_plan(make_trace([example["request"]], example["block_size"]))
        cache = _HyperClockCache(example["capacity"], example["max_score"])
        for position in example["cached_positions"]:
            node = plan.node_for_event[position]
            cache.write(node)
        end = len(plan.ids)
        states = self.native_operations(cache, [(4, 0, end), (5, example["write_start"], end),
                                               (4, 0, end), (5, end, end)], plan)
        self.assertEqual(states[0]["result"], [example["hit_tokens"], example["write_start"]])
        self.assertEqual(states[2]["result"], [example["next_hit_tokens"], end])
        for state, expected in zip(states, [example["scores_after_read"], example["scores_after_write"],
                                            example["next_scores"], example["next_scores"]]):
            scores = {node: score for node, score in state["slots"] if node}
            self.assertEqual([scores.get(node) for node in plan.node_for_event], expected)

    def test_operation_parity_with_holes_wraparound_and_wide_scores(self):
        rng = random.Random(71)
        for limit in (1, 3, 300, 2**64 - 1):
            initial = seed_scores([limit, None, 0, 1], max_score=limit, hand=1)
            cache = seed_scores([limit, None, 0, 1], max_score=limit, hand=1)
            operations, expected = [], []
            # A zero-score resident ensures the first victim needs no huge sweep.
            operations.append((3,))
            expected.append(snapshot(cache, cache.evict()))
            if limit == 2**64 - 1:
                # Test saturation and overflow without attempting 2^64 sweeps.
                for operation in ((1, 1), (0, 1), (2, 1), (0, 8), (2, 4), (0, 9)):
                    operations.append(operation)
                    method = (cache.write, cache.lookup, cache.discard)[operation[0]]
                    expected.append(snapshot(cache, method(operation[1])))
            else:
                for _ in range(300):
                    op = rng.randrange(4) if len(cache) else 0
                    operation = (op,) if op == 3 else (op, rng.randint(1, 12))
                    operations.append(operation)
                    if op == 3:
                        result = cache.evict()
                    else:
                        result = (cache.write, cache.lookup, cache.discard)[op](operation[1])
                    expected.append(snapshot(cache, result))
            self.assertEqual(self.native_operations(initial, operations), expected)

    def test_native_insertions_and_reinsertions_start_at_one(self):
        for limit in (1, 3, 7, 300, 2**64 - 1):
            with self.subTest(max_score=limit):
                states = self.native_operations(_HyperClockCache(2, limit),
                                                [(0, 1), (0, 2), (2, 1), (0, 3), (2, 3), (0, 1)])
                self.assertEqual(states[0]["slots"], [[1, 1]])
                self.assertEqual(states[1]["slots"], [[1, 1], [2, 1]])
                self.assertEqual(states[3]["slots"], [[3, 1], [2, 1]])
                self.assertEqual(states[5]["slots"], [[1, 1], [2, 1]])
                self.assertTrue(all(state["hand"] == 0 for state in states))
                state = self.native_operations(seed_scores([0, limit], max_score=limit), [(0, 3)])[0]
                self.assertEqual(state["slots"], [[3, 1], [2, limit]])
                self.assertEqual(state["hand"], 1)
        state = self.native_operations(seed_scores([0], max_score=7), [(0, 1)])[0]
        self.assertEqual(state["slots"], [[1, 1]])

    def test_cpp_python_replay_parity_including_documented_trace(self):
        example = documented_example("replay-example")
        rng = random.Random(83)
        traces = [make_trace(example["requests"]), make_trace([["A", "B"], ["A", "B", "C", "D"]]),
                  make_trace([["A"], ["A"]])]
        traces.extend(parse_trace_lines([
            json.dumps({"block_size": 8, "hash_ids": [rng.choice("ABCD") for _ in range(length)],
                        "input_length": length * 8 - 3})
            for length in [rng.randint(1, 6) for _ in range(30)]
        ]) for _ in range(5))
        for trace in traces:
            for fraction in (0, 0.5, 1):
                plan = build_execution_plan(trace, fraction)
                for limit in (1, 3, 7):
                    native = run_cpp_sweep(trace, scheduler="sequential", capacities=[0, 1, 2, 3, 4, 100], policies=["hyper-clock"],
                                           warmup_requests=plan.warmup_requests, hyper_clock_max_score=limit)
                    for capacity in (0, 1, 2, 3, 4, 100):
                        with self.subTest(events=len(trace.ids), warmup=fraction, limit=limit, capacity=capacity):
                            self.assertEqual(native.results[("hyper-clock", capacity)],
                                             simulate_policy(plan, "hyper-clock", capacity, hyper_clock_max_score=limit))
                    if trace is traces[0] and fraction == example["warmup_fraction"] and limit == example["max_score"]:
                        result = native.results[("hyper-clock", example["capacity"])]
                        self.assertEqual((result.hitTokens, result.totalTokens, result.hitRate),
                                         (example["hit_tokens"], example["total_tokens"], example["hit_rate"]))

    def test_native_modes_and_invalid_scores(self):
        example = documented_example("replay-example")
        trace = make_trace(example["requests"])
        binary = ensure_cpp_simulator()
        with tempfile.TemporaryDirectory() as directory:
            files = _write_binary_trace(trace, Path(directory))
            base = [str(binary), "--scheduler", "sequential", "--ids", str(files.ids), "--tokens", str(files.tokens),
                    "--request-ends", str(files.request_ends), "--request-count", str(trace.request_count),
                    "--total-blocks", str(len(trace.ids)), "--warmup-requests", "2", "--capacity", "3"]
            for mode in ("hyper-clock", "all", "batch"):
                args = base + ["--policy", mode, "--hyper-clock-max-score", "7"]
                if mode == "batch":
                    args += ["--capacities", "3", "--policies", "hyper-clock"]
                result = json.loads(subprocess.run(args, check=True, capture_output=True, text=True).stdout)
                if mode == "batch":
                    self.assertEqual(set(result["points"][0]), {"cacheBlocks", "hyper-clock"})
                    result = result["points"][0]["hyper-clock"]
                elif mode == "all":
                    result = result["hyper-clock"]
                self.assertEqual((result["hitTokens"], result["totalTokens"]), (3, 6))
            for value in ("0", "-1", "1.5", "abc", str(2**64)):
                result = subprocess.run(base + ["--policy", "hyper-clock", "--hyper-clock-max-score", value],
                                        capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("positive integer", result.stderr)

    def test_cli_backends_alias_metadata_and_nondefault_score(self):
        rng = random.Random(21)
        paths = [[rng.choice("ABCD")] for _ in range(100)]
        with tempfile.TemporaryDirectory() as directory:
            trace_file = Path(directory) / "trace.jsonl"
            trace_file.write_text("\n".join(json.dumps({"block_size": 1, "hash_ids": path, "input_length": 1})
                                             for path in paths), encoding="utf-8")
            base = ["--scheduler", "sequential", "--trace", str(trace_file), "--model", "qwen3-32b", "--policies", "hyper-clock",
                    "--budgets-gib", "0.0005", "--hyper-clock-max-score", "7", "--no-progress"]
            results = []
            for backend in ("python", "cpp"):
                for command in ("run", "sweep"):
                    result = subprocess.run([sys.executable, "-m", "kvcache_sim", command, *base,
                                             "--backend", backend, "--format", "json"],
                                            check=True, capture_output=True, text=True, cwd=PACKAGE_ROOT)
                    payload = json.loads(result.stdout)
                    self.assertEqual(payload["metadata"]["policyParameters"], {"hyper-clock": {"maxScore": 7}})
                    self.assertEqual(payload["policies"], ["hyper-clock"])
                    self.assertEqual(payload["points"][0]["cacheBlocks"], 2)
                    results.append(payload["points"])
            self.assertTrue(all(result == results[0] for result in results))
            table = subprocess.run([sys.executable, "-m", "kvcache_sim", "run", *base, "--backend", "python"],
                                   check=True, capture_output=True, text=True, cwd=PACKAGE_ROOT).stdout
            self.assertIn("Hyper Clock max score: 7", table)
            self.assertIn("HYPER-CLOCK hit", table)
            self.assertIn("HYPER-CLOCK speedup", table)


if __name__ == "__main__":
    unittest.main()
