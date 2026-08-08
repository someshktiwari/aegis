# DECISIONS.md — Aegis Idempotency Proxy Service

Every architectural and implementation decision made in building Aegis.
Each entry covers: the decision, every alternative considered, the reasoning,
and the trade-offs explicitly accepted.

> **How to use this document:**
> Read it before the engineer walkthrough. Read it before every interview.
> Every line of code in this codebase has a rationale here.

---

## Table of Contents

**Part I — Architectural Decisions**
D-001 through D-028 — permanent design choices

**Part II — Build Journal**
BJ-001 through BJ-011 — every bug fixed, every incorrect assumption corrected,
every change made during and after the build, with the reasoning behind each.

---

# Part I — Architectural Decisions

---

## D-001 · Reverse Proxy Architecture vs. Middleware Library

**Decision:** Aegis is a standalone HTTP reverse proxy, not an importable Python library.

**Alternatives considered:**
- A FastAPI/Starlette middleware class that upstream services import and mount
- A Python decorator (`@idempotent`) applied per endpoint
- A Django middleware class
- A WSGI/ASGI middleware package published to PyPI

**Why this:**
A reverse proxy requires zero changes to the upstream service. The idempotency
concern is cross-cutting infrastructure — it belongs at the network layer, not
inside application code. The same Aegis instance can protect any upstream
service regardless of framework (FastAPI, Flask, Express, a legacy monolith).

A middleware library creates tight coupling: every upstream service must import,
configure, and version-pin Aegis. A bug in Aegis affects every service that
adopted it at the library level, and upgrades must be coordinated across consumers.

**Trade-offs accepted:**
- An extra network hop (client → Aegis → upstream) adds ~1–3ms latency on loopback
- Aegis must faithfully proxy request headers, methods, and bodies

---

## D-002 · FastAPI over Flask or Django

**Decision:** FastAPI is the web framework for Aegis.

**Why this:**
Aegis is fundamentally I/O-bound. Every request reads from SQLite, potentially
writes to SQLite, then makes an outbound HTTP call to upstream. Python's asyncio
allows a single thread to handle many concurrent in-flight requests — but only
if every I/O call is awaitable.

Flask is synchronous by default. Django is designed for full-stack applications:
ORM, admin interface, templating, sessions — all overhead Aegis has no use for.

FastAPI provides: native async/await · Pydantic validation · automatic OpenAPI
at `/docs` · dependency injection · lifespan context manager for startup hooks.

**Trade-offs accepted:**
- FastAPI is newer than Flask; fewer Stack Overflow answers for edge cases
- Async programming requires discipline: no blocking calls inside `async def`

---

## D-003 · SQLite over PostgreSQL, MySQL, or Redis

**Decision:** SQLite via `aiosqlite` is the persistence layer.

**Alternatives considered:**
- PostgreSQL with `asyncpg` (production-grade relational DB)
- Redis with `aioredis` (in-memory, native TTL)
- In-memory Python dict (fastest, no persistence)
- A JSON file on disk

**Why this:**
Aegis is designed for single-node deployment. SQLite requires zero infrastructure:
no daemon, no connection string, no separate process. The entire database is one
file (`aegis.db`). ACID guarantees mean the DB is never corrupt after a crash.

**Why not Redis?**
Redis is not durable by default (AOF/RDB must be explicitly configured). A Redis
restart or OOM eviction silently deletes idempotency records. A client retrying
after eviction would see "key not found", treat the request as new, and trigger
a duplicate execution — exactly the failure mode Aegis exists to prevent.

**Trade-offs accepted:**
- SQLite does not support horizontal scaling
- Write throughput is limited versus PostgreSQL — acceptable for single-node
- Must use `aiosqlite` everywhere; synchronous `sqlite3` blocks the event loop

---

## D-004 · Per-Key `asyncio.Lock` for Concurrency Control

**Decision:** A per-key `asyncio.Lock` registry in `lock_manager.py` serialises
concurrent requests for the same idempotency key.

**The race condition without a lock:**
```
Request A:  read DB → not found
Request B:  read DB → not found         ← before A writes
Request A:  write in_flight → forward
Request B:  write in_flight → forward   ← DOUBLE EXECUTION
```

**Why this:**
The lock is held for the **full critical section**: DB read → upstream call →
DB write. A concurrent duplicate (Request B) blocks until A completes, then reads
`completed` from the DB and returns the cached response.

**Important:** during normal operation, concurrent duplicates **never see
`in_flight`**. They block on the lock. `in_flight` is only encountered by a
request that arrives after a crash wiped the in-process lock (crash recovery).

**Why not DB locking:**
- `SELECT FOR UPDATE` — not supported by SQLite
- `BEGIN EXCLUSIVE` — holds a write lock on the **entire DB** for the upstream call duration, blocking all other keys

**Trade-offs accepted:**
- In-process only — does not work across multiple Aegis nodes (see D-024 for v2)
- The lock registry is never pruned. It holds one `asyncio.Lock` per unique
  scoped key seen since process start, for the lifetime of the process — the
  TTL sweep deletes database rows and does not touch the registry. See D-020

---

## D-005 · SHA-256 for Body Fingerprinting

**Decision:** Requests are hashed with SHA-256 (`hashlib.sha256`).
The fingerprint covers `method + "\n" + path + "\n" + query + "\n" + body`.
Why the query string is in there is D-026; this entry covers the hash choice.

