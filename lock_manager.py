# lock_manager.py
# Per-key asyncio.Lock registry.
# See DECISIONS.md D-004 for the full rationale.
#
# Why asyncio.Lock and not a DB-level lock?
# - SQLite does not support SELECT FOR UPDATE.
# - BEGIN EXCLUSIVE would hold the write lock for the entire upstream round-trip
#   (potentially seconds), blocking ALL other DB writes — not just the same key.
# - asyncio.Lock is per-key and in-memory: only requests sharing the exact same
#   idempotency key contend. All other keys proceed without waiting.
#
# KNOWN LIMITATION — registry growth:
# cleanup() cannot be safely called outside the `async with` block without
# introducing a race: a waiting coroutine holds a reference to the old Lock
# object; if cleanup() deletes it from the registry and a new request creates
# a fresh Lock for the same key, two separate Lock objects exist for one key,
# breaking the mutual exclusion guarantee.
#
# For v1 (single-node, bounded key volume), the registry is bounded by the
# number of unique keys active within the TTL window. This is acceptable.
#
# The correct v2 fix: replace asyncio.Lock with a DB INSERT + IntegrityError
# gate (see DESIGN.md Section 10). No registry, no memory concern, survives
# restarts, and is the right design for a production system.
#
# In-process only — does not work across multiple Aegis nodes.

import asyncio
from typing import Dict


class LockManager:
    """
    Maintains a registry of asyncio.Lock objects, one per idempotency key.

    Usage in proxy.py:
        async with lock_manager.get(idempotency_key):
            # read DB, call upstream, write DB
    """

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> asyncio.Lock:
        """Return the lock for this key, creating it if it doesn't exist."""
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    @property
    def registry_size(self) -> int:
        """Number of keys currently in the registry. Useful for monitoring."""
        return len(self._locks)


# Module-level singleton — shared across all requests in the process
lock_manager = LockManager()
