# SETUP.md — Complete Setup & Run Guide

Everything from zero to a running Aegis instance with passing tests.
No steps are skipped. Follow in order.

---

## Prerequisites

```bash
python3 --version     # Must be 3.10 or higher
git --version         # Any version
curl --version        # Pre-installed on Mac
```

If Python is below 3.10:
```bash
brew install python@3.11
```

---

## Part 1 — Project Setup

### Step 1: Clone the repository

```bash
git clone https://github.com/someshktiwari/aegis.git
cd aegis
```

### Step 2: Create a virtual environment

```bash
python3 -m venv venv
```

This creates a self-contained Python environment inside the project folder.
It does **not** affect your system Python.

### Step 3: Activate the virtual environment

```bash
source venv/bin/activate
```

Your terminal prompt changes to show `(venv)`. Do this every time you open
a new terminal session for this project.

Confirm the correct Python is active:
```bash
which python3
# Should show: .../aegis/venv/bin/python3
```

### Step 4: Install dependencies

```bash
python3 -m pip install -r requirements.txt
```

Always use `python3 -m pip` — it guarantees you're installing into the active
venv and not the system Python.

Expected output ends with:
```
Successfully installed aiosqlite-0.x fastapi-0.x httpx-0.x ...
```

If you see `error: externally-managed-environment`, the venv is not active.
Re-run `source venv/bin/activate` and try again.

### Step 5: Create the `.env` configuration file

```bash
cat > .env << 'EOF'
UPSTREAM_URL=http://localhost:9000
DB_PATH=aegis.db
TTL_SECONDS=86400
PORT=8000
EVICTION_INTERVAL_SECONDS=300
UPSTREAM_TIMEOUT_SECONDS=30
EOF
```

### Step 6: Verify `pytest.ini`

```bash
cat pytest.ini
```

Should contain:
```ini
[pytest]
asyncio_mode = auto
pythonpath = .
```

If missing or different, recreate it:
```bash
cat > pytest.ini << 'EOF'
[pytest]
asyncio_mode = auto
pythonpath = .
EOF
```

---

## Part 2 — Running Aegis

Aegis is a proxy — it needs something to proxy to. You need two terminals:
one for the mock upstream, one for Aegis itself.

### Step 7: Start the mock upstream (Terminal 1)

```bash
source venv/bin/activate
uvicorn mock_upstream:app --port 9000
```

**Expected output:**
```
INFO:     Uvicorn running on http://127.0.0.1:9000 (Press CTRL+C to quit)
```

`mock_upstream.py` is a simple FastAPI app included in the repo. It accepts
any HTTP request and returns:
```json
{"status": "ok", "message": "payment accepted", "upstream": "mock", "path": "..."}
```

Leave this running.

### Step 8: Start Aegis (Terminal 2)

```bash
source venv/bin/activate
export UPSTREAM_URL=http://localhost:9000
uvicorn main:app --reload --port 8000
```

**Expected output:**
```
[Aegis] Started. Upstream: http://localhost:9000 | DB: aegis.db
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000
```

If you see a crash-recovery message, that is expected when a previous session
left a stuck record:
```
[Aegis] Recovered N stuck in_flight record(s) from previous crash.
```

### Step 9: Verify Aegis is alive

```bash
curl http://localhost:8000/health
```

Or open **http://localhost:8000/docs** in your browser for the full Swagger UI.

---

## Part 3 — Quick curl Tests

Run each command in a third terminal with the venv active.

### New key — should reach upstream

```bash
curl -X POST http://localhost:8000/payments \
  -H "X-API-Key: my-service" \
  -H "Idempotency-Key: test-001" \
  -H "Content-Type: application/json" \
  -d '{"amount": 500}'
```

**Expect:** `200 OK` and a JSON response from the mock upstream.

### Same key, same body — should return cached response

```bash
curl -X POST http://localhost:8000/payments \
  -H "X-API-Key: my-service" \
  -H "Idempotency-Key: test-001" \
  -H "Content-Type: application/json" \
  -d '{"amount": 500}'
```

**Expect:** identical response to the first call. Terminal 1 (upstream) shows
only **ONE** hit — the second response was served from cache.

### Same key, different body — should return 422

```bash
curl -X POST http://localhost:8000/payments \
  -H "X-API-Key: my-service" \
  -H "Idempotency-Key: test-001" \
  -H "Content-Type: application/json" \
  -d '{"amount": 999}'
```

**Expect:** `422 Unprocessable Content`

### Missing headers — 401 then 400

```bash
# Missing X-API-Key → 401
curl -X POST http://localhost:8000/payments \
  -H "Content-Type: application/json" \
  -d '{"amount": 100}'

# Missing Idempotency-Key (with valid API key) → 400
curl -X POST http://localhost:8000/payments \
  -H "X-API-Key: my-service" \
  -H "Content-Type: application/json" \
  -d '{"amount": 100}'
```

