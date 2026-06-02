# DECISIONS.md — Aegis Idempotency Proxy Service

Every architectural and implementation decision made in building Aegis.
Each entry covers: the decision, every alternative considered, the reasoning,
and the trade-offs explicitly accepted.

This document exists to make every line of code defensible in an interview.
Read it before the engineer walkthrough. Read it before every interview.

---

## D-001 · Reverse Proxy Architecture vs. Middleware Library

**Decision:** Aegis is a standalone HTTP reverse proxy, not an importable Python library.

**Alternatives considered:**
- A FastAPI/Starlette middleware class that upstream services import and mount
- A Python decorator (`@idempotent`) applied per endpoint in the upstream codebase
- A Django middleware class
- A WSGI/ASGI middleware package published to PyPI

**Why this:**
A reverse proxy requires zero changes to the upstream service. The idempotency
concern is cross-cutting infrastructure — it belongs at the network layer,
not inside application code. The same Aegis instance can protect any upstream
service regardless of what framework it uses (FastAPI, Flask, Express, Rails, a
legacy monolith). No code changes, no library upgrades, no deployment coupling.

A middleware library creates tight coupling: every upstream service must import,
configure, and version-pin Aegis. A bug in Aegis affects every service that
adopted it at the library level, and upgrades must be coordinated across all
consumers.

**Trade-offs accepted:**
- An extra network hop (client → Aegis → upstream) adds latency (~1–3ms on
  loopback, acceptable for the single-node use case)
- Aegis must faithfully proxy request headers, methods, and bodies
- Requires running Aegis as a separate process

---

## D-002 · FastAPI over Flask or Django

**Decision:** FastAPI is the web framework for Aegis.

**Alternatives considered:**
- Flask (synchronous, mature, widespread)
- Django + Django REST Framework (batteries-included)
- Starlette directly (FastAPI is built on it)
- aiohttp (async, lower-level)

**Why this:**
Aegis is fundamentally I/O-bound: every request reads from SQLite, potentially
writes to SQLite, then makes an outbound HTTP call to upstream. Python's asyncio
allows a single thread to handle many concurrent in-flight requests without
blocking — but only if every I/O call is awaitable.

Flask is synchronous by default. Running Flask with async requires workarounds
(`asgiref`, `gevent`) that add complexity and do not yield the full benefit of
the event loop model.

Django is designed for full-stack applications: ORM, admin interface, templating,
migrations, sessions. Every one of these is overhead Aegis has no use for.

FastAPI provides:
- Native async/await throughout
- Pydantic-based request/response validation
- Automatic OpenAPI documentation at `/docs`
- Dependency injection (used for DB connection via `app.state`)
- Lifespan context manager for startup/shutdown hooks
- Built on Starlette, which is battle-tested in production

Using Starlette directly would mean re-implementing the parts of FastAPI that
we need (routing, dependency injection, error handling) for no benefit.

**Trade-offs accepted:**
- FastAPI is newer than Flask; fewer Stack Overflow answers for edge cases
- Async programming requires discipline: no blocking calls inside `async def`

---

## D-003 · SQLite over PostgreSQL, MySQL, or Redis

**Decision:** SQLite via `aiosqlite` is the persistence layer.

**Alternatives considered:**
- PostgreSQL with `asyncpg` (production-grade relational DB)
- MySQL with `aiomysql`
- Redis with `aioredis` (in-memory key-value store with native TTL)
- An in-memory Python dict (fastest, no persistence)
- A JSON file on disk

**Why this:**
Aegis is designed for single-node deployment. SQLite requires zero
infrastructure: no daemon to run, no connection string to configure, no
separate process to keep alive. The entire database is one file (`aegis.db`).

SQLite's ACID guarantees mean that even if the process crashes mid-write,
the database will not be in a corrupt state. For the problem Aegis solves
(idempotency key storage with TTL eviction), SQLite handles thousands of
reads/writes per second on modern hardware — more than sufficient for
single-node.

PostgreSQL would be correct if Aegis needed multi-node deployment with a shared
database. It is not correct here because it introduces a deployment dependency
without enabling any capability the single-node version needs.

