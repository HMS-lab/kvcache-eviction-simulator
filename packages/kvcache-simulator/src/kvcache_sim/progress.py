from __future__ import annotations

import shutil
import re
import sys
import time


class ProgressBar:
    """Render progress on the calling thread, with one fixed row per worker."""

    def __init__(self, *, enabled: bool, width: int = 28) -> None:
        self.enabled = enabled
        self.width = width
        self._active = False
        self._line_width = 0
        self._row_count = 0
        self._workers: dict[int, tuple[int, str]] = {}
        self._overall = ""
        self._job_counts: tuple[int, int, int] | None = None
        self._last_draw = 0.0

    def update(self, done: float, total: float, label: str) -> None:
        if not self.enabled:
            return
        total_value = max(1.0, float(total))
        done_value = max(0.0, min(float(done), total_value))
        filled = round(self.width * done_value / total_value)
        bar = "#" * filled + "-" * (self.width - filled)
        percent = round(100 * done_value / total_value)
        if self._job_counts is None:
            self._overall = f"[{bar}] {percent:3d}% {label}"
        self._draw(force=done_value == total_value)

    def update_jobs(self, done: int, total: int, running: int) -> None:
        if not self.enabled:
            return
        first = self._job_counts is None
        self._job_counts = (done, total, running)
        self._overall = f"Jobs: {total} total | {done} finished | {running} running"
        self._draw(force=first or done == total)

    @staticmethod
    def _worker_label(label: str) -> str:
        match = re.match(r"^(.*?) \(cache capacity \d+/\d+, (\d+) blocks; policy \d+/\d+\)(.*)$", label)
        if match:
            policy, capacity, progress = match.groups()
            policy = policy.replace(":sample-size=", " sample=")
            return f"{policy} | {capacity} blocks{progress}"
        return label

    def update_worker(self, worker: int, job: int, label: str) -> None:
        if not self.enabled:
            return
        new_worker = worker not in self._workers
        self._workers[worker] = (job, self._worker_label(label))
        self._draw(force=new_worker)

    def _draw(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if self._workers and not force and now - self._last_draw < 0.2:
            return
        if not self._workers and self._row_count <= 1:
            sys.stderr.write("\r" + self._overall.ljust(self._line_width))
            self._line_width = len(self._overall)
            self._row_count = 1
        else:
            lines = [self._overall]
            for worker in range(max(self._workers, default=-1) + 1):
                if worker in self._workers:
                    job, label = self._workers[worker]
                    lines.append(f"Worker {worker}: {label}")
                else:
                    lines.append(f"Worker {worker}: waiting")
            # Keep each row on one terminal line, preserving the progress suffix.
            columns = max(4, shutil.get_terminal_size().columns - 1)
            def fit(line: str) -> str:
                if len(line) <= columns:
                    return line
                left = (columns - 3) // 2
                return line[:left] + "..." + line[-(columns - 3 - left):]
            if self._row_count > 1:
                sys.stderr.write(f"\x1b[{self._row_count - 1}A")
            count = max(self._row_count, len(lines))
            frame = ["\r\x1b[2K" + (fit(lines[n]) if n < len(lines) else "")
                     for n in range(count)]
            sys.stderr.write("\n".join(frame))
            if count > len(lines):
                sys.stderr.write(f"\x1b[{count - len(lines)}A")
            self._row_count = len(lines)
        sys.stderr.flush()
        self._last_draw = now
        self._active = True

    def finish(self, label: str = "done") -> None:
        if not self.enabled:
            return
        self._workers.clear()
        self._job_counts = None
        self.update(1, 1, label)
        self.close()

    def close(self) -> None:
        if not self.enabled:
            return
        if self._active:
            if self._workers:
                self._draw(force=True)
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._active = False
