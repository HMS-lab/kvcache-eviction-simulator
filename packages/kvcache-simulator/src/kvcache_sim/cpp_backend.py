from __future__ import annotations

from array import array
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Callable, Iterable

from .policies import PolicyResult, validate_hyper_clock_max_score
from ._resources import package_resource_path, user_temp_suffix
from .sampled import validate_parameters
from .trace import TraceData

UINT32_MAX = 2**32 - 1
UINT16_MAX = 2**16 - 1
ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class CppSweepResult:
    unique_blocks: int
    trie_node_count: int
    ceiling: PolicyResult
    results: dict[tuple[str, int], PolicyResult]


@dataclass(frozen=True)
class _BinaryTraceFiles:
    ids: Path
    tokens: Path
    request_ends: Path
    timing: tuple[Path, ...] = ()


def _cpp_source_path() -> Path:
    return package_resource_path("kv-cache-lab-native-sim.cc")


def _build_path_for_source(source: Path) -> Path:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"kvcache-sim-cpp{user_temp_suffix()}-{digest}"


def ensure_cpp_simulator(progress: ProgressCallback | None = None) -> Path:
    source = _cpp_source_path()
    output = _build_path_for_source(source)
    if output.exists() and os.access(output, os.X_OK):
        return output
    compiler = shutil.which("c++")
    if not compiler:
        raise RuntimeError("C++ simulator backend requires a C++ compiler. Install c++/clang++ or run with --backend python.")
    if progress:
        progress(0, 1, "compiling C++ simulator")
    subprocess.run([compiler, "-std=c++17", "-O3", str(source), "-o", str(output)], check=True, capture_output=True, text=True)
    return output


def _write_array(path: Path, typecode: str, values: Iterable[int]) -> None:
    data = array(typecode, values)
    expected_size = {"I": 4, "H": 2, "Q": 8}[typecode]
    if data.itemsize != expected_size:
        raise RuntimeError(f"Unexpected array('{typecode}') item size: {data.itemsize}")
    with path.open("wb") as handle:
        data.tofile(handle)


def _write_binary_trace(trace: TraceData, directory: Path) -> _BinaryTraceFiles:
    if len(trace.ids) > UINT32_MAX:
        raise ValueError("C++ backend supports at most 2^32 - 1 trace blocks")
    if trace.request_starts[-1] > UINT32_MAX:
        raise ValueError("C++ backend requires request end offsets to fit in uint32")
    if any(value < 0 or value > UINT32_MAX for value in trace.ids):
        raise ValueError("C++ backend requires interned hash ids to fit in uint32")
    if any(value <= 0 or value > UINT16_MAX for value in trace.tokens):
        raise ValueError("C++ backend requires per-block token weights to fit in uint16")

    ids_path = directory / "ids.u32"
    tokens_path = directory / "tokens.u16"
    request_ends_path = directory / "request_ends.u32"
    _write_array(ids_path, "I", trace.ids)
    _write_array(tokens_path, "H", trace.tokens)
    _write_array(request_ends_path, "I", trace.request_starts[1:])

    timing = ()
    if trace.arrivals is not None:
        timing = tuple(directory / name for name in ("arrivals.u64", "completions.u64", "order.u32"))
        for path, code, values in zip(timing, ("Q", "Q", "I"), (trace.arrivals, trace.completions, trace.completion_order)):
            if values is None or len(values) != trace.request_count:
                raise ValueError("Incomplete request timing data")
            _write_array(path, code, values)
    return _BinaryTraceFiles(ids=ids_path, tokens=tokens_path, request_ends=request_ends_path, timing=timing)


def _measurement_mode(measurement_start: int | None) -> str:
    return "underfilled_at_window" if measurement_start == -2 else "fixed_window"


def _to_policy_result(policy: str, capacity: int, warmup_requests: int, payload: dict) -> PolicyResult:
    measurement_start = int(payload.get("measurementStartRequest", warmup_requests))
    mode = _measurement_mode(measurement_start)
    return PolicyResult(
        policy=policy,
        cacheBlocks=capacity,
        warmupRequests=warmup_requests,
        measurementStartRequest=warmup_requests if mode == "fixed_window" else None,
        measurementMode=mode,
        hitTokens=int(payload.get("hitTokens", 0)),
        totalTokens=int(payload.get("totalTokens", 0)),
        hitRate=float(payload.get("hitRate", 0.0)),
    )


