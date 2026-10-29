from __future__ import annotations

from dataclasses import dataclass
import math
import multiprocessing as mp
from typing import Any, Callable

from .calculator import BYTES_PER_GIB, calculate_cache_size, load_models_data, models_by_id
from .cpp_backend import run_cpp_sweep
from .plan import ExecutionPlan, build_execution_plan
from .policies import PolicyResult, simulate_ceiling, simulate_policy, validate_hyper_clock_max_score
from .sampled import validate_parameters
from .trace import TraceData

DEFAULT_BUDGETS_GIB = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384]
DEFAULT_POLICIES = ["fifo", "lru", "lru-flat", "hyper-clock", "optimal", "sampled-lru"]
THROUGHPUT_EPSILON = 0.001
THROUGHPUT_DISPLAY_CAP = 1000.0

_WORKER_PLAN: ExecutionPlan | None = None
ProgressCallback = Callable[[float, float, str], None]


@dataclass
class SweepPoint:
    gib: float
    cacheBlocks: int
    results: dict[str, PolicyResult]


def throughput_from_hit_rate(hit_rate: float) -> float:
    miss_fraction = max(1.0 - max(0.0, min(1.0, hit_rate)), THROUGHPUT_EPSILON)
    return min(THROUGHPUT_DISPLAY_CAP, 1.0 / miss_fraction)


def cache_blocks_for_gib(gib: float, bytes_per_block: float) -> int:
    if not math.isfinite(bytes_per_block) or bytes_per_block <= 0:
        return 0
    return max(0, math.floor(gib * BYTES_PER_GIB / bytes_per_block))


def _worker_simulate(task: tuple[str, int, int, dict[str, Any]]) -> tuple[tuple[str, int], PolicyResult]:
    if _WORKER_PLAN is None:
        raise RuntimeError("Worker plan is not initialized")
    policy, capacity, max_score, sampled = task
    return (policy, capacity), simulate_policy(_WORKER_PLAN, policy, capacity, hyper_clock_max_score=max_score, **sampled)


def _simulate_tasks(plan: ExecutionPlan, tasks: list[tuple[str, int]], jobs: int, *, hyper_clock_max_score: int = 3, **sampled) -> dict[tuple[str, int], PolicyResult]:
    if not tasks:
        return {}
    if jobs <= 1 or "fork" not in mp.get_all_start_methods():
        return {task: simulate_policy(plan, task[0], task[1], hyper_clock_max_score=hyper_clock_max_score, **sampled) for task in tasks}

    global _WORKER_PLAN
    _WORKER_PLAN = plan
    context = mp.get_context("fork")
    try:
        with context.Pool(processes=min(jobs, len(tasks))) as pool:
            return dict(pool.imap_unordered(_worker_simulate, ((policy, capacity, hyper_clock_max_score, sampled) for policy, capacity in tasks)))
    finally:
        _WORKER_PLAN = None


def estimate_tokens_for_trace(trace: TraceData) -> int:
    return max(1, math.floor(trace.average_input_tokens or 1))


def warmup_requests_for_trace(trace: TraceData, warmup_fraction: float) -> int:
    return max(0, min(trace.request_count, math.floor(trace.request_count * warmup_fraction)))


def measured_tokens_for_trace(trace: TraceData, warmup_requests: int) -> int:
    total = 0
    for request_index in range(warmup_requests, trace.request_count):
        start = trace.request_starts[request_index]
        end = trace.request_starts[request_index + 1]
        total += sum(trace.tokens[start:end])
    return total


def _no_cache_result(policy: str, capacity: int, warmup_requests: int, total_measured_tokens: int) -> PolicyResult:
    return PolicyResult(policy, capacity, warmup_requests, warmup_requests, "fixed_window", 0, total_measured_tokens, 0.0)


