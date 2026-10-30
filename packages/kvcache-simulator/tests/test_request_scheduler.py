from __future__ import annotations

import json
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest

from kvcache_sim.cpp_backend import _write_binary_trace
from kvcache_sim.trace import parse_trace_lines

SOURCE = Path(__file__).resolve().parents[1] / 'src/kvcache_sim/resources/kv-cache-lab-native-sim.cc'

HARNESS = r'''
using Kind = ScheduledEventKind;
using Event = std::tuple<unsigned, Kind, std::uint64_t>;
std::vector<Event> drain(RequestScheduler& scheduler) {
  std::vector<Event> result;
  while (auto e = scheduler.next_event()) result.emplace_back(e->request, e->kind, e->timestamp);
  return result;
}
class AlternateScheduler final : public RequestScheduler {
 public:
  std::optional<ScheduledEvent> next_event() override {
    if (exhausted()) return std::nullopt;
    auto n = cursor++;
    return ScheduledEvent{n / 2, n % 2 ? Kind::Completion : Kind::Arrival, n};
  }
  bool exhausted() const override { return cursor == 4; }
  void discard_remaining() override { cursor = 4; }
 protected:
  bool at_start() const override { return cursor == 0; }
  void reset_impl() override { cursor = 0; }
 private:
  unsigned cursor = 0;
};
void sequential_lifecycle(bool premature) {
  {
    SequentialRequestScheduler concrete(3);
    auto& scheduler = RequestScheduler::get();
    assert(&scheduler == &concrete);
    if (premature) { scheduler.next_event(); scheduler.reset(); return; }
    const std::vector<Event> expected{
      {0,Kind::Arrival,0},{0,Kind::Completion,0},
      {1,Kind::Arrival,0},{1,Kind::Completion,0},
      {2,Kind::Arrival,0},{2,Kind::Completion,0}};
    assert(drain(scheduler) == expected);
    assert(scheduler.exhausted() && !scheduler.next_event());
    scheduler.reset(); assert(drain(scheduler) == expected);
    scheduler.reset(); scheduler.next_event(); scheduler.discard_remaining();
    scheduler.reset(); assert(drain(scheduler) == expected);
  }
  SequentialRequestScheduler empty(0);
  assert(empty.exhausted() && !empty.next_event());
  empty.reset();
}
void shared_policy_scheduler() {
  SimulationInput input;
  input.ids = {1,1,1}; input.tokens = {64,64,64}; input.request_ends = {1,2,3};
  input.arrivals = {0,1,2}; input.completions = {10,11,12};
  input.completion_order = {0,1,2}; input.timing_available = true;
  input.prefix = build_prefix_plan(input.ids,input.request_ends);
  for (const auto* strategy : {"sequential", "recorded"}) {
    Options options; options.warmup_requests = 1; options.scheduler = strategy;
    SimulationRunner runner(input, options);
    RequestScheduler* selected = &RequestScheduler::get();
    assert((dynamic_cast<RecordedTimeScheduler*>(selected) != nullptr) == (options.scheduler == "recorded"));
    for (unsigned capacity : {1,99,0,1}) {
      for (const auto* policy : {"ceiling","fifo","lru","lru-flat","hyper-clock","optimal","sampled-lru"}) {
        const auto result = runner.run(policy, capacity);
        assert(&RequestScheduler::get() == selected);
        if (capacity == 1) {
          if (options.scheduler == "sequential") assert(result.hit_tokens == 128);
          else if (std::string(policy) == "ceiling") assert(result.hit_tokens == 0);
          else assert(result.measurement_start_request == -2);
        }
      }
    }
  }
}
void legacy_regression() {
  SimulationInput input;
  const std::vector<std::vector<unsigned>> requests{
    {1,2,3},{1,2,4},{5,2,3},{1,2,3},{1,2,4},{1,6},{5,2,3},{1,2,3}};
  for (const auto& blocks : requests) {
    for (unsigned i = 0; i < blocks.size(); ++i) {
      input.ids.push_back(blocks[i]);
      input.tokens.push_back(i + 1 == blocks.size() ? 3 : 4);
    }
    input.request_ends.push_back(input.ids.size());
  }
  input.prefix = build_prefix_plan(input.ids,input.request_ends);
  Options options; options.warmup_requests = 4; options.scheduler = "sequential";
  SimulationRunner runner(input, options);
  const std::vector<std::string> policies{"fifo","lru","lru-flat","hyper-clock","optimal"};
  // Captured from the C++ executable before the scheduler extraction.
  const std::vector<unsigned> capacities{0,1,2,3,4,5,6,7,8,99,3};
  const std::vector<std::vector<unsigned>> hits{
    {0,0,0,0,0},{0,0,0,0,12},{0,8,0,0,20},{8,12,12,8,23},
    {12,16,12,12,26},{12,16,16,12,30},{12,24,16,20,34},
    {26,31,23,23,37},{0,0,0,0,0},{0,0,0,0,0},{8,12,12,8,23}};
  for (unsigned c = 0; c < capacities.size(); ++c) {
    for (unsigned p = 0; p < policies.size(); ++p) {
      auto result = runner.run(policies[p], capacities[c]);
      assert(result.hit_tokens == hits[c][p] && result.total_tokens == 40);
      assert(result.measurement_start_request == (capacities[c] >= 8 ? -2 : 4));
    }
  }
  auto ceiling = runner.run("ceiling", 0);
  assert(ceiling.hit_tokens == 37 && ceiling.total_tokens == 40);
}
void lifecycle(bool premature) {
  std::vector<std::uint64_t> arrivals{0,0,1,2,2}, completions{2,2,1,2,4};
  std::vector<std::uint32_t> ranks{1,0,2,3,4};
  {
  RecordedTimeScheduler concrete(arrivals,completions,ranks);
  RequestScheduler& scheduler = RequestScheduler::get();
  assert(&scheduler == &concrete);
  if (premature) { scheduler.next_event(); scheduler.reset(); return; }
  const std::vector<Event> expected{
    {0,Kind::Arrival,0},{1,Kind::Arrival,0},{2,Kind::Arrival,1},
    {2,Kind::Completion,1},{1,Kind::Completion,2},{0,Kind::Completion,2},
    {3,Kind::Arrival,2},{4,Kind::Arrival,2},{3,Kind::Completion,2},{4,Kind::Completion,4}};
  assert(drain(scheduler) == expected);
  assert(scheduler.exhausted());
  assert(!scheduler.next_event() && !scheduler.next_event());
  scheduler.reset(); assert(drain(scheduler) == expected);
  scheduler.reset(); scheduler.next_event(); scheduler.discard_remaining();
  assert(scheduler.exhausted()); scheduler.discard_remaining();
  scheduler.reset(); assert(drain(scheduler) == expected);
  }
  std::vector<std::uint64_t> empty;
  std::vector<std::uint32_t> no_ranks;
  RecordedTimeScheduler none(empty,empty,no_ranks);
  assert(none.exhausted() && !none.next_event()); none.reset();
}
void alternate() {
  SimulationInput input;
  input.ids = {1,1}; input.tokens = {64,64}; input.request_ends = {1,2};
  input.timing_available = true;
  input.prefix = build_prefix_plan(input.ids,input.request_ends);
  Options options; options.warmup_requests = 1;
  AlternateScheduler concrete;
  RequestScheduler& scheduler = RequestScheduler::get();
  assert(&scheduler == &concrete);
  auto result = simulate_sampled(input,1,options);
  assert(result.hit_tokens == 64 && result.total_tokens == 64);
  assert(&RequestScheduler::get() == &scheduler);
  simulate_sampled(input,0,options);
  assert(&RequestScheduler::get() == &scheduler);
  result = simulate_sampled(input,2,options);
  assert(result.measurement_start_request == -2);
  assert(&RequestScheduler::get() == &scheduler);
  assert(simulate_sampled(input,1,options).hit_tokens == 64);
}
void singleton_guard(const std::string& mode) {
  if (mode == "uninitialized") { RequestScheduler::get(); return; }
  AlternateScheduler scheduler;
  if (mode == "duplicate") { AlternateScheduler duplicate; return; }
  scheduler.next_event();
  if (mode == "consumed") drain(scheduler);
  if (mode == "discarded") scheduler.discard_remaining();
  RequestScheduler::get();
}
int main(int argc, char** argv) {
  assert(argc >= 2);
  std::string mode = argv[1];
  if (mode == "sequential") sequential_lifecycle(false);
  else if (mode == "sequential-premature") sequential_lifecycle(true);
  else if (mode == "shared-policy-scheduler") shared_policy_scheduler();
  else if (mode == "legacy-regression") legacy_regression();
  else if (mode == "lifecycle") lifecycle(false);
  else if (mode == "premature") lifecycle(true);
  else if (mode == "alternate") alternate();
  else if (mode == "uninitialized" || mode == "duplicate" || mode == "partial"
           || mode == "consumed" || mode == "discarded") singleton_guard(mode);
  else {
    auto expected = static_cast<unsigned>(std::stoul(mode));
    const auto result = simulator_main(argc-1,argv+1);
    assert(scheduler_constructions == expected);
    assert(sequential_constructions == 1 - expected);
    return result;
  }
}
'''


