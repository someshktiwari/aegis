# tests/test_proxy.py
# Integration tests for the idempotency proxy layer.
# Uses unittest.mock to patch forward_to_upstream so tests never need a
# real upstream service running.

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _make_upstream_response(status_code: int = 200, body: dict = None, headers: dict = None):
    """Build a fake httpx.Response-like object for mocking upstream calls."""
    body = body or {"result": "success"}
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.text = json.dumps(body)
    mock_resp.content = json.dumps(body).encode()
    mock_resp.headers = headers or {"content-type": "application/json"}
    return mock_resp


# ── Happy path ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_key_forwards_to_upstream(client):
    mock_resp = _make_upstream_response(201, {"id": "order-42"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)):
        response = await client.post(
            "/orders",
            json={"item": "book"},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-new-001"},
        )

    assert response.status_code == 201


@pytest.mark.asyncio
async def test_duplicate_key_same_body_returns_cached(client):
    mock_resp = _make_upstream_response(200, {"status": "paid"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)) as mock_fwd:
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-dup-001"},
        )
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-dup-001"},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Upstream must only have been called ONCE — second request served from cache
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_key_reuse_different_body_returns_422(client):
    mock_resp = _make_upstream_response(200, {"ok": True})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)):
        await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-mismatch-001"},
        )
        r2 = await client.post(
            "/payments",
            json={"amount": 999},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-mismatch-001"},
        )

    assert r2.status_code == 422
    assert "different request body" in r2.json()["error"]


@pytest.mark.asyncio
async def test_missing_idempotency_key_returns_400(client):
    response = await client.post("/payments", json={"amount": 100}, headers={"X-API-Key": "test-key"})
    assert response.status_code == 400
    assert "Idempotency-Key" in response.json()["error"]


@pytest.mark.asyncio
async def test_expired_key_treated_as_new(client, db):
    # Insert a completed record whose expires_at is in the past. The proxy's
    # on-access expiry check must treat it as expired, delete it, and forward fresh.
    past = time.time() - 999_999
    await db.execute(
        """
        INSERT INTO idempotency_keys
            (key, fingerprint, status, status_code, response_body, response_headers,
             created_at, expires_at)
        VALUES (?, ?, 'completed', 200, '{"old": true}', '{}', ?, ?)
        """,
        (
            "test-key:idem-expired-001",
            __import__("hashlib").sha256(b"POST\n/payments\n" + b'{"amount": 100}').hexdigest(),
            past,
            past,
        ),
    )
    await db.commit()

    mock_resp = _make_upstream_response(200, {"fresh": True})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)) as mock_fwd:
        response = await client.post(
            "/payments",
            content=b'{"amount": 100}',
            headers={
                "X-API-Key": "test-key",
                "Idempotency-Key": "idem-expired-001",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 200
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_get_without_key_passes_through(client):
    """GET without key passes through directly — no idempotency."""
    mock_resp = _make_upstream_response(200, {"items": []})

    with patch("main.forward_to_upstream", new=AsyncMock(return_value=mock_resp)):
        response = await client.get("/orders")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_get_with_key_passes_through_without_caching(client):
    """
    GET with an Idempotency-Key must pass through without caching.
    GET is idempotent by HTTP semantics, so it is never cached even with a key.
    """
    mock_resp = _make_upstream_response(200, {"items": ["a", "b"]})

    with patch("main.forward_to_upstream", new=AsyncMock(return_value=mock_resp)) as mock_fwd:
        r1 = await client.get("/orders", headers={"X-API-Key": "test-key", "Idempotency-Key": "get-key-001"})
        r2 = await client.get("/orders", headers={"X-API-Key": "test-key", "Idempotency-Key": "get-key-001"})

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Upstream called TWICE — GET is not cached even with a key
    assert mock_fwd.call_count == 2


# ── Failure path tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upstream_connection_error_returns_502_and_releases_key(client, db):
    """
    On a connection error, Aegis returns 502 and releases the key (deletes the
    in_flight row) so the client can retry with the same key.
    """
    with patch(
        "proxy.forward_to_upstream",
        new=AsyncMock(side_effect=httpx.ConnectError("upstream unreachable")),
    ):
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-conn-fail-001"},
        )

    assert r1.status_code == 502
    assert "Key released" in r1.json()["hint"]

    mock_resp = _make_upstream_response(200, {"status": "paid"})
    with patch(
        "proxy.forward_to_upstream",
        new=AsyncMock(return_value=mock_resp),
    ) as mock_fwd:
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-conn-fail-001"},
        )

    assert r2.status_code == 200
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_upstream_timeout_releases_key(client, db):
    """Timeout errors must also release the key."""
    with patch(
        "proxy.forward_to_upstream",
        new=AsyncMock(side_effect=httpx.TimeoutException("upstream timed out")),
    ):
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-timeout-001"},
        )

    assert r1.status_code == 502

    mock_resp = _make_upstream_response(200, {"status": "paid"})
    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=mock_resp)) as mock_fwd:
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-timeout-001"},
        )

    assert r2.status_code == 200
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_upstream_5xx_is_not_cached(client, db):
    """A transient upstream 500 must NOT be cached; the retry reaches upstream again."""
    error_resp = _make_upstream_response(500, {"error": "upstream overloaded"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=error_resp)):
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-5xx-001"},
        )

    assert r1.status_code == 500

    success_resp = _make_upstream_response(200, {"status": "paid"})
    with patch(
        "proxy.forward_to_upstream",
        new=AsyncMock(return_value=success_resp),
    ) as mock_fwd:
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-5xx-001"},
        )

    assert r2.status_code == 200
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_upstream_429_is_not_cached(client, db):
    """429 is transient — it must NOT be cached; the retry reaches upstream again."""
    rate_limit_resp = _make_upstream_response(429, {"error": "rate limit exceeded"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=rate_limit_resp)):
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-429-001"},
        )

    assert r1.status_code == 429

    success_resp = _make_upstream_response(200, {"status": "paid"})
    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=success_resp)) as mock_fwd:
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-429-001"},
        )

    assert r2.status_code == 200
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_upstream_4xx_deterministic_is_cached(client, db):
    """
    Deterministic client errors (e.g. 400 validation) ARE cached — the same
    invalid input always produces the same error, so caching prevents hammering.
    """
    error_resp = _make_upstream_response(400, {"error": "invalid amount"})

    with patch(
        "proxy.forward_to_upstream",
        new=AsyncMock(return_value=error_resp),
    ) as mock_fwd:
        r1 = await client.post(
            "/payments",
            json={"amount": -1},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-4xx-001"},
        )
        r2 = await client.post(
            "/payments",
            json={"amount": -1},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-4xx-001"},
        )

    assert r1.status_code == 400
    assert r2.status_code == 400
    # Upstream called only once — 400 is deterministic and correctly cached
    assert mock_fwd.call_count == 1


