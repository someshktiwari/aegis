# eviction.py
# Two responsibilities:
#   1. is_expired() — point-in-time check used in proxy.py on each key lookup
#   2. eviction_loop() — background asyncio task that periodically bulk-deletes
#
# See DECISIONS.md D-008 for why both lazy (on-access) and eager (background)
# eviction are used together.

import asyncio
import time

import aiosqlite

from config import settings
from store import delete_expired


def is_expired(created_at: float) -> bool:
    """
    Returns True if the record has lived longer than TTL.
    Called in proxy.py before serving a cached response.

    Why check on access AND run a background sweep?
    - On-access check ensures we never serve a stale cached response even if
      the sweep hasn't run yet.
    - Background sweep ensures expired rows are physically deleted and don't
      accumulate storage indefinitely (high-churn keys with no repeat access).
    """
    return (time.time() - created_at) > settings.ttl_seconds


async def eviction_loop(db: aiosqlite.Connection) -> None:
    """
    Background task started in main.py lifespan.
    Sleeps for eviction_interval_seconds, then deletes all expired rows.
    Runs until the FastAPI lifespan context exits (app shutdown).

    The asyncio.CancelledError on shutdown is intentional — FastAPI's lifespan
    cancels the task cleanly when the app stops.
    """
    while True:
        await asyncio.sleep(settings.eviction_interval_seconds)
        deleted = await delete_expired(db)
        if deleted:
            print(f"[Aegis eviction] Deleted {deleted} expired idempotency record(s)")