@unittest.skipUnless(shutil.which('c++'), 'C++ compiler required')
class NativeRequestSchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='native-scheduler-')
        cls.addClassCleanup(cls.directory.cleanup)
        source = SOURCE.read_text().replace(
            'class RecordedTimeScheduler final',
            'unsigned scheduler_constructions = 0;\nclass RecordedTimeScheduler final', 1)
        source = source.replace(': arrivals_(arrivals), completions_(completions) {',
                                ': arrivals_(arrivals), completions_(completions) {\n    ++scheduler_constructions;', 1)
        source = source.replace('class SequentialRequestScheduler final',
                                'unsigned sequential_constructions = 0;\nclass SequentialRequestScheduler final', 1)
        source = source.replace('event_count_(2ULL * request_count) {}',
                                'event_count_(2ULL * request_count) { ++sequential_constructions; }', 1)
        cpp = Path(cls.directory.name) / 'scheduler.cc'
        cls.binary = Path(cls.directory.name) / 'scheduler'
        cpp.write_text('#define main simulator_main\n' + source + '\n#undef main\n' + HARNESS)
        compiled = subprocess.run(['c++', '-std=c++17', '-O0', str(cpp), '-o', str(cls.binary)],
                                  capture_output=True, text=True)
        if compiled.returncode:
            raise AssertionError(compiled.stderr)

    def test_order_exhaustion_reset_and_discard(self):
        subprocess.run([str(self.binary), 'lifecycle'], check=True, capture_output=True)

    def test_sequential_order_exhaustion_reset_and_discard(self):
        subprocess.run([str(self.binary), 'sequential'], check=True, capture_output=True)

    def test_selected_scheduler_shared_after_all_return_paths(self):
        subprocess.run([str(self.binary), 'shared-policy-scheduler'], check=True, capture_output=True)

    def test_legacy_policies_match_results_before_extraction(self):
        subprocess.run([str(self.binary), 'legacy-regression'], check=True, capture_output=True)

    def test_sequential_reset_guard(self):
        for mode in ('sequential-premature',):
            with self.subTest(mode=mode):
                result = subprocess.run([str(self.binary), mode], capture_output=True, text=True,
                                        cwd=self.directory.name)
                self.assertEqual(result.returncode, -signal.SIGABRT)
                self.assertIn('Assertion', result.stderr)

    def test_premature_reset_asserts(self):
        result = subprocess.run([str(self.binary), 'premature'], capture_output=True, text=True,
                                cwd=self.directory.name)
        self.assertEqual(result.returncode, -signal.SIGABRT)
        self.assertIn('exhausted()', result.stderr)

    def test_singleton_rejects_missing_duplicate_or_nonzero_iterator(self):
        for mode in ('uninitialized', 'duplicate', 'partial', 'consumed', 'discarded'):
            with self.subTest(mode=mode):
                result = subprocess.run([str(self.binary), mode], capture_output=True, text=True,
                                        cwd=self.directory.name)
                self.assertEqual(result.returncode, -signal.SIGABRT)
                self.assertIn('Assertion', result.stderr)

    def test_alternate_strategy_and_early_returns_through_base_interface(self):
        subprocess.run([str(self.binary), 'alternate'], check=True, capture_output=True)

    def test_construct_once_and_reuse_across_capacities(self):
        trace = parse_trace_lines([json.dumps(dict(
            block_size=1, input_length=len(blocks), hash_ids=blocks,
            timestamp=arrival, completion_timestamp=finish, conversation_id=str(i)))
            for i, (arrival, finish, blocks) in enumerate([
                (0,1,['A']), (0,2,['B']), (2,4,['A']), (2,2,['B']),
                (3,5,['A','C']), (6,7,['A']), (6,6,['B']), (8,9,['A'])])])
        with tempfile.TemporaryDirectory() as directory:
            files = _write_binary_trace(trace, Path(directory))
            base = ['--ids', str(files.ids), '--tokens', str(files.tokens),
                    '--total-blocks', str(len(trace.ids)), '--request-ends', str(files.request_ends),
                    '--request-count', str(trace.request_count), '--warmup-requests', '4',
                    '--request-arrivals', str(files.timing[0]),
                    '--request-completions', str(files.timing[1]), '--request-order', str(files.timing[2])]

            def run(*args, expected=1):
                completed = subprocess.run([str(self.binary), str(expected), *base, '--scheduler', 'recorded' if expected else 'sequential', *args],
                                           capture_output=True, text=True, check=True)
                return json.loads(completed.stdout)

            capacities = [0, 99, 1, 2, 1]
            batch = run('--policy', 'batch', '--capacities', ','.join(map(str,capacities)))
            for capacity, point in zip(capacities,batch['points']):
                single = run('--policy', 'all', '--capacity', str(capacity))
                # Native all and batch expose the same named result payloads.
                for policy in ('fifo','lru','lru-flat','hyper-clock','optimal','sampled-lru'):
                    self.assertEqual(point[policy], single[policy])
                sampled = run('--policy', 'sampled-lru', '--capacity', str(capacity))
                self.assertEqual(point['sampled-lru']['hitTokens'], sampled['hitTokens'])
                self.assertEqual(point['sampled-lru']['measurementStartRequest'], sampled['measurementStartRequest'])
            self.assertEqual(batch['points'][1]['sampled-lru']['measurementStartRequest'], -2)
            run('--policy', 'batch', '--policies', 'lru', '--capacities', '1,2', expected=0)
            run('--policy', 'fifo', '--capacity', '1', expected=0)


if __name__ == '__main__':
    unittest.main()
