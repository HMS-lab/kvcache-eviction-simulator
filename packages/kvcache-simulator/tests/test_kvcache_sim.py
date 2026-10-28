from __future__ import annotations

from dataclasses import replace
import gzip
import json
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kvcache_sim.calculator import calculate_cache_size, load_models_data, models_by_id
from kvcache_sim.cpp_backend import _build_path_for_source, _write_binary_trace, ensure_cpp_simulator, run_cpp_sweep
from kvcache_sim.plan import build_execution_plan
from kvcache_sim.policies import _FlatLruCache, _read_flat_lru_prefix, _write_flat_lru_request, simulate_policy
from kvcache_sim._resources import package_resource_path, user_temp_suffix
from kvcache_sim.simulator import run_sweep
from kvcache_sim.trace import parse_trace_file, parse_trace_lines


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]


def make_trace(*hash_paths: list[str], block_size: int = 1):
    lines = [
        json.dumps({
            "block_size": block_size,
            "hash_ids": path,
            "input_length": len(path) * block_size,
        })
        for path in hash_paths
    ]
    return parse_trace_lines(lines)


class CalculatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.models_data = load_models_data()
        cls.models = models_by_id(cls.models_data)

    def test_standard_gqa_matches_web_calculator_constant(self) -> None:
        result = calculate_cache_size(
            self.models["qwen3-32b"],
            tokens=128000,
            precision="bf16_fp16",
            models_data=self.models_data,
        )

        self.assertEqual(result.bytes_per_token, 262144)
        self.assertAlmostEqual(result.total_gib, 31.25)

    def test_glm_5_2_indexer_precision_matches_web_calculator_constant(self) -> None:
        result = calculate_cache_size(
            self.models["glm-5.2"],
            tokens=128000,
            precision="fp8_int8",
            indexer_precision="fp4_int4",
            models_data=self.models_data,
        )

        self.assertEqual(result.indexer_precision, "fp4_int4")
        self.assertEqual(result.bytes_per_token, 46272)
        self.assertAlmostEqual(result.total_gib, 5.51605224609375)

    def test_minimax_m3_keeps_fixed_bf16_indexer_precision(self) -> None:
        result = calculate_cache_size(
            self.models["minimax-m3"],
            tokens=1048576,
            precision="fp8_int8",
            indexer_precision="fp4_int4",
            models_data=self.models_data,
        )

        self.assertEqual(result.indexer_precision, "bf16_fp16")
        self.assertEqual(result.indexer_precision_label, "BF16 / FP16")
        self.assertAlmostEqual(result.total_gib, 74.25)

    def test_kimi_k3_uses_mla_and_kda_state_layout(self) -> None:
        result = calculate_cache_size(
            self.models["kimi-k3"],
            tokens=1048576,
            precision="fp8_int8",
            include_linear_attention_state=True,
            models_data=self.models_data,
        )

        self.assertEqual(result.hit_rate_bytes_per_token, 13824)
        self.assertAlmostEqual(result.total_gib, 13.918510437011719)

    def test_kimi_k3_recurrent_state_precision_follows_the_selector(self) -> None:
        kwargs = {"tokens": 1048576, "precision": "fp8_int8", "include_linear_attention_state": True, "models_data": self.models_data}
        fp32 = calculate_cache_size(self.models["kimi-k3"], **kwargs)
        bf16 = calculate_cache_size(self.models["kimi-k3"], recurrent_state_precision="bf16_fp16", **kwargs)
        mla = 1048576 * 13824

        self.assertEqual(fp32.recurrent_state_precision, "fp32")
        self.assertEqual(bf16.recurrent_state_precision_label, "BF16 / FP16")
        self.assertEqual(fp32.total_bytes - mla, 449372160)
        self.assertEqual(bf16.total_bytes - mla, 232316928)
        with self.assertRaises(ValueError):
            calculate_cache_size(self.models["kimi-k3"], recurrent_state_precision="fp4", **kwargs)

    def test_qwen_recurrent_state_precision_matches_the_web_calculator(self) -> None:
        kwargs = {"tokens": 10241, "precision": "fp8_int8", "include_linear_attention_state": True, "models_data": self.models_data}
        kv = 10241 * 16 * 2 * 4 * 256   # 16 full-attention layers, 4 KV heads of 256
        for precision, checkpoint_mib in (("bf16_fp16", 74.8125), ("fp32", 146.8125)):
            result = calculate_cache_size(self.models["qwen3.8-27b"], recurrent_state_precision=precision, **kwargs)
            self.assertEqual(result.recurrent_state_precision, precision)
            self.assertAlmostEqual(result.total_bytes - kv, checkpoint_mib * 1024 ** 2)

    def test_models_without_linear_state_report_no_recurrent_precision(self) -> None:
        result = calculate_cache_size(self.models["kimi-k2.6"], tokens=4096, models_data=self.models_data)
        self.assertIsNone(result.recurrent_state_precision)

    def test_kimi_k3_mla_cache_uses_exact_logical_token_count(self) -> None:
        result = calculate_cache_size(
            self.models["kimi-k3"],
            tokens=1,
            precision="fp8_int8",
            include_linear_attention_state=False,
            models_data=self.models_data,
        )

        self.assertEqual(result.total_bytes, 13824)

    def test_bundled_models_match_web_calculator_catalog_when_in_repo(self) -> None:
        web_catalog = REPO_ROOT / "data" / "kv_cache_calculator" / "models.yaml"
        bundled_catalog = PACKAGE_ROOT / "src" / "kvcache_sim" / "resources" / "models.yaml"
        if not web_catalog.exists():
            self.skipTest("web calculator catalog is not present")

        self.assertEqual(bundled_catalog.read_text(encoding="utf-8"), web_catalog.read_text(encoding="utf-8"))


