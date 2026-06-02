# Aegis — Complete Setup & Run Guide

This document covers every step from zero to a running Aegis instance with passing tests.
No steps are skipped. Follow in order.

---

## Prerequisites

Before starting, confirm you have the following installed on your Mac:

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

### Step 1: Navigate to the project folder

Open Terminal. You should already have the `aegis` folder from the zip.

```bash
cd ~/Desktop/aegis/aegis
# Or wherever you extracted it. Confirm you see the .py files:
ls
# Expected output:
# config.py  eviction.py  fingerprint.py  lock_manager.py
# main.py  models.py  proxy.py  pytest.ini  requirements.txt
# store.py  tests/
```

### Step 2: Delete any broken venv and create a fresh one

```bash
rm -rf venv
python3 -m venv venv
```

This creates a self-contained Python environment inside the project folder.
It will NOT affect your system Python.

### Step 3: Activate the virtual environment

```bash
source venv/bin/activate
```

Your terminal prompt will change to show `(venv)` at the start.
You must do this every time you open a new terminal session.

To confirm the venv is active and using the right Python:
```bash
which python3
# Should show: .../aegis/venv/bin/python3
```

### Step 4: Install all dependencies

```bash
python3 -m pip install -r requirements.txt
```

Always use `python3 -m pip` on Mac — it guarantees you're using the pip
inside the active venv, not the system one.

Expected output ends with something like:
```
Successfully installed aiosqlite-0.20.0 fastapi-0.110.0 httpx-0.27.0 ...
```

If you see `error: externally-managed-environment`, it means the venv isn't
active. Re-run `source venv/bin/activate` and try again.

### Step 5: Create the .env configuration file

```bash
cat > .env << 'EOF'
UPSTREAM_URL=http://localhost:9000
DB_PATH=aegis.db
TTL_SECONDS=86400
PORT=8000
EVICTION_INTERVAL_SECONDS=300
EOF
```

Confirm it was created:
```bash
cat .env
```

### Step 6: Fix pytest.ini to resolve module imports

```bash
cat > pytest.ini << 'EOF'
[pytest]
asyncio_mode = auto
pythonpath = .
EOF
```

This tells pytest to look for modules in the current directory,
so `from main import app` works in tests.

---

## Part 2 — Running Aegis

Aegis is a proxy — it needs something to proxy TO. You need two terminal
windows: one for the mock upstream, one for Aegis itself.

### Step 7: Open a second terminal tab/window

In that second terminal:
```bash
cd ~/Desktop/aegis/aegis
source venv/bin/activate
```

### Step 8: Start the mock upstream server

In the second terminal:
```bash
python3 -m http.server 9000
```

You will see:
```
Serving HTTP on :: port 9000 (http://[::]:9000/) ...
```

Leave this running. This acts as the upstream service Aegis proxies to.

### Step 9: Start Aegis

Back in the first terminal (with venv active, inside the aegis folder):
```bash
uvicorn main:app --reload --port 8000
```

You will see:
```
INFO:     Started server process
INFO:     Waiting for application startup.
[Aegis] Started. Upstream: http://localhost:9000 | DB: aegis.db
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000
```

Aegis is now running.

### Step 10: Verify Aegis is alive

Open a third terminal tab, or use the browser:
```bash
curl http://localhost:8000/health
```

Or open `http://localhost:8000/docs` in your browser.
You will see the auto-generated FastAPI API documentation.

---

## Part 3 — Testing With curl

Run each curl command individually. Do NOT paste the comment lines (lines
starting with `#`) into the terminal — paste only the `curl` commands.

### Test 1: New key — should reach upstream

```bash
curl -X POST http://localhost:8000/payments \
  -H "Idempotency-Key: test-001" \
  -H "Content-Type: application/json" \
  -d '{"amount": 500}'
```

Expected: a response from the mock upstream (likely an HTML page or error
body — that is fine, what matters is Aegis responded without crashing).

### Test 2: Same key, same body — should return cached response

```bash
curl -X POST http://localhost:8000/payments \
  -H "Idempotency-Key: test-001" \
  -H "Content-Type: application/json" \
  -d '{"amount": 500}'
```

Expected: identical response to Test 1, but this time Aegis did NOT call
upstream again — it served from cache.

