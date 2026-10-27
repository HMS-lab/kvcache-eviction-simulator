#!/usr/bin/env python3
"""Convert nested SemiAnalysis conversations to chronologically ordered JSONL.

Only one source conversation is parsed at a time. Active conversations are
spooled to disk, with one serialized request per conversation held in the heap.
This script uses only the Python standard library and needs no package install.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import gzip
import heapq
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Iterator, TextIO


@dataclass
class ConversionStats:
    conversation_count: int = 0
    request_count: int = 0
    block_size: int = 0
    unique_hash_ids: int = 0
    replay_duration: float = 0.0


@dataclass
class _Conversation:
    index: int
    completion: float
    path: Path
    reader: TextIO


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _seconds(value: Any, name: str) -> float:
    try:
        valid = not isinstance(value, bool) and isinstance(value, (int, float))
        result = float(value) if valid else float("nan")
    except (OverflowError, ValueError):
        result = float("nan")
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be finite, nonnegative seconds")
    return result


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _flatten(
    entries: Any, path: str = "requests", agent_id: str | None = None,
) -> Iterator[tuple[dict[str, Any], str | None, str]]:
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path} must be a nonempty list")
    for index, entry in enumerate(entries):
        context = f"{path}[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{context} must be an object")
        if entry.get("type") == "subagent":
            inner_agent = _string(entry.get("agent_id"), f"{context}.agent_id")
            yield from _flatten(entry.get("requests"), f"{context}.requests", inner_agent)
        else:
            if "requests" in entry:
                raise ValueError(f"{context}: a request group must have type 'subagent'")
            yield entry, agent_id, context


@contextmanager
def _input_stream(path: str | Path) -> Iterator[TextIO]:
    if str(path) == "-":
        yield sys.stdin
    else:
        source = Path(path).expanduser()
        opener = gzip.open if source.suffix == ".gz" else open
        with opener(source, "rt", encoding="utf-8") as handle:
            yield handle


@contextmanager
def _output_stream(path: str | Path) -> Iterator[TextIO]:
    if str(path) == "-":
        yield sys.stdout
        return
    destination = Path(path).expanduser()
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(name)
    try:
        with ExitStack() as stack:
            raw = stack.enter_context(os.fdopen(descriptor, "wb"))
            stream = raw
            if destination.suffix == ".gz":
                stream = stack.enter_context(gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0))
            text = stack.enter_context(io.TextIOWrapper(stream, encoding="utf-8", newline="\n"))
            yield text
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _spool_next(
    lines: Iterator[tuple[int, str]], start: float, directory: Path, stats: ConversionStats,
) -> _Conversation | None:
    for line_number, raw in lines:
        if raw.strip():
            break
    else:
        return None
    try:
        conversation = json.loads(raw)
        del raw
        if not isinstance(conversation, dict):
            raise ValueError("conversation must be an object")
        conversation_id = _string(conversation.get("id"), "id")
        if conversation.get("hash_id_scope") != "local":
            raise ValueError("hash_id_scope must be 'local' for this converter")
        block_size = _integer(conversation.get("block_size"), "block_size", minimum=1)
        if stats.block_size and block_size != stats.block_size:
            raise ValueError(f"block_size {block_size} differs from earlier block_size {stats.block_size}")

        requests = []
        for request_index, (request, agent, context) in enumerate(_flatten(conversation.get("requests"))):
            timestamp = _seconds(request.get("t"), f"{context}.t")
            duration = _seconds(request.get("api_time"), f"{context}.api_time")
            completion = _seconds(start + timestamp + duration, f"{context} global completion")
            requests.append((timestamp, request_index, completion, request, agent, context))
        requests.sort(key=lambda item: (item[0], item[1]))

        index = stats.conversation_count
        path = directory / f"conversation-{index}.spool"
        # The map is local to this occurrence of a conversation. The monotonically
        # increasing output IDs avoid retaining a global map or long hash strings.
        hashes: dict[tuple[str, str], int] = {}
        completion = start
        with path.open("w", encoding="utf-8", newline="\n") as spool:
            for relative, request_index, end, request, agent, context in requests:
                model = _string(request.get("model"), f"{context}.model")
                input_length = _integer(request.get("in"), f"{context}.in", minimum=1)
                output_length = _integer(request.get("out"), f"{context}.out")
                hash_ids = request.pop("hash_ids", None)
                if not isinstance(hash_ids, list) or not hash_ids:
                    raise ValueError(f"{context}.hash_ids must be a nonempty list")
                for position, hash_id in enumerate(hash_ids):
                    if isinstance(hash_id, bool) or not isinstance(hash_id, (int, str)):
                        raise ValueError(f"{context}.hash_ids[{position}] must be an integer or string")
                    key = (model, str(hash_id))
                    assigned = hashes.get(key)
                    if assigned is None:
                        assigned = stats.unique_hash_ids
                        stats.unique_hash_ids += 1
                        hashes[key] = assigned
                    hash_ids[position] = assigned
                timestamp = start + relative
                record = {
                    "timestamp": timestamp,
                    "completion_timestamp": end,
                    "input_length": input_length,
                    "output_length": output_length,
                    "block_size": block_size,
                    "hash_ids": hash_ids,
                    # Always the root conversation ID, including nested requests.
                    "conversation_id": conversation_id,
                    "conversation_index": index,
                    "request_index": request_index,
                    "relative_timestamp": relative,
                    "conversation_start_timestamp": start,
                    "model": model,
                    "agent_id": agent,
                }
                for field in ("api_time", "think_time", "ttft", "type"):
                    if field in request:
                        record[field] = request[field]
                # A small header lets the merger schedule a serialized request
                # without parsing its potentially large hash array a second time.
                spool.write(f"{timestamp!r} {request_index}\n")
                spool.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
                completion = max(completion, end)
                stats.request_count += 1
        stats.conversation_count += 1
        stats.block_size = block_size
        stats.replay_duration = max(stats.replay_duration, completion)
        return _Conversation(index, completion, path, path.open("r", encoding="utf-8"))
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError(f"line {line_number}: {error}") from error


def _queue_request(conversation: _Conversation, events: list) -> None:
    header = conversation.reader.readline()
    if header:
        timestamp, index = header.split()
        serialized = conversation.reader.readline()
        heapq.heappush(events, (float(timestamp), 0, conversation.index, int(index), conversation, serialized))


def convert(
    input_path: str | Path,
    output_path: str | Path,
    *,
    concurrency: int = 1,
    temp_dir: str | Path | None = None,
    progress: bool = False,
) -> ConversionStats:
    """Convert using rolling slots; timestamps are seconds and indexes are zero-based."""
    _integer(concurrency, "concurrency", minimum=1)
    if str(input_path) != "-" and str(output_path) != "-":
        source, destination = Path(input_path).expanduser(), Path(output_path).expanduser()
        if source.resolve() == destination.resolve() or (
            source.exists() and destination.exists() and source.samefile(destination)
        ):
            raise ValueError("input and output must be different files")
    stats = ConversionStats()
    temp_root = Path(temp_dir).expanduser() if temp_dir is not None else None
    total_lines: int | str = "?"
    if progress and str(input_path) != "-":
        with _input_stream(input_path) as source:
            total_lines = sum(1 for _ in source)
    with _input_stream(input_path) as source, _output_stream(output_path) as output:
        with tempfile.TemporaryDirectory(prefix="kvcache-convert-", dir=temp_root) as directory:
            processed_lines = 0

            def numbered_lines() -> Iterator[tuple[int, str]]:
                nonlocal processed_lines
                for processed_lines, raw in enumerate(source, 1):
                    yield processed_lines, raw

            lines = numbered_lines()
            events: list = []
            active: dict[int, _Conversation] = {}

            def activate(start: float) -> bool:
                conversation = _spool_next(lines, start, Path(directory), stats)
                if progress:
                    print(f"\r{processed_lines}/{total_lines}", end="", file=sys.stderr, flush=True)
                if conversation is None:
                    return False
                active[conversation.index] = conversation
                _queue_request(conversation, events)
                # Submissions sort before completions at the same timestamp, so
                # even a final zero-duration request is written before cleanup.
                heapq.heappush(events, (conversation.completion, 1, conversation.index, -1, conversation, ""))
                return True

            try:
                if progress:
                    print(f"0/{total_lines}", end="", file=sys.stderr, flush=True)
                for _ in range(concurrency):
                    if not activate(0.0):
                        break
                if not active:
                    raise ValueError("input contains no conversations")
                while events:
                    timestamp, kind, _, _, conversation, serialized = heapq.heappop(events)
                    if kind == 0:
                        output.write(serialized)
                        _queue_request(conversation, events)
                    else:
                        conversation.reader.close()
                        conversation.path.unlink()
                        del active[conversation.index]
                        activate(timestamp)
            finally:
                for conversation in active.values():
                    conversation.reader.close()
                if progress:
                    print(file=sys.stderr)
    return stats


def _positive_integer(value: str) -> int:
    try:
        return _integer(int(value), "concurrency", minimum=1)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Source conversation JSONL/JSONL.GZ, or - for stdin")
    parser.add_argument("--output", required=True, help="Destination request JSONL/JSONL.GZ, or - for stdout")
    parser.add_argument("--concurrency", type=_positive_integer, default=1, help="Active conversation slots, including gaps between turns (default: 1)")
    parser.add_argument("--temp-dir", help="Directory for conversation spools (default: system temporary directory)")
    args = parser.parse_args(argv)
    try:
        stats = convert(args.input, args.output, concurrency=args.concurrency, temp_dir=args.temp_dir, progress=True)
    except (ValueError, OSError, EOFError) as error:
        print(f"Conversion failed: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Conversion interrupted.", file=sys.stderr)
        return 130
    print(
        f"Converted {stats.conversation_count} conversations, {stats.request_count} requests; "
        f"block_size={stats.block_size}; replay_duration={stats.replay_duration:.6f}s.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
