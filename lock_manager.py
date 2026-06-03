# lock_manager.py
# Maintains asyncio.Lock for every idempotency key.
# See DECISIONS.md D-004 for the full rationale.
#
# Why asyncio.Lock and not a DB-level lock?
# - SQLite does not support SELECT FOR UPDATE.
# - asyncio.Lock is per-key and in-memory: only requests sharing the exact
#   same idempotency key contend. All other keys proceed without waiting.
#
# KNOWN LIMITATION — registry growth:
# Without a registry lock, two concurrent requests for the same new key
# would both create separate Lock objects, breaking mutual exclusion and
# allowing duplicate upstream calls.
#
# In-process only — does not work across multiple Aegis nodes.

import asyncio
from typing import Dict


class LockManager:
    """Maintains a registry of asyncio.Lock objects, one per idempotency key."""

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def get(self, key: str) -> asyncio.Lock:
        """Return the lock for this key, creating it if it doesn't exist."""
        async with self._registry_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    @property
    def registry_size(self) -> int:
        """Number of keys currently in the registry."""
        return len(self._locks)


# Module-level singleton — shared across all requests in the process
lock_manager = LockManager()