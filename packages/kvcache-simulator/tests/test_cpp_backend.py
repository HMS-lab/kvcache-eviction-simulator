from __future__ import annotations

from array import array
import io
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import weakref

from kvcache_sim.cpp_backend import _write_binary_trace, ensure_cpp_simulator, run_cpp_sweep
from kvcache_sim.plan import build_execution_plan
from kvcache_sim.policies import simulate_policy
from kvcache_sim.progress import ProgressBar
from kvcache_sim.simulator import DEFAULT_POLICIES, run_sweep
from kvcache_sim.trace import parse_trace_lines


def make_trace():
    # More events than the replacement string that previously became total-blocks.
    return parse_trace_lines([
        json.dumps({"block_size": 1, "hash_ids": list(range(20)), "input_length": 20})
        for _ in range(4)
    ])


class BinaryTraceTests(unittest.TestCase):
    def test_progress_preserves_full_label_and_clears_previous_line(self):
        label = "hyper-clock (policy 3/4 cache capacity 12/20, 123456 blocks)"
        output = io.StringIO()
        with patch("sys.stderr", output):
            bar = ProgressBar(enabled=True)
            bar.update(1, 2, label)
            bar.finish()
        active, finished = output.getvalue().split("\r")[1:]
        self.assertIn(label, active)
        self.assertEqual(len(active), len(finished.rstrip("\n")))
        self.assertTrue(finished.rstrip().endswith("done"))

    def test_writing_binary_files_preserves_input(self):
        trace = make_trace()
        ids, tokens, starts = trace.ids, trace.tokens, trace.request_starts
        with tempfile.TemporaryDirectory() as directory:
            files = _write_binary_trace(trace, Path(directory))
            self.assertEqual(files.ids.stat().st_size, len(ids) * 4)
            self.assertEqual(files.tokens.stat().st_size, len(tokens) * 2)
            self.assertEqual(files.request_ends.stat().st_size, trace.request_count * 4)
        self.assertIs(trace.ids, ids)
        self.assertIs(trace.tokens, tokens)
        self.assertIs(trace.request_starts, starts)


@unittest.skipUnless(shutil.which("c++"), "C++ compiler is not available")
class NativeTraceHandoffTests(unittest.TestCase):
    def test_progress_identifies_each_policy_and_capacity_before_evaluation(self):
        updates = []
        result = run_cpp_sweep(
            make_trace(), scheduler="sequential", capacities=[4, 8, 4, -1],
            policies=["hyper-clock", "sampled-lru"], warmup_requests=2,
            progress=lambda done, total, label: updates.append((done, total, label)),
        )
        evaluations = [update for update in updates if " (policy " in update[2]]
        expected_labels = [
            f"{policy} (policy {policy_index}/2 cache capacity {capacity_index}/2, {capacity} blocks)"
            for capacity_index, capacity in enumerate([4, 8], 1)
            for policy_index, policy in enumerate(["sampled-lru", "hyper-clock"], 1)
        ]
        self.assertEqual([label for _, _, label in evaluations],
                         [label for label in expected_labels for _ in range(2)])
        # One trace-writing step and one ceiling step precede the four evaluations.
        self.assertEqual([(done, total) for done, total, _ in evaluations],
                         [(done, 6) for start in range(2, 6) for done in (start, start + 1)])
        self.assertEqual(updates[-1], (6, 6, "simulation complete"))
        self.assertEqual(len(result.results), 4)

    def test_lists_freed_before_process_starts_and_original_count_is_passed(self):
        class TrackedList(list):
            pass

        trace = make_trace()
        event_count = len(trace.ids)
        expected = simulate_policy(build_execution_plan(trace), "hyper-clock", 4)
        trace.ids = TrackedList(trace.ids)
        trace.tokens = TrackedList(trace.tokens)
        trace.request_starts = TrackedList(trace.request_starts)
        references = [weakref.ref(trace.ids), weakref.ref(trace.tokens), weakref.ref(trace.request_starts)]
        # Compile before patching Popen, which subprocess.run also uses.
        ensure_cpp_simulator()
        original_popen = subprocess.Popen

        def checked_popen(command, **kwargs):
            self.assertEqual((trace.ids, trace.tokens, trace.request_starts), ([], [], []))
            self.assertTrue(all(reference() is None for reference in references))
            self.assertEqual(command[command.index("--total-blocks") + 1], str(event_count))
            return original_popen(command, **kwargs)

        with patch("kvcache_sim.cpp_backend.subprocess.Popen", side_effect=checked_popen) as launch:
            result = run_cpp_sweep(trace, scheduler="sequential", capacities=[4], policies=["hyper-clock"],
                                   warmup_requests=2, release_trace_data=True)
        launch.assert_called_once()
        self.assertEqual(result.results[("hyper-clock", 4)], expected)
        self.assertEqual(trace.request_count, 4)

    def test_release_preserves_sweep_results_and_default_allows_reuse(self):
        trace = make_trace()
        kwargs = dict(model_id="qwen3-32b", precision="bf16_fp16", budgets_gib=[0.00025, 0.0005],
                      backend="cpp", policies=[p for p in DEFAULT_POLICIES if p != "sampled-lru"])
        retained = run_sweep(trace, scheduler="sequential", **kwargs)
        self.assertEqual(len(trace.ids), 80)
        consumed = run_sweep(trace, scheduler="sequential", **kwargs, release_trace_data=True)
        self.assertEqual(consumed, retained)
        self.assertEqual(consumed["metadata"]["eventCount"], 80)
        self.assertEqual((trace.ids, trace.tokens, trace.request_starts), ([], [], []))

    def test_native_rejects_inconsistent_counts_and_request_boundaries(self):
        trace = make_trace()
        binary = ensure_cpp_simulator()
        with tempfile.TemporaryDirectory() as directory:
            files = _write_binary_trace(trace, Path(directory))
            base = [str(binary), "--scheduler", "sequential", "--policy", "hyper-clock", "--ids", str(files.ids),
                    "--tokens", str(files.tokens), "--request-ends", str(files.request_ends),
                    "--request-count", "4", "--capacity", "4"]
            cases = [
                (27, [20, 40, 60, 80], "Invalid request end offset"),
                (80, [20, 19, 60, 80], "Invalid request end offset"),
                (80, [20, 40, 60, 79], "Final request end offset"),
            ]
            for event_count, ends, message in cases:
                with self.subTest(event_count=event_count, ends=ends):
                    with files.request_ends.open("wb") as handle:
                        array("I", ends).tofile(handle)
                    result = subprocess.run(base + ["--total-blocks", str(event_count)],
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(message, result.stderr)


if __name__ == "__main__":
    unittest.main()
