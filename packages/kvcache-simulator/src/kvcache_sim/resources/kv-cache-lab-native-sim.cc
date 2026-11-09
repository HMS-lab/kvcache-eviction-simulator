#include <algorithm>
#include <atomic>
#include <mutex>
#include <cassert>
#include <optional>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <list>
#include <memory>
#include <queue>
#include <random>
#include <tuple>
#include <thread>
#include <iterator>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {

constexpr std::size_t kChunkEvents = 1'000'000;

struct ProgressState {
  std::atomic<std::uint64_t> completed{0}, running{0};
};

struct Options {
  std::string policy;
  std::string scheduler;
  std::string ids_path;
  std::string tokens_path;
  std::string next_path;
  std::string request_ends_path;
  std::uint64_t total_blocks = 0;
  std::uint64_t warmup_event_start = 0;
  std::uint64_t request_count = 0;
  std::uint64_t warmup_requests = 0;
  std::uint32_t capacity = 0;
  std::uint32_t jobs = 1;
  std::uint64_t hyper_clock_max_score = 3;
  std::vector<std::uint32_t> capacities;
  std::vector<std::string> policies;
  bool progress = false;
  std::uint64_t progress_done = 0, progress_total = 1;
  std::string progress_label;
  ProgressState* progress_state = nullptr;
  std::int64_t progress_worker = -1;
  std::uint64_t progress_job = 0;
  bool progress_finished = false;
  std::string arrivals_path, completions_path, order_path;
  std::uint64_t sampled_threshold = 10000000, sampled_seed = 0;
  std::vector<std::uint32_t> sampled_sizes{16};
  std::uint32_t sampled_size = 16, sampled_max_evictions = 0xffffffffU;
};

struct Result {
  std::uint64_t hit_tokens = 0;
  std::uint64_t total_tokens = 0;
  std::uint64_t useful_cache_block_samples = 0;
  std::uint64_t useful_cache_samples = 0;
  std::int64_t measurement_start_request = -1;

  double hit_rate() const {
    return total_tokens == 0 ? 0.0 : static_cast<double>(hit_tokens) / static_cast<double>(total_tokens);
  }

  double useful_cache_rate(std::uint64_t capacity) const {
    return capacity == 0 || useful_cache_samples == 0
      ? 0.0
      : static_cast<double>(useful_cache_block_samples) / static_cast<double>(useful_cache_samples * capacity);
  }
};

std::uint64_t parse_u64(const char* value, const std::string& name) {
  if (!value || *value == '\0') throw std::runtime_error("Invalid integer for " + name);
  char* end = nullptr;
  const unsigned long long parsed = std::strtoull(value, &end, 10);
  if (end && *end != '\0') throw std::runtime_error("Invalid integer for " + name);
  return static_cast<std::uint64_t>(parsed);
}

std::uint64_t parse_clock_score(const std::string& value) {
  std::uint64_t score = 0;
  const auto result = std::from_chars(value.data(), value.data() + value.size(), score);
  if (result.ec != std::errc{} || result.ptr != value.data() + value.size() || score == 0) {
    throw std::runtime_error("--hyper-clock-max-score must be a positive integer in 1..2^64-1");
  }
  return score;
}

std::uint64_t parse_sampled_u64(const std::string& value, const std::string& name) {
  std::uint64_t parsed = 0;
  const auto result = std::from_chars(value.data(), value.data() + value.size(), parsed);
  if (result.ec != std::errc{} || result.ptr != value.data() + value.size() || value.empty())
    throw std::runtime_error("Invalid unsigned integer for " + name);
  return parsed;
}

std::vector<std::uint32_t> parse_u32_csv(const std::string& value, const std::string& name) {
  std::vector<std::uint32_t> result;
  std::stringstream stream(value);
  std::string part;
  while (std::getline(stream, part, ',')) {
    if (part.empty()) continue;
    const std::uint64_t parsed = parse_u64(part.c_str(), name);
    if (parsed > 0xffffffffULL) throw std::runtime_error(name + " contains a value larger than uint32");
    result.push_back(static_cast<std::uint32_t>(parsed));
  }
  return result;
}

std::vector<std::string> parse_string_csv(const std::string& value) {
  std::vector<std::string> result;
  std::stringstream stream(value);
  std::string part;
  while (std::getline(stream, part, ',')) {
    if (!part.empty()) result.push_back(part);
  }
  return result;
}

Options parse_args(int argc, char** argv) {
  Options options;
  for (int index = 1; index < argc; index += 1) {
    const std::string arg = argv[index];
    auto require_value = [&](const std::string& name) -> const char* {
      if (index + 1 >= argc) throw std::runtime_error("Missing value for " + name);
      return argv[++index];
    };
    if (arg == "--policy") options.policy = require_value(arg);
    else if (arg == "--jobs") {
      const auto n = parse_sampled_u64(require_value(arg), arg);
      if (!n || n > 0xffffffffULL) throw std::runtime_error("--jobs must be in 1..2^32-1");
      options.jobs = static_cast<std::uint32_t>(n);
    }
    else if (arg == "--scheduler") options.scheduler = require_value(arg);
    else if (arg == "--ids") options.ids_path = require_value(arg);
    else if (arg == "--tokens") options.tokens_path = require_value(arg);
    else if (arg == "--next") options.next_path = require_value(arg);
    else if (arg == "--request-ends") options.request_ends_path = require_value(arg);
    else if (arg == "--total-blocks") options.total_blocks = parse_u64(require_value(arg), arg);
    else if (arg == "--warmup-event-start") options.warmup_event_start = parse_u64(require_value(arg), arg);
    else if (arg == "--request-count") options.request_count = parse_u64(require_value(arg), arg);
    else if (arg == "--warmup-requests") options.warmup_requests = parse_u64(require_value(arg), arg);
    else if (arg == "--capacity") options.capacity = static_cast<std::uint32_t>(parse_u64(require_value(arg), arg));
    else if (arg == "--capacities") options.capacities = parse_u32_csv(require_value(arg), arg);
    else if (arg == "--policies") options.policies = parse_string_csv(require_value(arg));
    else if (arg == "--hyper-clock-max-score") options.hyper_clock_max_score = parse_clock_score(require_value(arg));
    else if (arg == "--request-arrivals") options.arrivals_path = require_value(arg);
    else if (arg == "--request-completions") options.completions_path = require_value(arg);
    else if (arg == "--request-order") options.order_path = require_value(arg);
    else if (arg == "--sampled-lru-threshold-us") options.sampled_threshold = parse_sampled_u64(require_value(arg), arg);
    else if (arg == "--sampled-lru-seed") options.sampled_seed = parse_sampled_u64(require_value(arg), arg);
    else if (arg == "--sampled-lru-sample-size") {
      std::string value = require_value(arg);
      options.sampled_sizes.clear();
      std::size_t start = 0;
      do {
        auto end = value.find(',', start);
        auto part = value.substr(start, end == std::string::npos ? end : end-start);
        auto first = part.find_first_not_of(" \t\r\n");
        auto last = part.find_last_not_of(" \t\r\n");
        if (first == std::string::npos) throw std::runtime_error("Empty sampled sample size");
        auto n = parse_sampled_u64(part.substr(first, last-first+1), arg);
        if (!n || n > 0xffffffffULL) throw std::runtime_error("Invalid sampled sample size");
        if (std::find(options.sampled_sizes.begin(), options.sampled_sizes.end(), n) == options.sampled_sizes.end())
          options.sampled_sizes.push_back(n);
        if (end == std::string::npos) break;
        start = end + 1;
      } while (true);
      options.sampled_size = options.sampled_sizes.front();
    }
    else if (arg == "--sampled-lru-max-evictions") {
      auto n = parse_sampled_u64(require_value(arg), arg);
      if (!n || n > 0xffffffffULL) throw std::runtime_error("Invalid sampled max evictions");
      options.sampled_max_evictions = n;
    }
    else if (arg == "--progress") options.progress = true;
    else throw std::runtime_error("Unknown argument: " + arg);
  }
  if (options.policy.empty() || options.ids_path.empty() || options.tokens_path.empty() || options.total_blocks == 0) {
    throw std::runtime_error("Usage: kv-cache-lab-native-sim --scheduler recorded|sequential --policy fifo|lru|lru-flat-touch-on-put|flat-lru-memcache|hyper-clock|optimal|sampling-lru-touch-on-put|sampling-lru-touch-on-get|all|batch|ceiling --ids PATH --tokens PATH --total-blocks N --request-ends PATH --request-count N --capacity N [--hyper-clock-max-score N] [--next PATH]");
  }
  if (options.policy != "build-next" && (options.request_ends_path.empty() || options.request_count == 0)) {
    throw std::runtime_error("--request-ends and --request-count are required");
  }
  if (options.policy != "build-next" && options.scheduler != "sequential" && options.scheduler != "recorded") {
    throw std::runtime_error("Select --scheduler sequential or --scheduler recorded");
  }
  if (options.sampled_sizes.size() > 1 &&
      (options.policy == "all" || options.policy.find("sampling-lru-") == 0)) {
    throw std::runtime_error("Multiple sample sizes require --policy batch with --capacities");
  }
  return options;
}

