# MANUAL_TESTS.md — Aegis Proof Guide

Step-by-step walkthrough that proves every behaviour of the idempotency proxy
by hand. Each section covers: what it proves, the exact commands, and the
expected output. Screenshot each PASS — these are your live demo evidence.

The automated suite (`pytest tests/ -v`, 24 tests) proves the same conditions
in code. This guide is for seeing them happen live.

---

## Setup

Open **three terminals**, all in the repo root with the venv active.

**Terminal 1 — mock upstream:**
```bash
source venv/bin/activate
uvicorn mock_upstream:app --port 9000
```

**Terminal 2 — Aegis:**
```bash
source venv/bin/activate
export UPSTREAM_URL=http://localhost:9000
rm -f aegis.db
uvicorn main:app --reload --port 8000
```
Look for: `[Aegis] Started. Upstream: http://localhost:9000 | DB: aegis.db`

**Terminal 3 — commands (run everything here).**

> **Required headers for all non-GET requests:**
> - `X-API-Key: demo-client` — scopes keys per caller
> - `Idempotency-Key: <value>` — deduplication key

> **Tip:** `rm -f aegis.db` + restart Aegis between unrelated test sections
> to avoid stale keys from earlier runs interfering.

---

## Section 1 — New key: forwarded and cached

**Proves:** a first-seen key reaches upstream once and the response is stored.

```bash
curl -i -X POST http://localhost:8000/orders \
  -H "X-API-Key: demo-client" \
  -H "Idempotency-Key: demo-001" \
  -H "Content-Type: application/json" \
  -d '{"item":"book"}'
```

**Expect:** `HTTP/1.1 200 OK` and JSON body from the upstream.

**Verify the DB row:**
```bash
sqlite3 aegis.db "SELECT key, status, status_code, expires_at FROM idempotency_keys WHERE key='demo-client:demo-001';"
```
**Expect:** `demo-client:demo-001|completed|200|<timestamp far in future>`

📸 **Screenshot `02-new-key.png`** — all three terminals, upstream log showing the hit.

---

## Section 2 — Duplicate key, same body: served from cache

**Proves:** a retry with the same key does NOT call upstream again.

Run the exact same command from Section 1:
```bash
curl -i -X POST http://localhost:8000/orders \
  -H "X-API-Key: demo-client" \
  -H "Idempotency-Key: demo-001" \
  -H "Content-Type: application/json" \
  -d '{"item":"book"}'
```

**Expect:** identical body to the first call, `200 OK`.

**Check Terminal 1 (mock upstream)** — it should show only **ONE** hit for
`/orders` total, not two. The second response came from Aegis's cache.

📸 **Screenshot `04-cached-replay.png`** — three terminals, upstream showing ONE hit.

---

## Section 3 — Same key, different body: 422

**Proves:** reusing a key with a changed payload is rejected.

```bash
curl -i -X POST http://localhost:8000/orders \
  -H "X-API-Key: demo-client" \
  -H "Idempotency-Key: demo-001" \
  -H "Content-Type: application/json" \
  -d '{"item":"DIFFERENT"}'
```

**Expect:** `HTTP/1.1 422 Unprocessable Content`
```json
{"error": "Idempotency-Key reused with a different request body.", ...}
```

📸 **Screenshot `05-mismatch-422.png`**

---

## Section 4 — Missing Idempotency-Key: 400

**Proves:** non-GET requests must carry the header.

```bash
curl -i -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"item":"book"}'
```

**Expect:** `HTTP/1.1 400 Bad Request`
```json
{"error": "Idempotency-Key header is required for non-GET requests"}
```

📸 **Screenshot `06-missing-key-400.png`**

---

## Section 4b — Missing X-API-Key: 401

**Proves:** authentication precedes idempotency — a non-GET request without
`X-API-Key` is rejected before any idempotency logic runs, even when an
`Idempotency-Key` is present.

```bash
curl -i -X POST http://localhost:8000/orders \
  -H "Idempotency-Key: demo-401" \
  -H "Content-Type: application/json" \
  -d '{"item":"book"}'
```

**Expect:** `HTTP/1.1 401 Unauthorized`
```json
{"error": "X-API-Key header is required"}
```

**Verify nothing was written** — the request never reached the idempotency layer:
```bash
sqlite3 aegis.db "SELECT COUNT(*) FROM idempotency_keys WHERE key LIKE '%demo-401';"
```
**Expect:** `0`

📸 **Screenshot `06b-missing-api-key-401.png`**

---

## Section 5 — GET passes through: never cached

**Proves:** GET is idempotent by HTTP semantics — Aegis never caches it.

```bash
curl -i http://localhost:8000/orders -H "Idempotency-Key: demo-001"
curl -i http://localhost:8000/orders -H "Idempotency-Key: demo-001"
```

**Expect:** both return `200 OK`, and the mock upstream log shows **TWO** hits —
the GET was forwarded both times, not served from cache.

📸 **Screenshot `07-get-passthrough.png`**

---

## Section 6 — Concurrent retries: exactly one execution

