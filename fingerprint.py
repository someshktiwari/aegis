# fingerprint.py
# Computes a SHA-256 fingerprint of the method, path, and raw request body.
# See DECISIONS.md D-005 for why SHA-256 over MD5 or CRC32.

import hashlib


def compute_fingerprint(method: str, path: str, body: bytes) -> str:
    """
    Returns a fixed-length 64-character hex string using method, path and body.
    This is used to uniquely identify a request — same key with different
    method, path, or body will produce a different fingerprint.

    Why SHA-256:
    - Collision-resistant: two different bodies, methods, or paths will not produce the same hash
    - Fixed output size: cheap to store in SQLite (64 chars vs. raw values)
    - In stdlib (hashlib): zero external dependencies

    Interviewers sometimes ask: "why not store the raw body?"
    Answer: bodies can be megabytes. One row per request storing the full
    payload would make the SQLite file balloon in any real workload.
    """
    canonical = method.upper().encode() + b"\n" + path.encode() + b"\n" + body
    return hashlib.sha256(canonical).hexdigest()