class TraceParserTests(unittest.TestCase):
    def test_jsonl_trace_parses_required_fields(self) -> None:
        trace = make_trace(["A", "B"], ["A", "C"], block_size=64)

        self.assertEqual(trace.request_count, 2)
        self.assertEqual(trace.block_size, 64)
        self.assertEqual(trace.total_input_tokens, 256)
        self.assertEqual(trace.unique_raw_blocks, 3)

    def test_missing_block_size_needs_cli_fallback(self) -> None:
        line = json.dumps({"hash_ids": ["A"], "input_length": 1})

        with self.assertRaisesRegex(ValueError, "without block_size need --block-size"):
            parse_trace_lines([line])

        trace = parse_trace_lines([line], block_size=64)
        self.assertEqual(trace.block_size, 64)

    def test_trace_block_size_overrides_cli_fallback(self) -> None:
        lines = [
            json.dumps({"hash_ids": ["A", "B"], "input_length": 32}),
            json.dumps({"block_size": 16, "hash_ids": ["A", "B"], "input_length": 32}),
        ]

        trace = parse_trace_lines(lines, block_size=64)

        self.assertEqual(trace.block_size, 16)
        self.assertEqual(trace.tokens, [16, 16, 16, 16])

    def test_explicit_block_tokens_preserve_per_block_weights(self) -> None:
        line = json.dumps({
            "block_size": 64,
            "hash_ids": ["A", "B", "C"],
            "input_length": 130,
            "block_tokens": [64, 32, 34],
        })

        trace = parse_trace_lines([line])

        self.assertEqual(trace.tokens, [64, 32, 34])
        self.assertEqual(trace.total_input_tokens, 130)

    def test_gzip_jsonl_trace_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "trace.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps({"block_size": 1, "hash_ids": ["A"], "input_length": 1}) + "\n")

            trace = parse_trace_file(path)

        self.assertEqual(trace.request_count, 1)
        self.assertEqual(trace.ids, [0])


