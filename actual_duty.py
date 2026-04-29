"""
actual_duty.py — ACS712-based actual cell output tracking.

Samples cell current at 1 Hz into a 1800-sample (30-minute) rolling window.
actual_duty_pct() returns the window average in amps as a percentage of the
expected full-on current (config.CELL_FULL_ON_AMPS), so dashboard "Cell Actual"
reflects real average amperes — not just gate on/off time.

confidence_pct() reports how much to trust the reading:
  - Warmup: ramps 0→100 over the first 30 min after start/reset.
  - After cell de-energizes: ramps 100→0 over the next 30 min, because
    the window data represents the past on-state, not the current state.
"""
import collections
import time

import config


class ActualDutyTracker:
    WINDOW_S = 1800  # 30-minute rolling window (SPEC §3.11)

    def __init__(self) -> None:
        self.samples: collections.deque = collections.deque()  # (timestamp, amps)
        self.start_time: float = time.time()
        self._deenergize_time: float | None = None

    def sample(self, current_a: float) -> None:
        now = time.time()
        self.samples.append((now, current_a))
        cutoff = now - self.WINDOW_S
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def actual_duty_pct(self) -> int:
        if not self.samples:
            return 0
        avg_a = sum(a for _, a in self.samples) / len(self.samples)
        return min(100, int(100 * avg_a / config.CELL_FULL_ON_AMPS))

    def confidence_pct(self) -> int:
        now = time.time()
        warmup_pct = int(100 * min(now - self.start_time, self.WINDOW_S) / self.WINDOW_S)
        if self._deenergize_time is not None:
            age = now - self._deenergize_time
            deenergize_pct = max(0, int(100 - 100 * age / self.WINDOW_S))
        else:
            deenergize_pct = 100
        return min(warmup_pct, deenergize_pct)

    def notify_cell_off(self) -> None:
        """Record the moment the cell de-energizes; confidence begins to ramp down."""
        self._deenergize_time = time.time()

    def reset(self) -> None:
        """Reset when cell re-enabled after being off; warmup and deenergize tracking restart."""
        self.samples.clear()
        self.start_time = time.time()
        self._deenergize_time = None