Redis has native key TTL support, which appears attractive. However, Redis is
not durable by default (AOF/RDB must be explicitly enabled and configured). A
Redis restart or out-of-memory eviction silently deletes idempotency records.
A client retrying after a Redis eviction would see a "key not found", treat the
request as new, and trigger a duplicate upstream execution — exactly the failure
mode Aegis exists to prevent.

An in-memory dict is fast but loses all state on Aegis restart. Any client
retry after a restart would re-execute.

A JSON file would require manual locking for concurrent access.

**Trade-offs accepted:**
- SQLite does not support horizontal scaling (multiple Aegis nodes sharing one DB)
- Write throughput is limited versus PostgreSQL — acceptable for single-node
- Must use `aiosqlite` (async wrapper) everywhere; the synchronous `sqlite3`
  module must never be used inside an async function (it would block the event loop)

---

## D-004 · asyncio.Lock for Concurrency Control

**Decision:** A per-key `asyncio.Lock` registry in `lock_manager.py` is used to
serialise concurrent requests for the same idempotency key.

**Alternatives considered:**
- DB-level locking (`SELECT FOR UPDATE` — not available in SQLite)
- SQLite `BEGIN EXCLUSIVE` transaction held across upstream call
- Optimistic concurrency: check-then-set, retry on conflict
- A `status` column in the DB only, no in-memory lock
- Redis `SET NX PX` (compare-and-set with TTL)

**Why this:**
The race condition: two identical requests (same key, same body) arrive
simultaneously. Without a lock, both read the DB, both find "not found",
both write PENDING, both call upstream — double execution.

Using `BEGIN EXCLUSIVE` would hold a write lock on the entire SQLite database
for the full duration of the upstream call (potentially seconds). This blocks
ALL other writes to the database — every request with a different key stalls.
This is unacceptable.

`SELECT FOR UPDATE` does not exist in SQLite.

A `status` column alone (without an in-process lock) would still have the
race: both requests could read "not found" before either writes PENDING.

The `asyncio.Lock` approach is per-key and in-process. The lock is held for
the FULL duration: (a) DB read, (b) upstream call, (c) DB write. A concurrent
duplicate (Request B) blocks on the lock until Request A completes. When B
acquires the lock, it finds COMPLETE in the DB and returns the cached response.
**B never sees PENDING during normal operation.**

**The lock + PENDING pattern:**
```
acquire lock(key)
  └─ read DB
      ├─ not found  → write PENDING → call upstream → write COMPLETE → release
      ├─ PENDING    → return 409 (crash recovery — orphaned from previous crash)
      └─ COMPLETE   → return cached or 422 → release
```

PENDING exists for crash recovery only. If Aegis crashes after writing PENDING
but before writing COMPLETE, the lock disappears (in-memory) but the PENDING
record survives in SQLite. The next request finds the orphaned PENDING and
returns 409 — correct behaviour, the original outcome is unknown.

**Trade-offs accepted:**
- In-process only: does not work across multiple Aegis nodes
- Lock registry grows without bound (cleanup() was removed — see lock_manager.py)
- PENDING records from crashed requests are stuck until TTL expiry

---

## D-005 · SHA-256 for Body Fingerprinting

**Decision:** Request bodies are hashed with SHA-256 (`hashlib.sha256`) before storage.

**Alternatives considered:**
- Storing the raw request body in SQLite
- MD5 hash
- CRC32 (faster, non-cryptographic)
- xxHash (faster, non-cryptographic)
- SHA-1 hash
- Comparing bodies in-memory without storing a hash

**Why this:**
Storing raw bodies is impractical. A request body for a file upload or a large
JSON payload could be megabytes. Storing the full payload for every idempotent
request would make `aegis.db` grow rapidly and make row comparisons slow.

SHA-256 produces a fixed 64-character hex string regardless of input size.
Storage cost is constant per row. Comparison is a string equality check on 64
characters, which SQLite handles in microseconds.

MD5 has known collision vulnerabilities. A collision means two different request
bodies produce the same hash. For Aegis, a collision would cause a legitimately
different request to be treated as a duplicate and served a cached response —
a silent data error in a payments context.

