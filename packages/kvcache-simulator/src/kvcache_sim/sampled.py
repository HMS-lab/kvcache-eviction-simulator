"""Recorded-time replay, isolated from the legacy policy implementations."""
from __future__ import annotations

import heapq
import random
from dataclasses import dataclass

from .trace import seconds_us


def validate_parameters(threshold: float, sample_size: int, seed: int) -> int:
    threshold_us = seconds_us(threshold)
    if isinstance(sample_size, bool) or not isinstance(sample_size, int) or not 1 <= sample_size <= 2**32 - 1:
        raise ValueError("sampled_lru_sample_size must be a positive uint32 integer")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**64:
        raise ValueError("sampled_lru_seed must be an unsigned 64-bit integer")
    return threshold_us


@dataclass
class Slot:
    node: int
    timestamp: int
    sequence: int


class SampledCache:
    def __init__(self, capacity: int, threshold_us: int, sample_size: int, seed: int):
        self.capacity, self.threshold, self.sample_size = capacity, threshold_us, sample_size
        self.rng = random.Random(seed)
        self.entries: dict[int, int] = {}
        self.slots: list[Slot | None] = []
        self.free: list[int] = []
        self.sequence = 0

    def lookup(self, node: int, now: int) -> bool:
        index = self.entries.get(node)
        if index is None:
            return False
        self.sequence += 1
        self.slots[index] = Slot(node, now, self.sequence)
        return True

    def discard(self, index: int) -> None:
        slot = self.slots[index]
        assert slot is not None
        del self.entries[slot.node]
        self.slots[index] = None
        self.free.append(index)

    def evict(self, now: int) -> None:
        # Called only when full: all allocated slots are occupied.
        eligible = lambda slot, threshold: threshold == 0 or now - slot.timestamp > threshold
        sampled = self.rng.sample(range(len(self.slots)), min(self.sample_size, len(self.slots)))
        victims = [i for i in sampled if eligible(self.slots[i], self.threshold)]
        for index in sorted(victims, key=lambda i: (self.slots[i].timestamp, self.slots[i].sequence, i)):
            self.discard(index)
        if victims:
            return
        threshold = self.threshold
        while True:
            for index, slot in enumerate(self.slots):
                if slot is not None and eligible(slot, threshold):
                    self.discard(index)
                    return
            threshold //= 2

    def write(self, node: int, now: int) -> None:
        if self.capacity <= 0 or self.lookup(node, now):
            return
        if len(self.entries) == self.capacity:
            self.evict(now)
        self.sequence += 1
        slot = Slot(node, now, self.sequence)
        if self.free:
            index = self.free.pop()
            self.slots[index] = slot
        else:
            index = len(self.slots)
            self.slots.append(slot)
        self.entries[node] = index


def simulate_sampled(plan, capacity, *, threshold=10.0, sample_size=16, seed=0):
    # Local import avoids coupling legacy policy code to this scheduler.
    from .policies import _finish, _no_cache, _underfilled
    threshold_us = validate_parameters(threshold, sample_size, seed)
    if any(values is None or len(values) != plan.request_count for values in
           (plan.arrivals, plan.completions, plan.completion_order)):
        raise ValueError("sampled-lru requires complete request timing data")
    if capacity <= 0:
        return _no_cache("sampled-lru", capacity, plan)
    cache = SampledCache(capacity, threshold_us, sample_size, seed)
    pending = []
    cursor = hits = 0
    full = False

    def complete(now):
        nonlocal full
        while pending and pending[0][0] == now:
            _, _, request = heapq.heappop(pending)
            for event in range(plan.request_starts[request], plan.request_starts[request + 1]):
                cache.write(plan.node_for_event[event], now)
                full |= len(cache.entries) == capacity

    while cursor < plan.request_count or pending:
        arrival = plan.arrivals[cursor] if cursor < plan.request_count else 2**64
        now = min(arrival, pending[0][0] if pending else 2**64)
        complete(now)
        while cursor < plan.request_count and plan.arrivals[cursor] == now:
            if cursor >= plan.warmup_requests and not full:
                return _underfilled("sampled-lru", capacity, plan)
            for event in range(plan.request_starts[cursor], plan.request_starts[cursor + 1]):
                if not cache.lookup(plan.node_for_event[event], now):
                    break
                if cursor >= plan.warmup_requests:
                    hits += plan.tokens[event]
            heapq.heappush(pending, (plan.completions[cursor], plan.completion_order[cursor], cursor))
            cursor += 1
        complete(now)
    if not full or plan.total_measured_tokens <= 0:
        return _underfilled("sampled-lru", capacity, plan)
    return _finish("sampled-lru", capacity, plan, hits, plan.total_measured_tokens)