std::mutex progress_output_mutex;

void report_progress(const Options& options, std::uint64_t done, std::uint64_t total,
                     const std::string& label) {
  if (!options.progress) return;
  std::unique_lock<std::mutex> lock(progress_output_mutex, std::try_to_lock);
  if (!lock.owns_lock()) return;
  // Read after locking so concurrent messages cannot publish an older count.
  if (options.progress_state) done = options.progress_state->completed.load(std::memory_order_relaxed);
  if (options.progress_state) {
    std::cerr << "KV_JOB_PROGRESS " << done << " " << total << " "
              << options.progress_state->running.load(std::memory_order_relaxed) << " "
              << options.progress_worker << " " << options.progress_job << " "
              << options.progress_finished << " " << label << "\n";
  } else if (options.progress_worker >= 0) {
    std::cerr << "KV_WORKER_PROGRESS " << done << " " << total << " "
              << options.progress_worker << " " << options.progress_job << " " << label << "\n";
  } else {
    std::cerr << "KV_PROGRESS " << done << " " << total << " " << label << "\n";
  }
}

std::vector<std::uint32_t> load_request_ends(const Options& options) {
  std::vector<std::uint32_t> request_ends(options.request_count);
  std::ifstream in(options.request_ends_path, std::ios::binary);
  if (!in) throw std::runtime_error("Failed to open request-ends file");
  in.read(reinterpret_cast<char*>(request_ends.data()), static_cast<std::streamsize>(request_ends.size() * sizeof(std::uint32_t)));
  if (in.gcount() != static_cast<std::streamsize>(request_ends.size() * sizeof(std::uint32_t))) {
    throw std::runtime_error("Short read while reading request-ends file");
  }
  return request_ends;
}

std::vector<std::uint32_t> load_ids_all(const Options& options) {
  std::vector<std::uint32_t> ids(options.total_blocks);
  std::ifstream in(options.ids_path, std::ios::binary);
  if (!in) throw std::runtime_error("Failed to open ids file");
  in.read(reinterpret_cast<char*>(ids.data()), static_cast<std::streamsize>(ids.size() * sizeof(std::uint32_t)));
  if (in.gcount() != static_cast<std::streamsize>(ids.size() * sizeof(std::uint32_t))) {
    throw std::runtime_error("Short read while reading ids file");
  }
  return ids;
}

std::vector<std::uint16_t> load_tokens_all(const Options& options) {
  std::vector<std::uint16_t> tokens(options.total_blocks);
  std::ifstream in(options.tokens_path, std::ios::binary);
  if (!in) throw std::runtime_error("Failed to open tokens file");
  in.read(reinterpret_cast<char*>(tokens.data()), static_cast<std::streamsize>(tokens.size() * sizeof(std::uint16_t)));
  if (in.gcount() != static_cast<std::streamsize>(tokens.size() * sizeof(std::uint16_t))) {
    throw std::runtime_error("Short read while reading tokens file");
  }
  return tokens;
}

template <typename ChunkFn>
void scan_chunks(const Options& options, ChunkFn&& fn) {
  std::ifstream ids(options.ids_path, std::ios::binary);
  if (!ids) throw std::runtime_error("Failed to open ids file");
  std::vector<std::uint32_t> id_buffer(kChunkEvents);
  for (std::uint64_t start = 0; start < options.total_blocks; start += kChunkEvents) {
    const std::size_t count = static_cast<std::size_t>(std::min<std::uint64_t>(kChunkEvents, options.total_blocks - start));
    ids.read(reinterpret_cast<char*>(id_buffer.data()), static_cast<std::streamsize>(count * sizeof(std::uint32_t)));
    if (ids.gcount() != static_cast<std::streamsize>(count * sizeof(std::uint32_t))) {
      throw std::runtime_error("Short read while scanning event stream");
    }
    fn(start, count, id_buffer.data());
  }
}

void build_next_file(const Options& options) {
  if (options.next_path.empty()) throw std::runtime_error("--next is required for build-next");
  if (options.total_blocks + 1ULL > 0xffffffffULL) throw std::runtime_error("total blocks exceed uint32 next-use encoding");

  std::ifstream ids(options.ids_path, std::ios::binary);
  std::fstream next(options.next_path, std::ios::binary | std::ios::in | std::ios::out | std::ios::trunc);
  if (!ids) throw std::runtime_error("Failed to open ids file");
  if (!next) throw std::runtime_error("Failed to open next file");

  next.seekp(static_cast<std::streamoff>(options.total_blocks * sizeof(std::uint32_t) - 1));
  const char zero = 0;
  next.write(&zero, 1);
  next.flush();

  std::unordered_map<std::uint32_t, std::uint32_t> last_seen;
  std::vector<std::uint32_t> id_buffer(kChunkEvents);
  std::vector<std::uint32_t> next_buffer(kChunkEvents);
  const std::uint32_t never = static_cast<std::uint32_t>(options.total_blocks + 1ULL);

  for (std::uint64_t end = options.total_blocks; end > 0; end -= std::min<std::uint64_t>(kChunkEvents, end)) {
    const std::uint64_t start = end > kChunkEvents ? end - kChunkEvents : 0;
    const std::size_t count = static_cast<std::size_t>(end - start);
    ids.seekg(static_cast<std::streamoff>(start * sizeof(std::uint32_t)));
    ids.read(reinterpret_cast<char*>(id_buffer.data()), static_cast<std::streamsize>(count * sizeof(std::uint32_t)));
    if (ids.gcount() != static_cast<std::streamsize>(count * sizeof(std::uint32_t))) {
      throw std::runtime_error("Short read while building next-use file");
    }
    for (std::size_t reverse = count; reverse > 0; reverse -= 1) {
      const std::size_t index = reverse - 1;
      const std::uint32_t id = id_buffer[index];
      const auto found = last_seen.find(id);
      next_buffer[index] = found == last_seen.end() ? never : found->second;
      last_seen[id] = static_cast<std::uint32_t>(start + index);
    }
    next.seekp(static_cast<std::streamoff>(start * sizeof(std::uint32_t)));
    next.write(reinterpret_cast<const char*>(next_buffer.data()), static_cast<std::streamsize>(count * sizeof(std::uint32_t)));
  }
}

struct PrefixPlan {
  std::vector<std::uint32_t> node_for_event;
  std::vector<std::uint32_t> next_request_for_event;
  std::vector<std::uint32_t> parent;
};

struct SimulationInput {
  std::vector<std::uint32_t> ids;
  std::vector<std::uint16_t> tokens;
  std::vector<std::uint32_t> request_ends;
  PrefixPlan prefix;
  std::vector<std::uint64_t> arrivals, completions;
  std::vector<std::uint32_t> completion_order;
  bool timing_available = false;
  std::uint32_t unique_blocks = 0;
};

enum class ScheduledEventKind { Arrival, Completion };

struct ScheduledEvent {
  std::uint32_t request;
  ScheduledEventKind kind;
  std::uint64_t timestamp;
};

// One selected, read-only schedule, constructed before simulations start.
// Its owner and the referenced SimulationInput must outlive all cursors.
class RequestScheduler {
 public:
  virtual ~RequestScheduler() { instance_ = nullptr; }
  RequestScheduler(const RequestScheduler&) = delete;
  RequestScheduler& operator=(const RequestScheduler&) = delete;

  static const RequestScheduler& get() {
    assert(instance_);
    return *instance_;
  }
  virtual std::uint64_t size() const = 0;
  virtual ScheduledEvent event_at(std::uint64_t index) const = 0;

 protected:
  RequestScheduler() {
    assert(!instance_);
    instance_ = this;
  }

 private:
  inline static const RequestScheduler* instance_ = nullptr;
};