class PrefixPolicyTests(unittest.TestCase):
    def test_context_aware_identity_does_not_reuse_same_raw_block_under_different_parent(self) -> None:
        trace = make_trace(["A", "B"], ["C", "B"])
        plan = build_execution_plan(trace)

        self.assertEqual(trace.unique_raw_blocks, 3)
        self.assertEqual(plan.unique_blocks, 4)
        self.assertEqual(len(set(plan.node_for_event)), 4)

    def test_middle_miss_stops_prefix_hit_but_later_blocks_still_enter_cache(self) -> None:
        trace = make_trace(["A", "B"], ["A", "C", "B"])
        plan = build_execution_plan(trace)

        lru = simulate_policy(plan, "lru", 2)
        fifo = simulate_policy(plan, "fifo", 2)

        self.assertEqual(lru.hitTokens, 1)
        self.assertEqual(lru.totalTokens, 3)
        self.assertEqual(lru.hitRate, 1 / 3)
        self.assertEqual(fifo.hitTokens, 1)
        self.assertEqual(fifo.totalTokens, 3)

    def test_repeated_prefix_can_hit_after_warmup(self) -> None:
        trace = make_trace(["A", "B"], ["A", "B"], ["A", "B"])
        plan = build_execution_plan(trace)

        result = simulate_policy(plan, "lru", 2)

        self.assertEqual(result.hitTokens, 4)
        self.assertEqual(result.totalTokens, 4)
        self.assertEqual(result.hitRate, 1)

    def test_optimal_bypasses_polluting_leaf(self) -> None:
        trace = make_trace(["A"], ["B"], ["A"])
        plan = build_execution_plan(trace)

        result = simulate_policy(plan, "optimal", 1)

        self.assertEqual(result.hitTokens, 1)
        self.assertEqual(result.totalTokens, 2)
        self.assertEqual(result.hitRate, 0.5)

    def test_underfilled_capacity_is_reported_when_not_full_before_measurement(self) -> None:
        trace = make_trace(["A"], ["A"], ["A"], ["A"])
        plan = build_execution_plan(trace)

        result = simulate_policy(plan, "lru", 2)

        self.assertEqual(result.measurementMode, "underfilled_at_window")