**Why include method and path (not body alone):**
Hashing only the body causes cross-endpoint collisions. A POST to `/payments`
with body `{"amount":100}` and a POST to `/refunds` with the same body would
produce the same fingerprint. They are different requests that must be tracked
independently. Including method and path in the hash makes fingerprints globally
unique per endpoint.

**Why SHA-256 over MD5 / CRC32:**
MD5 has known collision vulnerabilities. A collision means two different request
bodies produce the same hash — a different request would be served a cached
response from a different request. CRC32 and xxHash are designed for error
detection, not fingerprinting; their collision rates are orders of magnitude
higher. SHA-256 is in Python's standard library — zero extra dependencies.

**Trade-offs accepted:**
- SHA-256 is slower than CRC32 — irrelevant at HTTP request handler granularity
- Not using HMAC — fingerprinting for deduplication, not authenticity verification

---

## D-006 · 422 for Key Reuse with Mismatched Body

**Decision:** Reusing an `Idempotency-Key` with a different request body returns `422 Unprocessable Entity`.

**HTTP semantics:**
`422` means the request was syntactically valid but semantically incorrect. Reusing
a key with a different body violates the key-to-payload binding that idempotency
depends on — a semantic violation, not a syntax error.

`400 Bad Request` implies a malformed request. `409 Conflict` is reserved for
the crash-recovery signal (see D-007). Using `409` for both would conflate two
distinct errors: a client retrying on a `409` would loop forever on a `422`.

**Stripe reference:** Stripe returns 422 for key reuse with a different payload.
Aegis mirrors this deliberately.

**Trade-offs accepted:**
- Clients that do not differentiate 4xx codes miss the semantic distinction

---

## D-007 · 409 for Crash-Orphaned `in_flight` Records

**Decision:** A request that finds an `in_flight` record (orphaned by a crash)
returns `409 Conflict`.

**Critical:** `409` is a crash-recovery signal, not a concurrency signal.
Normal concurrent duplicates block on the lock and receive the cached response.
They never see `in_flight` and never get `409`.

`409` fires only when Aegis previously crashed after writing `in_flight` but
before writing `completed` or `failed`. The process died, the lock disappeared,
but the `in_flight` row survived. The next request finds the orphan and returns
`409` because the original outcome is unknown.

**Client action:** use a new `Idempotency-Key` for the retry. We cannot know
whether the upstream executed before the crash.

**Trade-offs accepted:**
- Client must use a new key after `409` — cannot retry the same key
- A very recent crash orphan (< 60s old) is not recovered by startup sweep; client gets `409`

---

## D-008 · Two-Layer TTL Eviction

**Decision:** Records are evicted in two ways:
1. **On access** — `is_expired(record.expires_at)` in `proxy.py`
2. **Background sweep** — `eviction_loop()` runs every `EVICTION_INTERVAL_SECONDS`

**Why both layers:**

Lazy-only: expired rows accumulate indefinitely if the key is never accessed again.
A high-churn workload (many unique keys, low repeat access) would grow `aegis.db`
without bound.

Eager-only: a client retry could receive a stale cached response if the sweep
hasn't run recently (e.g. a key expired 4 minutes 59 seconds ago, sweep runs
every 5 minutes).

The combined approach eliminates both problems:
- On-access ensures **correctness** — no stale response ever served
- Background sweep ensures **storage hygiene** — no unbounded growth

**TTL clock:** starts at `created_at`. Does not reset on cache hits.
Mirrors Stripe's behaviour.

**Trade-offs accepted:**
- Background task stops when Aegis stops; expired rows cleaned on next startup's first sweep

---

## D-009 · Single Shared `aiosqlite` Connection

**Decision:** One `aiosqlite.Connection` is opened at startup and shared across
all requests via `app.state.db`.

**Why:**
SQLite supports multiple concurrent readers but only one writer at a time.
Multiple connections attempting concurrent writes produce "database is locked"
errors. A single shared connection serialises all writes through one channel.

Since all DB operations are `async`, the single connection is not a bottleneck:
while one coroutine awaits a DB write, the event loop processes other requests.

**Trade-offs accepted:**
- If the DB connection is lost, all requests fail until Aegis restarts
- Writes are serialised — acceptable at single-node concurrency

---

## D-010 · Async-First Design Throughout

**Decision:** Every I/O operation uses async/await: `aiosqlite` for SQLite,
`httpx.AsyncClient` for upstream HTTP calls.

**The rule:** No blocking call (`sqlite3`, `time.sleep`, synchronous file I/O,
`requests`) may ever appear inside an `async def` function in Aegis.

**Why:**
A synchronous `sqlite3` call inside an `async def` blocks the event loop for
its entire duration. Every other in-flight request stalls. This eliminates
the entire concurrency benefit of FastAPI's async model.

`aiosqlite` wraps `sqlite3` in a thread, allowing the event loop to continue.
`httpx.AsyncClient` is natively async.

**Trade-offs accepted:**
- Async code is harder to debug than synchronous code
- Tracebacks and race conditions require more care to investigate

---

## D-011 · Per-Request `httpx.AsyncClient`

**Decision:** A new `httpx.AsyncClient` is created and closed for each upstream
call inside `forward_to_upstream()`.