// Each simulation owns a fresh cursor. Only its active pointer is thread-local;
// the schedule and event storage are shared through const references.
class SchedulerCursor {
 public:
  SchedulerCursor() : schedule_(RequestScheduler::get()) {
    assert(!active_);
    assert(position_ == 0);
    active_ = this;
  }
  ~SchedulerCursor() {
    assert(active_ == this);
    active_ = nullptr;
  }
  SchedulerCursor(const SchedulerCursor&) = delete;
  SchedulerCursor& operator=(const SchedulerCursor&) = delete;

  static SchedulerCursor& get() {
    assert(active_);
    assert(active_->position_ == 0);
    return *active_;
  }
  std::optional<ScheduledEvent> next_event() {
    if (exhausted()) return std::nullopt;
    return schedule_.event_at(position_++);
  }
  bool exhausted() const { return position_ == schedule_.size(); }

 private:
  const RequestScheduler& schedule_;
  std::uint64_t position_ = 0;
  inline static thread_local SchedulerCursor* active_ = nullptr;
};

// Generate adjacent read/write events without storing a timeline. Timestamps are unused.
class SequentialRequestScheduler final : public RequestScheduler {
 public:
  explicit SequentialRequestScheduler(std::uint32_t request_count)
      : event_count_(2ULL * request_count) {}

  std::uint64_t size() const override { return event_count_; }
  ScheduledEvent event_at(std::uint64_t index) const override {
    assert(index < size());
    return ScheduledEvent{static_cast<std::uint32_t>(index / 2),
      index % 2 ? ScheduledEventKind::Completion : ScheduledEventKind::Arrival, 0};
  }

 private:
  const std::uint64_t event_count_;
};

class RecordedTimeScheduler final : public RequestScheduler {
 public:
  RecordedTimeScheduler(const std::vector<std::uint64_t>& arrivals,
                        const std::vector<std::uint64_t>& completions,
                        const std::vector<std::uint32_t>& completion_order)
      : arrivals_(arrivals), completions_(completions) {
    assert(arrivals.size() == completions.size() && arrivals.size() == completion_order.size());
    events_.reserve(arrivals.size() * 2);
    using Pending = std::tuple<std::uint64_t, std::uint32_t, std::uint32_t>;
    std::priority_queue<Pending, std::vector<Pending>, std::greater<Pending>> pending;
    std::size_t cursor = 0;
    auto complete = [&](std::uint64_t now) {
      while (!pending.empty() && std::get<0>(pending.top()) == now) {
        append(std::get<2>(pending.top()), ScheduledEventKind::Completion);
        pending.pop();
      }
    };
    while (cursor < arrivals.size() || !pending.empty()) {
      const auto arrival = cursor < arrivals.size() ? arrivals[cursor] : UINT64_MAX;
      const auto now = std::min(arrival, pending.empty() ? UINT64_MAX : std::get<0>(pending.top()));
      complete(now);
      while (cursor < arrivals.size() && arrivals[cursor] == now) {
        append(static_cast<std::uint32_t>(cursor), ScheduledEventKind::Arrival);
        pending.emplace(completions[cursor], completion_order[cursor], static_cast<std::uint32_t>(cursor));
        ++cursor;
      }
      complete(now);
    }
  }

  std::uint64_t size() const override { return events_.size(); }
  ScheduledEvent event_at(std::uint64_t index) const override {
    assert(index < size());
    const auto encoded = events_[index];
    const auto request = static_cast<std::uint32_t>(encoded >> 1);
    const auto kind = (encoded & 1) ? ScheduledEventKind::Completion : ScheduledEventKind::Arrival;
    return ScheduledEvent{request, kind,
      kind == ScheduledEventKind::Arrival ? arrivals_[request] : completions_[request]};
  }

 private:
  void append(std::uint32_t request, ScheduledEventKind kind) {
    events_.push_back((static_cast<std::uint64_t>(request) << 1)
                     | (kind == ScheduledEventKind::Completion ? 1ULL : 0ULL));
  }

  const std::vector<std::uint64_t>& arrivals_;
  const std::vector<std::uint64_t>& completions_;
  std::vector<std::uint64_t> events_;
};

PrefixPlan build_prefix_plan(const std::vector<std::uint32_t>& ids,
                             const std::vector<std::uint32_t>& request_ends) {
  std::uint64_t previous_end = 0;
  for (const auto end : request_ends) {
    if (end < previous_end || end > ids.size()) {
      throw std::runtime_error("Invalid request end offset " + std::to_string(end) +
        ": offsets must be nondecreasing and within --total-blocks (" + std::to_string(ids.size()) + ")");
    }
    previous_end = end;
  }
  if (previous_end != ids.size()) {
    throw std::runtime_error("Final request end offset must equal --total-blocks");
  }
  PrefixPlan plan;
  plan.node_for_event.resize(ids.size());
  plan.next_request_for_event.resize(ids.size());
  plan.parent.push_back(0);

  std::unordered_map<std::uint64_t, std::uint32_t> edge_to_node;
  edge_to_node.reserve(std::min<std::size_t>(ids.size(), 8'000'000ULL));

  std::uint64_t start = 0;
  for (std::uint32_t end : request_ends) {
    std::uint32_t parent = 0;
    for (std::uint64_t index = start; index < end; index += 1) {
      const std::uint64_t key = (static_cast<std::uint64_t>(parent) << 32) | ids[index];
      auto found = edge_to_node.find(key);
      std::uint32_t node = 0;
      if (found == edge_to_node.end()) {
        node = static_cast<std::uint32_t>(plan.parent.size());
        edge_to_node.emplace(key, node);
        plan.parent.push_back(parent);
      } else {
        node = found->second;
      }
      plan.node_for_event[index] = node;
      parent = node;
    }
    start = end;
  }

  const std::uint32_t never_request = static_cast<std::uint32_t>(request_ends.size() + 1ULL);
  std::vector<std::uint32_t> last_use(plan.parent.size(), never_request);
  for (std::uint64_t reverse = request_ends.size(); reverse > 0; reverse -= 1) {
    const std::uint64_t request = reverse - 1;
    const std::uint64_t request_start = request == 0 ? 0 : request_ends[request - 1];
    const std::uint64_t request_end = request_ends[request];
    for (std::uint64_t index = request_start; index < request_end; index += 1) {
      plan.next_request_for_event[index] = last_use[plan.node_for_event[index]];
    }
    for (std::uint64_t index = request_start; index < request_end; index += 1) {
      last_use[plan.node_for_event[index]] = static_cast<std::uint32_t>(request);
    }
  }
  return plan;
}

template<typename T>
std::vector<T> load_timing(const std::string& path, std::size_t count) {
  std::ifstream stream(path, std::ios::binary | std::ios::ate);
  if (!stream || stream.tellg() != static_cast<std::streamoff>(count * sizeof(T)))
    throw std::runtime_error("Invalid timing file size: " + path);
  stream.seekg(0);
  std::vector<T> values(count);
  stream.read(reinterpret_cast<char*>(values.data()), count * sizeof(T));
  if (!stream) throw std::runtime_error("Cannot read timing file: " + path);
  return values;
}

SimulationInput load_simulation_input(const Options& options) {
  if (options.scheduler == "recorded" && (options.arrivals_path.empty()
      || options.completions_path.empty() || options.order_path.empty()))
    throw std::runtime_error("recorded scheduler requires complete request timing data");
  SimulationInput input;
  input.ids = load_ids_all(options);
  input.tokens = load_tokens_all(options);
  input.request_ends = load_request_ends(options);
  if (options.scheduler == "recorded") {
    input.arrivals = load_timing<std::uint64_t>(options.arrivals_path, options.request_count);
    input.completions = load_timing<std::uint64_t>(options.completions_path, options.request_count);
    input.completion_order = load_timing<std::uint32_t>(options.order_path, options.request_count);
    std::vector<bool> ranks(options.request_count, false);
    for (std::size_t i = 0; i < options.request_count; ++i) {
      if ((i && input.arrivals[i] < input.arrivals[i-1]) || input.completions[i] < input.arrivals[i]
          || input.arrivals[i] == UINT64_MAX || input.completions[i] == UINT64_MAX
          || input.completion_order[i] >= options.request_count || ranks[input.completion_order[i]])
        throw std::runtime_error("Invalid request timing or order");
      ranks[input.completion_order[i]] = true;
    }
    input.timing_available = true;
  }
  input.prefix = build_prefix_plan(input.ids, input.request_ends);
  input.unique_blocks = static_cast<std::uint32_t>(input.prefix.parent.size() - 1);
  return input;
}