@pytest.mark.asyncio
async def test_set_cookie_stripped_from_cached_response(client, db):
    """
    Set-Cookie must NOT be replayed from cache to a future caller.
    The first (fresh) response returns Set-Cookie; the cached replay does not.
    """
    resp_with_cookie = _make_upstream_response(
        200,
        {"status": "paid"},
        headers={
            "content-type": "application/json",
            "set-cookie": "session=secret-A; HttpOnly",
        },
    )

    with patch("proxy.forward_to_upstream", new=AsyncMock(return_value=resp_with_cookie)):
        r1 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-cookie-001"},
        )
        r2 = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "test-key", "Idempotency-Key": "idem-cookie-001"},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    # Cached response must not contain the session cookie
    assert "set-cookie" not in {k.lower() for k in r2.headers.keys()}

@pytest.mark.asyncio
async def test_missing_api_key_returns_401(client):
    """No X-API-Key header — must return 401 before reaching idempotency logic."""
    response = await client.post(
        "/payments",
        json={"amount": 100},
        headers={"Idempotency-Key": "some-key"},
    )
    assert response.status_code == 401
    assert "X-API-Key" in response.json()["error"]


@pytest.mark.asyncio
async def test_api_key_scopes_idempotency_keys(client):
    """
    Two callers sending the same Idempotency-Key are tracked independently.
    Client A's cache must not be served to Client B.
    """
    mock_resp_a = _make_upstream_response(200, {"client": "A"})
    mock_resp_b = _make_upstream_response(200, {"client": "B"})

    with patch("proxy.forward_to_upstream", new=AsyncMock(side_effect=[mock_resp_a, mock_resp_b])) as mock_fwd:
        r_a = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "client-a", "Idempotency-Key": "shared-key-001"},
        )
        r_b = await client.post(
            "/payments",
            json={"amount": 500},
            headers={"X-API-Key": "client-b", "Idempotency-Key": "shared-key-001"},
        )

    assert r_a.status_code == 200
    assert r_b.status_code == 200
    # Both requests must have reached upstream — same key, different API keys = different scopes
    assert mock_fwd.call_count == 2
    assert r_a.json()["client"] == "A"
    assert r_b.json()["client"] == "B"