**Proves:** the per-key `asyncio.Lock` prevents double-execution under a retry storm.

```bash
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -X POST http://localhost:8000/orders \
    -H "X-API-Key: demo-client" \
    -H "Idempotency-Key: race-001" \
    -H "Content-Type: application/json" \
    -d '{"item":"book"}' &
done
wait
echo "---done---"
```

**Verify exactly one row:**
```bash
sqlite3 aegis.db "SELECT COUNT(*) FROM idempotency_keys WHERE key='demo-client:race-001';"
```
**Expect:** `1`

Check Terminal 1 — it should show exactly **ONE** upstream hit for `/orders`.
5 concurrent retries, 1 execution. This is the headline guarantee.

📸 **Screenshot `08-concurrency.png`** — all three terminals + the `COUNT(*) = 1` result.

---

## Section 7 — Crash recovery: stuck in_flight becomes failed

**Proves:** a record orphaned by a crash is recovered on startup, not stuck forever.

**Step 1 — plant a stuck record (simulating a crash 5 minutes ago):**
```bash
sqlite3 aegis.db "INSERT INTO idempotency_keys \
  (key, fingerprint, status, created_at, expires_at) VALUES \
  ('demo-client:crash-001', 'fp-crash', 'in_flight', \
  $(python3 -c 'import time; print(time.time()-300)'), \
  $(python3 -c 'import time; print(time.time()+86100)'));"
```

**Step 2 — confirm it's stuck:**
```bash
sqlite3 aegis.db "SELECT key, status FROM idempotency_keys WHERE key='demo-client:crash-001';"
```
**Expect:** `demo-client:crash-001|in_flight`

**Step 3 — restart Aegis** (Ctrl+C in Terminal 2, then):
```bash
uvicorn main:app --reload --port 8000
```

**Watch Terminal 2 carefully for:**
```
[Aegis] Recovered 1 stuck in_flight record(s) from previous crash.
[Aegis] Started. Upstream: http://localhost:9000 | DB: aegis.db
```

**Step 4 — confirm recovery:**
```bash
sqlite3 aegis.db "SELECT key, status FROM idempotency_keys WHERE key='demo-client:crash-001';"
```
**Expect:** `demo-client:crash-001|failed`

📸 **Screenshot `09-crash-recovery.png`** — Terminal 2 showing the recovery line + DB showing `failed`.

---

## Section 8 — Failed record retry: retryable with same key

**Proves:** a `failed` record allows retry with the same key.

Using `crash-001` (now in `failed` state from Section 7):

```bash
curl -i -X POST http://localhost:8000/orders \
  -H "X-API-Key: demo-client" \
  -H "Idempotency-Key: crash-001" \
  -H "Content-Type: application/json" \
  -d '{"item":"book"}'
```

**Expect:** `HTTP/1.1 200 OK` — the failed record was cleared and re-run fresh.

**Verify:**
```bash
sqlite3 aegis.db "SELECT key, status FROM idempotency_keys WHERE key='demo-client:crash-001';"
```
**Expect:** `demo-client:crash-001|completed`

📸 **Screenshot `10-failed-retry.png`** — `200 OK` response + `demo-client:crash-001|completed` in DB.

---

## Section 9 — TTL eviction: expired records are deleted

**Proves:** the background sweep physically removes expired rows.

**Insert an already-expired record** (using `expires_at` in the past):
```bash
sqlite3 aegis.db "INSERT INTO idempotency_keys \
  (key, fingerprint, status, created_at, expires_at) VALUES \
  ('old-001', 'fp-old', 'completed', \
  $(python3 -c 'import time; print(time.time()-999999)'), \
  $(python3 -c 'import time; print(time.time()-1)'));"
```

Note: `expires_at` is set to **1 second in the past** — the eviction sweep
filters on `expires_at < now`, so this record will be deleted on the next sweep.

**Trigger a fast sweep** — restart Aegis with a short eviction interval:
```bash
EVICTION_INTERVAL_SECONDS=5 uvicorn main:app --reload --port 8000
```

**Wait 10 seconds, then:**
```bash
sqlite3 aegis.db "SELECT COUNT(*) FROM idempotency_keys WHERE key='old-001';"
```
**Expect:** `0` — the sweep deleted it.

---

## Summary — What Each Section Proves

| Section | Condition | Mechanism |
|---|---|---|
| 1 | New key | Forward + cache |
| 2 | Duplicate, same body | Cache hit, upstream not called |
| 3 | Same key, diff body | SHA-256 fingerprint mismatch → 422 |
| 4 | Missing Idempotency-Key | 400 contract enforcement |
| 4b | Missing X-API-Key | 401 — auth precedes idempotency |
| 5 | GET | Pass-through, never cached |
| 6 | Concurrent retries | Per-key `asyncio.Lock` → one execution |
| 7 | Crash recovery | Startup `recover_stuck_in_flight()` |
| 8 | Failed retry | `failed` records are retryable |
| 9 | TTL eviction | Background sweep on `expires_at` |

These ten sections cover every guarantee Aegis makes. Captured screenshots
are stronger interview evidence than "it has tests" — they show it running.