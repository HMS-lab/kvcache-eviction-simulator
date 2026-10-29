from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from kvcache_sim.trace import parse_trace_lines
from kvcache_sim.plan import build_execution_plan
from kvcache_sim.sampled import SampledCache, simulate_sampled
from kvcache_sim.policies import simulate_policy
from kvcache_sim.cpp_backend import run_cpp_sweep, ensure_cpp_simulator, _write_binary_trace
from kvcache_sim.simulator import run_sweep, DEFAULT_POLICIES
from kvcache_sim.formatting import render_table

LEGACY = [p for p in DEFAULT_POLICIES if p != 'sampled-lru']


def trace(requests, **kwargs):
    records = []
    for i, (arrival, finish, blocks) in enumerate(requests):
        records.append(json.dumps(dict(timestamp=arrival, completion_timestamp=finish,
                                      conversation_id=str(i), hash_ids=blocks,
                                      input_length=len(blocks), block_size=1)))
    return parse_trace_lines(records, **kwargs)


class TimingTests(unittest.TestCase):
    def test_normalization_and_fallback(self):
        t = parse_trace_lines([json.dumps(dict(timestamp=1.234567, api_time=2, block_size=1, hash_ids=['A'], input_length=1))], timing_mode='required')
        self.assertEqual(t.arrivals, [1234567])
        self.assertEqual(t.completions, [3234567])

    def test_invalid_optional_required_and_ignore(self):
        base = dict(timestamp=1, api_time=1, input_length=1, hash_ids=['A'], block_size=1)
        for changes in [dict(timestamp=None), dict(timestamp=True), dict(api_time=-1),
                        dict(timestamp=float('nan')), dict(timestamp=1e30), dict(completion_timestamp=0),
                        dict(completion_timestamp=10)]:
            lines = [json.dumps(base | changes)]
            self.assertIsNone(parse_trace_lines(lines).arrivals)
            with self.assertRaises(ValueError):
                parse_trace_lines(lines, timing_mode='required')
            self.assertEqual(parse_trace_lines(lines, timing_mode='ignore').ids, [0])
        self.assertIsNone(trace([(1, 2, ['A']), (0, 1, ['A'])]).arrivals)
        self.assertEqual(trace([(0, 0, ['A']), (0, 0, ['A'])]).arrivals, [0, 0])


class CacheTests(unittest.TestCase):
    def test_all_sampled_eligible_and_surplus_slots(self):
        c = SampledCache(4, 10, 4, 0)
        for node in range(4): c.write(node, 0)
        c.lookup(3, 20)
        c.write(4, 20)
        self.assertEqual(set(c.entries), {3, 4})
        self.assertEqual(len(c.free), 2)
        with patch.object(c.rng, 'sample', side_effect=AssertionError('sampled without pressure')):
            c.write(5, 20); c.write(6, 20)
        self.assertEqual(len(c.entries), 4)

    def test_controlled_sample_and_scan(self):
        c = SampledCache(3, 10, 1, 0)
        for n in range(3): c.write(n, 0)
        c.lookup(2, 20)
        with patch.object(c.rng, 'sample', return_value=[2]):
            c.write(3, 20)
        self.assertEqual(set(c.entries), {1, 2, 3})

    def test_halving_is_local_and_zero_includes_now(self):
        c = SampledCache(2, 10, 2, 0)
        c.write(1, 0); c.write(2, 0)
        c.write(3, 0)
        self.assertEqual(c.threshold, 10)
        # At age ten the strict configured threshold excludes both existing entries.
        # Fallback halves it and removes only the first slot, not the whole sample.
        c.write(4, 10)
        self.assertEqual(len(c.entries), 2)
        self.assertEqual(c.threshold, 10)
        z = SampledCache(2, 0, 2, 0)
        z.write(1, 0); z.write(2, 0); z.write(3, 0)
        self.assertEqual(set(z.entries), {3})