**Why:**
For the MVP scope, a per-request client is simpler and correct. It avoids
connection state (cookies, auth headers) leaking between requests, and avoids
managing a shared client's lifecycle (e.g. broken state after upstream downtime).

The `async with httpx.AsyncClient()` pattern creates a client, makes the request,
and cleanly closes all connections — no resource leaks.

**For a production extension:** a module-level `AsyncClient` with connection
pooling would improve throughput by reusing TCP connections. See DESIGN.md §12
(Post-MVP Extensions).

**Trade-offs accepted:**
- A new TCP connection is established for each upstream call
- Slightly higher latency per request than a pooled client

---

## D-012 · Pydantic BaseSettings for Configuration

**Decision:** All configuration is loaded from environment variables via
`pydantic-settings.BaseSettings` in `config.py`.

**Why:**
`BaseSettings` reads environment variables with type validation and default values,
all in one place. The single `settings` object is the one source of truth for config.
`os.getenv()` scattered through code has no type safety, no central documentation,
and makes testing harder.

**Trade-offs accepted:**
- Requires `pydantic-settings` as a dependency (separate from `pydantic`)

---

## D-013 · Module-Level Singleton for `lock_manager`

**Decision:** `lock_manager.py` exports a single `lock_manager = LockManager()`
instance at module level, shared across all requests.

**Why:**
The lock registry must be shared across all concurrent requests in the process.
A per-request `LockManager` would give each request its own registry — two
concurrent requests with the same key would each get their own lock and never
contend, defeating the purpose entirely.

A module-level singleton is simpler than attaching to `app.state` and is idiomatic
for process-global state.

**Trade-offs accepted:**
- Module-level state makes unit testing slightly harder — mitigated by using a fresh key per test

---

## D-014 · Write `in_flight` Before Calling Upstream

**Decision:** `insert_in_flight()` is called **before** `forward_to_upstream()`.

**Why:**
If upstream is called before writing to DB, a concurrent duplicate arriving during
the upstream call will find "key not found" and also forward — a double execution.
The `in_flight` write must happen first to signal "this key is in-flight."

The combined lock + write-before-call sequence guarantees:
1. Lock prevents two requests from entering the critical section simultaneously
2. `in_flight` is written before any upstream call begins
3. Any concurrent request blocks on the lock until after `in_flight` is written

**Trade-offs accepted:**
- If the upstream call fails, the `in_flight` record must be cleaned up — handled
  in `proxy.py`'s try/except block (marks as `failed` or deletes on exception)

---

## D-015 · Hop-by-Hop Header Filtering

**Decision:** HTTP hop-by-hop headers are stripped in both directions —
from requests forwarded upstream, and from responses returned or cached.
`content-encoding` is stripped on the response side **only**.

**Hop-by-hop set (both directions):**
`connection`, `keep-alive`, `proxy-authenticate`, `proxy-authorization`,
`te`, `trailers`, `transfer-encoding`, `upgrade`, `content-length`

**Why:**
Hop-by-hop headers (RFC 2616 §13.5.1) are meaningful only for a single transport
hop and must not be relayed by a proxy. Forwarding `Transfer-Encoding: chunked`
confuses the upstream. `Content-Length` is stripped because `httpx` recalculates
it for the forwarded request.

**Why `content-encoding` is response-only (it is not hop-by-hop per the RFC):**
`httpx` automatically decompresses *response* bodies. Replaying the original
`Content-Encoding: gzip` alongside already-decompressed bytes would make the
client try to gunzip plain text and corrupt the response. The header describes
an encoding that no longer exists.

Nothing equivalent happens on the request side. Starlette hands the handler the
request body exactly as it arrived — still compressed if the client compressed
it. Stripping `Content-Encoding` there while forwarding the compressed bytes
would leave the upstream unable to decode its own payload. The asymmetry between
the two strip-lists is deliberate and is the answer to "why isn't there one
list?": on the response side a library has already transformed the body, on the
request side nothing has.

---

## D-016 · In-Memory SQLite for Tests

**Decision:** Tests use `aiosqlite.connect(":memory:")` rather than a file.

**Why:**
`:memory:` creates a database that exists only for the lifetime of the connection.
No disk I/O, no cleanup, no risk of previous test data affecting the current run.
Each test fixture gets a fresh, isolated database.

---

## D-017 · `ASGITransport` for Test HTTP Client

**Decision:** Tests use `httpx.AsyncClient(transport=ASGITransport(app=app))`
rather than spinning up a real server.

**Why:**
`ASGITransport` sends requests directly to the FastAPI ASGI app in-process,
without any network stack. Tests run at in-memory speed. The full request/response
cycle (middleware, routing, dependency injection) is exercised.

---

## D-018 · GET Pass-Through Without Caching

**Decision:** GET requests are forwarded directly to upstream without
idempotency processing, regardless of whether an `Idempotency-Key` is present.

**Why:**
GET is defined as idempotent by HTTP semantics (RFC 7231). Two identical GETs
produce the same result with no side effects. There is no reason to deduplicate
GET requests — the worst case is reading the same data twice, which is harmless.

---

## D-019 · Index on `expires_at` Column

**Decision:** A database index is created on the `expires_at` column.

```sql
CREATE INDEX IF NOT EXISTS idx_expires_at ON idempotency_keys (expires_at);
```