CRC32 and xxHash are designed for error detection, not fingerprinting. Their
collision rates are orders of magnitude higher than SHA-256. Using them here
would trade correctness for speed that isn't needed (hashing happens once per
request, not in a tight loop).

SHA-256 is in Python's standard library (`hashlib`) — zero external dependencies.
An empty body (`b""`) produces a consistent, predictable hash, so empty-body
requests are handled correctly.

**Trade-offs accepted:**
- SHA-256 is slower than CRC32 — irrelevant at HTTP request handler granularity
- Not using HMAC (keyed hash) — we are fingerprinting for deduplication, not
  verifying authenticity; a plain hash is correct here

---

## D-006 · 422 Unprocessable Entity for Key Reuse with Mismatched Body

**Decision:** When an `Idempotency-Key` is reused with a different request body,
Aegis returns `422 Unprocessable Entity`.

**Alternatives considered:**
- `409 Conflict` for both mismatched-body and in-flight cases
- `400 Bad Request`
- `403 Forbidden`
- `200 OK` with a custom error field in the body

**Why this:**
HTTP semantics matter. The RFC definition of `422` is: the request was
syntactically valid (correct headers, parseable body) but semantically incorrect
(the content violates a business rule). Reusing an idempotency key with a
different body is precisely a semantic violation — the client has broken the
key-to-payload binding that idempotency depends on.

`409 Conflict` means a state conflict in the server's current state. It is
reserved for the in-flight duplicate case (D-007) where two requests are
racing. Using `409` for both mismatched-body and in-flight would conflate two
distinct error conditions. A client that handles 409 with a retry would
incorrectly retry a 422 (mismatched body), which would keep failing — an
infinite retry loop.

`400 Bad Request` implies the request itself is malformed (bad syntax, missing
required field). The request is syntactically correct; the problem is the
semantic contract violation.

`403 Forbidden` implies an authorization failure. There is no authorization
involved here.

**Stripe reference:** Stripe, the canonical reference for idempotency APIs,
returns 422 for key reuse with a different payload. Aegis mirrors this
deliberately.

**Trade-offs accepted:**
- Clients that do not differentiate between 4xx status codes will not benefit
  from the semantic distinction — that is a client quality issue, not an Aegis issue

---

## D-007 · 409 Conflict for Orphaned PENDING Records

**Decision:** When a request finds a PENDING record for its key (from a previous
Aegis crash), Aegis returns `409 Conflict`.

**Important:** 409 is a crash-recovery signal, not a concurrency signal.
During normal operation, concurrent duplicates block on the asyncio.Lock (D-004)
and never see PENDING — they wait and receive the cached COMPLETE response.
409 only fires when Aegis previously crashed after writing PENDING but before
writing COMPLETE, leaving an orphaned record in SQLite.

**Alternatives considered:**
- Queue the second request and wait for the first to complete, then return
  the same response
- `503 Service Unavailable` with `Retry-After` header
- `202 Accepted` with a polling endpoint for the result

**Why this:**
`409 Conflict` accurately describes the situation: there is a resource state
conflict — the key has an unresolved in-flight state from a previous crash.
The outcome of the original request is unknown. The client must use a new key.

`503` implies the server is unavailable. Aegis is healthy.

`202` + polling adds disproportionate complexity for this edge case.

**The expected client behaviour:**
Client receives 409 → treats original outcome as unknown → uses a new
Idempotency-Key for the retry. This is correct: we cannot know if the upstream
executed before Aegis crashed.

**Trade-offs accepted:**
- Client must use a new key after receiving 409 — it cannot retry the same key
- PENDING records are stuck until TTL expiry (24h by default)
- A PENDING timeout (e.g. 60s) would reduce the window — documented in DESIGN.md

---

## D-008 · TTL-Based Eviction: Combined Lazy + Eager Strategy

**Decision:** Expired records are evicted in two ways:
1. On every key access (lazy eviction via `eviction.is_expired()`)
2. Periodically in a background task (eager eviction via `eviction_loop()`)

