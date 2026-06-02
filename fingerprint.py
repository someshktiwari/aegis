# fingerprint.py
# Computes a SHA-256 fingerprint of the raw request body bytes.
# See DECISIONS.md D-005 for why SHA-256 over MD5 or CRC32.

import hashlib


def compute_fingerprint(body: bytes) -> str:
    """
    Returns a fixed-length 64-character hex string regardless of body size.

    Why SHA-256:
    - Collision-resistant: two different bodies will not produce the same hash
    - Fixed output size: cheap to store in SQLite (64 chars vs. raw body)
    - In stdlib (hashlib): zero external dependencies
    - Empty body b"" produces a consistent, stable fingerprint

    Interviewers sometimes ask: "why not store the raw body?"
    Answer: bodies can be megabytes. One row per request storing the full
    payload would make the SQLite file balloon in any real workload.
    """
    return hashlib.sha256(body).hexdigest()