std::uint64_t total_tokens(const SimulationInput& input, std::uint64_t start_request = 0) {
  std::uint64_t total = 0;
  const std::uint64_t request_count = input.request_ends.size();
  for (std::uint64_t request = std::min(start_request, request_count); request < request_count; request += 1) {
    const std::uint64_t start = request == 0 ? 0 : input.request_ends[request - 1];
    const std::uint64_t end = input.request_ends[request];
    for (std::uint64_t index = start; index < end; index += 1) total += input.tokens[index];
  }
  return total;
}

Result no_cache_result(const SimulationInput& input, const Options& options) {
  Result result;
  result.total_tokens = total_tokens(input, options.warmup_requests);
  result.measurement_start_request = static_cast<std::int64_t>(options.warmup_requests);
  return result;
}

Result underfilled_result(const SimulationInput& input, const Options& options) {
  Result result;
  result.total_tokens = total_tokens(input, options.warmup_requests);
  result.measurement_start_request = -2;
  return result;
}

Result simulate_ceiling(const SimulationInput& input, const Options& options) {
  Result result;
  std::vector<std::uint8_t> seen(input.prefix.parent.size(), 0);
  SchedulerCursor cursor;
  auto& scheduler = SchedulerCursor::get();
  while (const auto event = scheduler.next_event()) {
    const auto request = event->request;
    const std::uint64_t start = request ? input.request_ends[request - 1] : 0;
    const std::uint64_t end = input.request_ends[request];
    if (event->kind == ScheduledEventKind::Arrival) {
      const bool measured = request >= options.warmup_requests;
      bool prefix_alive = true;
      for (std::uint64_t index = start; index < end; index += 1) {
        const std::uint32_t node = input.prefix.node_for_event[index];
        const bool hit = seen[node] != 0;
        if (measured) result.total_tokens += input.tokens[index];
        if (measured && prefix_alive && hit) {
          result.hit_tokens += input.tokens[index];
        } else if (!hit) {
          prefix_alive = false;
        }
      }
    } else {
      for (std::uint64_t index = start; index < end; index += 1) {
        seen[input.prefix.node_for_event[index]] = 1;
      }
    }
  }
  result.measurement_start_request = static_cast<std::int64_t>(options.warmup_requests);
  return result;
}

Result simulate_fifo(const SimulationInput& input, std::uint32_t capacity, const Options& options) {
  if (capacity == 0 || input.ids.empty()) return no_cache_result(input, options);
  Result result;
  std::vector<std::uint8_t> in_cache(input.prefix.parent.size(), 0);
  std::vector<std::uint32_t> queue;
  queue.reserve(std::min<std::size_t>(input.ids.size(), static_cast<std::size_t>(capacity) * 4ULL + 1024ULL));
  std::size_t head = 0;
  std::uint32_t cache_size = 0;
  bool full_before_measurement = false;

  SchedulerCursor cursor;
  auto& scheduler = SchedulerCursor::get();
  while (const auto event = scheduler.next_event()) {
    const auto request = event->request;
    const std::uint64_t start = request ? input.request_ends[request - 1] : 0;
    const std::uint64_t end = input.request_ends[request];
    if (event->kind == ScheduledEventKind::Arrival) {
      if (request >= options.warmup_requests && !full_before_measurement) {
        return underfilled_result(input, options);
      }
      const bool measured = request >= options.warmup_requests;
      bool prefix_alive = true;
      for (std::uint64_t index = start; index < end; index += 1) {
        const std::uint32_t id = input.prefix.node_for_event[index];
        const bool hit = in_cache[id] != 0;
        if (measured) {
          result.total_tokens += input.tokens[index];
          if (prefix_alive && hit) result.hit_tokens += input.tokens[index];
        }
        if (!hit) prefix_alive = false;
      }
    } else {
      for (std::uint64_t index = start; index < end; index += 1) {
        const std::uint32_t id = input.prefix.node_for_event[index];
        if (in_cache[id]) continue;
        if (cache_size >= capacity) {
          while (head < queue.size()) {
            const std::uint32_t victim = queue[head++];
            if (in_cache[victim]) {
              in_cache[victim] = 0;
              cache_size -= 1;
              break;
            }
          }
        }
        if (cache_size < capacity) {
          in_cache[id] = 1;
          cache_size += 1;
          queue.push_back(id);
          if (cache_size >= capacity && request < options.warmup_requests) full_before_measurement = true;
        }
        if (head > 1'000'000 && head * 2 > queue.size()) {
          queue.erase(queue.begin(), queue.begin() + static_cast<std::ptrdiff_t>(head));
          head = 0;
        }
      }
    }
  }
  if (!full_before_measurement || result.total_tokens == 0) return underfilled_result(input, options);
  result.measurement_start_request = static_cast<std::int64_t>(options.warmup_requests);
  return result;
}

// Node IDs retain prefix identity; eviction ignores parent/child relationships.
class FlatLruCache {
 public:
  explicit FlatLruCache(std::uint32_t capacity, bool touch_on_put = true)
      : capacity_(capacity), touch_on_put_(touch_on_put) {}

  bool contains(std::uint32_t node) const { return entries_.find(node) != entries_.end(); }
  std::size_t size() const { return entries_.size(); }

  void touch(std::uint32_t node) {
    order_.splice(order_.begin(), order_, entries_.at(node));
  }

  void write(std::uint32_t node) {
    if (capacity_ == 0) return;
    if (contains(node)) {
      if (touch_on_put_) touch(node);
      return;
    }
    if (size() >= capacity_) {
      entries_.erase(order_.back());
      order_.pop_back();
    }
    order_.push_front(node);
    entries_.emplace(node, order_.begin());
  }

 private:
  std::uint32_t capacity_;
  bool touch_on_put_;
  std::list<std::uint32_t> order_;  // Most recently used first.
  std::unordered_map<std::uint32_t, std::list<std::uint32_t>::iterator> entries_;
};

std::uint64_t read_flat_lru_prefix(
    FlatLruCache& cache, const SimulationInput& input, std::uint64_t start, std::uint64_t end) {
  std::uint64_t hit_tokens = 0;
  for (std::uint64_t index = start; index < end; index += 1) {
    const std::uint32_t node = input.prefix.node_for_event[index];
    if (!cache.contains(node)) break;
    cache.touch(node);
    hit_tokens += input.tokens[index];
  }
  return hit_tokens;
}

void write_flat_lru_request(
    FlatLruCache& cache, const SimulationInput& input, std::uint64_t start, std::uint64_t end) {
  // Check live membership for the whole request, independently of the read.
  // Do not restart if a later insertion evicts an earlier node.
  for (std::uint64_t index = start; index < end; index += 1) {
    cache.write(input.prefix.node_for_event[index]);
  }
}

Result simulate_flat_lru(const SimulationInput& input, std::uint32_t capacity, const Options& options, bool touch_on_put = true) {
  if (capacity == 0 || input.ids.empty()) return no_cache_result(input, options);
  Result result;
  FlatLruCache cache(capacity, touch_on_put);
  bool full_before_measurement = false;
  SchedulerCursor cursor;
  auto& scheduler = SchedulerCursor::get();
  while (const auto event = scheduler.next_event()) {
    const auto request = event->request;
    const std::uint64_t start = request ? input.request_ends[request - 1] : 0;
    const std::uint64_t end = input.request_ends[request];
    if (event->kind == ScheduledEventKind::Arrival) {
      if (request >= options.warmup_requests && !full_before_measurement) {
        return underfilled_result(input, options);
      }
      const std::uint64_t request_hits = read_flat_lru_prefix(cache, input, start, end);
      if (request >= options.warmup_requests) {
        result.hit_tokens += request_hits;
        for (std::uint64_t index = start; index < end; index += 1) result.total_tokens += input.tokens[index];
      }
    } else {
      write_flat_lru_request(cache, input, start, end);
      if (request < options.warmup_requests && cache.size() >= capacity) full_before_measurement = true;
    }
  }
  if (!full_before_measurement || result.total_tokens == 0) return underfilled_result(input, options);
  result.measurement_start_request = static_cast<std::int64_t>(options.warmup_requests);
  return result;
}

// Internal state mirrors the Python cache. Node zero is the uncached trie root.
struct HyperClockCache {
  struct Slot {
    std::uint32_t node = 0;
    std::uint64_t score = 0;
  };

  std::uint32_t capacity;
  std::uint64_t max_score;
  std::unordered_map<std::uint32_t, std::size_t> entries;
  std::vector<Slot> slots;
  std::vector<std::size_t> free_slots;
  std::size_t hand = 0;

