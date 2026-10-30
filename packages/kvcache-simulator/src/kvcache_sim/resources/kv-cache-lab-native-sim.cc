#include <algorithm>
#include <cassert>
#include <optional>
#include <charconv>
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
#include <iterator>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace {

constexpr std::size_t kChunkEvents = 1'000'000;

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
  std::uint64_t hyper_clock_max_score = 3;
  std::vector<std::uint32_t> capacities;
  std::vector<std::string> policies;
  bool progress = false;
  std::string arrivals_path, completions_path, order_path;
  std::uint64_t sampled_threshold = 10000000, sampled_seed = 0;
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
      auto n = parse_sampled_u64(require_value(arg), arg);
      if (!n || n > 0xffffffffULL) throw std::runtime_error("Invalid sampled sample size");
      options.sampled_size = n;
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
    throw std::runtime_error("Usage: kv-cache-lab-native-sim --scheduler recorded|sequential --policy fifo|lru|lru-flat|hyper-clock|optimal|sampled-lru|all|batch|ceiling --ids PATH --tokens PATH --total-blocks N --request-ends PATH --request-count N --capacity N [--hyper-clock-max-score N] [--next PATH]");
  }
  if (options.policy != "build-next" && (options.request_ends_path.empty() || options.request_count == 0)) {
    throw std::runtime_error("--request-ends and --request-count are required");
  }
  if (options.policy != "build-next" && options.scheduler != "sequential" && options.scheduler != "recorded") {
    throw std::runtime_error("Select --scheduler sequential or --scheduler recorded");
  }
  return options;
}