**Why:**
The eviction sweep runs `DELETE FROM idempotency_keys WHERE expires_at < now()`.
Without an index, SQLite performs a full table scan on every sweep. With the
B-tree index, SQLite finds matching rows in O(log N). The sweep is fast and the
write lock is held briefly.

*(Previously indexed on `created_at`. Updated to `expires_at` when the eviction
sweep was changed from `created_at + TTL` to the stored `expires_at` column.)*

---

## D-020 · `cleanup()` Intentionally Omitted from `LockManager`

**Decision:** `LockManager` has no `cleanup()` method. The lock registry grows
monotonically for the lifetime of the process.

**Why no cleanup:**
Safely removing a lock entry mid-flight is racy. A waiting coroutine holds a
reference to the `Lock` object. If we delete the entry from the registry dict
while another coroutine holds that reference, the next `get(key)` call creates
a new, independent `Lock` — breaking mutual exclusion.

**Why it's acceptable:**
Note what the bound is *not*. The TTL sweep in `eviction.py` deletes rows from
SQLite; it never touches this dict. So the registry is not bounded by the TTL
window — it holds one entry per unique scoped key seen since the process
started, and only a restart clears it.

It is accepted anyway. An `asyncio.Lock` is tens of bytes, so a process serving
a million unique keys between deploys carries a registry in the tens of
megabytes, and Aegis is restarted on every deploy. The honest framing is that
this is a leak with a known ceiling on process lifetime rather than a bounded
cache — and that the fix is not cleanup but removing the registry entirely.

**The v2 fix:**
Replace the in-memory lock entirely with a DB `INSERT` gated by the `PRIMARY KEY /
IntegrityError`. No registry, no leak, no cleanup concern, survives restarts,
works across processes. This is the correct production path — and it is the same
change D-024 needs for multi-node support, which is why it is the one v2 item
worth doing before any other.

---

## D-021 · Non-Cacheable HTTP Status Codes

**Decision:** The following upstream responses are **not cached** — the record
is marked `failed` (retryable) rather than `completed`:

| Code | Reason |
|---|---|
| `5xx` | Transient upstream failure — the upstream may recover |
| `429` | Rate limit — will reset |
| `408` | Request Timeout — transient |
| `425` | Too Early — TLS 1.3 early data race condition |

**Caching principle:** only cache responses that are **deterministic for the same
input**. A `400 Bad Request` (invalid payload) is deterministic — the same input
always produces the same error. A `429` is not — the same input will succeed after
the rate limit resets.

**This is an exclusion rule, not an allow-list.** `_NON_CACHEABLE_STATUS` names
what is *not* cached; everything else is, including `3xx` redirects. That is a
consequence of the rule rather than a separate decision, and it is the honest
answer to "do you cache redirects?" — yes, because a deterministic redirect is
as replayable as a deterministic `404`. Should that prove wrong for a given
upstream, the change is to add `3xx` to the frozenset, not to special-case it in
the handler.

---

## D-022 · Response Header Deny-List

**Decision:** The following headers are stripped before storing in SQLite
and before replaying from cache:

- `set-cookie`
- `www-authenticate`
- `authorization`

**Why:**
Replaying client A's session cookie to a later caller via a cached response is
a direct session-leak vulnerability. These headers are still returned on the
**first (live) response** — only the stored/replayed copy is scrubbed.

**Scope: this is a cache deny-list, not a forwarding deny-list.** It is applied
in `_cacheable_headers()`, which only ever sees *response* headers on their way
into SQLite. It has no effect on the request path. The `authorization` entry
therefore means "never store an upstream's `Authorization` response header",
not "never forward the caller's credentials" — see D-025.

---

## D-023 · `expires_at` Per-Row TTL Column

**Decision:** The `expires_at` Unix epoch float is computed at insert time
(`created_at + TTL_SECONDS`) and stored on every row.

**Alternatives considered:**
- Compute expiry on access: `time.time() - created_at > ttl_seconds` (removed)
- Store only `created_at` and recompute on every eviction sweep

**Why stored per-row:**

**Correctness:** the on-access expiry check becomes a direct comparison:
`time.time() > expires_at` — no TTL constant needed, no risk of the setting
changing between insert and check.

**Performance:** the eviction sweep query becomes:
```sql
DELETE FROM idempotency_keys WHERE expires_at < ?
```
This uses the `idx_expires_at` index directly. The previous approach
(`created_at + TTL < now`) cannot use a simple index because the filter
involves arithmetic on the stored column.

**Extensibility:** per-key variable TTLs become trivial — set a different
`expires_at` at insert time. The sweep query never changes.

**Trade-offs accepted:**
- Every INSERT must compute and store `expires_at` (negligible overhead)
- Schema migration required to add the column to existing deployments

---


## D-024 · Single-Process Concurrency Scope — Distributed Locking Deferred

**Decision:** v1 correctness guarantees are explicitly scoped to a single Aegis
process. The per-key `asyncio.Lock` (D-004) provides mutual exclusion within one
event loop only. Distributed locking is deferred to v2.

**What breaks with multiple Aegis instances:**
The lock registry lives in process memory. Two Aegis nodes behind a load balancer
each hold their own registry, so two concurrent requests with the same key landing
on different nodes both pass `get(key) → not found` and both attempt
`insert_in_flight()`.

