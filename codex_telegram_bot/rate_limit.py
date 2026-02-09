from __future__ import annotations

import asyncio
import time


class RateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self._interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait_turn(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_allowed - now)
            if wait > 0:
                self._next_allowed += self._interval
            else:
                self._next_allowed = now + self._interval

        if wait > 0:
            await asyncio.sleep(wait)
