# lock_manager.py
# Maintains an asyncio.Lock for every idempotency key.
# See DECISIONS.md D-004 for the full rationale.
#
# Why asyncio.Lock and not a DB-level lock?
# - SQLite does not support SELECT FOR UPDATE.
# - BEGIN EXCLUSIVE would hold a write lock on the entire database for the
#   duration of the upstream call, blocking every other key.
# - asyncio.Lock is per-key and in-memory: only requests sharing the exact
#   same scoped key contend. All other keys proceed without waiting.
#
# Why get() is async and guarded by _registry_lock:
# Without it, two concurrent requests for the same brand-new key would both
# find the key absent from the dict and each create a separate Lock object.
# They would then hold independent locks, contend with nobody, and both
# forward to upstream — the exact double execution the lock exists to prevent.
#
# KNOWN LIMITATION — the registry only ever grows:
# There is no eviction of lock entries. The TTL sweep in eviction.py deletes
# database rows; it does not touch this dict. The registry therefore holds one
# Lock per unique scoped key seen since the process started, for the lifetime
# of the process — not, as might be assumed, only the keys currently within
# the TTL window.
#
# This is accepted rather than solved: removing entries safely is racy (see
# D-020), an asyncio.Lock is tens of bytes, and the process is expected to be
# restarted on deploy. The real fix is not cleanup — it is removing the
# in-memory registry entirely in favour of a DB INSERT gated by the PRIMARY
# KEY constraint, which is the v2 path in D-024.
#
# In-process only — does not work across multiple Aegis nodes.

import asyncio
from typing import Dict


class LockManager:
    """Maintains a registry of asyncio.Lock objects, one per scoped key."""

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}
        self._registry_lock = asyncio.Lock()

    async def get(self, key: str) -> asyncio.Lock:
        """
        Return the lock for this key, creating it if it does not exist.

        The registry mutation is guarded so that two coroutines racing on the
        same new key receive the same Lock object rather than one each.
        """
        async with self._registry_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    @property
    def registry_size(self) -> int:
        """Number of keys currently in the registry. Grows monotonically."""
        return len(self._locks)


# Module-level singleton — shared across all requests in the process.
# A per-request LockManager would give every request its own registry, so two
# concurrent requests for one key would never contend. See D-013.
lock_manager = LockManager()
