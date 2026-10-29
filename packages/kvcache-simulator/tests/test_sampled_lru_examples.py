"""Worked examples linked from docs/SAMPLED_LRU.md; times below are seconds."""
from __future__ import annotations

import json
import shutil
import unittest
from unittest.mock import patch

from kvcache_sim.cpp_backend import run_cpp_sweep
from kvcache_sim.plan import build_execution_plan
from kvcache_sim.sampled import SampledCache, simulate_sampled
from kvcache_sim.trace import parse_trace_lines, seconds_us


def make_cache(capacity, sample_size):
    return SampledCache(capacity, seconds_us(10), sample_size, seed=0)


def slot_state(cache):
    return [None if slot is None else (slot.node, slot.timestamp) for slot in cache.slots]


def make_trace(requests):
    return parse_trace_lines([
        json.dumps(dict(timestamp=start, completion_timestamp=finish,
                        conversation_id=str(index), block_size=64,
                        hash_ids=blocks, input_length=len(blocks) * 64))
        for index, (start, finish, blocks) in enumerate(requests)
    ], timing_mode="required")


class DocumentedExamplesTests(unittest.TestCase):
    def test_batch_eviction_and_free_slot_reuse(self):
        # A=1, B=2, C=3, D=4, E=5, F=6; slots are zero-based.
        cache = make_cache(capacity=4, sample_size=3)
        for node, time in [(1, 0), (2, 4), (3, 10), (4, 18)]:
            cache.write(node, seconds_us(time))
        self.assertEqual([(seconds_us(20) - slot.timestamp) // 1_000_000 for slot in cache.slots],
                         [20, 16, 10, 2])
        with patch.object(cache.rng, "sample", return_value=[0, 1, 2]) as sample:
            cache.write(5, seconds_us(20))
            sample.assert_called_once_with(range(4), 3)
        # C's age is exactly ten seconds, so the strict threshold protects it.
        self.assertEqual(slot_state(cache), [None, (5, seconds_us(20)),
                                             (3, seconds_us(10)), (4, seconds_us(18))])
        self.assertEqual(cache.free, [0])
        with patch.object(cache.rng, "sample", side_effect=AssertionError("free slot available")):
            cache.write(6, seconds_us(21))
        self.assertEqual(slot_state(cache), [(6, seconds_us(21)), (5, seconds_us(20)),
                                             (3, seconds_us(10)), (4, seconds_us(18))])
        self.assertEqual(cache.free, [])

    def test_sample_miss_falls_back_to_first_eligible_slot(self):
        # A=1, B=2, C=3, D=4. Sampling C cannot find the eligible A.
        cache = make_cache(capacity=3, sample_size=1)
        for node, time in [(1, 0), (2, 15), (3, 19)]:
            cache.write(node, seconds_us(time))
        with patch.object(cache.rng, "sample", return_value=[2]) as sample, \
                patch.object(cache, "discard", wraps=cache.discard) as discard:
            cache.write(4, seconds_us(20))
            sample.assert_called_once_with(range(3), 1)
            discard.assert_called_once_with(0)
        self.assertEqual(slot_state(cache), [(4, seconds_us(20)), (2, seconds_us(15)),
                                             (3, seconds_us(19))])
        self.assertEqual(cache.threshold, seconds_us(10))

    def test_threshold_halving_resets_and_reaches_zero(self):
        # Count fallback passes to verify the documented 10 -> 5 -> 2.5 -> 1.25.
        class ScannedSlots(list):
            scans = 0

            def __iter__(self):
                self.scans += 1
                return super().__iter__()

        cache = make_cache(capacity=2, sample_size=2)
        cache.write(1, seconds_us(8))
        cache.write(2, seconds_us(8))
        cache.slots = ScannedSlots(cache.slots)
        cache.write(3, seconds_us(10))
        self.assertEqual(cache.slots.scans, 4)
        self.assertEqual(slot_state(cache), [(3, seconds_us(10)), (2, seconds_us(8))])
        # Start the next operation at ten again; one scan at 1.25 would be wrong.
        cache.slots.scans = 0
        cache.write(4, seconds_us(10))
        self.assertEqual(cache.slots.scans, 4)
        self.assertEqual(slot_state(cache), [(3, seconds_us(10)), (4, seconds_us(10))])
        cache.write(5, seconds_us(10))
        self.assertEqual(slot_state(cache), [(5, seconds_us(10)), (4, seconds_us(10))])
        self.assertEqual(cache.threshold, seconds_us(10))

    def test_prefix_hole_and_completion_refresh(self):
        data = make_trace([(10, 12, ["A", "B", "C"]), (13, 14, ["A", "B", "C"])])
        plan = build_execution_plan(data)
        a, b, c = plan.node_for_event[:3]
        cache = make_cache(capacity=3, sample_size=3)
        cache.write(a, 0)
        cache.write(c, 0)
        reads, writes = [], []
        lookup, write = cache.lookup, cache.write

        def observe_lookup(node, now):
            hit = lookup(node, now)
            if now in (seconds_us(10), seconds_us(13)):
                reads.append((node, now, hit, slot_state(cache)))
            return hit

        def observe_write(node, now):
            write(node, now)
            writes.append((node, now, slot_state(cache)))

        with patch("kvcache_sim.sampled.SampledCache", return_value=cache), \
                patch.object(cache, "lookup", side_effect=observe_lookup), \
                patch.object(cache, "write", side_effect=observe_write):
            result = simulate_sampled(plan, 3)
        self.assertEqual([(n, hit) for n, t, hit, _ in reads if t == seconds_us(10)],
                         [(a, True), (b, False)])
        self.assertEqual(reads[1][3], [(a, seconds_us(10)), (c, 0)])
        self.assertEqual([(n, t) for n, t, _ in writes],
                         [(n, seconds_us(t)) for t in (12, 14) for n in (a, b, c)])
        self.assertEqual(writes[2][2], [(a, seconds_us(12)), (c, seconds_us(12)),
                                        (b, seconds_us(12))])
        self.assertEqual([hit for _, t, hit, _ in reads if t == seconds_us(13)], [True]*3)
        self.assertEqual(writes[-1][2], [(a, seconds_us(14)), (c, seconds_us(14)),
                                        (b, seconds_us(14))])
        self.assertEqual((result.hitTokens, result.totalTokens), (192, 192))
        # The first request is warmup, so its 64-token prefix hit is unmeasured.
        self.assertEqual(sum(hit for _, t, hit, _ in reads if t == seconds_us(10)) * 64, 64)
        branches = build_execution_plan(make_trace([
            (0, 1, ["A", "B", "C"]), (2, 3, ["X", "B", "C"]),
        ]))
        self.assertNotEqual(branches.node_for_event[1], branches.node_for_event[4])
        self.assertNotEqual(branches.node_for_event[2], branches.node_for_event[5])

    def test_clock_overlap_and_completion_before_arrival(self):
        data = make_trace([
            (0, 1, ["W"]), (0, 5, ["A"]), (2, 6, ["A"]), (5, 7, ["A"]),
        ])
        plan = build_execution_plan(data)
        w, a = plan.node_for_event[:2]
        cache = make_cache(capacity=1, sample_size=1)
        reads, writes = [], []
        lookup, write = cache.lookup, cache.write
        writing = False

        def observe_lookup(node, now):
            hit = lookup(node, now)
            if not writing:
                reads.append((node, now, hit))
            return hit

        def observe_write(node, now):
            nonlocal writing
            writing = True
            write(node, now)
            writing = False
            writes.append((node, now))

        with patch("kvcache_sim.sampled.SampledCache", return_value=cache), \
                patch.object(cache, "lookup", side_effect=observe_lookup), \
                patch.object(cache, "write", side_effect=observe_write):
            result = simulate_sampled(plan, 1, sample_size=1)
        self.assertEqual(reads, [(w, 0, False), (a, 0, False),
                                 (a, seconds_us(2), False), (a, seconds_us(5), True)])
        self.assertEqual(writes, [(w, seconds_us(1)), (a, seconds_us(5)),
                                  (a, seconds_us(6)), (a, seconds_us(7))])
        self.assertEqual(slot_state(cache), [(a, seconds_us(7))])
        self.assertEqual((result.hitTokens, result.totalTokens, result.hitRate), (64, 128, 0.5))
        if shutil.which("c++"):
            native = run_cpp_sweep(data, capacities=[1], policies=["sampled-lru"], warmup_requests=2,
                                   sampled_lru_sample_size=1)
            self.assertEqual(native.results["sampled-lru", 1], result)


if __name__ == "__main__":
    unittest.main()
