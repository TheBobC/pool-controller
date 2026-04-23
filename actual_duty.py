"""
actual_duty.py — ACS712-based actual gate duty cycle tracking.

Tracks the fraction of time the cell gate was physically energized (ACS712
reading > CURRENT_THRESHOLD_A) over a rolling WINDOW_S window.

confidence_pct() reports what fraction of the full window has accumulated
data since the last reset, so the dashboard can shade the tile during the
initial warmup period when only partial data is available.
"""
import collections
import time


class ActualDutyTracker:
    WINDOW_S = 86400         # 24-hour rolling window
    CURRENT_THRESHOLD_A = 1.0

    def __init__(self) -> None:
        self.samples: collections.deque = collections.deque()
        self.start_time: float = time.time()

    def sample(self, current_a: float) -> None:
        now = time.time()
        self.samples.append((now, current_a > self.CURRENT_THRESHOLD_A))
        cutoff = now - self.WINDOW_S
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def actual_duty_pct(self) -> int:
        if not self.samples:
            return 0
        on_count = sum(1 for _, on in self.samples if on)
        return int(100 * on_count / len(self.samples))

    def confidence_pct(self) -> int:
        elapsed = min(time.time() - self.start_time, self.WINDOW_S)
        return int(100 * elapsed / self.WINDOW_S)

    def reset(self) -> None:
        """Reset when cell re-enabled after being off; warmup restarts."""
        self.samples.clear()
        self.start_time = time.time()