  explicit HyperClockCache(std::uint32_t capacity, std::uint64_t max_score = 3)
      : capacity(capacity), max_score(max_score) {
    if (max_score == 0) throw std::runtime_error("Hyper Clock max score must be positive");
  }

  bool contains(std::uint32_t node) const { return entries.find(node) != entries.end(); }
  std::size_t size() const { return entries.size(); }

  bool lookup(std::uint32_t node) {
    const auto found = entries.find(node);
    if (found == entries.end()) return false;
    auto& score = slots[found->second].score;
    if (score < max_score) ++score;  // Avoid overflow even at UINT64_MAX.
    return true;
  }

  void discard(std::uint32_t node) {
    const auto found = entries.find(node);
    if (found == entries.end()) return;
    const auto index = found->second;
    entries.erase(found);
    slots[index] = Slot{};
    free_slots.push_back(index);
  }

  std::uint32_t evict() {
    if (entries.empty()) throw std::runtime_error("Hyper Clock victim selection requires a resident entry");
    while (true) {
      const auto index = hand;
      hand = (hand + 1) % slots.size();
      auto& slot = slots[index];
      if (slot.node == 0) continue;
      if (slot.score > 0) {
        --slot.score;
        continue;
      }
      const auto victim = slot.node;
      discard(victim);
      return victim;
    }
  }

  void write(std::uint32_t node) {
    if (capacity == 0 || lookup(node)) return;
    if (size() >= capacity) evict();
    std::size_t index;
    if (!free_slots.empty()) {
      index = free_slots.back();
      free_slots.pop_back();
      slots[index] = Slot{node, 1};
    } else {
      index = slots.size();
      slots.push_back(Slot{node, 1});
    }
    entries.emplace(node, index);
  }
};

struct ClockPrefixRead {
  std::uint64_t hit_tokens;
  std::uint64_t write_start;
};

ClockPrefixRead read_hyper_clock_prefix(
    HyperClockCache& cache, const SimulationInput& input, std::uint64_t start, std::uint64_t end) {
  std::uint64_t hits = 0;
  for (auto index = start; index < end; ++index) {
    if (!cache.lookup(input.prefix.node_for_event[index])) return {hits, index};
    hits += input.tokens[index];
  }
  return {hits, end};
}

void write_hyper_clock_suffix(
    HyperClockCache& cache, const SimulationInput& input, std::uint64_t start, std::uint64_t end) {
  for (auto index = start; index < end; ++index) cache.write(input.prefix.node_for_event[index]);
}

Result simulate_hyper_clock(const SimulationInput& input, std::uint32_t capacity, const Options& options) {
  if (capacity == 0 || input.ids.empty()) return no_cache_result(input, options);
  Result result;
  HyperClockCache cache(capacity, options.hyper_clock_max_score);
  bool full_before_measurement = false;
  SchedulerCursor cursor;
  auto& scheduler = SchedulerCursor::get();
  std::unordered_map<std::uint32_t, std::uint64_t> write_starts;
  while (const auto event = scheduler.next_event()) {
    const auto request = event->request;
    const std::uint64_t start = request ? input.request_ends[request - 1] : 0;
    const std::uint64_t end = input.request_ends[request];
    if (event->kind == ScheduledEventKind::Arrival) {
      if (request >= options.warmup_requests && !full_before_measurement) {
        return underfilled_result(input, options);
      }
      const auto read = read_hyper_clock_prefix(cache, input, start, end);
      if (request >= options.warmup_requests) {
        result.hit_tokens += read.hit_tokens;
        for (auto index = start; index < end; ++index) result.total_tokens += input.tokens[index];
      }
      write_starts.emplace(request, read.write_start);
    } else {
      const auto write_start = write_starts.at(request);
      write_starts.erase(request);
      // Restore read hits evicted in flight without refreshing surviving hits twice.
      for (auto index = start; index < write_start; ++index) {
        const auto node = input.prefix.node_for_event[index];
        if (!cache.contains(node)) cache.write(node);
      }
      write_hyper_clock_suffix(cache, input, write_start, end);
      if (request < options.warmup_requests && cache.size() >= capacity) full_before_measurement = true;
    }
  }
  if (!full_before_measurement || result.total_tokens == 0) return underfilled_result(input, options);
  result.measurement_start_request = static_cast<std::int64_t>(options.warmup_requests);
  return result;
}

struct HeapItem {
  std::uint32_t node = 0;
  std::uint32_t key = 0;
  std::uint32_t version = 0;
};

class BinaryHeap {
 public:
  explicit BinaryHeap(bool max_heap) : max_heap_(max_heap) {}

  void push(const HeapItem& item) {
    items_.push_back(item);
    std::size_t index = items_.size() - 1;
    while (index > 0) {
      const std::size_t parent = (index - 1) >> 1;
      if (!better(items_[index], items_[parent])) break;
      std::swap(items_[index], items_[parent]);
      index = parent;
    }
  }

  bool pop(HeapItem& out) {
    if (items_.empty()) return false;
    out = items_[0];
    const HeapItem last = items_.back();
    items_.pop_back();
    if (!items_.empty()) {
      items_[0] = last;
      std::size_t index = 0;
      for (;;) {
        const std::size_t left = index * 2 + 1;
        const std::size_t right = left + 1;
        std::size_t best = index;
        if (left < items_.size() && better(items_[left], items_[best])) best = left;
        if (right < items_.size() && better(items_[right], items_[best])) best = right;
        if (best == index) break;
        std::swap(items_[best], items_[index]);
        index = best;
      }
    }
    return true;
  }

 private:
  bool better(const HeapItem& left, const HeapItem& right) const {
    if (left.key == right.key) return left.node > right.node;
    return max_heap_ ? left.key > right.key : left.key < right.key;
  }

  bool max_heap_ = false;
  std::vector<HeapItem> items_;
};

