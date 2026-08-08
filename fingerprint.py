# fingerprint.py
# Computes a SHA-256 fingerprint of the method, path, query string and raw
# request body. See DECISIONS.md D-005 for why SHA-256 over MD5 or CRC32,
# and D-026 for why the query string is part of request identity.

import hashlib


def compute_fingerprint(method: str, path: str, query: str, body: bytes) -> str:
    """
    Returns a fixed-length 64-character hex string identifying this request.

    The fingerprint covers the four components that make a request what it is:
    method, path, query string, and body. The same Idempotency-Key paired with
    a different value in ANY of the four produces a different fingerprint and
    is rejected with 422 (see D-006).

    Why the query string is included:
    `POST /pay?account=alice` and `POST /pay?account=bob` are different
    operations even when the body is byte-identical. The query is forwarded to
    the upstream verbatim, so it is part of what the upstream executes — and
    anything the upstream executes on must be part of request identity.

    Why the raw query bytes, not parsed-and-sorted parameters:
    `?a=1&b=2` and `?b=2&a=1` fingerprint differently, and that is deliberate.
    Normalising would mean parsing, then owning decisions about repeated keys,
    percent-encoding, and empty values forever. A client that sends
    semantically-equal-but-textually-different queries under a single
    Idempotency-Key has already broken the key-to-request binding that
    idempotency rests on. See DECISIONS.md D-026.

    Why SHA-256:
    - Collision-resistant: two different requests will not produce the same hash
    - Fixed output size: cheap to store in SQLite (64 chars vs. raw values)
    - In stdlib (hashlib): zero external dependencies

    Interviewers sometimes ask: "why not store the raw body?"
    Answer: bodies can be megabytes. One row per request storing the full
    payload would make the SQLite file balloon in any real workload.

    The newline separators are load-bearing: without them, method "POST",
    path "/pay" and query "ment" would hash identically to method "POST",
    path "/payment" and an empty query.

    `query` is a required argument rather than defaulting to "". A default
    would let a future call site omit it silently and reintroduce a cache
    that serves one caller's response to another.
    """
    canonical = (
        method.upper().encode() + b"\n"
        + path.encode() + b"\n"
        + query.encode() + b"\n"
        + body
    )
    return hashlib.sha256(canonical).hexdigest()