class FlatLruTests(unittest.TestCase):
    def test_read_refresh_changes_the_fifo_victim(self) -> None:
        plan = build_execution_plan(make_trace(["A"], ["B"], ["A"], ["C"], ["A"]))
        self.assertEqual(simulate_policy(plan, "lru-flat", 2).hitRate, 2 / 3)
        self.assertEqual(simulate_policy(plan, "fifo", 2).hitRate, 1 / 3)

    def test_internal_node_eviction_leaves_an_unusable_tail(self) -> None:
        plan = build_execution_plan(make_trace(["A", "B"], ["X"], ["A", "B"], ["A", "B"]))
        self.assertEqual(simulate_policy(plan, "lru-flat", 2).hitTokens, 2)
        self.assertEqual(simulate_policy(plan, "lru", 2).hitTokens, 3)

    def test_same_raw_block_under_another_prefix_is_not_refreshed(self) -> None:
        plan = build_execution_plan(make_trace(["X"], ["D", "C"], ["A", "B", "C"]))
        x, _d, other_c, a, b, c = plan.node_for_event
        self.assertNotEqual(other_c, c)
        cache = _FlatLruCache(4)
        for node in (x, other_c, a):
            cache.write(node)

        self.assertEqual(_read_flat_lru_prefix(cache, plan, 3, 6), 1)
        self.assertEqual(list(cache.entries), [x, other_c, a])
        _write_flat_lru_request(cache, plan, 3, 6)
        self.assertEqual(list(cache.entries), [other_c, a, b, c])
        # Rewriting the same exact nodes refreshes rather than duplicates them.
        _write_flat_lru_request(cache, plan, 3, 6)
        self.assertEqual(list(cache.entries), [other_c, a, b, c])
        cache.write(x)
        self.assertNotIn(other_c, cache)
        self.assertIn(c, cache)

    def test_existing_suffix_is_refreshed_only_when_written(self) -> None:
        plan = build_execution_plan(make_trace(["A", "B", "C"], ["X"]))
        a, b, c, x = plan.node_for_event
        cache = _FlatLruCache(3)
        for node in (c, x, a):
            cache.write(node)
        self.assertEqual(_read_flat_lru_prefix(cache, plan, 0, 3), 1)
        self.assertEqual(list(cache.entries), [c, x, a])
        # This is a write to the exact orphaned suffix node, not another raw C.
        cache.write(c)
        self.assertEqual(list(cache.entries), [x, a, c])
        cache.write(b)
        self.assertEqual(list(cache.entries), [a, c, b])

    def test_writer_restores_nodes_evicted_after_the_read(self) -> None:
        plan = build_execution_plan(make_trace(["A", "B"], ["X"]))
        a, b, x = plan.node_for_event
        cache = _FlatLruCache(2)
        _write_flat_lru_request(cache, plan, 0, 2)
        self.assertEqual(_read_flat_lru_prefix(cache, plan, 0, 2), 2)
        cache.write(x)
        self.assertNotIn(a, cache)
        self.assertIn(b, cache)  # Evicting the parent did not discard its tail.
        self.assertEqual(_read_flat_lru_prefix(cache, plan, 0, 2), 0)
        _write_flat_lru_request(cache, plan, 0, 2)
        self.assertEqual(list(cache.entries), [a, b])

    def test_tail_write_can_evict_the_head_in_one_request(self) -> None:
        plan = build_execution_plan(make_trace(["A", "B"], ["A", "B", "C"]))
        a, b, c = plan.node_for_event[2:]
        cache = _FlatLruCache(2)
        _write_flat_lru_request(cache, plan, 0, 2)
        self.assertEqual(_read_flat_lru_prefix(cache, plan, 2, 5), 2)
        _write_flat_lru_request(cache, plan, 2, 5)
        self.assertEqual(list(cache.entries), [b, c])
        self.assertNotIn(a, cache)
        result = simulate_policy(plan, "lru-flat", 2)
        self.assertEqual((result.hitTokens, result.totalTokens), (2, 3))

    def test_boundaries_and_weighted_prefix_hits(self) -> None:
        weighted = parse_trace_lines([
            json.dumps({"block_size": 8, "hash_ids": ["A", "B"], "input_length": 11, "block_tokens": [8, 3]}),
            json.dumps({"block_size": 8, "hash_ids": ["A", "C"], "input_length": 10, "block_tokens": [8, 2]}),
        ])
        plan = build_execution_plan(weighted)
        result = simulate_policy(plan, "lru-flat", 2)
        self.assertEqual((result.hitTokens, result.totalTokens, result.hitRate), (8, 10, 0.8))
        self.assertEqual(simulate_policy(plan, "lru-flat", 0).hitTokens, 0)
        self.assertEqual(simulate_policy(plan, "lru-flat", 3).measurementMode, "underfilled_at_window")
        self.assertEqual(simulate_policy(build_execution_plan(make_trace(["A"], ["A"])), "lru-flat", 1).hitRate, 1)
        empty = replace(weighted, ids=[], tokens=[], request_starts=[0], request_count=0,
                        unique_raw_blocks=0, total_input_tokens=0, average_input_tokens=0.0)
        self.assertEqual(simulate_policy(build_execution_plan(empty), "lru-flat", 1).totalTokens, 0)
        cache = _FlatLruCache(1)
        _write_flat_lru_request(cache, plan, 0, 2)
        self.assertEqual(len(cache), 1)
        self.assertIn(plan.node_for_event[1], cache)

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler is not available")
    def test_native_read_and_write_operations(self) -> None:
        # Include the core in one translation unit to exercise private operations
        # between read and write, without introducing a cache-seeding CLI option.
        source = package_resource_path("kv-cache-lab-native-sim.cc")
        harness = r'''
#include <cassert>
#define main simulator_cli_main
#include SOURCE_PATH
#undef main
int main() {
  SimulationInput input;
  input.ids = {0, 1, 2, 3, 4, 2};  // X | D C | A B C
  input.tokens = {1, 1, 1, 1, 1, 1};
  input.request_ends = {1, 3, 6};
  input.prefix = build_prefix_plan(input.ids, input.request_ends);
  const auto& nodes = input.prefix.node_for_event;
  const auto x = nodes[0], other_c = nodes[2], a = nodes[3], b = nodes[4], c = nodes[5];
  FlatLruCache cache(4);
  cache.write(x); cache.write(other_c); cache.write(a);
  assert(read_flat_lru_prefix(cache, input, 3, 6) == 1);
  write_flat_lru_request(cache, input, 3, 6);
  assert(cache.size() == 4 && cache.contains(other_c) && cache.contains(c));
  cache.write(x);
  assert(!cache.contains(other_c) && cache.contains(c));

  FlatLruCache small(2);
  write_flat_lru_request(small, input, 3, 5);
  assert(read_flat_lru_prefix(small, input, 3, 5) == 2);
  small.write(x);
  assert(!small.contains(a) && small.contains(b));
  assert(read_flat_lru_prefix(small, input, 3, 5) == 0);
  write_flat_lru_request(small, input, 3, 5);
  assert(small.contains(a) && small.contains(b));
  write_flat_lru_request(small, input, 3, 6);
  assert(small.size() == 2 && !small.contains(a) && small.contains(b) && small.contains(c));
}
'''.replace("SOURCE_PATH", json.dumps(str(source)))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "flat-lru-test.cc"
            binary = Path(tmpdir) / "flat-lru-test"
            path.write_text(harness, encoding="utf-8")
            subprocess.run([shutil.which("c++"), "-std=c++17", str(path), "-o", str(binary)], check=True, capture_output=True, text=True)
            subprocess.run([str(binary)], check=True, capture_output=True, text=True, timeout=10)

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler is not available")
    def test_cpp_matches_python_across_traces_and_capacities(self) -> None:
        rng = random.Random(42)
        traces = [
            make_trace(["A"], ["B"], ["A"], ["C"], ["A"]),
            make_trace(["A", "B"], ["X"], ["A", "B"], ["A", "B"]),
            make_trace(["A", "B"], ["A", "B", "C"]),
            make_trace(["A"], ["A"]),
        ]
        for _ in range(8):
            traces.append(parse_trace_lines([
                json.dumps({"block_size": 8, "hash_ids": [rng.choice("ABCD") for _ in range(length)],
                            "input_length": length * 8 - 3})
                for length in [rng.randint(1, 6) for _ in range(24)]
            ]))
        for trace in traces:
            for warmup_fraction in (0, 0.5, 1):
                plan = build_execution_plan(trace, warmup_fraction)
                native = run_cpp_sweep(trace, capacities=[0, 1, 2, 4, 100], policies=["lru-flat"], warmup_requests=plan.warmup_requests)
                for capacity in (0, 1, 2, 4, 100):
                    with self.subTest(events=len(trace.ids), warmup=warmup_fraction, capacity=capacity):
                        self.assertEqual(native.results[("lru-flat", capacity)], simulate_policy(plan, "lru-flat", capacity))

    @unittest.skipUnless(shutil.which("c++"), "C++ compiler is not available")
    def test_native_single_batch_and_all_modes(self) -> None:
        trace = make_trace(["A", "B"], ["X"], ["A", "B"], ["A", "B"])
        binary = ensure_cpp_simulator()
        with tempfile.TemporaryDirectory() as tmpdir:
            files = _write_binary_trace(trace, Path(tmpdir))
            base = [str(binary), "--ids", str(files.ids), "--tokens", str(files.tokens),
                    "--request-ends", str(files.request_ends), "--request-count", "4",
                    "--total-blocks", str(len(trace.ids)), "--warmup-requests", "2", "--capacity", "2"]
            def run(*args):
                return json.loads(subprocess.run(base + list(args), check=True, capture_output=True, text=True).stdout)
            single = run("--policy", "lru-flat")
            all_results = run("--policy", "all")
            batch = run("--policy", "batch", "--capacities", "2", "--policies", "lru-flat")
            default_batch = run("--policy", "batch", "--capacities", "2")
        self.assertEqual(single["hitTokens"], 2)
        self.assertEqual(all_results["lru-flat"]["hitTokens"], 2)
        self.assertEqual(set(batch["points"][0]), {"cacheBlocks", "lru-flat"})
        self.assertEqual(set(default_batch["points"][0]), {"cacheBlocks", "fifo", "lru", "lru-flat", "hyper-clock", "optimal"})


class SweepAndCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.models_data = load_models_data()

    def test_run_sweep_uses_fixed_50_percent_window_and_omits_underfilled_larger_budgets(self) -> None:
        trace = make_trace(["A"], ["A"], ["B"], ["C"])
        result = run_sweep(
            trace,
            model_id="qwen3-32b",
            precision="bf16_fp16",
            budgets_gib=[0.00025, 0.0005, 0.001],
            policies=["fifo", "lru", "optimal"],
            backend="python",
            models_data=self.models_data,
        )

        self.assertEqual(result["metadata"]["warmupFraction"], 0.5)
        self.assertEqual(result["metadata"]["totalMeasuredTokens"], 2)
        self.assertEqual([point["cacheBlocks"] for point in result["points"]], [1])
        self.assertEqual(result["points"][0]["results"]["fifo"]["totalTokens"], 2)

    def test_run_sweep_sorts_budgets_before_early_breaks(self) -> None:
        trace = make_trace(["A"], ["B"], ["A"], ["B"])
        result = run_sweep(
            trace,
            model_id="qwen3-32b",
            precision="bf16_fp16",
            budgets_gib=[0.001, 0.00025, 0.00025],
            policies=["fifo"],
            backend="python",
            models_data=self.models_data,
        )

        self.assertEqual([point["cacheBlocks"] for point in result["points"]], [1])
        self.assertEqual(result["points"][0]["gib"], 0.00025)

    def test_multiprocess_sweep_matches_single_process(self) -> None:
        trace = make_trace(["A"], ["B"], ["A"], ["B"])
        serial = run_sweep(
            trace,
            model_id="qwen3-32b",
            precision="bf16_fp16",
            budgets_gib=[0.00025],
            jobs=1,
            backend="python",
            models_data=self.models_data,
        )
        parallel = run_sweep(
            trace,
            model_id="qwen3-32b",
            precision="bf16_fp16",
            budgets_gib=[0.00025],
            jobs=2,
            backend="python",
            models_data=self.models_data,
        )

        self.assertEqual(serial["points"], parallel["points"])

    def test_cpp_backend_matches_python_backend_when_compiler_is_available(self) -> None:
        if not shutil.which("c++"):
            self.skipTest("c++ compiler is not available")
        trace = make_trace(["A"], ["B"], ["A"], ["B"])
        python_result = run_sweep(
            trace,
            model_id="qwen3-32b",
            precision="bf16_fp16",
            budgets_gib=[0.00025],
            jobs=1,
            backend="python",
            models_data=self.models_data,
        )
        cpp_result = run_sweep(
            trace,
            model_id="qwen3-32b",
            precision="bf16_fp16",
            budgets_gib=[0.00025],
            backend="cpp",
            models_data=self.models_data,
        )

        self.assertEqual(cpp_result["metadata"]["backend"], "cpp")
        self.assertEqual(python_result["points"], cpp_result["points"])
        self.assertEqual(python_result["hitRateCeiling"], cpp_result["hitRateCeiling"])

    def test_cli_outputs_table_by_default_and_json_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = Path(tmpdir) / "trace.jsonl"
            trace_path.write_text(
                "\n".join([
                    json.dumps({"block_size": 1, "hash_ids": ["A"], "input_length": 1}),
                    json.dumps({"block_size": 1, "hash_ids": ["B"], "input_length": 1}),
                    json.dumps({"block_size": 1, "hash_ids": ["A"], "input_length": 1}),
                    json.dumps({"block_size": 1, "hash_ids": ["B"], "input_length": 1}),
                ])
                + "\n",
                encoding="utf-8",
            )
            base = [
                sys.executable,
                "-m",
                "kvcache_sim",
                "sweep",
                "--trace",
                str(trace_path),
                "--model",
                "qwen3-32b",
                "--kv-precision",
                "bf16_fp16",
                "--budgets-gib",
                "0.00025",
                "--backend",
                "python",
            ]

            table = subprocess.run(base, cwd=Path(__file__).resolve().parents[1], check=True, text=True, capture_output=True)
            as_json = subprocess.run(base + ["--format", "json"], cwd=Path(__file__).resolve().parents[1], check=True, text=True, capture_output=True)
            selected = subprocess.run(base + ["--policies", "lru-flat", "--format", "json"], cwd=PACKAGE_ROOT, check=True, text=True, capture_output=True)
            if shutil.which("c++"):
                # Omit --backend to exercise the default native backend.
                native = subprocess.run(base[:-2] + ["--format", "json"], cwd=PACKAGE_ROOT, check=True, text=True, capture_output=True)
                self.assertEqual(json.loads(native.stdout)["points"], json.loads(as_json.stdout)["points"])

        self.assertIn("Budget", table.stdout)
        self.assertIn("FIFO hit", table.stdout)
        self.assertIn("LRU-FLAT hit", table.stdout)
        self.assertIn("LRU-FLAT speedup", table.stdout)
        parsed = json.loads(as_json.stdout)
        self.assertEqual(parsed["policies"], ["fifo", "lru", "lru-flat", "hyper-clock", "optimal"])
        self.assertIn("HYPER-CLOCK hit", table.stdout)
        self.assertIn("HYPER-CLOCK speedup", table.stdout)
        self.assertIn("Hyper Clock max score: 3", table.stdout)
        self.assertIn("idealPrefillSpeedup", parsed["points"][0]["results"]["lru-flat"])
        selected_result = json.loads(selected.stdout)
        self.assertEqual(selected_result["policies"], ["lru-flat"])
        self.assertEqual(set(selected_result["points"][0]["results"]), {"lru-flat"})
        self.assertEqual(parsed["metadata"]["modelId"], "qwen3-32b")
        self.assertEqual(parsed["points"][0]["cacheBlocks"], 1)
        self.assertIn("Measurement: hit rates use the last 50% of requests", table.stdout)
        self.assertIn("Speedup: 1.0x means no-cache prefill throughput", table.stdout)

    def test_cli_run_command_matches_sweep_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            trace_path = Path(tmpdir) / "trace.jsonl"
            trace_path.write_text(
                "\n".join([
                    json.dumps({"block_size": 1, "hash_ids": ["A"], "input_length": 1}),
                    json.dumps({"block_size": 1, "hash_ids": ["B"], "input_length": 1}),
                    json.dumps({"block_size": 1, "hash_ids": ["A"], "input_length": 1}),
                    json.dumps({"block_size": 1, "hash_ids": ["B"], "input_length": 1}),
                ])
                + "\n",
                encoding="utf-8",
            )
            args = [
                "--trace",
                str(trace_path),
                "--model",
                "qwen3-32b",
                "--kv-precision",
                "bf16_fp16",
                "--budgets-gib",
                "0.00025",
                "--backend",
                "python",
                "--format",
                "json",
            ]

            run_result = subprocess.run([sys.executable, "-m", "kvcache_sim", "run", *args], cwd=Path(__file__).resolve().parents[1], check=True, text=True, capture_output=True)
            sweep_result = subprocess.run([sys.executable, "-m", "kvcache_sim", "sweep", *args], cwd=Path(__file__).resolve().parents[1], check=True, text=True, capture_output=True)

        self.assertEqual(json.loads(run_result.stdout)["points"], json.loads(sweep_result.stdout)["points"])


class TempPathTests(unittest.TestCase):
    def test_temp_paths_include_user_suffix_when_available(self) -> None:
        suffix = user_temp_suffix()
        if not suffix:
            self.skipTest("platform does not expose os.getuid")

        resource_path = package_resource_path("models.yaml")
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "sim.cc"
            source.write_text("int main() { return 0; }\n", encoding="utf-8")
            binary_path = _build_path_for_source(source)

        if tempfile.gettempdir() in str(resource_path):
            self.assertIn(suffix, resource_path.name)
        self.assertIn(suffix, binary_path.name)


if __name__ == "__main__":
    unittest.main()
