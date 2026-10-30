from __future__ import annotations

import sys


class ProgressBar:
    def __init__(self, *, enabled: bool, width: int = 28) -> None:
        self.enabled = enabled
        self.width = width
        self._active = False
        self._line_width = 0

    def update(self, done: float, total: float, label: str) -> None:
        if not self.enabled:
            return
        total_value = max(1.0, float(total))
        done_value = max(0.0, min(float(done), total_value))
        filled = round(self.width * done_value / total_value)
        bar = "#" * filled + "-" * (self.width - filled)
        percent = round(100 * done_value / total_value)
        line = f"[{bar}] {percent:3d}% {label}"
        sys.stderr.write("\r" + line.ljust(self._line_width))
        self._line_width = len(line)
        sys.stderr.flush()
        self._active = True

    def finish(self, label: str = "done") -> None:
        if not self.enabled:
            return
        self.update(1, 1, label)
        self.close()

    def close(self) -> None:
        if not self.enabled:
            return
        if self._active:
            sys.stderr.write("\n")
            sys.stderr.flush()
            self._active = False