def run_sweep(
    trace: TraceData,
    *,
    model_id: str,
    precision: str | None = None,
    indexer_precision: str | None = None,
    budgets_gib: list[float] | None = None,
    policies: list[str] | None = None,
    hyper_clock_max_score: int = 3,
    sampled_lru_threshold_seconds: float = 10.0,
    sampled_lru_sample_size: int = 16,
    sampled_lru_seed: int = 0,
    jobs: int = 1,
    backend: str = "cpp",
    progress: ProgressCallback | None = None,
    warmup_fraction: float = 0.5,
    estimate_tokens: int | None = None,
    include_draft_kv_cache: bool = False,
    models_data: dict[str, Any] | None = None,
    release_trace_data: bool = False,
) -> dict[str, Any]:
    """Simulate budgets; release_trace_data consumes event lists on the C++ backend."""
    validate_hyper_clock_max_score(hyper_clock_max_score)
    event_count = len(trace.ids)
    data = models_data or load_models_data()
    models = models_by_id(data)
    if model_id not in models:
        raise ValueError(f"Unknown model id: {model_id}")
    model = models[model_id]
    warmup_requests = warmup_requests_for_trace(trace, warmup_fraction)
    total_measured_tokens = measured_tokens_for_trace(trace, warmup_requests)
    accounting_tokens = max(1, int(estimate_tokens or estimate_tokens_for_trace(trace)))
    if progress:
        progress(1, 4, "calculating model memory")
    cache_size = calculate_cache_size(
        model,
        tokens=accounting_tokens,
        precision=precision,
        indexer_precision=indexer_precision,
        block_size=trace.block_size,
        include_draft_kv_cache=include_draft_kv_cache,
        include_linear_attention_state=False,
        models_data=data,
    )
    bytes_per_block = float(cache_size.bytes_per_block or cache_size.bytes_per_token * trace.block_size)
    selected_budgets = sorted(set(budgets_gib or DEFAULT_BUDGETS_GIB))
    selected_policies = list(policies or DEFAULT_POLICIES)
    sampled = dict(sampled_lru_threshold_seconds=sampled_lru_threshold_seconds,
                   sampled_lru_sample_size=sampled_lru_sample_size, sampled_lru_seed=sampled_lru_seed)
    skipped = {}
    if "sampled-lru" in selected_policies:
        validate_parameters(sampled_lru_threshold_seconds, sampled_lru_sample_size, sampled_lru_seed)
        if any(values is None or len(values) != trace.request_count for values in
               (trace.arrivals, trace.completions, trace.completion_order)):
            reason = trace.timing_error or "Complete request timing data is required"
            if policies:
                raise ValueError(f"sampled-lru: {reason}")
            skipped["sampled-lru"] = reason
            selected_policies.remove("sampled-lru")
    for policy in selected_policies:
        if policy not in DEFAULT_POLICIES:
            raise ValueError(f"Unsupported policy: {policy}")
    if backend not in {"cpp", "python"}:
        raise ValueError(f"Unsupported backend: {backend}")

    capacities = [(gib, cache_blocks_for_gib(gib, bytes_per_block)) for gib in selected_budgets]
    if backend == "cpp":
        cpp_capacities = []
        for _gib, capacity in capacities:
            if capacity > 0 and capacity not in cpp_capacities:
                cpp_capacities.append(capacity)
        def cpp_progress(done: int, total: int, label: str) -> None:
            if not progress:
                return
            progress(1 + (max(0, done) / max(1, total)) * 2, 4, label)

        cpp_result = run_cpp_sweep(
            trace,
            capacities=cpp_capacities,
            policies=selected_policies,
            warmup_requests=warmup_requests,
            hyper_clock_max_score=hyper_clock_max_score,
            **sampled,
            release_trace_data=release_trace_data,
            progress=cpp_progress,
        )
        unique_blocks = cpp_result.unique_blocks
        sim_results = cpp_result.results
        ceiling = cpp_result.ceiling
    else:
        if progress:
            progress(2, 4, "building Python execution plan")
        plan = build_execution_plan(trace, warmup_fraction=warmup_fraction)
        unique_blocks = plan.unique_blocks
        tasks: list[tuple[str, int]] = []
        seen_tasks: set[tuple[str, int]] = set()
        for _gib, capacity in capacities:
            if capacity <= 0:
                continue
            if unique_blocks > 0 and capacity > unique_blocks:
                break
            for policy in selected_policies:
                task = (policy, capacity)
                if task not in seen_tasks:
                    seen_tasks.add(task)
                    tasks.append(task)
        if progress:
            progress(3, 4, "running Python simulation")
        sim_results = _simulate_tasks(plan, tasks, max(1, int(jobs or 1)), hyper_clock_max_score=hyper_clock_max_score, **sampled)
        ceiling = simulate_ceiling(plan)
    points: list[dict[str, Any]] = []
    for gib, capacity in capacities:
        if unique_blocks > 0 and capacity > unique_blocks:
            break
        results: dict[str, Any] = {}
        underfilled = False
        for policy in selected_policies:
            if capacity <= 0:
                result = _no_cache_result(policy, capacity, warmup_requests, total_measured_tokens)
            else:
                result = sim_results[(policy, capacity)]
            if result.measurementMode == "underfilled_at_window":
                if policy != "sampled-lru" or len(selected_policies) == 1:
                    underfilled = True
            result_json = result.to_json()
            result_json["idealPrefillSpeedup"] = throughput_from_hit_rate(result.hitRate)
            results[policy] = result_json
        if underfilled:
            break
        points.append({"gib": gib, "cacheBlocks": capacity, "results": results})
    if progress:
        progress(4, 4, "formatting results")

    return {
        "metadata": {
            **({"policyParameters": {"hyper-clock": {"maxScore": hyper_clock_max_score}}} if "hyper-clock" in selected_policies else {}),
            "skippedPolicies": skipped,
            "replayModes": {p: "recorded" if p == "sampled-lru" else "sequential" for p in selected_policies},
            "ceilingReplayMode": "sequential",
            **({"sampledLruParameters": {"thresholdSeconds": sampled_lru_threshold_seconds,
                "sampleSize": sampled_lru_sample_size, "seed": sampled_lru_seed}} if "sampled-lru" in selected_policies else {}),
            "modelId": cache_size.model_id,
            "modelLabel": cache_size.model_label,
            "precision": cache_size.precision,
            "precisionLabel": cache_size.precision_label,
            "indexerPrecision": cache_size.indexer_precision,
            "indexerPrecisionLabel": cache_size.indexer_precision_label,
            "includeDraftKvCache": include_draft_kv_cache,
            "blockSize": trace.block_size,
            "bytesPerToken": cache_size.bytes_per_token,
            "bytesPerBlock": bytes_per_block,
            "estimateTokens": accounting_tokens,
            "requestCount": trace.request_count,
            "eventCount": event_count,
            "rawUniqueBlocks": trace.unique_raw_blocks,
            "uniqueBlocks": unique_blocks,
            "backend": backend,
            "warmupFraction": warmup_fraction,
            "warmupRequests": warmup_requests,
            "measurementWindow": "last_50_percent_requests" if warmup_fraction == 0.5 else f"last_{max(0.0, 1.0 - warmup_fraction):.6f}_fraction_requests",
            "underfilledBudgetPolicy": "omit_budget_and_larger_points",
            **({"sampledLruUnderfilledPolicy": "mark_unavailable_preserve_legacy_points"}
               if "sampled-lru" in selected_policies and len(selected_policies) > 1 else {}),
            "speedupBaseline": "1.0x equals no-cache prefill throughput where every prefill input token is computed.",
            "totalMeasuredTokens": total_measured_tokens,
            "averageInputTokens": trace.average_input_tokens,
            "parseErrors": trace.parse_errors,
            "skippedRecords": trace.skipped_records,
        },
        "hitRateCeiling": ceiling.hitRate,
        "ceiling": ceiling.to_json(),
        "points": points,
        "policies": selected_policies,
    }