**What actually happens then:**
The `key TEXT PRIMARY KEY` column rejects the second INSERT. Combined with
write-before-forward (D-014), the upstream is never executed twice even across
nodes — but the losing request crashes with an unhandled `IntegrityError` and the
client receives a 500, instead of blocking and receiving the cached response as it
would on a single node. The safety property survives by accident; the behavioural
contract does not.

**Alternatives considered for v2:**

| Option | Mechanism | Cost |
|---|---|---|
| Redis `SET NX PX` | Atomic acquire with TTL as a lock lease | New infrastructure dependency; lease expiry vs. long upstream calls must be handled |
| Postgres advisory locks | `pg_advisory_xact_lock(hash(key))` | Requires the Postgres migration first; lock lifetime tied to a transaction |
| DB INSERT gate | Catch `IntegrityError` on the in_flight INSERT, then poll the row until it resolves | No new infrastructure; turns blocking into polling (added latency) |

**Why deferred and not built now:**
Per D-003, v1 runs on SQLite — a single-writer, single-file store that cannot be
shared across nodes in the first place. Multi-node Aegis therefore requires the
Postgres (or Redis) storage migration *before* distributed locking has anything to
coordinate. Building cross-node locks on top of a single-node store would be
coordination for a topology the storage layer cannot support. Single-process scope
is a deliberate cut, not an oversight.

**The v2 path:**
The DB INSERT gate (already identified in D-020) is the primary candidate — it
removes the in-memory registry entirely, survives restarts, and works across
processes with zero new infrastructure once Postgres lands.

**Trade-offs accepted:**
- Horizontal scaling of Aegis itself is not supported in v1
- A second node degrades duplicate handling from “block and replay” to “500 on conflict”
- Availability is bounded by the single process (mitigated by fast startup + crash recovery on boot)

---

## D-025 · X-API-Key Header for Multi-Tenant Key Scoping

**Decision:** Every non-GET request must carry an `X-API-Key` header.
Idempotency keys are scoped per caller using a composite DB key:
`{api_key}:{idempotency_key}`.

**The problem without scoping:**
Without an API key, any client that guesses or reuses an `Idempotency-Key`
value already in the DB would receive another client's cached response.
In a payment proxy, that means Client B could receive Client A's transaction
result — a direct data leak.

**Why composite key and not a separate column:**
Storing `api_key` as a separate column would require a composite primary key
`(api_key, idempotency_key)` and a compound index. The composite string
`{api_key}:{idempotency_key}` achieves the same isolation using the existing
`key TEXT PRIMARY KEY` column and single-column index — zero schema change.
The `:` separator is safe because it cannot appear in a standard UUID or
slug idempotency key.

**Why `X-API-Key` and not `Authorization: Bearer`:**
The caller's `Authorization` header belongs to the upstream and is forwarded
untouched — a transparent proxy that stripped caller credentials would break
every authenticated upstream. Aegis needs a tenant identifier of its own that
it can consume and remove without interfering with that, so it uses a separate
header. This also keeps the Aegis authentication concern visually distinct from
the upstream's.

(`authorization` does appear in `_DENY_CACHE_HEADERS`, but that list governs
response headers on their way into the cache — see D-022. It has no effect on
what is forwarded.)

**Why X-API-Key is checked before Idempotency-Key:**
Authentication precedes all other validation. A request without a valid
API key must never reach idempotency logic regardless of what other
headers it carries. The check order in `main.py`: GET pass-through →
401 (no X-API-Key) → idempotency path → 400 (no Idempotency-Key).

**Why GET bypasses the API key check:**
GET requests have no side effects and are not cached. There is no data
leak risk from an unauthenticated read-through. Requiring X-API-Key on
GET would break every browser and health-check that accesses the proxy.

**Why the server does not validate key values:**
Aegis is a proxy, not an identity provider. Key validation (checking
against a registry of allowed keys) would require either a local
allowlist (operational burden) or an outbound call to an auth service
(latency + coupling). For v1.1, any non-empty string is accepted.
The correct v2 path: validate against a configurable allowlist in
`config.py`, or delegate to a sidecar auth service.

**Header stripping:**
`X-API-Key` is added to `_STRIP_FORWARD` in `proxy.py` and is never
forwarded to the upstream service. It is an Aegis concern only.

**Trade-offs accepted:**
- No key validation — any string is accepted as a valid API key
- No key rotation or revocation mechanism
- The composite key format assumes `:` does not appear in idempotency key values


---

## D-026 · Query String Is Part of Request Identity

**Decision:** The fingerprint covers `method + "\n" + path + "\n" + query + "\n" + body`.
The query string is hashed as raw bytes, exactly as received — not parsed,
not sorted, not normalised.

**Why the query is in the fingerprint at all:**
`forward_to_upstream()` appends the query to the target URL, so the upstream
executes on it. `POST /pay?account=alice` and `POST /pay?account=bob` are
different operations even when the body is byte-identical. Anything the
upstream acts on is part of what the request *is*, and therefore part of what
the fingerprint must cover.

Leaving it out has a specific and severe failure mode: a second request with
the same key, the same body and a different query is a fingerprint match, so
it is served the first request's cached response and the upstream is never
called. Bob receives alice's result, and Aegis reports success for an
operation it never performed. That is the exact class of wrong answer
idempotency exists to prevent, arrived at from the other direction.