**Expect:** `401 Unauthorized` for the first command, `400 Bad Request` for the second.

### GET request — should pass through

```bash
curl -X GET http://localhost:8000/payments
```

**Expect:** `200 OK` — GET is always forwarded, never cached.

---

## Part 4 — Running the Test Suite

The test suite uses in-memory SQLite and mocks all upstream calls.
You do **not** need the servers running for tests.

```bash
source venv/bin/activate
pytest tests/ -v
```

**Expected output:**
```
tests/test_proxy.py::test_new_key_forwards_to_upstream PASSED
tests/test_proxy.py::test_duplicate_key_same_body_returns_cached PASSED
tests/test_proxy.py::test_key_reuse_different_body_returns_422 PASSED
tests/test_proxy.py::test_missing_idempotency_key_returns_400 PASSED
tests/test_proxy.py::test_expired_key_treated_as_new PASSED
tests/test_proxy.py::test_get_without_key_passes_through PASSED
tests/test_proxy.py::test_get_with_key_passes_through_without_caching PASSED
tests/test_proxy.py::test_upstream_connection_error_returns_502_and_releases_key PASSED
tests/test_proxy.py::test_upstream_timeout_releases_key PASSED
tests/test_proxy.py::test_upstream_5xx_is_not_cached PASSED
tests/test_proxy.py::test_upstream_429_is_not_cached PASSED
tests/test_proxy.py::test_upstream_4xx_deterministic_is_cached PASSED
tests/test_proxy.py::test_set_cookie_stripped_from_cached_response PASSED
tests/test_proxy.py::test_missing_api_key_returns_401 PASSED
tests/test_proxy.py::test_api_key_scopes_idempotency_keys PASSED
tests/test_proxy.py::test_concurrent_duplicates_execute_upstream_exactly_once PASSED
tests/test_proxy.py::test_orphaned_in_flight_returns_409 PASSED
tests/test_store.py::test_insert_in_flight_creates_record PASSED
tests/test_store.py::test_get_record_returns_none_for_unknown_key PASSED
tests/test_store.py::test_update_complete_transitions_status PASSED
tests/test_store.py::test_delete_record_removes_row PASSED
tests/test_store.py::test_delete_expired_removes_old_rows PASSED
tests/test_store.py::test_delete_expired_preserves_fresh_rows PASSED
tests/test_store.py::test_recover_stuck_in_flight_flips_old_records_to_failed PASSED

24 passed in 0.25s
```

---

## Part 5 — Inspecting the Database

Install DB Browser for SQLite (free, Mac):
```bash
brew install --cask db-browser-for-sqlite
```

Open `aegis.db` from inside the project folder. Browse to the
`idempotency_keys` table to see records being created in real time.

All 8 columns are visible: `key`, `fingerprint`, `status`, `status_code`,
`response_body`, `response_headers`, `created_at`, `expires_at`.

---

## Part 6 — Full Manual Proof

To prove every behaviour step by step — new key, cache hit, 422, 400, GET
pass-through, concurrency, crash recovery, failed-retry, TTL eviction — follow:

```
MANUAL_TESTS.md
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `zsh: command not found: pip` | Use `python3 -m pip` instead |
| `error: externally-managed-environment` | Run `source venv/bin/activate` first |
| `ModuleNotFoundError: No module named 'main'` | Ensure `pytest.ini` has `pythonpath = .` |
| Port already in use | `lsof -i :8000` then `kill -9 <PID>` |
| `uvicorn: command not found` | Venv not active — run `source venv/bin/activate` |
| `409 Conflict` on repeated curl | Use a fresh key or `rm -f aegis.db` + restart |

---

## Daily Workflow

```bash
# Terminal 1 — mock upstream
cd ~/Projects/aegis && source venv/bin/activate
uvicorn mock_upstream:app --port 9000

# Terminal 2 — Aegis
cd ~/Projects/aegis && source venv/bin/activate
export UPSTREAM_URL=http://localhost:9000
uvicorn main:app --reload --port 8000

# Terminal 3 — tests / curl
cd ~/Projects/aegis && source venv/bin/activate
pytest tests/ -v
```

---

## Required Headers

Every non-GET request must include both headers:

```bash
X-API-Key: <your-api-key>       # Scopes idempotency keys per caller
Idempotency-Key: <unique-key>   # Deduplication key for this operation
```

GET requests require neither header.

---

## Documentation

| Document | Location | Contents |
|---|---|---|
| Architecture | `docs/DESIGN.md` | Components, DB schema, state machine, sequence diagrams |
| Decisions | `docs/DECISIONS.md` | Every architectural choice with rationale + build journal |
| Manual tests | `MANUAL_TESTS.md` | Step-by-step proof of every behaviour |
| This guide | `SETUP.md` | Setup, run, test, troubleshoot |