**Alternatives considered:**
- Lazy eviction only (check on access, never sweep)
- Eager eviction only (background task, no on-access check)
- Redis native TTL (rejected — see D-003)
- No eviction (keys live forever)
- Evict immediately after serving a cached response

**Why this:**
Lazy-only eviction means expired rows accumulate indefinitely if the key is
never accessed again. A high-churn workload (many unique keys, low repeat
access) would grow the SQLite file without bound.

Eager-only eviction (background task only) risks serving a stale cached
response if the sweep hasn't run recently. If the eviction interval is 5
minutes and a key expired 4 minutes and 59 seconds ago, a client retry could
receive the old cached response rather than a fresh upstream call.

The combined approach eliminates both problems:
- On-access check: guarantees correctness (no stale response ever served)
- Background sweep: guarantees storage hygiene (expired rows are physically deleted)

The background task uses `asyncio.sleep()` between sweeps, which yields control
to the event loop — it does not block request handling.

**TTL clock:** The TTL clock starts at `created_at` (when the key was first
seen). It does NOT reset when a cached response is served. A key created at
time T expires at T + TTL regardless of how many times it was accessed. This
mirrors Stripe's behaviour.

**Trade-offs accepted:**
- A PENDING record that expires before the upstream call completes is
  edge-case behaviour (handled: client retries with a new key)
- The background task stops when Aegis stops; expired rows are cleaned on
  the next startup's first sweep

---

## D-009 · Single Shared aiosqlite Connection

**Decision:** One `aiosqlite.Connection` is opened at startup (in `lifespan`)
and shared across all requests via `app.state.db`.

**Alternatives considered:**
- A connection pool (open N connections, assign per request)
- Open a new connection per request
- `aiosqlite` with a connection pool library

**Why this:**
SQLite supports multiple concurrent readers but only one writer at a time.
Multiple connections attempting concurrent writes produce "database is locked"
errors.

A single shared connection serialises all writes through one channel,
eliminating lock contention. Since all DB operations are async (using
`aiosqlite`), the single connection does not become a bottleneck: while one
coroutine awaits a DB write, the event loop processes other requests.

Opening a new connection per request would mean each request opens, uses, and
closes a file handle. This is slower than reusing one connection and reintroduces
the multi-writer locking problem.

**Trade-offs accepted:**
- If the DB connection is lost (file corrupted, disk full), all requests fail
  until Aegis restarts — acceptable for single-node
- Single connection means writes are serialised — acceptable at the
  concurrency level Aegis is designed for

---

## D-010 · Async-First Design Throughout

**Decision:** Every I/O operation in Aegis uses async/await: `aiosqlite` for
SQLite, `httpx.AsyncClient` for upstream HTTP calls.

**Alternatives considered:**
- Synchronous SQLite (`sqlite3`) with a thread pool executor
- Synchronous `requests` library for upstream calls
- Mixed sync/async (async routes, sync DB calls)

**Why this:**
Aegis is an I/O-bound proxy: every request waits for SQLite and waits for the
upstream response. Async I/O allows a single event loop thread to handle many
concurrent requests during these wait periods — one request is waiting for SQLite
while another is waiting for upstream, and a third is being parsed.

Using synchronous `sqlite3` inside an `async def` function blocks the event
loop for the duration of every DB call. Every other in-flight request stalls
until the DB call returns. This eliminates the concurrency benefit of FastAPI's
async model entirely.

Using the `requests` library (synchronous) for upstream calls has the same
problem: the entire event loop freezes during the network call.

`aiosqlite` wraps `sqlite3` in a thread, allowing the event loop to continue.
`httpx.AsyncClient` is natively async.

**The rule:** No blocking call (`sqlite3`, `time.sleep`, synchronous file I/O,
`requests`) may ever appear inside an `async def` function in Aegis. This must
be enforced in every code review.

**Trade-offs accepted:**
- Async code requires more care to write correctly than synchronous code
- Debugging async code (tracebacks, race conditions) is harder

---

## D-011 · httpx.AsyncClient Per-Request vs. Module-Level