**Alternatives considered:**

| Option | Why not |
|---|---|
| Parse and sort parameters | Requires owning repeated-key semantics (`?a=1&a=2`), percent-encoding equivalence, and empty-vs-absent values — permanently, across every upstream Aegis fronts |
| Hash only the path | The failure above |
| Include the full URL string | Includes scheme and host, which are Aegis's concern rather than the request's identity |

**Why raw bytes and not normalisation:**
`?a=1&b=2` and `?b=2&a=1` fingerprint differently, and that is the intended
behaviour. A client sending semantically-equal-but-textually-different queries
under a single `Idempotency-Key` has already broken the key-to-request binding
that D-006 rests on; Aegis returning `422` is a correct signal, not a false
positive. Normalisation would trade one line of code for a permanent surface of
encoding edge cases, in exchange for tolerating clients that should be fixed.

**What would change this:** a client library outside our control that reorders
query parameters between retries. That is a real thing in the wild, and if one
appears the answer is canonicalisation behind a config flag, defaulting off.

**Trade-offs accepted:**
- Reordered query parameters produce `422` rather than a cache hit
- Adding a component changes every fingerprint, including for requests with no
  query at all — the canonical form gains a separator either way. Records
  written before this change mismatch on next access and return `422` until
  they age out of the TTL window. Acceptable for a change shipped between
  versions; a fingerprint scheme expected to change repeatedly would carry a
  version prefix in the canonical string and treat unknown versions as expired

---

## D-027 · `IdempotencyRecord` Is a DTO, Not an ORM Entity

**Decision:** `models.py` defines a plain Pydantic `BaseModel`. The schema is
owned by the hand-written DDL in `store.py`. `sqlmodel` and `SQLAlchemy` are
not dependencies.

**What was there before:** `IdempotencyRecord(SQLModel, table=True)`, which
declares an ORM-mapped table. It was never used as one — `SQLModel.metadata.create_all()`
is never called, every query in `store.py` is hand-written SQL, and the model's
`idempotency_key` / `state` attributes do not match the table's `key` / `status`
columns. The declaration implied a mapping that did not exist, and carried two
dependencies to do nothing.

**Why a DTO is the right shape here:**
Aegis issues eight statements against one table. An ORM buys query construction,
relationship loading and migrations; Aegis needs none of the three. What it does
need is that a row comes back as something with attributes instead of `row[0]`
through `row[7]`, and a Pydantic model provides exactly that with a dependency
already present via FastAPI.

**Why the names still differ from the columns:**
`key` and `status` are the right names in SQL — `idempotency_keys.idempotency_key`
would stutter, and `status` is conventional for a state column. `idempotency_key`
and `state` are the right names in Python, where there is no table name for
context and `State` is the enum's name. The translation happens in exactly one
place, `store.get_record()`, which is also the only function that needs to know
column order.

**Trade-offs accepted:**
- Schema changes require editing the DDL and `get_record()` together
- No migration tooling; a column addition is a manual `ALTER TABLE`

---

## D-028 · Unresolved Outcomes Hold the Key Rather Than Releasing It

**Decision:** If Aegis fails *after* the upstream may have executed, the
`in_flight` row is left in place and the client receives `409`. The key is not
released and no `failed` record is written.

**The three failure paths, and why they differ:**

| Failure | Did the upstream run? | Action | Response |
|---|---|---|---|
| `httpx.RequestError` (unreachable, timeout) | Assumed no | Delete the row — key released | `502` |
| Non-cacheable status (`5xx`/`408`/`425`/`429`) | Yes, and it told us so | Mark `failed` — retryable | Upstream's status |
| Anything else, after the call was made | **Unknown** | Leave `in_flight` — key held | `409` |

**Why the third row cannot behave like the first:**
The tempting cleanup is to delete the row in a broad `except` so no key is ever
stuck. That is precisely wrong. If the upstream executed and Aegis then failed
while writing the outcome, deleting the row releases the key, and the client's
retry executes the operation a second time. A stuck key is an inconvenience; a
duplicate charge is the failure this entire service exists to prevent. When the
outcome is unknown, saying so is the only safe answer.

**Why `409` and not `500`:**
The client's required action is identical to the crash-orphan case — use a new
`Idempotency-Key`, because the original outcome cannot be determined. Same
situation from the caller's side, same status, same hint.

**How the key eventually clears:**
Via `recover_stuck_in_flight()` on the next restart, or by ageing out of the
TTL window. DESIGN.md §12 carries the runtime version of that sweep as a
post-MVP extension.

**Trade-offs accepted:**
- A rare Aegis-side fault renders one key unusable until restart or TTL expiry
- The client must generate a new key, which requires them to handle `409`


---

# Part II — Build Journal

> This section documents every significant bug, incorrect assumption, and
> deliberate change made during the Aegis build process.
> It exists so that every line of code can be defended in an interview —
> including the lines that were wrong first.

---

## BJ-001 · Fingerprint Scope: Body-Only → Method + Path + Body

**What was wrong:**
`compute_fingerprint()` originally hashed only the request body:
```python
return hashlib.sha256(body).hexdigest()
```

