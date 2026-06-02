# DESIGN.md — Aegis Idempotency Proxy Service

This document describes the internal architecture of Aegis: how components fit
together, how data flows through the system, and how each of the four
idempotency scenarios is handled end-to-end.

---

## 1. System Context

```
┌─────────────────────────────────────────────────────┐
│                     CLIENT                          │
│  (mobile app, backend service, payment processor)   │
└─────────────────────┬───────────────────────────────┘
                      │ HTTP + Idempotency-Key header
                      ▼
┌─────────────────────────────────────────────────────┐
│                   AEGIS PROXY                        │
│                  (port 8000)                         │
│                                                      │
│  ┌────────────┐   ┌──────────────┐   ┌───────────┐  │
│  │  proxy.py  │──▶│   store.py   │──▶│ aegis.db  │  │
│  │ (logic)    │   │ (SQLite CRUD)│   │ (SQLite)  │  │
│  └─────┬──────┘   └──────────────┘   └───────────┘  │
│        │                                             │
│  ┌─────▼──────┐   ┌──────────────┐                  │
│  │lock_manager│   │  eviction.py │                  │
│  │(asyncio)   │   │ (background) │                  │
│  └────────────┘   └──────────────┘                  │
└─────────────────────┬───────────────────────────────┘
                      │ HTTP (forwarded request)
                      ▼
┌─────────────────────────────────────────────────────┐
│                UPSTREAM SERVICE                      │
│  (any HTTP API — payments, orders, notifications)    │
└─────────────────────────────────────────────────────┘
```

---

## 2. Component Responsibilities

| File | Responsibility |
|---|---|
| `main.py` | FastAPI app, lifespan (DB init + eviction task), catch-all route |
| `proxy.py` | Core idempotency logic — all four scenarios |
| `store.py` | All SQLite reads and writes via aiosqlite |
| `fingerprint.py` | SHA-256 hash of request body |
| `lock_manager.py` | Per-key asyncio.Lock registry |
| `eviction.py` | TTL check (on-access) + background bulk delete |
| `models.py` | Pydantic models: IdempotencyRecord, KeyStatus enum |
| `config.py` | All settings from environment variables |

---

## 3. Database Schema

Single table: `idempotency_keys`

```sql
CREATE TABLE idempotency_keys (
    key              TEXT PRIMARY KEY,    -- client-supplied Idempotency-Key header
    fingerprint      TEXT NOT NULL,       -- SHA-256 of request body
    status           TEXT NOT NULL,       -- 'PENDING' or 'COMPLETE'
    response_status  INTEGER,             -- HTTP status from upstream (null until COMPLETE)
    response_body    TEXT,                -- upstream response body as string (null until COMPLETE)
    response_headers TEXT,               -- upstream response headers, JSON-serialised (null until COMPLETE)
    created_at       REAL NOT NULL        -- Unix timestamp (float), used for TTL
);

CREATE INDEX idx_created_at ON idempotency_keys (created_at);
-- Index exists solely to speed up the eviction sweep:
-- DELETE FROM idempotency_keys WHERE created_at < ?
```

### Row lifecycle

```
INSERT (status=PENDING, response fields NULL)
           │
           │  upstream call completes
           ▼
UPDATE (status=COMPLETE, response fields populated)
           │
           │  created_at + TTL_SECONDS < now()
           ▼
DELETE (by eviction loop or on-access check)
```

---

## 4. Request Flow — All Four Scenarios

### Scenario A: New Key (Happy Path)

```
Client                    Aegis                      SQLite        Upstream
  │                          │                          │              │
  │── POST /payments ────────►│                          │              │
  │   Idempotency-Key: k1     │                          │              │
  │                          │── get_record(k1) ────────►│              │
  │                          │◄── None ─────────────────│              │
  │                          │                          │              │
  │                          │── acquire lock(k1) ──────►(in memory)   │
  │                          │── insert_pending(k1) ────►│              │
  │                          │                          │              │
  │                          │────────────────────────────── forward ──►│
  │                          │◄───────────────────────────── 201 ───────│
  │                          │                          │              │
  │                          │── update_complete(k1) ───►│              │
  │                          │── release lock(k1) ──────►(in memory)   │
  │                          │                          │              │
  │◄── 201 ──────────────────│                          │              │
```

### Scenario B: Duplicate Key, Same Body (Cache Hit)

```
Client                    Aegis                      SQLite        Upstream
  │                          │                          │              │
  │── POST /payments ────────►│                          │              │
  │   Idempotency-Key: k1     │                          │              │
  │   (same body as before)   │                          │              │
  │                          │── get_record(k1) ────────►│              │
  │                          │◄── COMPLETE, fp matches ──│              │
  │                          │                          │              │
  │◄── 201 (cached) ─────────│                          │  (not called)│
```

Upstream is NOT called. Response served from SQLite in ~1ms.

### Scenario C: Key Reuse with Different Body → 422

```
Client                    Aegis                      SQLite        Upstream
  │                          │                          │              │
  │── POST /payments ────────►│                          │              │
  │   Idempotency-Key: k1     │                          │              │
  │   body: {amount: 999}     │                          │              │
  │   (different from first)  │                          │              │
  │                          │── get_record(k1) ────────►│              │
  │                          │◄── COMPLETE, fp MISMATCH ─│              │
  │                          │                          │              │
  │◄── 422 ──────────────────│                          │  (not called)│
```

### Scenario D: In-Flight Concurrent Duplicate → 409