Result simulate_trie_policy(const SimulationInput& input, std::uint32_t capacity, bool optimal, const Options& options) {
  if (capacity == 0 || input.ids.empty()) return no_cache_result(input, options);

  Result result;
  const std::size_t node_count = input.prefix.parent.size();
  std::vector<std::uint8_t> present(node_count, 0);
  std::vector<std::uint32_t> child_count(node_count, 0);
  std::vector<std::uint32_t> state_version(node_count, 0);
  std::vector<std::uint32_t> state_key(node_count, 0);
  std::vector<std::uint32_t> protected_mark(node_count, 0);
  std::vector<std::uint32_t> next_use;
  if (optimal) next_use.assign(node_count, static_cast<std::uint32_t>(input.request_ends.size() + 1ULL));
  BinaryHeap heap(optimal);
  present[0] = 1;
  std::uint32_t cache_size = 0;
  std::uint32_t clock = 0;
  std::uint32_t mark_value = 1;
  bool full_before_measurement = false;

  auto push_leaf = [&](std::uint32_t node) {
    if (node == 0 || !present[node] || child_count[node] != 0) return;
    state_version[node] += 1;
    heap.push({ node, state_key[node], state_version[node] });
  };

  auto touch_lru = [&](std::uint32_t node) {
    if (node == 0 || !present[node]) return;
    state_key[node] = ++clock;
    push_leaf(node);
  };

  auto update_optimal = [&](std::uint32_t node, std::uint32_t next_use) {
    if (node == 0 || !present[node]) return;
    state_key[node] = next_use;
    push_leaf(node);
  };

  auto add_node = [&](std::uint32_t node) {
    const std::uint32_t parent = input.prefix.parent[node];
    present[node] = 1;
    cache_size += 1;
    child_count[parent] += 1;
    if (optimal) update_optimal(node, next_use[node]);
    else touch_lru(node);
  };

  auto evict_leaf = [&](std::uint32_t candidate_key) {
    std::vector<HeapItem> skipped;
    HeapItem top;
    for (;;) {
      if (!heap.pop(top)) {
        for (const HeapItem& item : skipped) heap.push(item);
        return false;
      }
      const std::uint32_t node = top.node;
      if (!present[node] || child_count[node] != 0 || state_version[node] != top.version || state_key[node] != top.key) {
        continue;
      }
      if (protected_mark[node] == mark_value) {
        skipped.push_back(top);
        continue;
      }
      if (optimal && top.key <= candidate_key) {
        heap.push(top);
        for (const HeapItem& item : skipped) heap.push(item);
        return false;
      }
      present[node] = 0;
      cache_size -= 1;
      const std::uint32_t parent = input.prefix.parent[node];
      child_count[parent] -= 1;
      if (parent > 0 && present[parent] && child_count[parent] == 0) push_leaf(parent);
      for (const HeapItem& item : skipped) heap.push(item);
      return true;
    }
  };

  auto mark_protected_path = [&](std::uint64_t start, std::uint64_t end) {
    mark_value += 1;
    if (mark_value == 0x7fffffffU) {
      std::fill(protected_mark.begin(), protected_mark.end(), 0);
      mark_value = 1;
    }
    protected_mark[0] = mark_value;
    for (std::uint64_t index = start; index < end; index += 1) {
      const std::uint32_t node = input.prefix.node_for_event[index];
      if (present[node]) protected_mark[node] = mark_value;
    }
  };

  SchedulerCursor cursor;
  auto& scheduler = SchedulerCursor::get();
  while (const auto event = scheduler.next_event()) {
    const auto request = event->request;
    const std::uint64_t start = request ? input.request_ends[request - 1] : 0;
    const std::uint64_t end = input.request_ends[request];
    if (event->kind == ScheduledEventKind::Arrival) {
      if (request >= options.warmup_requests && !full_before_measurement) {
        return underfilled_result(input, options);
      }
      if (optimal) {
        for (auto index = start; index < end; ++index) {
          next_use[input.prefix.node_for_event[index]] = input.prefix.next_request_for_event[index];
        }
      }
      const bool measured = request >= options.warmup_requests;
      bool prefix_alive = true;
      for (std::uint64_t index = start; index < end; index += 1) {
        const std::uint32_t node = input.prefix.node_for_event[index];
        const bool hit = present[node] != 0;
        if (measured) {
          result.total_tokens += input.tokens[index];
          if (prefix_alive && hit) result.hit_tokens += input.tokens[index];
        }
        if (prefix_alive && hit) {
          if (optimal) update_optimal(node, next_use[node]);
          else touch_lru(node);
        } else if (!hit) {
          prefix_alive = false;
        }
      }
    } else {
      mark_protected_path(start, end);
      for (std::uint64_t index = start; index < end; index += 1) {
        const std::uint32_t node = input.prefix.node_for_event[index];
        if (present[node]) continue;
        if (cache_size >= capacity) {
          const std::uint32_t candidate_key = optimal ? next_use[node] : 0;
          if (!evict_leaf(candidate_key)) break;
        }
        if (cache_size < capacity && present[input.prefix.parent[node]]) {
          add_node(node);
          if (cache_size >= capacity && request < options.warmup_requests) full_before_measurement = true;
        } else {
          break;
        }
      }
    }
  }
  if (!full_before_measurement || result.total_tokens == 0) return underfilled_result(input, options);
  result.measurement_start_request = static_cast<std::int64_t>(options.warmup_requests);
  return result;
}

struct SampledSlot {
  std::uint32_t node = 0;
  std::uint64_t timestamp = 0, sequence = 0;
};
struct SampledCounters {
  std::uint64_t attempts = 0, fallbacks = 0, inspected = 0;
  std::uint64_t sampled_evictions = 0, fallback_evictions = 0;
};

struct SampledProgress {
  const Options& options;
  const SampledCounters& counters;
  std::uint64_t total_events, completed_events = 0, work = 0;
  std::string label;
  std::chrono::steady_clock::time_point last_update = std::chrono::steady_clock::now();

  void report(const std::string& status = "") {
    if (!options.progress) return;
    const auto percent = total_events ? 100 * completed_events / total_events : 100;
    report_progress(options, options.progress_done, options.progress_total,
        label + " (events " + std::to_string(completed_events) + "/" + std::to_string(total_events) +
        " (" + std::to_string(percent) + "%))" + status);
    last_update = std::chrono::steady_clock::now();
  }

  void tick() {
    // Check wall time every 4096 operations, including work inside long events.
    if ((++work & 4095) != 0) return;
    if (std::chrono::steady_clock::now() - last_update >= std::chrono::seconds(1)) report();
  }
};

struct SampledCache {
  std::uint32_t capacity, sample_size, max_evictions;
  std::uint64_t threshold, sequence = 0;
  bool touch_on_put;
  std::mt19937_64 rng;
  std::vector<SampledSlot> slots;
  std::vector<std::size_t> free;
  static constexpr std::uint32_t absent = 0xffffffffU;
  std::vector<std::uint32_t> node_to_slot;
  SampledCounters counters;
  SampledProgress* progress = nullptr;
  SampledCache(std::uint32_t cap, const Options& o, std::size_t node_count, bool touch_on_put = true)
      : capacity(cap), sample_size(o.sampled_size), max_evictions(o.sampled_max_evictions), threshold(o.sampled_threshold), touch_on_put(touch_on_put), rng(o.sampled_seed), node_to_slot(node_count, absent) {}
  std::size_t size() const { return slots.size() - free.size(); }
  bool contains(std::uint32_t node) const { return node_to_slot[node] != absent; }
  bool lookup(std::uint32_t node, std::uint64_t now) {
    const auto index = node_to_slot[node];
    if (index == absent) return false;
    auto& slot = slots[index]; slot.timestamp = now; slot.sequence = ++sequence;
    return true;
  }
  void discard(std::size_t index) {
    node_to_slot[slots[index].node] = absent; slots[index] = {}; free.push_back(index);
  }
  bool eligible(const SampledSlot& slot, std::uint64_t now, std::uint64_t age) const {
    return slot.node != 0 && (age == 0 || now - slot.timestamp > age);
  }
  void evict(std::uint64_t now) {
    ++counters.attempts;
    std::vector<SampledSlot> sample;
    const auto count = std::min<std::size_t>(sample_size, slots.size());
    sample.reserve(count);
    std::unordered_set<std::size_t> selected;
    selected.reserve(count);
    std::uniform_int_distribution<std::size_t> draw(0, slots.size() - 1);
    while (sample.size() < count) {
      const auto index = draw(rng);
      if (selected.insert(index).second) sample.push_back(slots[index]);
      if (progress) progress->tick();
    }
    std::sort(sample.begin(), sample.end(), [](const auto& a, const auto& b) {
      return std::tie(a.timestamp, a.sequence) < std::tie(b.timestamp, b.sequence);
    });
    std::uint32_t evicted = 0;
    for (const auto& slot : sample) {
      if (progress) progress->tick();
      if (eligible(slot, now, threshold)) {
        discard(node_to_slot[slot.node]);
        ++counters.sampled_evictions;
        if (++evicted == max_evictions) break;
      }
    }
    if (!free.empty()) return;
    ++counters.fallbacks;
    // Build once. Threshold halving checks only the oldest resident entry.
    std::vector<std::size_t> candidates;
    candidates.reserve(slots.size());
    for (std::size_t i = 0; i < slots.size(); ++i) {
      if (slots[i].node != 0) candidates.push_back(i);
      ++counters.inspected;
      if (progress) progress->tick();
    }
    const auto newer = [&](std::size_t a, std::size_t b) {
      return std::tie(slots[a].timestamp, slots[a].sequence) >
             std::tie(slots[b].timestamp, slots[b].sequence);
    };
    std::make_heap(candidates.begin(), candidates.end(), newer);
    assert(!candidates.empty());
    auto age = threshold;
    while (!eligible(slots[candidates.front()], now, age)) age /= 2;
    while (!candidates.empty() && evicted < max_evictions &&
           eligible(slots[candidates.front()], now, age)) {
      std::pop_heap(candidates.begin(), candidates.end(), newer);
      const auto index = candidates.back();
      candidates.pop_back();
      discard(index);
      ++counters.fallback_evictions;
      ++evicted;
      if (progress) progress->tick();
    }
  }
  void write(std::uint32_t node, std::uint64_t now) {
    if (!capacity) return;
    if (contains(node)) {
      if (touch_on_put) lookup(node, now);
      return;
    }
    if (size() == capacity) evict(now);
    std::size_t index;
    if (free.empty()) { index = slots.size(); slots.push_back({}); }
    else { index = free.back(); free.pop_back(); }
    slots[index] = {node, now, ++sequence}; node_to_slot[node] = static_cast<std::uint32_t>(index);
  }
};