**The bug:**
A `POST /payments` with `{"amount":100}` and a `POST /refunds` with the same
body produce the same fingerprint. If both were registered under different keys,
a fingerprint comparison would incorrectly flag a mismatch when one key was
looked up on a different endpoint.

More critically: the Idempotency-Key is client-supplied and not endpoint-scoped.
A client reusing the same key on a different endpoint should get a 422 — but
with body-only fingerprinting, same body + different endpoint = same fingerprint
= silent cache hit serving the wrong response.

**The fix:**
```python
canonical = method.upper().encode() + b"\n" + path.encode() + b"\n" + body
return hashlib.sha256(canonical).hexdigest()
```

`method.upper()` normalises casing so `post` and `POST` fingerprint identically;
working on bytes avoids decoding the body (which may not be valid UTF-8).

The newline separators are intentional: they prevent `POST` + `/pay` + `ment`
from producing the same hash as `POST` + `/payment` + `""`.

**Follow-up:** this fix was still incomplete — the query string remained
outside the hash until the v1.2.0 audit. See D-026 and BJ-011.

---

## BJ-002 · Two-State → Three-State Machine

**What was wrong:**
The original code used a two-state machine: `PENDING` and `COMPLETE` (a `KeyStatus`
enum). There was no `failed` state.

**The consequence:**
When upstream returned a `5xx` or timed out, the code deleted the `PENDING` record
and returned the error. The key was released — but the client had no way to know
whether to retry with the same key or a new one. Worse, `5xx` responses were being
cached as `COMPLETE` in some paths, bricking the key for 24 hours.

**The fix:**
Three states: `in_flight` · `completed` · `failed`.

- `failed` records are explicitly retryable — the next request clears the record
  and re-runs the request fresh.
- `completed` records are cached and replayed forever until TTL.
- The distinction means a transient upstream error never permanently bricks a key.

**Class rename:** `KeyStatus` → `State`. Field rename: `state` (not `status`).

---

## BJ-003 · 409 Semantics: Concurrency Signal → Crash-Recovery Signal

**What was wrong:**
Original documentation and study materials described `409` as the response to a
concurrent duplicate request. This was incorrect.

**The reality:**
With the `asyncio.Lock`, concurrent duplicates **block** on the lock and receive
the cached response. They never see `in_flight` and never get `409`.

`409` fires only for a crash-orphaned `in_flight` record — one that survived
a process crash and was not recovered by `recover_stuck_in_flight()` (either
because it was newer than 60 seconds at startup, or because it was created in
a session that never restarted).

**Why this matters in an interview:**
An interviewer who asks "why does a concurrent duplicate get 409?" expects the
answer "it doesn't — it blocks on the lock and gets the cached response." The
previous answer would have been wrong.

---

## BJ-004 · `cleanup()` Removal from `LockManager`

**What was wrong:**
The original `lock_manager.py` had a `cleanup(key)` method:
```python
def cleanup(self, key: str) -> None:
    lock = self._locks.get(key)
    if lock and not lock.locked():
        del self._locks[key]
```

**The problem:**
This is racy. A coroutine can hold a reference to the `Lock` object while
`cleanup()` removes the entry from the dict. The next `get(key)` call then
creates a new, independent `Lock` — and two coroutines now hold different
locks for the same key. Mutual exclusion is broken.

**The fix:**
Removed `cleanup()` entirely. The registry now grows monotonically and is
cleared only by process restart — see D-020, which corrects an earlier claim
that the TTL window bounded it. V2 path: replace the in-memory lock with a DB
`PRIMARY KEY / IntegrityError` gate.

---

## BJ-005 · Registry Lock Addition

**What was wrong:**
The original `LockManager.get()` was not thread/coroutine-safe for new keys:
```python
def get(self, key: str) -> asyncio.Lock:
    if key not in self._locks:
        self._locks[key] = asyncio.Lock()
    return self._locks[key]
```

**The race:**
Two coroutines for the same brand-new key both enter `get()`, both find
`key not in self._locks`, and both create separate `Lock` objects. They
hold independent locks and never contend — the race condition the lock
was supposed to prevent now goes undetected.

**The fix:**
Made `get()` async and guarded the registry dict with `_registry_lock`:
```python
async def get(self, key: str) -> asyncio.Lock:
    async with self._registry_lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]
```

---

## BJ-006 · `expires_at` Wired In (Was Defined but Never Stored)

**What was wrong:**
The `IdempotencyRecord` model had an `expires_at` field with a `default_factory`,
but the SQLite table had no `expires_at` column, and `store.py` never wrote or
read it. The field existed in the model and nowhere else.

**The consequence:**
`eviction.py`'s `is_expired()` was computing expiry from `created_at` at runtime:
```python
return (time.time() - created_at) > settings.ttl_seconds
```
This worked but prevented per-key variable TTLs and made the eviction sweep
use arithmetic on a stored column (not directly indexable).

**The fix:**
- Added `expires_at REAL NOT NULL` to the table DDL
- Changed `insert_in_flight()` to compute and store `expires_at = now + ttl_seconds`
- Updated `get_record()` to read `expires_at` from row[7]
- Changed `is_expired(expires_at)` to `return time.time() > expires_at`
- Changed eviction sweep to `WHERE expires_at < now` (indexed)
- Moved index from `idx_created_at` to `idx_expires_at`

---

## BJ-007 · `updated_at` Removed