def _read_progress(process: subprocess.Popen[str], progress: ProgressCallback | None, total_prefix: int) -> list[str]:
    stderr_lines: list[str] = []
    if process.stderr is None:
        return stderr_lines
    for raw in process.stderr:
        line = raw.rstrip("\n")
        if line.startswith("KV_PROGRESS "):
            parts = line.split(" ", 3)
            if len(parts) == 4 and progress:
                try:
                    done = int(parts[1])
                    total = int(parts[2])
                except ValueError:
                    stderr_lines.append(line)
                    continue
                progress(total_prefix + done, total_prefix + total, parts[3])
            continue
        stderr_lines.append(line)
    return stderr_lines


def run_cpp_sweep(
    trace: TraceData,
    *,
    capacities: list[int],
    policies: list[str],
    warmup_requests: int,
    hyper_clock_max_score: int = 3,
    sampled_lru_threshold_seconds: float = 10.0,
    sampled_lru_sample_size: int = 16,
    sampled_lru_seed: int = 0,
    release_trace_data: bool = False,
    progress: ProgressCallback | None = None,
) -> CppSweepResult:
    """Run native replay; optionally consume the trace's event lists after writing."""
    validate_hyper_clock_max_score(hyper_clock_max_score)
    threshold_us = (validate_parameters(sampled_lru_threshold_seconds, sampled_lru_sample_size, sampled_lru_seed)
                    if "sampled-lru" in policies else 10_000_000)
    if "sampled-lru" in policies and trace.arrivals is None:
        raise ValueError("sampled-lru requires request timing data")
    event_count = len(trace.ids)
    binary = ensure_cpp_simulator(progress)
    safe_capacities = []
    for capacity in capacities:
        if capacity < 0:
            continue
        if capacity > UINT32_MAX:
            continue
        if capacity not in safe_capacities:
            safe_capacities.append(capacity)

    with tempfile.TemporaryDirectory(prefix="kvcache-sim-cpp-") as tmpdir:
        if progress:
            progress(0, max(1, len(safe_capacities) + 2), "writing binary trace")
        files = _write_binary_trace(trace, Path(tmpdir))
        if release_trace_data:
            # Keep scalar metadata, but drop the large lists before C++ loads
            # its own arrays. Reassignment also keeps TraceData's field types.
            trace.ids = []
            trace.tokens = []
            trace.request_starts = []
            trace.arrivals = trace.completions = trace.completion_order = None
        command = [
            str(binary),
            "--policy",
            "batch",
            "--ids",
            str(files.ids),
            "--tokens",
            str(files.tokens),
            "--request-ends",
            str(files.request_ends),
            "--request-count",
            str(trace.request_count),
            "--total-blocks",
            str(event_count),
            "--warmup-requests",
            str(warmup_requests),
            "--hyper-clock-max-score",
            str(hyper_clock_max_score),
            "--capacities",
            ",".join(str(capacity) for capacity in safe_capacities),
            "--policies",
            ",".join(policies),
        ]
        if "sampled-lru" in policies:
            for flag, path in zip(("--request-arrivals", "--request-completions", "--request-order"), files.timing):
                command.extend((flag, str(path)))
            command.extend(("--sampled-lru-threshold-us", str(threshold_us),
                            "--sampled-lru-sample-size", str(sampled_lru_sample_size),
                            "--sampled-lru-seed", str(sampled_lru_seed)))
        if progress:
            command.append("--progress")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stderr_lines: list[str] = []
        stderr_thread = threading.Thread(target=lambda: stderr_lines.extend(_read_progress(process, progress, 1)), daemon=True)
        stderr_thread.start()
        stdout = process.stdout.read() if process.stdout else ""
        return_code = process.wait()
        stderr_thread.join()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
        if return_code != 0:
            detail = "\n".join(stderr_lines).strip()
            raise RuntimeError(f"C++ simulator failed with exit code {return_code}" + (f": {detail}" if detail else ""))
    payload = json.loads(stdout)
    results: dict[tuple[str, int], PolicyResult] = {}
    for point in payload.get("points", []):
        capacity = int(point["cacheBlocks"])
        for policy in policies:
            if policy in point:
                results[(policy, capacity)] = _to_policy_result(policy, capacity, warmup_requests, point[policy])
    ceiling_capacity = max(1, int(payload.get("uniqueBlocks", 0)))
    ceiling = _to_policy_result("ceiling", ceiling_capacity, warmup_requests, payload["ceiling"])
    return CppSweepResult(
        unique_blocks=int(payload.get("uniqueBlocks", 0)),
        trie_node_count=int(payload.get("trieNodeCount", 0)),
        ceiling=ceiling,
        results=results,
    )
