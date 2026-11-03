#!/usr/bin/env python3
"""Microbenchmark full event scans against the active-claim scheduler."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import time
import tracemalloc


@dataclass(frozen=True)
class Event:
    event_id: str
    effective_day: int
    registration_day: int


def _measure_reference_scan(events: list[Event], session_count: int) -> tuple[float, int, str]:
    digest = hashlib.sha256()
    checks = 0
    tracemalloc.start()
    started = time.perf_counter()
    for day in range(session_count):
        active_ids = tuple(
            event.event_id for event in events
            if event.effective_day <= day < event.registration_day
        )
        checks += len(events)
        digest.update("\0".join(active_ids).encode("utf-8"))
    elapsed = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return elapsed, peak, digest.hexdigest()


def _measure_active_set(events: list[Event], session_count: int) -> tuple[float, int, str, int]:
    starts: dict[int, list[Event]] = {}
    ends: dict[int, list[Event]] = {}
    active: dict[str, Event] = {}
    for event in events:
        if event.effective_day < 0 < event.registration_day:
            active[event.event_id] = event
        else:
            starts.setdefault(event.effective_day, []).append(event)
        if event.registration_day >= 0:
            ends.setdefault(event.registration_day, []).append(event)

    digest = hashlib.sha256()
    checks = 0
    tracemalloc.start()
    started = time.perf_counter()
    for day in range(session_count):
        for event in ends.get(day, ()):
            active.pop(event.event_id, None)
        for event in starts.get(day, ()):
            if event.effective_day <= day < event.registration_day:
                active[event.event_id] = event
        active_ids = tuple(sorted(active))
        checks += len(active)
        digest.update("\0".join(active_ids).encode("utf-8"))
    elapsed = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return elapsed, peak, digest.hexdigest(), checks


def run_benchmark(*, event_count: int, session_count: int, active_count: int) -> dict[str, object]:
    if not 0 < active_count <= event_count:
        raise ValueError("active_count 必须在 1..event_count 范围内")
    events = [
        Event(f"historical-{index:08d}", -2, -1)
        for index in range(event_count - active_count)
    ]
    events.extend(
        Event(f"active-{index:08d}", -1, session_count + 1)
        for index in range(active_count)
    )
    reference_seconds, reference_peak, reference_digest = _measure_reference_scan(events, session_count)
    active_seconds, active_peak, active_digest, active_checks = _measure_active_set(events, session_count)
    return {
        "scenario": "synthetic scheduler microbenchmark; not market or strategy performance",
        "event_count": event_count,
        "session_count": session_count,
        "active_claim_count": active_count,
        "reference_event_checks": event_count * session_count,
        "active_set_event_checks": active_checks,
        "reference_seconds": round(reference_seconds, 6),
        "active_set_seconds": round(active_seconds, 6),
        "speedup": round(reference_seconds / active_seconds, 2) if active_seconds else None,
        "reference_peak_tracemalloc_bytes": reference_peak,
        "active_set_peak_tracemalloc_bytes": active_peak,
        "identical_daily_active_claims": reference_digest == active_digest,
        "active_claim_digest": active_digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=20_000)
    parser.add_argument("--sessions", type=int, default=250)
    parser.add_argument("--active-claims", type=int, default=200)
    args = parser.parse_args()
    print(json.dumps(run_benchmark(
        event_count=args.events,
        session_count=args.sessions,
        active_count=args.active_claims,
    ), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