### Test 3: Same key, different body — should return 422

```bash
curl -X POST http://localhost:8000/payments \
  -H "Idempotency-Key: test-001" \
  -H "Content-Type: application/json" \
  -d '{"amount": 999}'
```

Expected:
```json
{"error": "Idempotency-Key reused with a different request body.", "hint": "..."}
```
HTTP status: 422

### Test 4: Missing Idempotency-Key — should return 400

```bash
curl -X POST http://localhost:8000/payments \
  -H "Content-Type: application/json" \
  -d '{"amount": 100}'
```

Expected:
```json
{"error": "Idempotency-Key header is required"}
```
HTTP status: 400

### Test 5: GET request without key — should pass through

```bash
curl -X GET http://localhost:8000/anything
```

Expected: response from upstream, no 400 error.

---

## Part 4 — Running the Test Suite

The test suite uses in-memory SQLite and mocks the upstream call,
so you do NOT need the mock upstream server running for tests.

Stop Aegis (Ctrl+C in the uvicorn terminal) if you want a clean test run,
or just open a new terminal tab.

```bash
cd ~/Desktop/aegis/aegis
source venv/bin/activate
PYTHONPATH=. pytest tests/ -v
```

Expected output:
```
tests/test_proxy.py::test_new_key_forwards_to_upstream PASSED
tests/test_proxy.py::test_duplicate_key_same_body_returns_cached PASSED
tests/test_proxy.py::test_key_reuse_different_body_returns_422 PASSED
tests/test_proxy.py::test_missing_idempotency_key_returns_400 PASSED
tests/test_proxy.py::test_expired_key_treated_as_new PASSED
tests/test_proxy.py::test_get_without_key_passes_through PASSED
tests/test_store.py::test_insert_pending_creates_record PASSED
tests/test_store.py::test_get_record_returns_none_for_unknown_key PASSED
tests/test_store.py::test_update_complete_transitions_status PASSED
tests/test_store.py::test_delete_record_removes_row PASSED
tests/test_store.py::test_delete_expired_removes_old_rows PASSED
tests/test_store.py::test_delete_expired_preserves_fresh_rows PASSED

12 passed in X.XXs
```

---

## Part 5 — Inspecting the Database

To see the idempotency records being created in real time, install
DB Browser for SQLite (free):

```bash
brew install --cask db-browser-for-sqlite
```

Open `aegis.db` from inside the project folder. You will see the
`idempotency_keys` table populate as you send curl requests.

This is valuable for the engineer walkthrough — you can visually show
the PENDING → COMPLETE lifecycle of each record.

---

## Troubleshooting

### `zsh: command not found: pip`
Use `python3 -m pip` instead of `pip`.

### `error: externally-managed-environment`
The venv is not active. Run `source venv/bin/activate` first.

### `ModuleNotFoundError: No module named 'main'` in pytest
Make sure `pytest.ini` contains `pythonpath = .` and run with `PYTHONPATH=. pytest tests/ -v`.

### `Internal Server Error` on curl
No upstream is running on port 9000. Start `python3 -m http.server 9000` in a second terminal.

### `409 Conflict` on repeated curl to same key
A previous request left the record in PENDING state (usually because upstream was unreachable).
Use a fresh key (`test-002`, `test-003`, etc.) or delete `aegis.db` and restart Aegis.

### `uvicorn: command not found`
Venv not active, or uvicorn not installed. Run:
```bash
source venv/bin/activate
python3 -m pip install uvicorn
python3 -m uvicorn main:app --reload
```

### Port already in use
```bash
lsof -i :8000         # Find what's using port 8000
kill -9 <PID>         # Kill it
uvicorn main:app --reload
```

---

## Daily Workflow (After Initial Setup)

Every time you come back to work on this project:

```bash
# Terminal 1 — Mock upstream
cd ~/Desktop/aegis/aegis
source venv/bin/activate
python3 -m http.server 9000

# Terminal 2 — Aegis
cd ~/Desktop/aegis/aegis
source venv/bin/activate
uvicorn main:app --reload

# Terminal 3 — Tests / curl
cd ~/Desktop/aegis/aegis
source venv/bin/activate
PYTHONPATH=. pytest tests/ -v
```