Result simulate_sampled(const SimulationInput& input, std::uint32_t capacity, const Options& options, bool touch_on_put = true) {
  SchedulerCursor cursor;
  auto& scheduler = SchedulerCursor::get();
  SampledCache cache(capacity, options, capacity ? input.prefix.parent.size() : 0, touch_on_put);
  auto label = options.progress_label.empty()
      ? std::string(touch_on_put ? "sampling-lru-touch-on-put" : "sampling-lru-touch-on-get") +
        ":sample-size=" + std::to_string(options.sampled_size) + " (" + std::to_string(capacity) + " blocks)"
      : options.progress_label;
  if (label.find(":sample-size=") == std::string::npos)
    label += " | sample size " + std::to_string(options.sampled_size);
  SampledProgress progress{options, cache.counters, 2ULL * input.request_ends.size(), 0, 0, label};
  if (!capacity) {
    progress.report(" | skipped: zero capacity");
    return no_cache_result(input, options);
  }
  if (options.progress) {
    cache.progress = &progress;
    progress.report();
  }
  Result result;
  result.measurement_start_request = options.warmup_requests;
  bool full = false;
  while (const auto event = scheduler.next_event()) {
    const auto request = event->request;
    const auto start = request ? input.request_ends[request - 1] : 0;
    const auto end = input.request_ends[request];
    if (event->kind == ScheduledEventKind::Completion) {
      for (auto i = start; i < end; ++i) {
        cache.write(input.prefix.node_for_event[i], event->timestamp);
        if (cache.size() == capacity) full = true;
        if (cache.progress) progress.tick();
      }
    } else {
      if (request >= options.warmup_requests && !full) {
        progress.report(" | stopped: cache underfilled at measurement boundary");
        return underfilled_result(input, options);
      }
      bool alive = true;
      for (auto i = start; i < end; ++i) {
        if (request >= options.warmup_requests) result.total_tokens += input.tokens[i];
        if (alive) {
          alive = cache.lookup(input.prefix.node_for_event[i], event->timestamp);
          if (alive && request >= options.warmup_requests) result.hit_tokens += input.tokens[i];
        }
        if (cache.progress) progress.tick();
      }
    }
    ++progress.completed_events;
    if (cache.progress) progress.tick();
  }
  progress.report();
  if (!full || !result.total_tokens) return underfilled_result(input, options);
  return result;
}

struct AllResults {
  Result fifo;
  Result lru;
  Result flat_lru_touch_on_put;
  Result flat_lru_memcache;
  Result hyper_clock;
  Result optimal;
  Result sampled;
  Result sampled_get;
};

bool has_policy(const Options& options, const std::string& policy) {
  if (options.policies.empty()) return true;
  return std::find(options.policies.begin(), options.policies.end(), policy) != options.policies.end();
}

std::uint64_t selected_policy_count(const Options& options) {
  std::uint64_t count = 0;
  for (const auto* policy : {"sampling-lru-touch-on-put", "sampling-lru-touch-on-get", "fifo", "lru", "lru-flat-touch-on-put", "flat-lru-memcache", "hyper-clock", "optimal"}) {
    if (has_policy(options, policy)) count += std::string(policy).find("sampling-lru-") == 0 ? options.sampled_sizes.size() : 1;
  }
  return count;
}

void print_result(const std::string& policy, std::uint64_t cache_blocks, const Result& result, std::uint64_t trie_node_count, std::uint64_t warmup_requests) {
  std::cout << "{"
            << "\"policy\":\"" << policy << "\","
            << "\"cacheBlocks\":" << cache_blocks << ","
            << "\"trieNodeCount\":" << trie_node_count << ","
            << "\"warmupRequests\":" << warmup_requests << ","
            << "\"measurementStartRequest\":" << result.measurement_start_request << ","
            << "\"hitTokens\":" << result.hit_tokens << ","
            << "\"totalTokens\":" << result.total_tokens << ","
            << "\"hitRate\":" << std::setprecision(17) << result.hit_rate() << ","
            << "\"usefulCacheBlockSamples\":" << result.useful_cache_block_samples << ","
            << "\"usefulCacheSamples\":" << result.useful_cache_samples << ","
            << "\"usefulCacheRate\":" << std::setprecision(17) << result.useful_cache_rate(cache_blocks)
            << "}\n";
}

void print_named_result(const char* policy, std::uint64_t cache_blocks, const Result& result, std::uint64_t warmup_requests) {
  std::cout << "\"policy\":\"" << policy << "\","
            << "\"cacheBlocks\":" << cache_blocks << ","
            << "\"warmupRequests\":" << warmup_requests << ","
            << "\"measurementStartRequest\":" << result.measurement_start_request << ","
            << "\"hitTokens\":" << result.hit_tokens << ","
            << "\"totalTokens\":" << result.total_tokens << ","
            << "\"hitRate\":" << std::setprecision(17) << result.hit_rate() << ","
            << "\"usefulCacheBlockSamples\":" << result.useful_cache_block_samples << ","
            << "\"usefulCacheSamples\":" << result.useful_cache_samples << ","
            << "\"usefulCacheRate\":" << std::setprecision(17) << result.useful_cache_rate(cache_blocks);
}

void print_all_results(std::uint64_t cache_blocks, const AllResults& results, std::uint64_t trie_node_count, std::uint64_t warmup_requests) {
  std::cout << "{"
            << "\"cacheBlocks\":" << cache_blocks << ","
            << "\"trieNodeCount\":" << trie_node_count << ","
            << "\"fifo\":{";
  print_named_result("fifo", cache_blocks, results.fifo, warmup_requests);
  std::cout << "},\"lru\":{";
  print_named_result("lru", cache_blocks, results.lru, warmup_requests);
  std::cout << "},\"lru-flat-touch-on-put\":{";
  print_named_result("lru-flat-touch-on-put", cache_blocks, results.flat_lru_touch_on_put, warmup_requests);
  std::cout << "},\"flat-lru-memcache\":{";
  print_named_result("flat-lru-memcache", cache_blocks, results.flat_lru_memcache, warmup_requests);
  std::cout << "},\"hyper-clock\":{";
  print_named_result("hyper-clock", cache_blocks, results.hyper_clock, warmup_requests);
  std::cout << "},\"optimal\":{";
  print_named_result("optimal", cache_blocks, results.optimal, warmup_requests);
  std::cout << "}";
  std::cout << ",\"sampling-lru-touch-on-put\":{";
  print_named_result("sampling-lru-touch-on-put", cache_blocks, results.sampled, warmup_requests);
  std::cout << "}";
  std::cout << ",\"sampling-lru-touch-on-get\":{";
  print_named_result("sampling-lru-touch-on-get", cache_blocks, results.sampled_get, warmup_requests);
  std::cout << "}";
  std::cout << "}\n";
}

// Construct one selected, read-only schedule for all simulation cursors.
std::unique_ptr<const RequestScheduler> make_scheduler(const SimulationInput& input, const Options& options) {
  if (options.scheduler == "sequential")
    return std::make_unique<SequentialRequestScheduler>(input.request_ends.size());
  if (options.scheduler == "recorded") {
    if (!input.timing_available) throw std::runtime_error("recorded scheduler requires complete request timing data");
    return std::make_unique<RecordedTimeScheduler>(input.arrivals, input.completions, input.completion_order);
  }
  throw std::runtime_error("Select --scheduler sequential or --scheduler recorded");
}

struct SimulationJob {
  std::size_t result_index;
  std::string policy;
  std::string id;
  std::uint32_t capacity;
  std::size_t capacity_index;  // One-based output point, or zero for the ceiling.
  std::uint32_t sample_size;
  std::string label;
};

// All jobs are known before execution. Only the next-job index changes.
// Consumers claim jobs independently through the atomic index.
class SimulationJobQueue {
 public:
  explicit SimulationJobQueue(std::vector<SimulationJob> jobs)
      : jobs_(std::move(jobs)), order_(jobs_.size()) {
    for (std::size_t n = 0; n < order_.size(); ++n) order_[n] = n;
    // Keep descriptors in report order. Dispatch the ceiling first, then larger
    // capacities, preserving policy/sample order and ties between capacities.
    std::stable_sort(order_.begin(), order_.end(), [&](auto a, auto b) {
      const bool ceiling_a = jobs_[a].policy == "ceiling";
      const bool ceiling_b = jobs_[b].policy == "ceiling";
      if (ceiling_a != ceiling_b) return ceiling_a;
      return jobs_[a].capacity > jobs_[b].capacity;
    });
  }

  const SimulationJob* take() {
    const auto index = next_.fetch_add(1, std::memory_order_relaxed);
    return index < order_.size() ? &jobs_[order_[index]] : nullptr;
  }
  std::size_t pending() const {
    return jobs_.size() - std::min(jobs_.size(), next_.load(std::memory_order_relaxed));
  }
  std::size_t size() const { return jobs_.size(); }
  const std::vector<SimulationJob>& jobs() const { return jobs_; }