class ReplayTests(unittest.TestCase):
    def result(self, requests, capacity=2, **kwargs):
        return simulate_sampled(build_execution_plan(trace(requests)), capacity, **kwargs)

    def test_overlap_and_completion_before_arrival(self):
        r = self.result([(0, 1, ['W']), (0, 5, ['A']), (2, 6, ['A']), (5, 7, ['A'])], capacity=1)
        self.assertEqual((r.hitTokens, r.totalTokens), (1, 2))

    def test_zero_duration_batch_does_not_publish_between_arrivals(self):
        r = self.result([(0, 0, ['A']), (1, 1, ['B']), (2, 2, ['C']), (2, 2, ['C'])], capacity=1)
        self.assertEqual(r.hitTokens, 0)

    def test_lexical_completion_order(self):
        t = trace([(0, 1, ['B']), (0, 1, ['A']), (2, 3, ['B']), (4, 5, ['B'])])
        # Input request 1 completes first, request 0 last: B remains in capacity one.
        t.completion_order = [1, 0, 2, 3]
        self.assertEqual(simulate_sampled(build_execution_plan(t), 1).hitTokens, 2)

    def test_prefix_hole_no_refresh_and_entire_input_write(self):
        t = trace([(0, 1, ['A', 'B', 'C']), (2, 3, ['A', 'B', 'C'])])
        p = build_execution_plan(t)
        a,b,c = p.node_for_event[:3]
        cache = SampledCache(3, 0, 3, 0)
        cache.write(a, 0); cache.write(c, 0)
        with patch('kvcache_sim.sampled.SampledCache', return_value=cache), patch.object(cache, 'lookup', wraps=cache.lookup) as reads:
            r = simulate_sampled(p, 3)
        self.assertEqual([call.args[0] for call in reads.call_args_list if call.args[1] == 0], [a,b])
        self.assertEqual(r.hitTokens, 3)
        self.assertTrue(all(s.timestamp == 3000000 for s in cache.slots if s))
        self.assertNotEqual(build_execution_plan(trace([(0,1,['A','X']), (2,3,['B','X'])])).node_for_event[1],
                            build_execution_plan(trace([(0,1,['A','X']), (2,3,['B','X'])])).node_for_event[3])

    def test_oversized_and_seed_repeatability(self):
        p = build_execution_plan(trace([(i*2, i*2+1, list(range(5))) for i in range(10)]))
        for cap in (0, 1, 3):
            self.assertEqual(simulate_sampled(p, cap), simulate_sampled(p, cap))

    def test_defaults_skip_and_explicit_error(self):
        t = parse_trace_lines([json.dumps(dict(block_size=1, hash_ids=['A'], input_length=1))]*4)
        args = dict(model_id='qwen3-32b', budgets_gib=[0.00025], backend='python')
        r = run_sweep(t, **args)
        self.assertEqual(r['policies'], LEGACY)
        self.assertIn('sampled-lru', r['metadata']['skippedPolicies'])
        with self.assertRaises(ValueError): run_sweep(t, policies=['sampled-lru'], **args)

    def test_legacy_results_and_budget_points_unchanged(self):
        t = trace([(0, 10, ['A']), (1, 10, ['B']), (2, 11, ['A']), (3, 12, ['B'])])
        for backend in ['python'] + (['cpp'] if shutil.which('c++') else []):
            args = dict(model_id='qwen3-32b', budgets_gib=[0.00025], backend=backend)
            old = run_sweep(t, policies=LEGACY, **args)
            new = run_sweep(t, **args)
            self.assertEqual(new['policies'], DEFAULT_POLICIES)
            self.assertEqual(len(old['points']), len(new['points']))
            for a,b in zip(old['points'], new['points']):
                self.assertEqual(a['results'], {k:v for k,v in b['results'].items() if k in LEGACY})
            self.assertIn('N/A', render_table(new))
        changed = trace([(i*100, i*100+50, blocks) for i, blocks in enumerate([['A'],['B'],['A'],['B']])])
        for policy in LEGACY:
            self.assertEqual(simulate_policy(build_execution_plan(t), policy, 1), simulate_policy(build_execution_plan(changed), policy, 1))

    def test_parallel(self):
        t = trace([(i*2, i*2+1, ['A','B']) for i in range(8)])
        kw = dict(model_id='qwen3-32b', budgets_gib=[0.00025], backend='python')
        self.assertEqual(run_sweep(t, jobs=1, **kw)['points'], run_sweep(t, jobs=2, **kw)['points'])

    def test_cli_default_selection_and_skip_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'trace.jsonl'
            records = [dict(timestamp=i*2, api_time=1, hash_ids=['A'], input_length=1, block_size=1) for i in range(4)]
            path.write_text('\n'.join(json.dumps(r) for r in records))
            cmd = [sys.executable, '-m', 'kvcache_sim', 'run', '--trace', str(path), '--model', 'qwen3-32b',
                   '--budgets-gib', '0.00025', '--backend', 'python', '--format', 'json']
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            parsed = json.loads(result.stdout)
            self.assertEqual(parsed['policies'], DEFAULT_POLICIES)
            self.assertEqual(parsed['metadata']['sampledLruParameters']['thresholdSeconds'], 10)
            for r in records: del r['timestamp']; del r['api_time']
            path.write_text('\n'.join(json.dumps(r) for r in records))
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)['policies'], LEGACY)
            self.assertIn('Skipping sampled-lru', result.stderr)
            self.assertNotEqual(subprocess.run(cmd + ['--policies','sampled-lru'], capture_output=True).returncode, 0)

    def test_parameter_validation(self):
        p = build_execution_plan(trace([(0,1,['A']), (2,3,['A'])]))
        for kw in [dict(threshold=-1), dict(threshold=float('inf')), dict(threshold=10**500),
                   dict(sample_size=0), dict(sample_size=True), dict(seed=-1), dict(seed=2**64)]:
            with self.assertRaises(ValueError): simulate_sampled(p, 1, **kw)



