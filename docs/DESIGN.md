# DESIGN.md — Aegis Idempotency Proxy Service

> Internal architecture reference. Read this before the engineer walkthrough.
> Every diagram, every state transition, and every component decision is here.

---

## Table of Contents

1. [System Context](#1-system-context)
2. [Component Responsibilities](#2-component-responsibilities)
3. [Database Schema](#3-database-schema)
4. [State Machine](#4-state-machine)
5. [Request Flow — All Six Scenarios](#5-request-flow--all-six-scenarios)
6. [Concurrency Model](#6-concurrency-model)
7. [Crash Recovery](#7-crash-recovery)
8. [TTL and Eviction](#8-ttl-and-eviction)
9. [Error Taxonomy](#9-error-taxonomy)
10. [File Dependency Graph](#10-file-dependency-graph)
11. [Explicit Non-Goals](#11-explicit-non-goals)
12. [Post-MVP Extensions](#12-post-mvp-extensions)

---

## 1. System Context

```
┌─────────────────────────────────────────────────────────────────┐
│                          CLIENT                                 │
│   (mobile app · backend service · payment processor · retry)    │
└─────────────────────────────┬───────────────────────────────────┘
                              │  HTTP + Idempotency-Key header
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       AEGIS  :8000                               │
│                                                                  │
│  ┌─────────────┐    ┌──────────────────┐    ┌────────────────┐  │
│  │  proxy.py   │───►│    store.py      │───►│   aegis.db     │  │
│  │ (core logic)│    │  (SQLite CRUD)   │    │   (SQLite)     │  │
│  └──────┬──────┘    └──────────────────┘    └────────────────┘  │
│         │                                                        │
│  ┌──────▼──────┐    ┌──────────────────┐                        │
│  │lock_manager │    │   eviction.py    │                        │
│  │(asyncio.Lock│    │ (background task)│                        │
│  │  registry)  │    └──────────────────┘                        │
│  └─────────────┘                                                 │
└─────────────────────────────┬───────────────────────────────────┘
                              │  HTTP (forwarded only when needed)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   UPSTREAM SERVICE  :9000                        │
│        (any HTTP API — payments, orders, notifications)          │
└─────────────────────────────────────────────────────────────────┘
```

**Key insight:** the upstream is only called on a first-seen key or a retryable
failure. Every other request is resolved inside Aegis without a network hop.

---

## 2. Component Responsibilities

| File | Responsibility |
|---|---|
| `main.py` | FastAPI app · lifespan (DB init + crash recovery + eviction task) · catch-all route |
| `proxy.py` | Core idempotency logic — all six scenarios |
| `store.py` | All SQLite reads and writes via `aiosqlite` |
| `fingerprint.py` | SHA-256 hash of `method + path + body` |
| `lock_manager.py` | Per-key `asyncio.Lock` registry guarded by a registry-level lock |
| `eviction.py` | On-access TTL check + background bulk-delete sweep |
| `models.py` | `State` enum · `IdempotencyRecord` SQLModel |
| `config.py` | All settings loaded from environment variables via `pydantic-settings` |

---

## 2b. Request Routing Order

Every non-GET request is validated in this order before reaching idempotency logic:

```
1. GET?           → pass-through to upstream (no auth required)
2. X-API-Key?     → 401 if missing
3. Idempotency-Key? → 400 if missing
4. handle_request() → idempotency logic
```

**Why this order:** authentication precedes all other validation.
A request without a valid API key must never reach idempotency logic.
GET bypasses auth entirely — reads have no side effects.

**Header lookup:** `"x-api-key" in request.headers` — FastAPI's `Headers`
object provides O(1) case-insensitive lookup. The previous implementation
used a list comprehension (`[h.lower() for h in request.headers.keys()]`)
which was O(n) on every request.

---

## 3. Database Schema

**Single table:** `idempotency_keys`

```sql
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key              TEXT    PRIMARY KEY,   -- scoped key: "{api_key}:{idempotency_key}"
    fingerprint      TEXT    NOT NULL,      -- SHA-256(method + "\n" + path + "\n" + body)
    status           TEXT    NOT NULL       -- 'in_flight' | 'completed' | 'failed'
                             DEFAULT 'in_flight',
    status_code      INTEGER,               -- HTTP status from upstream (null until resolved)
    response_body    TEXT,                  -- upstream response body (null until resolved)
    response_headers TEXT,                  -- upstream headers, JSON-serialised (null until resolved)
    created_at       REAL    NOT NULL,      -- Unix epoch float (time.time())
    expires_at       REAL    NOT NULL       -- Unix epoch float (created_at + TTL_SECONDS)
);

-- Index on expires_at — used by the eviction sweep every cycle
-- DELETE FROM idempotency_keys WHERE expires_at < now()
CREATE INDEX IF NOT EXISTS idx_expires_at ON idempotency_keys (expires_at);
```

**Why epoch floats, not datetime?**
SQLite has no native datetime type — it stores datetimes as TEXT, INTEGER, or REAL.
Epoch floats avoid timezone-string ambiguity and allow direct numeric comparison in
the eviction sweep. Migration to PostgreSQL would use `timestamptz`; that change is
contained entirely within `store.py`.

**Why `expires_at` stored per-row?**
Storing the absolute expiry timestamp at insert time (rather than recomputing
`created_at + TTL` on every sweep) allows future per-key variable TTLs and makes
the eviction query a single indexed comparison. See DECISIONS.md D-023.

### Row Lifecycle

```
INSERT (status = in_flight · response fields NULL · expires_at = now + TTL)
    │
    │  upstream call completes successfully
    ▼
UPDATE (status = completed · response fields populated)
    │
    │  upstream returns 5xx / 429 / timeout / crash
    ▼
UPDATE (status = failed · response fields populated)
    │
    │  client retries with same key
    ▼
DELETE + re-INSERT (failed record cleared · fresh in_flight inserted)
    │
    │  expires_at < now()
    ▼
DELETE (by eviction loop or on-access check)
```

---

## 4. State Machine

```
                         ┌──────────────┐
    new request    ────► │  in_flight   │
                         └──────┬───────┘
                                │
           ┌────────────────────┼──────────────────────┐
           │                    │                       │
     upstream 2xx /       upstream 5xx /          crash (orphan
     deterministic 4xx    429 / timeout /          recovered on
                          non-cacheable            startup)
           │                    │                       │
           ▼                    ▼                       ▼
    ┌────────────┐        ┌──────────┐           ┌──────────┐
    │ completed  │        │  failed  │           │  failed  │
    └────────────┘        └────┬─────┘           └────┬─────┘
     cached · replayed         │                      │
     on every retry            │  client retries      │
                               │  with same key       │
                               ▼                      ▼
                         ┌──────────────┐
                         │  in_flight   │  (fresh execution)
                         └──────────────┘
```

| State | Meaning | What happens next |
|---|---|---|
| `in_flight` | Request is being forwarded to upstream | Resolves to `completed` or `failed`; crash leaves it orphaned |
| `completed` | Upstream responded; response is cached | Every subsequent duplicate gets the cached response |
| `failed` | Transient failure or crash orphan | Client may retry with the same key — re-executes fresh |

**Critical distinction:** during normal operation, concurrent duplicates
**never see `in_flight`** — they block on the per-key `asyncio.Lock` and receive
the cached response once the first request completes. `in_flight` is only visible
to a request that arrives after a crash wiped the in-process lock.

---

## 5. Request Flow — All Six Scenarios

<details>
<summary><strong>Scenario A — New key (happy path)</strong></summary>

```
Client                  Aegis                    SQLite        Upstream
  │                        │                        │               │
  ├── POST /payments ──────►│                        │               │
  │   Idempotency-Key: k1   │── get_record(k1) ──────►│               │
  │                        │◄── None ───────────────│               │
  │                        │── acquire lock(k1)     │               │
  │                        │── insert_in_flight(k1) ►│               │
  │                        │────────────────────────────── forward ──►│
  │                        │   (lock still held)     │               │
  │                        │◄───────────────────────────── 201 ───────│
  │                        │── update_complete(k1) ──►│               │
  │                        │── release lock(k1)     │               │
  │◄── 201 ────────────────│                        │               │
```

</details>

<details>
<summary><strong>Scenario B — Duplicate key, same body (cache hit)</strong></summary>

```
Client                  Aegis                    SQLite        Upstream
  │                        │                        │               │
  ├── POST /payments ──────►│                        │               │
  │   Idempotency-Key: k1   │── try lock(k1) → BLOCKS               │
  │   (same body as before) │   (A still holds it)   │               │
  │                        │── A releases lock ─────►│               │
  │                        │── acquire lock(k1)     │               │
  │                        │── get_record(k1) ──────►│               │
  │                        │◄── completed, fp match ─│               │
  │◄── 201 (cached) ───────│                        │          (not called)
```

Upstream is **not called**. Response served from SQLite in ~1ms.

</details>

<details>
<summary><strong>Scenario C — Same key, different body → 422</strong></summary>

```
Client                  Aegis                    SQLite        Upstream
  │                        │                        │               │
  ├── POST /payments ──────►│                        │               │
  │   Idempotency-Key: k1   │── acquire lock(k1)     │               │
  │   body: {amount: 999}   │── get_record(k1) ──────►│               │
  │   (different body)      │◄── completed, FP MISMATCH              │
  │◄── 422 ────────────────│                        │          (not called)
```

</details>

<details>
<summary><strong>Scenario D — Concurrent duplicate (blocks on lock)</strong></summary>

```
Request A               Aegis                    SQLite        Upstream
  ├── POST k1 ──────────►│── acquire lock(k1)     │               │
  │                      │── insert in_flight ────►│               │
  │                      │────────────────────────────── forward ──►│
  │                      │   (lock held)           │               │
Request B                │                        │               │
  ├── POST k1 ──────────►│── try lock(k1)         │               │
  │                      │   BLOCKS ──────────────────────────────  │
  │                      │◄───────────────────────────── 201 ───────│
  │                      │── update completed ────►│               │
  │                      │── release lock(k1)     │               │
  │                      │── B acquires lock(k1)  │               │
  │                      │── B reads completed ───►│               │
  │◄── 201 cached ───────│                        │          (not called)
```

B never sees `in_flight`. B never gets an error. B simply waits.

</details>

<details>
<summary><strong>Scenario E — Crash recovery (in_flight orphan → failed)</strong></summary>

```
Previous run:  insert in_flight → [CRASH] → lock gone · in_flight row survives

On next startup:
  recover_stuck_in_flight()
      → UPDATE status = 'failed'
        WHERE status = 'in_flight'
        AND created_at < (now - 60s)

Next request with same key:
Client                  Aegis                    SQLite
  ├── POST k1 ──────────►│── acquire lock(k1)     │
  │                      │── get_record(k1) ──────►│
  │                      │◄── failed ─────────────│
  │                      │── delete_record(k1) ───►│
  │                      │── treat as new key     │
  │                      │── insert in_flight ────►│
  │◄── 200 (fresh) ──────│                        │
```

</details>

<details>
<summary><strong>Scenario F — Failed record retry</strong></summary>

```
Client                  Aegis                    SQLite        Upstream
  │                        │                        │               │
  ├── POST k1 ─────────────►│── acquire lock(k1)     │               │
  │   (5xx on prev attempt) │── get_record(k1) ──────►│               │
  │                        │◄── failed ─────────────│               │
  │                        │── delete_record(k1) ───►│               │
  │                        │── insert_in_flight(k1) ►│               │
  │                        │────────────────────────────── forward ──►│
  │                        │◄───────────────────────────── 200 ───────│
  │                        │── update_complete(k1) ──►│               │
  │◄── 200 (fresh) ────────│                        │               │
```

</details>

---

## 6. Concurrency Model

### The Race Condition

Two identical requests arrive simultaneously. Without a lock:

```
Request A:  read DB → not found
Request B:  read DB → not found       ← reads before A writes
Request A:  write in_flight → forward to upstream
Request B:  write in_flight → forward to upstream   ← DOUBLE EXECUTION
```

### The Fix: Per-Key `asyncio.Lock`

Each idempotency key has its own `asyncio.Lock` in `lock_manager.py`.
The lock is held for the **full critical section**: DB read → upstream call → DB write.

```
Request A:  acquire lock(k1) ─────────────────────────────► release lock(k1)
                               ↓                             ↑
                        read DB  write in_flight  forward  write completed
Request B:  try lock(k1) → BLOCKS ───────────────────────────► acquire lock(k1)
                                                                       ↓
                                                               read DB: completed
                                                               return cached response
```

### The Registry Lock

A second concern: two requests for the same **brand-new** key could call `get(key)`
simultaneously, both find the key absent from the registry dict, and each create a
separate `Lock` object. They would then hold independent locks and race.

`LockManager.get()` is `async` and guarded by `_registry_lock`:

```python
async def get(self, key: str) -> asyncio.Lock:
    async with self._registry_lock:        # guard the dict itself
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]
```

Only one coroutine can mutate the registry at a time. Two simultaneous new-key
requests get the same `Lock` object and contend correctly.

### Why `asyncio.Lock` and Not DB Locking

| Approach | Problem |
|---|---|
| `SELECT FOR UPDATE` | Not supported by SQLite |
| `BEGIN EXCLUSIVE` | Holds a write lock on the **entire DB** for the duration of the upstream call — blocks all other keys |
| Status column alone | Race still exists: both requests read "not found" before either writes `in_flight` |
| `asyncio.Lock` | Per-key · in-memory · held only for the duration of one request |

**Single-node limitation:** `asyncio.Lock` is in-process only. Multiple Aegis nodes
sharing a DB would not be protected. The correct distributed solution is a DB
`INSERT` gated by `PRIMARY KEY / IntegrityError` — no in-memory state needed.

---

## 7. Crash Recovery

### The Problem

Aegis crashes after `insert_in_flight()` but before `update_complete()`.

```
State after crash:
  - In-memory lock: GONE (process died)
  - SQLite row:     SURVIVES (ACID durability)
  - Row status:     in_flight (stuck — will never transition)
```

Without recovery, the next request with that key finds `in_flight` and gets a
`409` indefinitely until the record expires (up to 24 hours).

### The Fix: `recover_stuck_in_flight()` on Startup

Called during FastAPI's `lifespan` context before the server begins accepting requests:

```python
async def recover_stuck_in_flight(db: aiosqlite.Connection) -> int:
    cutoff = time.time() - 60          # records stuck for > 60 seconds
    cursor = await db.execute(
        """
        UPDATE idempotency_keys
        SET status = 'failed'
        WHERE status = 'in_flight'
        AND created_at < ?
        """,
        (cutoff,),
    )
    await db.commit()
    return cursor.rowcount
```

**Why `created_at` and not `expires_at` here?**
Recovery is about how long ago the request *started*, not when it expires.
A request that started 2 minutes ago and has not resolved is a crash orphan.
`expires_at` is 24 hours in the future and would never trigger the cutoff.

**The 60-second threshold:** long enough to avoid false positives (a legitimate
slow upstream call), short enough to recover quickly after a restart.

### Post-Recovery State

```
Before recovery:  in_flight (stuck · unreachable)
After recovery:   failed    (retryable · client can retry with same key)
```

---

## 8. TTL and Eviction

### Why TTL Exists

Idempotency records must not live forever. A key from a payment made six months
ago should not block a new request with the same client-generated key (which could
legitimately be reused after expiry).

### `expires_at`: Per-Row Absolute Expiry

`expires_at` is computed once at insert time and stored on the row:

```python
expires_at = time.time() + settings.ttl_seconds
```

This is preferred over recomputing `created_at + TTL` on every access because:
- The eviction sweep becomes a single indexed comparison: `WHERE expires_at < now()`
- Per-key variable TTLs are possible in future (different keys can carry different
  lifetimes without changing the sweep query)

### Two-Layer Eviction

```
Layer 1 — On-access (lazy):
  Every key lookup in proxy.py calls is_expired(record.expires_at).
  If expired → delete immediately → treat as new key.
  Guarantees: no stale cached response is ever served.

Layer 2 — Background sweep (eager):
  eviction_loop() runs every EVICTION_INTERVAL_SECONDS (default: 5 minutes).
  DELETE FROM idempotency_keys WHERE expires_at < now()
  Uses the idx_expires_at index — O(log N) lookup, not a full table scan.
  Guarantees: expired rows are physically deleted even if never accessed again.
```

### TTL Clock

The TTL clock starts at `created_at` (first seen). It does **not** reset when a
cached response is served. A key created at time T expires at T + TTL regardless
of access frequency. This mirrors Stripe's behaviour.

---

## 9. Error Taxonomy

| Status | Scenario | Meaning | Client Action |
|---|---|---|---|
| `200 / 201` | New key · cached replay · failed-retry | Success | — |
| `401` | Missing `X-API-Key` | Authentication required | Add the header |
| `400` | Missing `Idempotency-Key` on non-GET | Header is required | Add the header |
| `409` | `in_flight` orphan found (crash recovery) | Original outcome unknown | Use a new key |
| `422` | Same key, different body | Semantic violation — key-to-body binding broken | Fix the request |
| `502` | Upstream unreachable or timed out | Infrastructure error | Retry later (key is released) |

**Non-cacheable status codes** (upstream response passes through, key is released):

| Code | Reason |
|---|---|
| `5xx` | Transient upstream failure — may recover |
| `429` | Rate limit — will reset |
| `408` | Request timeout — transient |
| `425` | Too Early — TLS 1.3 race condition |

**Cacheable status codes** — cached and replayed forever until TTL:

All `2xx` and deterministic `4xx` (e.g. `400`, `404`, `422`). The same invalid
input will always produce the same error; caching it prevents hammering upstream.

**Why 502 and not 500 for upstream errors?**
`500 Internal Server Error` implies the fault is in Aegis. `502 Bad Gateway`
is the semantically correct status for a proxy that cannot reach its upstream.
Aegis is healthy; the upstream is not.

---

## 10. File Dependency Graph

```
main.py
  ├── config.py
  ├── store.py ──────────── models.py
  │                         config.py
  ├── eviction.py ────────── store.py
  │                         config.py
  └── proxy.py
        ├── fingerprint.py
        ├── lock_manager.py
        ├── store.py
        ├── eviction.py
        ├── models.py
        └── config.py
```

`main.py` is the only entry point. All other modules are imported by it
or by `proxy.py`. There are no circular imports.

---

## 11. Explicit Non-Goals

| Feature | Why excluded |
|---|---|
| Redis | Adds infrastructure dependency; not durable by default; SQLite sufficient for single-node |
| Horizontal scaling | Requires distributed lock or DB gate; out of scope |
| Prometheus metrics | Out of scope for this iteration |
| Admin UI | Out of scope |
| Library / SDK mode | Reverse proxy is cleaner — zero upstream changes needed |
| LLM / AI | Unrelated to the problem domain |
| Kubernetes manifests | Out of scope |

---

## 12. Post-MVP Extensions

Natural next steps after the MVP ships. None are in scope for the current version.

| Extension | What changes |
|---|---|
| **Prometheus metrics** | Add `/metrics` endpoint. Instrument cache hits, misses, upstream latency, 409/422 rates. |
| **DB PRIMARY-KEY gate** | Replace `asyncio.Lock` with a DB `INSERT + IntegrityError` gate. Removes the in-memory registry entirely. Survives restarts and works across processes. |
| **PostgreSQL backend** | Replace `aiosqlite` with `asyncpg`. Schema migration: `REAL` → `TIMESTAMPTZ`, `status TEXT` → enum type. Change is contained entirely within `store.py`. |
| **Distributed lock** | Replace `asyncio.Lock` with Redis `SET NX PX` for multi-node deployments. Only `lock_manager.py` changes. |
| **Response streaming** | Buffer the full response before caching. Large responses would benefit from chunked caching. |
| **Variable TTL per key** | The `expires_at` column already supports this. Add a `TTL-Override` header that overrides the global `TTL_SECONDS` at insert time. |
| **httpx connection pool** | Replace per-request `AsyncClient` with a module-level pooled client. Reuses TCP connections, reduces upstream latency. |

---

*Author: Somesh Kant Tiwari*
*Last updated: June 2026 — v1.1.0*