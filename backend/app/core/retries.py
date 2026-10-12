from __future__ import annotations

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import isfinite


def retry_after_seconds(value):
    if not value:
        return None
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            stamp = parsedate_to_datetime(value)
            delay = (stamp - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, delay) if isfinite(delay) else None


def retry_delay(attempt, *, retry_after=None, maximum=5.0):
    # Do not retry earlier than Retry-After. A long wait exceeds this bounded
    # workflow's budget, so return the error rather than sending another request.
    if retry_after is not None and retry_after > maximum:
        return None
    base = min(0.25 * (2 ** attempt), maximum)
    return max(retry_after or 0, min(maximum, base + random.uniform(0, base)))