**Decision:** A new `httpx.AsyncClient` is created and closed for each upstream
call inside `forward_to_upstream()`.

**Alternatives considered:**
- A module-level `AsyncClient` instance shared across all requests
- A connection pool managed at the application level

**Why this:**
For the MVP scope, a per-request client is simpler and correct. It avoids
connection state (cookies, auth headers) leaking between requests, and avoids
the complexity of managing a shared client's lifecycle (what happens if the
upstream is down and the client enters a broken state?).

The `async with httpx.AsyncClient()` pattern creates a client, makes the
request, and cleanly closes all connections — no resource leaks.

**For a production extension:** A module-level `AsyncClient` with connection
pooling would improve throughput by reusing TCP connections. This is a natural
next step noted in DESIGN.md Section 10 but is out of scope for the MVP.

**Trade-offs accepted:**
- A new TCP connection is established for each upstream call (no connection reuse)
- Slightly higher latency per request than a pooled client

---

## D-012 · Pydantic BaseSettings for Configuration

**Decision:** All configuration is loaded from environment variables via
`pydantic-settings.BaseSettings` in `config.py`.

**Alternatives considered:**
- `os.getenv()` calls scattered throughout the codebase
- A `config.yaml` or `config.json` file
- Hardcoded defaults only
- `python-dotenv` alone

**Why this:**
`BaseSettings` reads environment variables with type validation and default
values, all in one place. The single `settings` object imported by all modules
is the one source of truth for configuration.

`os.getenv()` scattered through the code has no type safety (always returns
strings), no central documentation of what config exists, and makes testing
harder (must mock individual `os.getenv` calls rather than swapping a settings
object).

YAML/JSON config files require committing a config file (risk of committing
secrets) or managing a separate config deployment pipeline.

The `.env` file is gitignored. In production, environment variables are set
directly in the deployment environment (Docker, systemd, etc.).

**Trade-offs accepted:**
- Requires `pydantic-settings` as a dependency (separate from `pydantic`)

---

## D-013 · Module-Level Singleton for lock_manager

**Decision:** `lock_manager.py` exports a single `lock_manager = LockManager()`
instance at module level, shared across all requests.

**Alternatives considered:**
- Attach `LockManager` to `app.state` alongside the DB connection
- Create a new `LockManager` per request
- Use a module-level dict directly without a class

**Why this:**
The lock registry must be shared across all concurrent requests in the process.
If each request created its own `LockManager`, two concurrent requests with the
same key would each get their own lock and never contend — defeating the purpose.

Attaching to `app.state` would work but requires threading `app.state` through
to every call site. A module-level singleton is simpler and idiomatic for
process-global state.

A plain module-level dict would work but the class encapsulates the `get()` and
`cleanup()` logic cleanly and makes the intent explicit.

**Trade-offs accepted:**
- Module-level state makes unit testing slightly harder (must reset between tests)
  — mitigated by the fact that tests use a fresh key per test case

---

## D-014 · Write PENDING Before Calling Upstream

**Decision:** `insert_pending()` is called BEFORE `forward_to_upstream()`, not after.

**Alternatives considered:**
- Call upstream first, then write the result to DB
- Call upstream first, write PENDING, then update to COMPLETE

**Why this:**
If upstream is called before writing to DB, a concurrent duplicate request
arriving during the upstream call will find "not found" in the DB and also
forward to upstream — a double execution. The PENDING write must happen FIRST
to signal "this key is in-flight" before the upstream call begins.

The write-before-call sequence, combined with the `asyncio.Lock`, guarantees
that:
1. The lock prevents two requests from entering the critical section simultaneously
2. PENDING is written before the upstream call begins
3. Any concurrent request acquiring the lock after (1) blocks — it cannot read
   PENDING because the lock is still held. It only reads the DB after A releases,
   at which point the record is COMPLETE.

**Trade-offs accepted:**
- If the upstream call fails (exception or 5xx), the PENDING record is now
  deleted and the key is released — client can retry with the same key (fixed
  in proxy.py try/except block)

---

## D-015 · Hop-by-Hop Header Filtering

**Decision:** HTTP hop-by-hop headers are stripped from both forwarded requests
and cached responses.

