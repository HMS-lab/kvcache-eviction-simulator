"""Execute every documented example against the real C++ implementation.

Only the temporary harness adds sample overrides and observation hooks. Production
source and algorithms are unchanged; no Python cache implementation is used.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src/kvcache_sim/resources/kv-cache-lab-native-sim.cc"

HOOKS = r'''
struct Snapshot {
  unsigned node; std::uint64_t time;
  std::vector<std::pair<unsigned, std::uint64_t>> slots;
};
std::vector<std::size_t> forced_sample;
std::vector<std::uint64_t> eligibility_ages;
std::vector<std::tuple<unsigned, std::uint64_t, bool>> reads;
std::vector<Snapshot> writes;
std::vector<std::pair<unsigned, std::uint64_t>> preload;
bool writing = false;
template<class Cache> void record_write(const Cache& cache, unsigned node, std::uint64_t time) {
  Snapshot s{node, time, {}};
  for (const auto& slot : cache.slots) s.slots.emplace_back(slot.node, slot.timestamp);
  writes.push_back(s);
}
'''

CASES = r'''
constexpr std::uint64_t S = 1000000;
using State = std::vector<std::pair<unsigned, std::uint64_t>>;
State state(const SampledCache& c) {
  State s; for (const auto& x : c.slots) s.emplace_back(x.node, x.timestamp); return s;
}
Options options(unsigned sample) { Options o; o.sampled_size = sample; o.sampled_threshold = 10*S; return o; }
SimulationInput input(std::vector<unsigned> ids, std::vector<unsigned> ends,
                      std::vector<std::uint64_t> arrivals, std::vector<std::uint64_t> finishes) {
  SimulationInput i; i.ids = ids; i.tokens.assign(ids.size(), 64); i.request_ends = ends;
  i.arrivals = arrivals; i.completions = finishes; i.timing_available = true;
  for (unsigned n = 0; n < ends.size(); ++n) i.completion_order.push_back(n);
  i.prefix = build_prefix_plan(i.ids, i.request_ends);
  return i;
}
void batch() {
  auto o = options(3); SampledCache c(4,o);
  c.write(1,0); c.write(2,4*S); c.write(3,10*S); c.write(4,18*S);
  forced_sample = {0,1,2}; c.write(5,20*S);
  assert((state(c) == State{{0,0},{5,20*S},{3,10*S},{4,18*S}}));
  assert((c.free == std::vector<std::size_t>{0}));
  // If another sample is taken, this sentinel would be consumed by the hook.
  forced_sample = {0}; c.write(6,21*S);
  assert((forced_sample == std::vector<std::size_t>{0}));
  assert((state(c) == State{{6,21*S},{5,20*S},{3,10*S},{4,18*S}}));
  assert(c.free.empty());
}
void fallback() {
  auto o = options(1); SampledCache c(3,o);
  c.write(1,0); c.write(2,15*S); c.write(3,19*S);
  forced_sample = {2}; eligibility_ages.clear(); c.write(4,20*S);
  assert((state(c) == State{{4,20*S},{2,15*S},{3,19*S}}));
  assert((eligibility_ages == std::vector<std::uint64_t>{10*S,10*S}));
  assert(c.threshold == 10*S);
}
void halving() {
  auto o = options(2); SampledCache c(2,o); c.write(1,8*S); c.write(2,8*S);
  eligibility_ages.clear(); c.write(3,10*S);
  assert((eligibility_ages == std::vector<std::uint64_t>{10*S,10*S,10*S,10*S,5*S,5*S,2500000,2500000,1250000}));
  assert((state(c) == State{{3,10*S},{2,8*S}}));
  eligibility_ages.clear(); c.write(4,10*S);
  assert((eligibility_ages == std::vector<std::uint64_t>{10*S,10*S,10*S,10*S,5*S,5*S,2500000,2500000,1250000,1250000}));
  assert((state(c) == State{{3,10*S},{4,10*S}}));
  eligibility_ages.clear(); c.write(5,10*S);
  assert(eligibility_ages.back() == 0);
  assert((state(c) == State{{5,10*S},{4,10*S}}));
  assert(c.threshold == 10*S);
}
void prefix() {
  auto i = input({1,2,3,1,2,3}, {3,6}, {10*S,13*S}, {12*S,14*S});
  const auto a=i.prefix.node_for_event[0], b=i.prefix.node_for_event[1], c=i.prefix.node_for_event[2];
  preload = {{a,0},{c,0}};
  auto o=options(3); o.warmup_requests=1;
  auto r=simulate_sampled(i,3,o);
  assert((reads == std::vector<std::tuple<unsigned,std::uint64_t,bool>>{
    {a,10*S,true},{b,10*S,false},{a,13*S,true},{b,13*S,true},{c,13*S,true}}));
  assert(writes.size()==6);
  assert((writes[0].slots == State{{a,12*S},{c,0}})); // C was not read/refreshed beyond B's hole.
  assert(writes[0].node==a && writes[1].node==b && writes[2].node==c);
  assert((writes[2].slots == State{{a,12*S},{c,12*S},{b,12*S}}));
  assert(writes[3].node==a && writes[4].node==b && writes[5].node==c);
  assert((writes[5].slots == State{{a,14*S},{c,14*S},{b,14*S}}));
  assert(r.hit_tokens==192 && r.total_tokens==192 && r.hit_rate()==1.0);
  auto branches=build_prefix_plan({1,2,3,4,2,3},{3,6});
  assert(branches.node_for_event[1]!=branches.node_for_event[4]);
  assert(branches.node_for_event[2]!=branches.node_for_event[5]);
}
void clock_example() {
  auto i=input({1,2,2,2},{1,2,3,4},{0,0,2*S,5*S},{S,5*S,6*S,7*S});
  auto o=options(1); o.warmup_requests=2;
  auto r=simulate_sampled(i,1,o);
  assert((reads == std::vector<std::tuple<unsigned,std::uint64_t,bool>>{
    {1,0,false},{2,0,false},{2,2*S,false},{2,5*S,true}}));
  assert(writes.size()==4);
  assert(writes[0].node==1 && writes[0].time==S);
  assert(writes[1].node==2 && writes[1].time==5*S);
  assert(writes[2].node==2 && writes[2].time==6*S);
  assert(writes[3].node==2 && writes[3].time==7*S);
  assert((writes[3].slots==State{{2,7*S}}));
  assert(r.hit_tokens==64 && r.total_tokens==128 && r.hit_rate()==0.5);
}
int main(int argc,char** argv) {
  assert(argc==2); std::string name=argv[1];
  if(name=="batch") batch(); else if(name=="fallback") fallback();
  else if(name=="halving") halving(); else if(name=="prefix") prefix();
  else if(name=="clock") clock_example(); else assert(false);
}
'''


@unittest.skipUnless(shutil.which("c++"), "C++ compiler required for native examples")
class NativeDocumentedExamplesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="sampled-native-examples-")
        cls.addClassCleanup(cls.directory.cleanup)
        source = SOURCE.read_text()
        # Hooks observe the actual implementation; they do not replace policy logic.
        anchor = "struct SampledSlot {"
        source = source.replace(anchor, HOOKS + "\n" + anchor, 1)
        source = source.replace("rng(o.sampled_seed) {}", """rng(o.sampled_seed) {
          for (auto item : preload) write(item.first,item.second);
          preload.clear(); reads.clear(); writes.clear();
        }""", 1)
        begin = source.index("struct SampledCache {")
        end = source.index("Result simulate_sampled(", begin)
        cache = source[begin:end]
        cache = cache.replace("if (it == entries.end()) return false;", """if (it == entries.end()) {
          if (!writing) reads.emplace_back(node,now,false); return false;
        }
        if (!writing) reads.emplace_back(node,now,true);""", 1)
        cache = cache.replace("return slot.node != 0 &&", "eligibility_ages.push_back(age);\n    return slot.node != 0 &&", 1)
        cache = cache.replace("rng);", """rng);
    if (!forced_sample.empty()) {
      sample.clear(); for (auto index : forced_sample) sample.push_back(slots.at(index));
      forced_sample.clear();
    }""", 1)
        cache = cache.replace("void write(std::uint32_t node, std::uint64_t now) {", """void write(std::uint32_t node, std::uint64_t now) {
    struct Capture {
      SampledCache& cache; unsigned node; std::uint64_t now;
      ~Capture() { record_write(cache,node,now); writing=false; }
    } capture{*this,node,now};
    writing=true;""", 1)
        source = source[:begin] + cache + source[end:]
        cpp = Path(cls.directory.name) / "examples.cc"
        cls.binary = Path(cls.directory.name) / "examples"
        cpp.write_text("#include <cassert>\n#include <utility>\n#define main simulator_main\n" + source + "\n#undef main\n" + CASES)
        compiled = subprocess.run(["c++", "-std=c++17", "-O0", str(cpp), "-o", str(cls.binary)],
                                  capture_output=True, text=True)
        if compiled.returncode:
            raise AssertionError(compiled.stderr)

    def run_example(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_batch_eviction_and_free_slot_reuse(self):
        self.run_example("batch")

    def test_sample_miss_falls_back_to_first_eligible_slot(self):
        self.run_example("fallback")

    def test_threshold_halving_resets_and_reaches_zero(self):
        self.run_example("halving")

    def test_prefix_hole_and_completion_refresh(self):
        self.run_example("prefix")

    def test_clock_overlap_and_completion_before_arrival(self):
        self.run_example("clock")


if __name__ == "__main__":
    unittest.main()