 private:
  const std::vector<SimulationJob> jobs_;
  std::vector<std::size_t> order_;
  std::atomic<std::size_t> next_{0};
};

[[noreturn]] void fail_worker(std::size_t worker, const SimulationJob* job, const char* cause) {
  std::lock_guard<std::mutex> lock(progress_output_mutex);
  std::cerr << "Worker " << worker << " failed";
  if (job) std::cerr << " in job " << job->result_index << " (" << job->label << ")";
  std::cerr << ": " << cause << std::endl;
  std::_Exit(EXIT_FAILURE);
}

class SimulationRunner {
 public:
  SimulationRunner(const SimulationInput& input, const Options& options)
      : input(input), options(options), scheduler(make_scheduler(input, options)) {}

  Result run(const std::string& policy, std::uint32_t capacity) {
    if (policy == "sampling-lru-touch-on-put") return simulate_sampled(input, capacity, options);
    if (policy == "sampling-lru-touch-on-get") return simulate_sampled(input, capacity, options, false);
    if (policy == "ceiling") return simulate_ceiling(input, options);
    if (policy == "fifo") return simulate_fifo(input, capacity, options);
    if (policy == "lru") return simulate_trie_policy(input, capacity, false, options);
    if (policy == "lru-flat-touch-on-put") return simulate_flat_lru(input, capacity, options);
    if (policy == "flat-lru-memcache") return simulate_flat_lru(input, capacity, options, false);
    if (policy == "hyper-clock") return simulate_hyper_clock(input, capacity, options);
    if (policy == "optimal") return simulate_trie_policy(input, capacity, true, options);
    throw std::runtime_error("Unsupported policy: " + policy);
  }

  AllResults simulate_all(std::uint32_t capacity) {
    return {
      run("fifo", capacity), run("lru", capacity), run("lru-flat-touch-on-put", capacity), run("flat-lru-memcache", capacity),
      run("hyper-clock", capacity), run("optimal", capacity),
      run("sampling-lru-touch-on-put", capacity), run("sampling-lru-touch-on-get", capacity),
    };
  }

  std::vector<std::string> selected_names() const {
    if (options.policy != "batch" && options.policy != "all") return {options.policy};
    if (!options.policies.empty()) return options.policies;
    return {"fifo", "lru", "lru-flat-touch-on-put", "flat-lru-memcache", "hyper-clock", "optimal", "sampling-lru-touch-on-put", "sampling-lru-touch-on-get"};
  }

  SimulationJobQueue make_job_queue() const {
    std::vector<SimulationJob> jobs;
    jobs.push_back({0, "ceiling", "ceiling", 0, 0, options.sampled_size, "ceiling"});
    const auto policy_count = selected_policy_count(options);
    auto names = selected_names();
    std::stable_partition(names.begin(), names.end(), [](const auto& p) { return p.find("sampling-lru-") == 0; });
    for (std::size_t point = 0; point < options.capacities.size(); ++point) {
      const auto capacity = options.capacities[point];
      std::uint64_t policy_index = 0;
      for (const auto& policy : names) {
        const bool sampling = policy.find("sampling-lru-") == 0;
        const auto sizes = sampling ? options.sampled_sizes : std::vector<std::uint32_t>{0};
        for (auto size : sizes) {
          const auto id = sampling && sizes.size() > 1 ? policy + ":sample-size=" + std::to_string(size) : policy;
          const std::string label = id + " (cache capacity " + std::to_string(point + 1) + "/" + std::to_string(options.capacities.size()) + ", " + std::to_string(capacity) + " blocks; policy " + std::to_string(++policy_index) + "/" + std::to_string(policy_count) + ")";
          jobs.push_back({jobs.size(), policy, id, capacity, point + 1, size, label});
        }
      }
    }
    return SimulationJobQueue(std::move(jobs));
  }

  void print_batch_results() {
    auto queue = make_job_queue();
    std::vector<Result> results(queue.size());
    const std::uint64_t total_steps = queue.size();
    ProgressState progress_state;

    auto execute = [&](const SimulationJob* job, std::size_t worker) {
      try {
        progress_state.running.fetch_add(1, std::memory_order_relaxed);
        Options configured = options;
        configured.sampled_size = job->sample_size;
        configured.progress_done = progress_state.completed.load(std::memory_order_relaxed);
        configured.progress_state = &progress_state;
        configured.progress_worker = worker;
        configured.progress_job = job->result_index;
        configured.progress_total = total_steps;
        configured.progress_label = job->label;
        report_progress(configured, 0, total_steps, job->label);
        const bool sampling = job->policy.find("sampling-lru-") == 0;
        results[job->result_index] = sampling
            ? simulate_sampled(input, job->capacity, configured, job->policy == "sampling-lru-touch-on-put")
            : run(job->policy, job->capacity);
        progress_state.running.fetch_sub(1, std::memory_order_relaxed);
        progress_state.completed.fetch_add(1, std::memory_order_relaxed);
        configured.progress_finished = true;
        report_progress(configured, 0, total_steps, job->label);
      } catch (const std::exception& error) {
        fail_worker(worker, job, error.what());
      } catch (...) {
        fail_worker(worker, job, "unknown exception");
      }
    };

    // Complete the ceiling before workers start claiming capacity simulations.
    execute(queue.take(), 0);
    const auto worker_count = std::min<std::size_t>(options.jobs, queue.pending());
    auto consume = [&](std::size_t worker) {
      while (const auto* job = queue.take()) execute(job, worker);
    };
    if (worker_count == 1) {
      consume(0);
    } else {
      std::vector<std::thread> workers;
      workers.reserve(worker_count);
      for (std::size_t worker = 0; worker < worker_count; ++worker) {
        try {
          workers.emplace_back(consume, worker);
        } catch (const std::exception& error) {
          fail_worker(worker, nullptr, error.what());
        }
      }
      for (auto& worker : workers) worker.join();
    }

    std::cout << "{"
              << "\"trieNodeCount\":" << input.prefix.parent.size() << ","
              << "\"uniqueBlocks\":" << input.unique_blocks << ","
              << "\"warmupRequests\":" << options.warmup_requests << ","
              << "\"ceiling\":{";
    print_named_result("ceiling", input.unique_blocks, results[0], options.warmup_requests);
    std::cout << "},\"points\":[";

    std::size_t job_index = 1;
    for (std::size_t point = 0; point < options.capacities.size(); ++point) {
      if (point != 0) std::cout << ",";
      std::cout << "{\"cacheBlocks\":" << options.capacities[point];
      while (job_index < queue.size() && queue.jobs()[job_index].capacity_index == point + 1) {
        const auto& job = queue.jobs()[job_index++];
        std::cout << ",\"" << job.id << "\":{";
        print_named_result(job.policy.c_str(), job.capacity, results[job.result_index], options.warmup_requests);
        std::cout << "}";
      }
      std::cout << "}";
    }
    Options final_progress = options;
    final_progress.progress_state = &progress_state;
    final_progress.progress_finished = true;
    report_progress(final_progress, total_steps, total_steps, "simulation complete");
    std::cout << "]}\n";
  }

 private:
  const SimulationInput& input;
  const Options& options;
  std::unique_ptr<const RequestScheduler> scheduler;
};

}  // namespace

int main(int argc, char** argv) {
  try {
    Options options = parse_args(argc, argv);
    if (options.policy == "build-next") {
      build_next_file(options);
      std::cout << "{\"policy\":\"build-next\",\"cacheBlocks\":0,\"hitTokens\":0,\"totalTokens\":0,\"hitRate\":0}\n";
      return 0;
    }

    report_progress(options, 0, 1 + options.capacities.size() * selected_policy_count(options), "load");
    const SimulationInput input = load_simulation_input(options);
    SimulationRunner runner(input, options);
    const std::uint64_t trie_node_count = input.prefix.parent.size();
    if (options.policy == "batch") runner.print_batch_results();
    else if (options.policy == "all") print_all_results(options.capacity, runner.simulate_all(options.capacity), trie_node_count, options.warmup_requests);
    else print_result(options.policy, options.capacity, runner.run(options.policy, options.capacity), trie_node_count, options.warmup_requests);
    return 0;
  } catch (const std::exception& error) {
    std::cerr << error.what() << "\n";
    return 1;
  }
}