**Headers filtered:**
`connection`, `keep-alive`, `proxy-authenticate`, `proxy-authorization`,
`te`, `trailers`, `transfer-encoding`, `upgrade`, `content-length`

**Why this:**
Hop-by-hop headers (defined in RFC 2616 §13.5.1) are meaningful only for a
single transport hop and must not be forwarded to the next server. Forwarding
`Transfer-Encoding: chunked` to the upstream confuses it (the upstream expects
a direct connection, not proxied chunked encoding). Forwarding `Connection:
keep-alive` is meaningless for the Aegis→upstream connection.

`Content-Length` is stripped because `httpx` calculates and sets the correct
`Content-Length` for the forwarded request based on the actual body bytes.
Forwarding the original `Content-Length` could cause a mismatch if any
transformation occurred.

**Trade-offs accepted:**
- Some non-standard hop-by-hop headers (specified via `Connection: X-Custom`)
  are not filtered — acceptable for the MVP

---

## D-016 · In-Memory SQLite for Tests

**Decision:** Tests use `aiosqlite.connect(":memory:")` rather than a temporary
file on disk.

**Alternatives considered:**
- A temporary file (`tempfile.mkstemp()`) deleted after each test
- A fixed test DB file (`test.db`) recreated before each run
- Mocking the store module entirely

**Why this:**
`:memory:` creates a database that exists only for the lifetime of the
connection. There is no disk I/O, no cleanup required, and no risk of a
previous test run's data affecting the current run. Each test fixture gets a
fresh, empty database.

A temporary file on disk adds file system operations (create, delete) to every
test, slowing the suite and creating flaky failures on network file systems or
slow disks.

Mocking the store module would test the proxy logic in isolation but would not
test the integration between proxy logic and the DB layer — bugs in
`insert_pending` or `update_complete` would go undetected.

**Trade-offs accepted:**
- `:memory:` databases cannot be inspected after the test run (they disappear
  when the connection closes) — use a file DB for debugging specific tests

---

## D-017 · ASGITransport for Test HTTP Client

**Decision:** Tests use `httpx.AsyncClient(transport=ASGITransport(app=app))`
rather than spinning up a real server.

**Alternatives considered:**
- `TestClient` from `starlette.testclient` (synchronous)
- Spinning up a real `uvicorn` server on a random port in a fixture
- Mocking the FastAPI route handlers directly

**Why this:**
`ASGITransport` sends requests directly to the FastAPI ASGI app in-process
without any network stack. Tests run at in-memory speed with zero network
latency and no port conflicts. The full request/response cycle (middleware,
route matching, dependency injection, response serialisation) is exercised.

`starlette.testclient` is synchronous and wraps the async app in a thread —
this can mask async-specific bugs (like accidentally blocking the event loop)
that `ASGITransport` will surface correctly.

Spinning up a real uvicorn server works but adds startup time, requires finding
a free port, and adds complexity to the fixture.

**Trade-offs accepted:**
- Does not test uvicorn-specific behaviour (HTTP/2, TLS, connection handling)
  — acceptable for unit/integration tests

---

## D-018 · GET Pass-Through Without Idempotency Key

**Decision:** GET requests that do not include an `Idempotency-Key` header are
forwarded directly to upstream without idempotency processing.

**Alternatives considered:**
- Require `Idempotency-Key` for all methods including GET
- Return 400 for all requests without an `Idempotency-Key`
- Apply idempotency logic to GET requests too

**Why this:**
GET is defined as an idempotent method by HTTP semantics (RFC 7231). Fetching
a resource twice produces the same result and has no side effects. There is no
reason to deduplicate GET requests — the worst case of two identical GETs is
reading the same data twice, which is harmless.

Requiring `Idempotency-Key` for GET would break every existing client that
makes GET requests through Aegis without the header. It provides no safety
benefit.

**Trade-offs accepted:**
- GET responses are not cached by Aegis (a separate caching proxy would handle this)
- Clients could theoretically supply `Idempotency-Key` on a GET — this is allowed
  and processed normally, providing caching behaviour for GET as a bonus