```
Client A                  Aegis                      SQLite        Upstream
  │                          │                          │              │
  │── POST /payments ────────►│                          │              │
  │   Idempotency-Key: k1     │── insert_pending(k1) ───►│              │
  │   (in flight)             │── lock held ─────────────►              │
  │                          │────────────────────────────── forward ──►│
  │                          │   (waiting for response)  │              │
  │                                                                     │
Client B                  Aegis                      SQLite
  │                          │                          │
  │── POST /payments ────────►│                          │
  │   Idempotency-Key: k1     │── get_record(k1) ────────►│
  │   (same body)             │◄── PENDING ───────────────│
  │                          │                          │
  │◄── 409 ──────────────────│
  │   "already in progress"   │
```

Client B gets 409 immediately and should retry after a brief delay.
When Client A's request completes, Client B's retry will hit Scenario B
(cache hit) and get the cached response.

---

## 5. Concurrency Model

### The Problem

Two identical requests arrive simultaneously (same key, same body).
Without a lock, both requests query the DB, both find "not found",
and both forward to upstream — causing a double execution.

### The Solution

Each idempotency key has its own `asyncio.Lock` in `lock_manager.py`.

```
Request 1 arrives → acquires lock(k1) → reads DB (not found) → inserts PENDING
Request 2 arrives → tries to acquire lock(k1) → WAITS

Request 1 → calls upstream → updates to COMPLETE → releases lock(k1)

Request 2 → acquires lock(k1) → reads DB → finds COMPLETE → returns cached response
```

This means:
- Two simultaneous requests do NOT both hit upstream
- The second request is either served from cache or gets 409 if still pending
- Locks for different keys never contend with each other

### Why asyncio.Lock and not DB locking

SQLite does not support `SELECT FOR UPDATE`. Using `BEGIN EXCLUSIVE`
would hold a write lock on the entire database for the duration of the
upstream call (potentially seconds), blocking ALL other keys.

`asyncio.Lock` is per-key and in-memory — only requests with the same key
contend. All other keys proceed without waiting.

**Limitation:** `asyncio.Lock` is in-process only. If Aegis runs as multiple
processes or on multiple nodes, this lock provides no protection. That is an
accepted constraint — Aegis is a single-node service by design.

---

## 6. TTL and Eviction

### Why TTL exists

Idempotency records must not live forever. A payment made 6 months ago
should not prevent a new payment with the same client-generated key
(which could legitimately be reused after expiry).

### Two-layer eviction

**Layer 1 — On-access (lazy):**
Every time a key is looked up in `proxy.py`, `eviction.is_expired()` is
called. If the record is expired, it is deleted immediately and treated
as a new key. This ensures no expired response is ever served.

**Layer 2 — Background sweep (eager):**
`eviction_loop()` runs as an asyncio background task, sleeping for
`EVICTION_INTERVAL_SECONDS` (default: 5 minutes) between sweeps. It bulk-
deletes all rows where `created_at < now() - TTL_SECONDS`. This handles
keys that expired but were never accessed again.

```
TTL = 86400 seconds (24 hours, default)

created_at = T
Key is accessible until T + 86400
At T + 86400 + ε, next access triggers deletion
Background sweep running every 300s deletes all expired rows
```

### Edge case: crash during upstream call

If Aegis crashes after `insert_pending()` but before `update_complete()`,
the record is stuck in `PENDING` status permanently (until TTL expires).

On the next access with that key, the row will either:
- Be found as PENDING → client gets 409, should retry with a new key
- Have expired → row deleted, treated as new key

This is an accepted trade-off for the single-node design. A production
system would add a PENDING age check (if PENDING for > 60s, treat as failed).

---

## 7. Error Taxonomy

| Scenario | Status | Meaning |
|---|---|---|
| Missing `Idempotency-Key` header | 400 | Client error — header is required for non-GET |
| Key reuse with different body | 422 | Semantic violation — key+body binding broken |
| In-flight duplicate | 409 | Concurrency conflict — retry after delay |
| Upstream unreachable | 500 | Infrastructure error — upstream is down |
| GET without key | pass-through | GET is idempotent by nature, no key needed |

---

## 8. What Aegis Does NOT Do

These are explicit non-goals, not omissions:

| Feature | Why excluded |
|---|---|
| Redis | Adds infrastructure dependency; SQLite sufficient for single-node |
| Horizontal scaling | Requires distributed lock; out of scope |
| Prometheus metrics | Out of scope for this iteration |
| Admin UI | Out of scope |
| Library/SDK mode | Reverse proxy is cleaner — zero upstream changes needed |
| LLM / AI | Unrelated to the problem domain |
| Kubernetes manifests | Out of scope |

---

## 9. File Dependency Graph

```
main.py
  ├── config.py
  ├── store.py ──── models.py
  ├── eviction.py ── store.py, config.py
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

## 10. How to Extend Aegis (Post-MVP)

The following extensions are natural next steps after the MVP is shipped.
None are in scope for the current version.

- **Prometheus metrics:** Add a `/metrics` endpoint. Instrument cache hits,
  cache misses, upstream latency, and 409/422 rates.

- **PENDING age check:** If a PENDING record is older than N seconds, treat
  the upstream call as failed and allow the client to retry with the same key.

- **Response streaming:** Currently Aegis buffers the full upstream response
  before caching. Large responses would benefit from streaming with chunked
  caching.

- **PostgreSQL backend:** Replace `aiosqlite` with `asyncpg` for multi-node
  deployments. The `store.py` abstraction makes this a contained change.

- **Distributed lock:** Replace `asyncio.Lock` with Redis `SET NX PX` for
  multi-node in-flight detection. Again, `lock_manager.py` is the only
  file that changes.

---

*Author: Somesh Kant Tiwari*
*Last updated: May 2026*
