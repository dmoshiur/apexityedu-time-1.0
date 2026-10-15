"""Exponential backoff with jitter — shared by sync + collectors."""
from __future__ import annotations

import random


def backoff_seconds(attempt: int, *, base: float = 30.0, cap: float = 1800.0,
                    jitter: float = 0.2) -> float:
    """Delay before retry number ``attempt`` (attempt starts at 1)."""
    attempt = max(1, attempt)
    delay = min(base * (2 ** (attempt - 1)), cap)
    spread = delay * jitter
    return max(1.0, delay - spread + 2 * spread * random.random())