@unittest.skipUnless(shutil.which('c++'), 'compiler unavailable')
class NativeTests(unittest.TestCase):
    def test_full_population_sampling_agrees_and_legacy_matches(self):
        rng = random.Random(12)
        for iteration in range(12):
            t = trace([(i*20, i*20+1, [rng.randrange(3) for _ in range(rng.randint(1,4))]) for i in range(10)])
            native = run_cpp_sweep(t, capacities=[0,1,3], policies=DEFAULT_POLICIES, warmup_requests=5)
            p = build_execution_plan(t)
            for cap in (0,1,3):
                for policy in DEFAULT_POLICIES:
                    self.assertEqual(native.results[policy,cap], simulate_policy(p,policy,cap))

    def test_timing_lists_released_before_launch(self):
        class L(list): pass
        t = trace([(i*2, i*2+1, ['A']) for i in range(4)])
        for name in ('arrivals','completions','completion_order'): setattr(t,name,L(getattr(t,name)))
        refs = [weakref.ref(getattr(t,n)) for n in ('arrivals','completions','completion_order')]
        ensure_cpp_simulator()
        original = subprocess.Popen
        def launch(*a, **kw):
            self.assertTrue(all(r() is None for r in refs))
            return original(*a, **kw)
        with patch('kvcache_sim.cpp_backend.subprocess.Popen', side_effect=launch):
            run_cpp_sweep(t, capacities=[1], policies=['sampled-lru'], warmup_requests=2, release_trace_data=True)
        self.assertEqual(t.ids, [])

    def test_native_batch_eviction_and_fallback(self):
        source = Path(__file__).resolve().parents[1] / 'src/kvcache_sim/resources/kv-cache-lab-native-sim.cc'
        harness = '#define main replay_main\n#include "' + str(source) + '"\n#undef main\n#include <cassert>\n' + r"""
int main() {
  Options o; o.sampled_size = 4; o.sampled_threshold = 10;
  SampledCache c(4, o);
  for (unsigned i = 1; i <= 4; ++i) c.write(i, 0);
  c.lookup(4, 20); c.write(5, 20);
  assert(c.entries.size() == 2 && c.entries.count(4) && c.entries.count(5));
  c.write(6, 20); c.write(7, 20);
  assert(c.entries.size() == 4 && c.free.empty());
  c.write(8, 20);
  assert(c.threshold == 10 && c.entries.size() == 4);
  o.sampled_threshold = 0;
  SampledCache z(2, o); z.write(1, 0); z.write(2, 0); z.write(3, 0);
  assert(z.entries.size() == 1 && z.entries.count(3));
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            cpp, binary = Path(tmp)/'check.cc', Path(tmp)/'check'
            cpp.write_text(harness)
            subprocess.run(['c++','-std=c++17','-O0',str(cpp),'-o',str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, timeout=10)

    def test_native_overlapping_and_zero_duration_events(self):
        for requests in [
            [(0,1,['W']), (0,5,['A']), (2,6,['A']), (5,7,['A'])],
            [(0,0,['A']), (1,1,['B']), (2,2,['C']), (2,2,['C'])],
            [(0,1,['B']), (0,1,['A']), (2,3,['B']), (4,5,['B'])],
        ]:
            t = trace(requests)
            t.completion_order = [1,0,2,3]
            expected = simulate_sampled(build_execution_plan(t), 1)
            actual = run_cpp_sweep(t, capacities=[1], policies=['sampled-lru'], warmup_requests=2)
            self.assertEqual(expected, actual.results['sampled-lru',1])

    def test_seed_reproducible_with_small_samples(self):
        t = trace([(i*2, i*2+1, [i%5, (i+1)%5]) for i in range(20)])
        kw = dict(capacities=[3], policies=['sampled-lru'], warmup_requests=10,
                  sampled_lru_threshold_seconds=0, sampled_lru_sample_size=2, sampled_lru_seed=123)
        self.assertEqual(run_cpp_sweep(t, **kw), run_cpp_sweep(t, **kw))
        p = build_execution_plan(t)
        self.assertEqual(simulate_sampled(p, 3, threshold=0, sample_size=2, seed=123),
                         simulate_sampled(p, 3, threshold=0, sample_size=2, seed=123))

    def test_native_timing_validation(self):
        t = trace([(0,1,['A']), (2,3,['A'])])
        with tempfile.TemporaryDirectory() as tmp:
            f = _write_binary_trace(t, Path(tmp))
            cmd = [str(ensure_cpp_simulator()), '--policy','sampled-lru','--ids',str(f.ids),'--tokens',str(f.tokens),
                   '--request-ends',str(f.request_ends),'--request-count','2','--total-blocks','2','--capacity','1']
            self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            for flag,path in zip(('--request-arrivals','--request-completions','--request-order'), f.timing): cmd += [flag,str(path)]
            f.timing[0].write_bytes(b'')
            self.assertNotEqual(subprocess.run(cmd,capture_output=True).returncode, 0)


if __name__ == '__main__': unittest.main()