**What was wrong:**
`IdempotencyRecord` had an `updated_at` field with a `default_factory`. Like the
original `expires_at`, it was never written to the DB or read back. It existed
only in the model — dead weight with a misleading name.

**The fix:**
Removed from the model. The correct audit story: `updated_at` is a v2 feature
for incident investigation ("how long did this record sit in `in_flight`?").
It is not in scope for v1 and should not appear in the schema until it is used.

---

## BJ-008 · Timestamp Type: `datetime` → `float`

**What was wrong:**
The model originally used `datetime` objects for `created_at` and `expires_at`,
with `default_factory=lambda: datetime.now(timezone.utc) + timedelta(hours=24)`.

**The problem:**
SQLite has no native datetime type. `store.py` wrote and read timestamps as
`REAL` (Unix epoch floats via `time.time()`). The model and the DB layer used
different types — `get_record()` was passing a float into a `datetime` field,
which Pydantic would coerce inconsistently.

**The fix:**
Changed `created_at` and `expires_at` to `float` in the model. This matches
SQLite `REAL`, matches `time.time()`, and matches `eviction.py`'s float
comparison. The `datetime` import was removed entirely from `models.py`.

**Interview answer for "why floats not datetime?":**
SQLite has no native datetime type — storing epoch floats avoids timezone-string
ambiguity and gives direct numeric comparison for the eviction sweep. In a
Postgres migration I'd switch to `timestamptz` — a contained change in `store.py`.

---

## BJ-009 · `deprecation warning` on `datetime.utcnow()`

**What was wrong:**
Python 3.12 deprecated `datetime.utcnow()`. The original model used it in
`default_factory` for timestamp fields, producing `DeprecationWarning` on
every test run.

**The fix:**
First corrected to `datetime.now(timezone.utc)`, then superseded entirely by
switching to `float` timestamps (BJ-008), which removed all `datetime` usage
from the model.

---

## BJ-010 · Upstream Error Status: `500` → `502`

**What was wrong:**
When upstream was unreachable or timed out, Aegis returned `500 Internal Server Error`.

**The problem:**
`500` implies the fault is in Aegis itself. Aegis is healthy — the upstream is not.
The correct HTTP status for a proxy that cannot reach its upstream is `502 Bad Gateway`.

**The fix:**
Changed all connection-error and timeout-error responses to `502`. Updated the
corresponding test assertion from `assert r.status_code == 500` to
`assert r.status_code == 502`.

---

---

## BJ-011 · Documentation Drift Audit (v1.1.1 → v1.2.0)

**What prompted it:**
The docs and the code were written alongside each other and then edited
separately. Between v1.0 and v1.1.1 several claims in DECISIONS.md and the
README stopped being true of the code they described, and nothing catches that
— tests assert on behaviour, not on prose. Every doc claim was therefore
checked against the running code, and the ones that failed are below.

**What the audit found in the code:**

- **Query string absent from the fingerprint.** The most serious defect in the
  project's history: the query was forwarded upstream but excluded from the
  hash, so the same key with a different query was a silent cache hit serving
  another caller's response. Reproduced live before fixing; regression test in
  `test_key_reuse_different_query_string_returns_422`. Now D-026.
- **Hop-by-hop headers were never stripped on the forward path.** `_HOP_BY_HOP`
  was referenced only by the two response helpers, while D-015 and two code
  comments asserted it applied in both directions. Fixing it surfaced the
  `content-encoding` asymmetry now documented in D-015.
- **Only `httpx.RequestError` was handled around the upstream call.** Any other
  exception left an orphaned `in_flight` row and returned an unhandled `500`.
  Now D-028.
- **`/health` did not exist.** SETUP.md told the reader to curl it to verify
  Aegis was alive; the catch-all forwarded it, so the response came from the
  upstream and would report `502` whenever the upstream — not Aegis — was down.
  Now `/_aegis/health`.

**What the audit found in the docs alone:**

- D-004, D-020 and BJ-004 all claimed the lock registry was "bounded by keys
  active within the TTL window". Nothing in the code connects the two. D-004's
  own wording gave it away: "grows without bound, bounded by…". Corrected to
  describe a monotonic registry cleared only by restart.
- D-025 claimed the caller's `Authorization` header was stripped before
  forwarding. It was not, and it must not be — the upstream still has to
  authenticate the caller. The deny-list it referred to governs cached response
  headers only. Corrected in D-022 and D-025.
- D-021 and DESIGN §9 described caching as an allow-list ("all 2xx and
  deterministic 4xx") when the code excludes a named set and caches everything
  else, `3xx` included. Restated as the exclusion rule it is.
- `MANUAL_TESTS.md` §4 sent neither header and expected `400`; the actual
  response is `401`, because auth is checked first exactly as D-025 specifies.
  The section predated D-025 and had never been re-run.
- The README claimed SQLModel mapped the record class to the table. It did not.
  Now D-027.

**The lesson worth keeping:**
Every one of these was introduced by a change that was *correct in the code*
and left a doc sentence behind. The failure mode is not carelessness at the
time of writing, it is that prose has no test. The cheapest guard is the one
used here: read each claim with the code open and check it, on a schedule
rather than when something feels wrong.


---

*Author: Somesh Kant Tiwari*
*Last updated: August 2026 — v1.2.0*