void report_progress(const Options& options, std::uint64_t done, std::uint64_t total, const std::string& label) {
  if (!options.progress) return;
  std::cerr << "KV_PROGRESS " << done << " " << total << " " << label << "\n";
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

class RequestScheduler {
 public:
  virtual ~RequestScheduler() { instance_ = nullptr; }
  RequestScheduler(const RequestScheduler&) = delete;
  RequestScheduler& operator=(const RequestScheduler&) = delete;

  static RequestScheduler& get() {
    assert(instance_);
    assert(instance_->at_start());
    return *instance_;
  }
  virtual std::optional<ScheduledEvent> next_event() = 0;
  virtual bool exhausted() const = 0;
  virtual void discard_remaining() = 0;

  void reset() {
    assert(exhausted());
    reset_impl();
  }

 protected:
  RequestScheduler() {
    assert(!instance_);
    instance_ = this;
  }
  virtual bool at_start() const = 0;
  virtual void reset_impl() = 0;

 private:
  inline static RequestScheduler* instance_ = nullptr;
};

// Generate adjacent read/write events without storing a timeline. Timestamps are unused.
class SequentialRequestScheduler final : public RequestScheduler {
 public:
  explicit SequentialRequestScheduler(std::uint32_t request_count)
      : event_count_(2ULL * request_count) {}

  std::optional<ScheduledEvent> next_event() override {
    if (exhausted()) return std::nullopt;
    const auto event = cursor_++;
    return ScheduledEvent{static_cast<std::uint32_t>(event / 2),
      event % 2 ? ScheduledEventKind::Completion : ScheduledEventKind::Arrival, 0};
  }
  bool exhausted() const override { return cursor_ == event_count_; }
  void discard_remaining() override { cursor_ = event_count_; }

 protected:
  bool at_start() const override { return cursor_ == 0; }
  void reset_impl() override { cursor_ = 0; }

 private:
  const std::uint64_t event_count_;
  std::uint64_t cursor_ = 0;
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

  std::optional<ScheduledEvent> next_event() override {
    if (exhausted()) return std::nullopt;
    const auto encoded = events_[cursor_++];
    const auto request = static_cast<std::uint32_t>(encoded >> 1);
    const auto kind = (encoded & 1) ? ScheduledEventKind::Completion : ScheduledEventKind::Arrival;
    return ScheduledEvent{request, kind,
      kind == ScheduledEventKind::Arrival ? arrivals_[request] : completions_[request]};
  }

  bool exhausted() const override { return cursor_ == events_.size(); }
  void discard_remaining() override { cursor_ = events_.size(); }

 protected:
  bool at_start() const override { return cursor_ == 0; }
  void reset_impl() override { cursor_ = 0; }

 private:
  void append(std::uint32_t request, ScheduledEventKind kind) {
    events_.push_back((static_cast<std::uint64_t>(request) << 1)
                     | (kind == ScheduledEventKind::Completion ? 1ULL : 0ULL));
  }

  const std::vector<std::uint64_t>& arrivals_;
  const std::vector<std::uint64_t>& completions_;
  std::vector<std::uint64_t> events_;
  std::size_t cursor_ = 0;
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
  auto& scheduler = RequestScheduler::get();
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
  scheduler.reset();
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

  auto& scheduler = RequestScheduler::get();
  while (const auto event = scheduler.next_event()) {
    const auto request = event->request;
    const std::uint64_t start = request ? input.request_ends[request - 1] : 0;
    const std::uint64_t end = input.request_ends[request];
    if (event->kind == ScheduledEventKind::Arrival) {
      if (request >= options.warmup_requests && !full_before_measurement) {
        scheduler.discard_remaining();
        scheduler.reset();
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
  scheduler.reset();
  if (!full_before_measurement || result.total_tokens == 0) return underfilled_result(input, options);
  result.measurement_start_request = static_cast<std::int64_t>(options.warmup_requests);
  return result;
}

// Node IDs retain prefix identity; eviction ignores parent/child relationships.
class FlatLruCache {
 public:
  explicit FlatLruCache(std::uint32_t capacity) : capacity_(capacity) {}

  bool contains(std::uint32_t node) const { return entries_.find(node) != entries_.end(); }
  std::size_t size() const { return entries_.size(); }

  void touch(std::uint32_t node) {
    order_.splice(order_.begin(), order_, entries_.at(node));
  }

  void write(std::uint32_t node) {
    if (capacity_ == 0) return;
    if (contains(node)) {
      touch(node);
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

Result simulate_flat_lru(const SimulationInput& input, std::uint32_t capacity, const Options& options) {
  if (capacity == 0 || input.ids.empty()) return no_cache_result(input, options);
  Result result;
  FlatLruCache cache(capacity);
  bool full_before_measurement = false;
  auto& scheduler = RequestScheduler::get();
  while (const auto event = scheduler.next_event()) {
    const auto request = event->request;
    const std::uint64_t start = request ? input.request_ends[request - 1] : 0;
    const std::uint64_t end = input.request_ends[request];
    if (event->kind == ScheduledEventKind::Arrival) {
      if (request >= options.warmup_requests && !full_before_measurement) {
        scheduler.discard_remaining();
        scheduler.reset();
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
  scheduler.reset();
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
  auto& scheduler = RequestScheduler::get();
  std::unordered_map<std::uint32_t, std::uint64_t> write_starts;
  while (const auto event = scheduler.next_event()) {
    const auto request = event->request;
    const std::uint64_t start = request ? input.request_ends[request - 1] : 0;
    const std::uint64_t end = input.request_ends[request];
    if (event->kind == ScheduledEventKind::Arrival) {
      if (request >= options.warmup_requests && !full_before_measurement) {
        scheduler.discard_remaining();
        scheduler.reset();
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
  scheduler.reset();
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

  auto& scheduler = RequestScheduler::get();
  while (const auto event = scheduler.next_event()) {
    const auto request = event->request;
    const std::uint64_t start = request ? input.request_ends[request - 1] : 0;
    const std::uint64_t end = input.request_ends[request];
    if (event->kind == ScheduledEventKind::Arrival) {
      if (request >= options.warmup_requests && !full_before_measurement) {
        scheduler.discard_remaining();
        scheduler.reset();
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
  scheduler.reset();
  if (!full_before_measurement || result.total_tokens == 0) return underfilled_result(input, options);
  result.measurement_start_request = static_cast<std::int64_t>(options.warmup_requests);
  return result;
}

struct SampledSlot {
  std::uint32_t node = 0;
  std::uint64_t timestamp = 0, sequence = 0;
};
struct SampledCache {
  std::uint32_t capacity, sample_size, max_evictions;
  std::uint64_t threshold, sequence = 0;
  std::mt19937_64 rng;
  std::vector<SampledSlot> slots;
  std::vector<std::size_t> free;
  std::unordered_map<std::uint32_t, std::size_t> entries;
  SampledCache(std::uint32_t cap, const Options& o)
      : capacity(cap), sample_size(o.sampled_size), max_evictions(o.sampled_max_evictions), threshold(o.sampled_threshold), rng(o.sampled_seed) {}
  bool lookup(std::uint32_t node, std::uint64_t now) {
    auto it = entries.find(node);
    if (it == entries.end()) return false;
    auto& slot = slots[it->second]; slot.timestamp = now; slot.sequence = ++sequence;
    return true;
  }
  void discard(std::size_t index) {
    entries.erase(slots[index].node); slots[index] = {}; free.push_back(index);
  }
  bool eligible(const SampledSlot& slot, std::uint64_t now, std::uint64_t age) const {
    return slot.node != 0 && (age == 0 || now - slot.timestamp > age);
  }
  void evict(std::uint64_t now) {
    std::vector<SampledSlot> sample;
    std::sample(slots.begin(), slots.end(), std::back_inserter(sample),
                std::min<std::size_t>(sample_size, slots.size()), rng);
    std::sort(sample.begin(), sample.end(), [](const auto& a, const auto& b) {
      return std::tie(a.timestamp, a.sequence) < std::tie(b.timestamp, b.sequence);
    });
    std::uint32_t evicted = 0;
    for (const auto& slot : sample) {
      if (eligible(slot, now, threshold)) {
        discard(entries.at(slot.node));
        if (++evicted == max_evictions) break;
      }
    }
    if (!free.empty()) return;
    auto age = threshold;
    for (;;) {
      for (std::size_t i = 0; i < slots.size(); ++i) if (eligible(slots[i], now, age)) { discard(i); return; }
      age /= 2;
    }
  }
  void write(std::uint32_t node, std::uint64_t now) {
    if (!capacity || lookup(node, now)) return;
    if (entries.size() == capacity) evict(now);
    std::size_t index;
    if (free.empty()) { index = slots.size(); slots.push_back({}); }
    else { index = free.back(); free.pop_back(); }
    slots[index] = {node, now, ++sequence}; entries[node] = index;
  }
};

Result simulate_sampled(const SimulationInput& input, std::uint32_t capacity, const Options& options) {
  auto& scheduler = RequestScheduler::get();
  if (!capacity) {
    scheduler.discard_remaining();
    scheduler.reset();
    return no_cache_result(input, options);
  }
  SampledCache cache(capacity, options);
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
        if (cache.entries.size() == capacity) full = true;
      }
    } else {
      if (request >= options.warmup_requests && !full) {
        scheduler.discard_remaining();
        scheduler.reset();
        return underfilled_result(input, options);
      }
      bool alive = true;
      for (auto i = start; i < end; ++i) {
        if (request >= options.warmup_requests) result.total_tokens += input.tokens[i];
        if (alive) {
          alive = cache.lookup(input.prefix.node_for_event[i], event->timestamp);
          if (alive && request >= options.warmup_requests) result.hit_tokens += input.tokens[i];
        }
      }
    }
  }
  scheduler.reset();
  if (!full || !result.total_tokens) return underfilled_result(input, options);
  return result;
}

struct AllResults {
  Result fifo;
  Result lru;
  Result flat_lru;
  Result hyper_clock;
  Result optimal;
  Result sampled;
};

bool has_policy(const Options& options, const std::string& policy) {
  if (options.policies.empty()) return true;
  return std::find(options.policies.begin(), options.policies.end(), policy) != options.policies.end();
}

std::uint64_t selected_policy_count(const Options& options) {
  std::uint64_t count = 0;
  for (const auto* policy : {"sampled-lru", "fifo", "lru", "lru-flat", "hyper-clock", "optimal"}) {
    if (has_policy(options, policy)) ++count;
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
  std::cout << "},\"lru-flat\":{";
  print_named_result("lru-flat", cache_blocks, results.flat_lru, warmup_requests);
  std::cout << "},\"hyper-clock\":{";
  print_named_result("hyper-clock", cache_blocks, results.hyper_clock, warmup_requests);
  std::cout << "},\"optimal\":{";
  print_named_result("optimal", cache_blocks, results.optimal, warmup_requests);
  std::cout << "}";
  std::cout << ",\"sampled-lru\":{";
  print_named_result("sampled-lru", cache_blocks, results.sampled, warmup_requests);
  std::cout << "}";
  std::cout << "}\n";
}

// Construct one selected scheduler for the entire run. Policies use its singleton accessor.
std::unique_ptr<RequestScheduler> make_scheduler(const SimulationInput& input, const Options& options) {
  if (options.scheduler == "sequential")
    return std::make_unique<SequentialRequestScheduler>(input.request_ends.size());
  if (options.scheduler == "recorded") {
    if (!input.timing_available) throw std::runtime_error("recorded scheduler requires complete request timing data");
    return std::make_unique<RecordedTimeScheduler>(input.arrivals, input.completions, input.completion_order);
  }
  throw std::runtime_error("Select --scheduler sequential or --scheduler recorded");
}

class SimulationRunner {
 public:
  SimulationRunner(const SimulationInput& input, const Options& options)
      : input(input), options(options), scheduler(make_scheduler(input, options)) {}

  Result run(const std::string& policy, std::uint32_t capacity) {
    if (policy == "sampled-lru") return simulate_sampled(input, capacity, options);
    if (policy == "ceiling") return simulate_ceiling(input, options);
    if (policy == "fifo") return simulate_fifo(input, capacity, options);
    if (policy == "lru") return simulate_trie_policy(input, capacity, false, options);
    if (policy == "lru-flat") return simulate_flat_lru(input, capacity, options);
    if (policy == "hyper-clock") return simulate_hyper_clock(input, capacity, options);
    if (policy == "optimal") return simulate_trie_policy(input, capacity, true, options);
    throw std::runtime_error("Unsupported policy: " + policy);
  }

  AllResults simulate_all(std::uint32_t capacity) {
    return {
      run("fifo", capacity), run("lru", capacity), run("lru-flat", capacity),
      run("hyper-clock", capacity), run("optimal", capacity),
      run("sampled-lru", capacity),
    };
  }

  void print_batch_results() {
    const std::uint64_t trie_node_count = input.prefix.parent.size();
    const std::uint64_t policy_count = selected_policy_count(options);
    const std::uint64_t total_steps = 1 + options.capacities.size() * policy_count;
    std::uint64_t completed_steps = 0;

    report_progress(options, completed_steps, total_steps, "ceiling");
    const Result ceiling = run("ceiling", 0);
    completed_steps += 1;

    std::cout << "{"
              << "\"trieNodeCount\":" << trie_node_count << ","
              << "\"uniqueBlocks\":" << input.unique_blocks << ","
              << "\"warmupRequests\":" << options.warmup_requests << ","
              << "\"ceiling\":{";
    print_named_result("ceiling", input.unique_blocks, ceiling, options.warmup_requests);
    std::cout << "},\"points\":[";

    bool first_point = true;
    std::uint64_t capacity_index = 0;
    for (std::uint32_t capacity : options.capacities) {
      ++capacity_index;
      std::uint64_t policy_index = 0;
      auto evaluate = [&](const std::string& policy) {
        ++policy_index;
        const std::string label = policy + " (policy " + std::to_string(policy_index) + "/" +
            std::to_string(policy_count) + " cache capacity " + std::to_string(capacity_index) + "/" +
            std::to_string(options.capacities.size()) + ", " + std::to_string(capacity) + " blocks)";
        report_progress(options, completed_steps, total_steps, label);
        Result result = run(policy, capacity);
        ++completed_steps;
        report_progress(options, completed_steps, total_steps, label);
        return result;
      };
      Result fifo;
      Result lru;
      Result flat_lru;
      Result hyper_clock;
      Result optimal;
      Result sampled;
      const bool run_sampled = has_policy(options, "sampled-lru");
      if (run_sampled) sampled = evaluate("sampled-lru");
      if (has_policy(options, "fifo")) fifo = evaluate("fifo");
      if (has_policy(options, "lru")) lru = evaluate("lru");
      if (has_policy(options, "lru-flat")) flat_lru = evaluate("lru-flat");
      if (has_policy(options, "hyper-clock")) hyper_clock = evaluate("hyper-clock");
      if (has_policy(options, "optimal")) optimal = evaluate("optimal");

      if (!first_point) std::cout << ",";
      first_point = false;
      std::cout << "{"
                << "\"cacheBlocks\":" << capacity;
      if (has_policy(options, "fifo")) {
        std::cout << ",\"fifo\":{";
        print_named_result("fifo", capacity, fifo, options.warmup_requests);
        std::cout << "}";
      }
      if (has_policy(options, "lru")) {
        std::cout << ",\"lru\":{";
        print_named_result("lru", capacity, lru, options.warmup_requests);
        std::cout << "}";
      }
      if (has_policy(options, "lru-flat")) {
        std::cout << ",\"lru-flat\":{";
        print_named_result("lru-flat", capacity, flat_lru, options.warmup_requests);
        std::cout << "}";
      }
      if (has_policy(options, "hyper-clock")) {
        std::cout << ",\"hyper-clock\":{";
        print_named_result("hyper-clock", capacity, hyper_clock, options.warmup_requests);
        std::cout << "}";
      }
      if (has_policy(options, "optimal")) {
        std::cout << ",\"optimal\":{";
        print_named_result("optimal", capacity, optimal, options.warmup_requests);
        std::cout << "}";
      }
      if (run_sampled) {
        std::cout << ",\"sampled-lru\":{";
        print_named_result("sampled-lru", capacity, sampled, options.warmup_requests);
        std::cout << "}";
      }
      std::cout << "}";
    }
    report_progress(options, total_steps, total_steps, "simulation complete");
    std::cout << "]}\n";
  }

 private:
  const SimulationInput& input;
  const Options& options;
  std::unique_ptr<RequestScheduler> scheduler;
};

}  // namespace

int main(int argc, char** argv) {
  try {
    const Options options = parse_args(argc, argv);
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
