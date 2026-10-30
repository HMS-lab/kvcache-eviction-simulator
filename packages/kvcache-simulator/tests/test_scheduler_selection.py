from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from kvcache_sim.cpp_backend import _write_binary_trace, ensure_cpp_simulator, run_cpp_sweep
from kvcache_sim.simulator import DEFAULT_POLICIES, run_sweep
from kvcache_sim.trace import parse_trace_lines


def timed_trace(requests):
    return parse_trace_lines([json.dumps(dict(
        block_size=1, input_length=len(blocks), hash_ids=blocks,
        timestamp=arrival, completion_timestamp=finish, conversation_id=str(i)))
        for i, (arrival, finish, blocks) in enumerate(requests)])


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class SchedulerSelectionTests(unittest.TestCase):
    def native(self, requests, scheduler='recorded', **kwargs):
        return run_cpp_sweep(timed_trace(requests), scheduler=scheduler, capacities=[1],
                             policies=DEFAULT_POLICIES, warmup_requests=2, **kwargs)

    def test_every_policy_and_ceiling_use_selected_event_order(self):
        requests = [(0,1,['W']), (0,5,['A']), (2,6,['A']), (5,7,['A'])]
        for scheduler, hits in [('recorded', 1), ('sequential', 2)]:
            result = self.native(requests, scheduler)
            self.assertEqual((result.ceiling.hitTokens, result.ceiling.totalTokens), (hits, 2))
            for policy in DEFAULT_POLICIES:
                with self.subTest(scheduler=scheduler, policy=policy):
                    actual = result.results[policy, 1]
                    self.assertEqual((actual.hitTokens, actual.totalTokens), (hits, 2))
                    self.assertEqual(actual.measurementMode, 'fixed_window')

    def test_zero_duration_arrival_batch_for_every_policy(self):
        requests = [(0,0,['A']), (1,1,['B']), (2,2,['C']), (2,2,['C'])]
        for scheduler, hits in [('recorded', 0), ('sequential', 1)]:
            result = self.native(requests, scheduler)
            self.assertEqual(result.ceiling.hitTokens, hits)
            for policy in DEFAULT_POLICIES:
                self.assertEqual(result.results[policy, 1].hitTokens, hits, (scheduler, policy))

    def test_completion_tie_order_for_every_policy(self):
        trace = timed_trace([(0,1,['B']), (0,1,['A']), (2,3,['B']), (4,5,['B'])])
        trace.completion_order = [1,0,2,3]
        result = run_cpp_sweep(trace, scheduler='recorded', capacities=[1],
                               policies=DEFAULT_POLICIES, warmup_requests=2)
        for policy in DEFAULT_POLICIES:
            self.assertEqual(result.results[policy, 1].hitTokens, 2, policy)

    def test_hyper_clock_keeps_each_inflight_write_offset(self):
        result = self.native([(0,1,['W']), (2,10,['A']), (3,4,['B']), (11,12,['A'])])
        self.assertEqual(result.results['hyper-clock', 1].hitTokens, 1)

    def test_hyper_clock_restores_hits_evicted_inflight(self):
        result = self.native([(0,1,['A']), (2,10,['A']), (3,4,['B']), (11,12,['A'])])
        self.assertEqual(result.results['hyper-clock', 1].hitTokens, 1)

    def test_optimal_completion_priority_excludes_past_arrivals(self):
        result = self.native([(0,1,['B']), (2,10,['A']), (3,4,['A']), (11,12,['B'])])
        self.assertEqual(result.results['optimal', 1].hitTokens, 1)
        self.assertEqual(result.results['fifo', 1].hitTokens, 0)
        self.assertEqual(result.ceiling.hitTokens, 1)

    def test_recorded_requires_timing_even_without_sampled_lru(self):
        trace = parse_trace_lines([json.dumps(dict(block_size=1, input_length=1, hash_ids=['A']))] * 4)
        kwargs = dict(capacities=[1], policies=['fifo'], warmup_requests=2)
        with self.assertRaisesRegex(ValueError, 'recorded scheduler requires complete request timing'):
            run_cpp_sweep(trace, scheduler='recorded', **kwargs)
        result = run_cpp_sweep(trace, scheduler='sequential', **kwargs)
        self.assertEqual(result.results['fifo', 1].hitTokens, 2)

    def test_native_requires_scheduler_and_only_recorded_loads_timing(self):
        trace = timed_trace([(0,1,['A']), (2,3,['A'])])
        with tempfile.TemporaryDirectory() as directory:
            files = _write_binary_trace(trace, Path(directory))
            base = [str(ensure_cpp_simulator()), '--policy', 'fifo', '--ids', str(files.ids),
                    '--tokens', str(files.tokens), '--request-ends', str(files.request_ends),
                    '--total-blocks', '2', '--request-count', '2', '--warmup-requests', '1', '--capacity', '1']
            for flags in [[], ['--scheduler', 'unknown']]:
                result = subprocess.run(base + flags, capture_output=True, text=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn('--scheduler', result.stderr)
            missing = subprocess.run(base + ['--scheduler', 'recorded'], capture_output=True, text=True)
            self.assertEqual(missing.returncode, 1)
            self.assertIn('timing', missing.stderr)
            timing_flags = [item for flag, path in zip(
                ('--request-arrivals', '--request-completions', '--request-order'), files.timing)
                for item in (flag, str(path))]
            files.timing[0].write_bytes(b'')
            result = subprocess.run(base + ['--scheduler', 'sequential'] + timing_flags,
                                    check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(result.stdout)['hitTokens'], 1)
            result = subprocess.run(base + ['--scheduler', 'recorded'] + timing_flags,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Invalid timing file size', result.stderr)

    def test_cli_requires_scheduler_and_reports_selected_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.jsonl'
            records = [dict(block_size=1, input_length=1, hash_ids=['A'], timestamp=i*2, api_time=1)
                       for i in range(4)]
            path.write_text('\n'.join(map(json.dumps, records)))
            for command in ['run', 'sweep']:
                base = [sys.executable, '-m', 'kvcache_sim', command, '--trace', str(path),
                        '--model', 'qwen3-32b', '--budgets-gib', '0.00025', '--format', 'json', '--no-progress']
                missing = subprocess.run(base, text=True, capture_output=True)
                self.assertNotEqual(missing.returncode, 0)
                self.assertIn('--scheduler', missing.stderr)
                for scheduler in ['sequential', 'recorded']:
                    actual = subprocess.run(base + ['--scheduler', scheduler], text=True,
                                            capture_output=True, check=True)
                    payload = json.loads(actual.stdout)
                    self.assertEqual(payload['policies'], DEFAULT_POLICIES)
                    self.assertEqual(payload['metadata']['scheduler'], scheduler)
                    self.assertEqual(payload['metadata']['ceilingReplayMode'], scheduler)
                    self.assertEqual(set(payload['metadata']['replayModes'].values()), {scheduler})
            for record in records:
                del record['timestamp']; del record['api_time']
            path.write_text('\n'.join(map(json.dumps, records)))
            missing_timing = subprocess.run(base + ['--scheduler', 'recorded', '--policies', 'fifo'],
                                             text=True, capture_output=True)
            self.assertNotEqual(missing_timing.returncode, 0)
            self.assertIn('timing', missing_timing.stderr.lower())
            subprocess.run(base + ['--scheduler', 'sequential'], capture_output=True, check=True)

    def test_api_requires_scheduler_and_rejects_unknown_selection(self):
        trace = timed_trace([(0,1,['A']), (2,3,['A'])])
        with self.assertRaises(TypeError):
            run_cpp_sweep(trace, capacities=[1], policies=['fifo'], warmup_requests=1)
        with self.assertRaises(TypeError):
            run_sweep(trace, model_id='qwen3-32b')
        with self.assertRaisesRegex(ValueError, 'Unsupported scheduler'):
            run_cpp_sweep(trace, capacities=[1], policies=['fifo'], warmup_requests=1, scheduler='other')


if __name__ == '__main__':
    unittest.main()