---

## D-019 · Index on created_at Column

**Decision:** A database index is created on the `created_at` column.

```sql
CREATE INDEX idx_created_at ON idempotency_keys (created_at);
```

**Why this:**
The eviction sweep runs the query:
```sql
DELETE FROM idempotency_keys WHERE created_at < ?
```

Without an index, SQLite performs a full table scan on every sweep — reading
every row to check whether `created_at < cutoff`. At scale (tens of thousands
of rows), this is slow and holds the write lock for longer than necessary.

With the index, SQLite uses a B-tree lookup to find rows where `created_at < cutoff`
in O(log N) time. The sweep is fast and the write lock is held briefly.

The primary key (`key`) index already exists by virtue of `PRIMARY KEY`.
The `created_at` index is the only additional index needed.

**Trade-offs accepted:**
- Every insert and delete updates the index — negligible overhead at this scale

---

## D-020 · Lock Cleanup After Use

**Decision:** `lock_manager.cleanup(key)` is called after the lock is released
to remove the lock from the in-memory registry.

**Why this:**
Without cleanup, the lock registry grows indefinitely — one entry per unique
idempotency key ever seen. For high-churn workloads (millions of unique keys),
this would consume significant memory.

`cleanup()` checks that the lock is not currently held before deleting it, so
it is safe to call immediately after releasing.

**When cleanup is called:**
- After `update_complete()` on a new key (happy path)
- After returning a cached response (scenario B)
- The lock is NOT cleaned up in the 409 and 422 paths — the lock is released
  by the `async with` block exiting, but cleanup is not called because the key
  may still be in active use by the original in-flight request

**Trade-offs accepted:**
- A small window exists where a key is complete but its lock entry still exists
  (between `update_complete` and `cleanup`) — this is a microsecond window and
  has no correctness impact

---

*Author: Somesh Kant Tiwari*
*Last updated: May 2026*

---

## D-021 · Non-Cacheable HTTP Status Codes

**Decision:** The following status codes are never cached — the PENDING record is deleted
and the response is passed through so the client can retry with the same key:

- **5xx (500–599):** Transient upstream failures. The upstream may recover.
- **408 Request Timeout:** Transient — the connection timed out, not an input problem.
- **425 Too Early:** Transient — TLS 1.3 early data race condition.
- **429 Too Many Requests:** Transient — the rate limit will reset.

**Caching principle:** only cache responses that are *deterministic for the same input*.
A `400 Bad Request` (invalid payload) is deterministic — the same invalid input will
always produce the same error; caching it prevents hammering upstream needlessly.
A `429` is not deterministic — the same input will succeed after the rate limit resets.

**Stripe's semantics:** Stripe does not cache upstream errors. Aegis mirrors this.

**Trade-offs accepted:**
- A 429 received during a brief rate-limit window leaves the key available for retry,
  which is correct — but it means Aegis does not protect the upstream from retry storms
  during a rate-limit period

---

## D-022 · Response Header Caching Deny-List

**Decision:** The following response headers are stripped before storing headers in SQLite
and before replaying headers from cache:

- `set-cookie`
- `www-authenticate`
- `authorization`

**Why this:**
`Set-Cookie` is the critical one. If client A's request sets a session cookie in the
upstream response, and a later request (by any caller) shares the same idempotency key,
replaying `Set-Cookie: session=secret-A` to that later caller is a direct session-leak.

`www-authenticate` and `authorization` have the same class of risk.

**Important distinction:** these headers are still returned on the **first (fresh)**
response — the real original caller should receive their session cookie. They are only
stripped from what is stored in the DB and replayed to future callers.

**What is NOT addressed:** multi-value headers (e.g. multiple `Set-Cookie` entries) are
currently collapsed to the last value because the cache stores a plain dict. This is a
known limitation — a full fix would use a list-of-tuples header representation.

**Trade-offs accepted:**
- Upstream APIs that rely on cookie propagation through a proxy will not have cookies
  replayed on cached retries — this is the correct and safe behaviour for an idempotency
  proxy, but callers should be aware that cached responses will not carry session cookies
